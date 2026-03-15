import asyncio

import pytest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from lipservice.app import app
from lipservice.auth import token_store
from lipservice.routes import proxy
from lipservice.state import ChannelState, MemberInfo, NetworkState
from lipservice.storage import MemoryBackend


@pytest.fixture(autouse=True)
def _reset_state():
    proxy.networks.clear()
    proxy.storage = MemoryBackend(max_backlog=1000)
    proxy.event_bus._counter = 0
    proxy.event_bus._subscribers.clear()
    proxy._join_waiters.clear()
    token_store._tokens.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    resp = client.post("/api/auth/token", json={
        "username": "admin", "password": "changeme",
    })
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def mock_network():
    """A connected network with a mocked IRC client, one channel, and some messages."""
    mock_client = AsyncMock()
    mock_client.connected = True

    async def _fake_join(channel: str, key: str | None = None) -> None:
        await proxy.handle_irc_event("testnet", "join", {
            "channel": channel, "nick": "testbot", "user": "", "host": "",
        })

    mock_client.join.side_effect = _fake_join

    net = NetworkState(
        name="testnet", host="irc.example.com", port=6697,
        tls=True, nick="testbot",
    )
    net.state = "connected"
    net.client = mock_client

    ch = ChannelState(name="#test", topic="Test topic", topic_set_by="someone")
    ch.members["testbot"] = MemberInfo(nick="testbot", prefix="@")
    ch.members["alice"] = MemberInfo(nick="alice", user="alice", host="user/alice")
    proxy.storage.append_channel_message("testnet", "#test", {
        "id": "msg_000000000001", "time": "2026-03-14T10:00:00Z",
        "from": "alice", "type": "privmsg", "text": "hello",
    })
    proxy.storage.append_channel_message("testnet", "#test", {
        "id": "msg_000000000002", "time": "2026-03-14T10:01:00Z",
        "from": "testbot", "type": "privmsg", "text": "hi alice",
    })
    net.channels["#test"] = ch

    proxy.networks["testnet"] = net
    return mock_client
