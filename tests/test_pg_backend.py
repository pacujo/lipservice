from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest

from lipservice.pg_backend import PostgresBackend


def _make_backend() -> PostgresBackend:
    backend = PostgresBackend.__new__(PostgresBackend)
    backend._uri = "postgres://test"
    backend._passphrase = "test"
    return backend


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        if self._conn.fail_execute:
            raise psycopg.OperationalError("the connection is closed")
        self._conn.executed.append(sql)

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(
        self, *, cursor_error: bool = False, fail_execute: bool = False,
    ) -> None:
        self.closed = False
        self.autocommit = False
        self._cursor_error = cursor_error
        self.fail_execute = fail_execute
        self.close_calls = 0
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        if self._cursor_error:
            raise psycopg.OperationalError("the connection is closed")
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


def test_run_reconnects_when_closed() -> None:
    backend = _make_backend()
    dead = _FakeConn()
    dead.closed = True
    live = _FakeConn()
    backend._conn = dead  # type: ignore[assignment]

    with patch.object(backend, "_reconnect", side_effect=lambda: setattr(backend, "_conn", live)):
        backend._run(lambda cur: cur.execute("SELECT 1"))

    assert live.executed == ["SELECT 1"]


def test_run_retries_after_cursor_error() -> None:
    backend = _make_backend()
    dead = _FakeConn(cursor_error=True)
    live = _FakeConn()
    backend._conn = dead  # type: ignore[assignment]
    reconnects = 0

    def reconnect() -> None:
        nonlocal reconnects
        reconnects += 1
        backend._conn = live  # type: ignore[assignment]

    with patch.object(backend, "_reconnect", side_effect=reconnect):
        backend._run(lambda cur: cur.execute("SELECT 1"))

    assert reconnects == 1
    assert dead.close_calls == 1
    assert live.executed == ["SELECT 1"]


def test_run_retries_after_execute_error() -> None:
    backend = _make_backend()
    flaky = _FakeConn(fail_execute=True)
    live = _FakeConn()
    backend._conn = flaky  # type: ignore[assignment]
    reconnects = 0

    def reconnect() -> None:
        nonlocal reconnects
        reconnects += 1
        backend._conn = live  # type: ignore[assignment]

    with patch.object(backend, "_reconnect", side_effect=reconnect):
        backend._run(lambda cur: cur.execute("SELECT 1"))

    assert reconnects == 1
    assert flaky.close_calls == 1
    assert live.executed == ["SELECT 1"]


def test_run_raises_after_two_failures() -> None:
    backend = _make_backend()
    dead = _FakeConn(cursor_error=True)
    backend._conn = dead  # type: ignore[assignment]

    with patch.object(backend, "_reconnect"):
        with pytest.raises(psycopg.OperationalError, match="closed"):
            backend._run(lambda cur: cur.execute("SELECT 1"))

    assert dead.close_calls == 2


def test_ping_returns_false_when_db_unreachable() -> None:
    backend = _make_backend()
    backend._conn = _FakeConn(cursor_error=True)  # type: ignore[assignment]

    with patch.object(backend, "_reconnect"):
        assert backend.ping() is False
