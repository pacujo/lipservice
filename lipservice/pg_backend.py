from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from lipservice.storage import StorageBackend

_INSERT = """\
INSERT INTO messages (id, network, kind, target, time, from_nick, type, text)
VALUES (%(id)s, %(network)s, %(kind)s, %(target)s,
        %(time)s, %(from)s, %(type)s, %(text)s)
"""

_SELECT_CHANNEL = """\
SELECT id, time, from_nick AS "from", type, text
FROM messages
WHERE network = %s AND kind = 'channel' AND target = %s
ORDER BY id
"""

_SELECT_META = """\
SELECT id, time, from_nick AS "from", type, text
FROM messages
WHERE network = %s AND kind = 'meta'
ORDER BY id
"""

_SELECT_PRIVATE = """\
SELECT id, time, from_nick AS "from", type, text
FROM messages
WHERE network = %s AND kind = 'private' AND target = %s
ORDER BY id
"""


class PostgresBackend(StorageBackend):
    """PostgreSQL message store using psycopg 3."""

    def __init__(self, uri: str) -> None:
        self._conn = psycopg.connect(uri, row_factory=dict_row)
        self._conn.autocommit = True

    def next_message_id(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute("SELECT nextval('message_id_seq')")
            row = cur.fetchone()
            assert row is not None
            val: int = row["nextval"]
        return f"msg_{val:012d}"

    def _insert(
        self, network: str, kind: str, target: str, msg: dict[str, Any],
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_INSERT, {
                **msg, "network": network, "kind": kind, "target": target,
            })

    def append_channel_message(
        self, network: str, channel: str, msg: dict[str, Any],
    ) -> None:
        self._insert(network, "channel", channel, msg)

    def append_meta_message(
        self, network: str, msg: dict[str, Any],
    ) -> None:
        self._insert(network, "meta", "", msg)

    def append_private_message(
        self, network: str, nick: str, msg: dict[str, Any],
    ) -> None:
        self._insert(network, "private", nick, msg)

    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_CHANNEL, (network, channel))
            return cur.fetchall()

    def get_meta_messages(self, network: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_META, (network,))
            return cur.fetchall()

    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_PRIVATE, (network, nick))
            return cur.fetchall()

    def remove_network(self, network: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE network = %s", (network,))
