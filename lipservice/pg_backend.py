from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from lipservice.models import Message, Session
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
        self, network: str, kind: str, target: str, msg: Message,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_INSERT, {
                **msg, "network": network, "kind": kind, "target": target,
            })

    def append_channel_message(
        self, network: str, channel: str, msg: Message,
    ) -> None:
        self._insert(network, "channel", channel, msg)

    def append_meta_message(
        self, network: str, msg: Message,
    ) -> None:
        self._insert(network, "meta", "", msg)

    def append_private_message(
        self, network: str, nick: str, msg: Message,
    ) -> None:
        self._insert(network, "private", nick, msg)

    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[Message]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_CHANNEL, (network, channel))
            return cur.fetchall()  # type: ignore[return-value]

    def get_meta_messages(self, network: str) -> list[Message]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_META, (network,))
            return cur.fetchall()  # type: ignore[return-value]

    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[Message]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_PRIVATE, (network, nick))
            return cur.fetchall()  # type: ignore[return-value]

    def list_private_peers(self, network: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT target FROM messages"
                " WHERE network = %s AND kind = 'private'"
                " ORDER BY target",
                (network,),
            )
            return [row["target"] for row in cur.fetchall()]

    def remove_private_peer(
        self, network: str, nick: str,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM messages"
                " WHERE network = %s AND kind = 'private' AND target = %s",
                (network, nick),
            )

    def get_session(self) -> Session:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT current_network, current_channel, current_query"
                " FROM session WHERE id = 1",
            )
            row = cur.fetchone()
        return Session(
            current_network=row["current_network"] if row else None,
            current_channel=row["current_channel"] if row else None,
            current_query=row["current_query"] if row else None,
            pointers=self.get_all_pointers(),
        )

    def set_session(
        self, current_network: str | None,
        current_channel: str | None, current_query: str | None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session (id, current_network,"
                " current_channel, current_query)"
                " VALUES (1, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET"
                " current_network = EXCLUDED.current_network,"
                " current_channel = EXCLUDED.current_channel,"
                " current_query = EXCLUDED.current_query",
                (current_network, current_channel, current_query),
            )

    def get_pointer(self, network: str, target: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT last_read_id FROM pointers"
                " WHERE network = %s AND target = %s",
                (network, target),
            )
            row = cur.fetchone()
            return row["last_read_id"] if row else None

    def set_pointer(
        self, network: str, target: str, last_read_id: str,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pointers (network, target, last_read_id)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (network, target) DO UPDATE"
                " SET last_read_id = EXCLUDED.last_read_id",
                (network, target, last_read_id),
            )

    def get_all_pointers(self) -> dict[str, str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT network, target, last_read_id FROM pointers")
            return {
                f"{r['network']}/{r['target']}": r["last_read_id"]
                for r in cur.fetchall()
            }

    def remove_network(self, network: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE network = %s", (network,))
            cur.execute("DELETE FROM pointers WHERE network = %s", (network,))
