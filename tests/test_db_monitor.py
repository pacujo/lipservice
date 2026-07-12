import asyncio

import pytest

from lipservice.state import NetworkState, ProxyState
from lipservice.storage import MemoryBackend


class PingStorage(MemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.ping_ok = True

    def ping(self) -> bool:
        return self.ping_ok


@pytest.fixture
def db_proxy() -> ProxyState:
    return ProxyState(storage=PingStorage())


@pytest.fixture
def sample_network(db_proxy: ProxyState) -> NetworkState:
    net = NetworkState(
        name="testnet", host="irc.example.com", port=6697,
        tls=True, nick="bot",
    )
    db_proxy.networks["testnet"] = net
    return net


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_db_lost_emits_ephemeral_meta(
    db_proxy: ProxyState, sample_network: NetworkState,
) -> None:
    async def _test() -> None:
        q = db_proxy.event_bus.subscribe()
        await db_proxy._on_db_lost()

        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event["event"] == "message"
        assert event["data"]["network"] == "testnet"
        assert event["data"]["type"] == "meta"
        assert event["data"]["text"] == "Database connection lost"
        assert event["data"]["id"].startswith("tmp_")

        storage = db_proxy.storage
        assert isinstance(storage, PingStorage)
        assert storage.get_meta_messages("testnet") == []
        assert len(db_proxy._pending_db_meta) == 1

    _run(_test())


def test_db_restored_persists_pending_and_notifies(
    db_proxy: ProxyState, sample_network: NetworkState,
) -> None:
    async def _test() -> None:
        db_proxy._db_ok = False
        await db_proxy._on_db_lost()

        q = db_proxy.event_bus.subscribe()
        await db_proxy._on_db_restored()

        storage = db_proxy.storage
        assert isinstance(storage, PingStorage)
        meta = storage.get_meta_messages("testnet")
        assert len(meta) == 2
        assert meta[0]["text"] == "Database connection lost"
        assert meta[0]["id"].startswith("msg_")
        assert meta[1]["text"] == "Database connection restored"
        assert db_proxy._pending_db_meta == []

        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event["data"]["text"] == "Database connection restored"

    _run(_test())


def test_db_monitor_loop_detects_transitions(
    db_proxy: ProxyState, sample_network: NetworkState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test() -> None:
        storage = db_proxy.storage
        assert isinstance(storage, PingStorage)

        monkeypatch.setattr("lipservice.state._DB_PROBE_INTERVAL", 0.01)
        await db_proxy.start_db_monitor()

        storage.ping_ok = False
        await asyncio.sleep(0.05)
        assert db_proxy._db_ok is False
        assert len(db_proxy._pending_db_meta) == 1

        storage.ping_ok = True
        await asyncio.sleep(0.05)
        assert db_proxy._db_ok is True
        assert len(storage.get_meta_messages("testnet")) == 2

        await db_proxy.stop_db_monitor()

    _run(_test())


def test_memory_backend_skips_monitor(db_proxy: ProxyState) -> None:
    async def _test() -> None:
        db_proxy.storage = MemoryBackend()
        await db_proxy.start_db_monitor()
        assert db_proxy._db_monitor_task is None

    _run(_test())
