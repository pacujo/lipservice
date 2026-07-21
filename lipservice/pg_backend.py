from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import psycopg
from psycopg.rows import dict_row

from lipservice.crypto import decrypt, encrypt
from lipservice.models import Message, NetworkConfig, Session
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

_T = TypeVar("_T")


class PostgresBackend(StorageBackend):
    """PostgreSQL message store using psycopg 3."""

    _PROBE_PLAINTEXT = "lipservice-probe"

    def __init__(self, uri: str, passphrase: str) -> None:
        self._uri = uri
        self._conn = psycopg.connect(uri, row_factory=dict_row)
        self._conn.autocommit = True
        self._passphrase = passphrase
        self._verify_probe()

    def _close_conn(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _reconnect(self) -> None:
        self._close_conn()
        self._conn = psycopg.connect(self._uri, row_factory=dict_row)
        self._conn.autocommit = True

    def _run(self, fn: Callable[[psycopg.Cursor], _T]) -> _T:
        for attempt in range(2):
            if self._conn.closed:
                self._reconnect()
            try:
                with self._conn.cursor() as cur:
                    return fn(cur)
            except Exception:
                self._close_conn()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def ping(self) -> bool:
        """Return True when the database accepts a trivial query."""
        try:
            self._run(lambda cur: cur.execute("SELECT 1"))
            return True
        except Exception:
            self._close_conn()
            return False

    def _verify_probe(self) -> None:
        row = self._run(
            lambda cur: (
                cur.execute("SELECT token FROM passphrase_probe WHERE id = 1"),
                cur.fetchone(),
            )[1],
        )
        if not row:
            return
        try:
            result = decrypt(row["token"], self._passphrase)
        except Exception as exc:
            raise RuntimeError(
                "LIPSERVICE_PASS does not match the stored passphrase probe. "
                "Use 'python -m lipservice.pg_admin passwd' to re-key.",
            ) from exc
        if result != self._PROBE_PLAINTEXT:
            raise RuntimeError(
                "Passphrase probe decrypted but content mismatch.",
            )

    def next_message_id(self) -> str:
        def work(cur: psycopg.Cursor) -> str:
            cur.execute("SELECT nextval('message_id_seq')")
            row = cur.fetchone()
            assert row is not None
            val: int = row["nextval"]
            return f"msg_{val:012d}"

        return self._run(work)

    def _insert(
        self, network: str, kind: str, target: str, msg: Message,
    ) -> None:
        self._run(lambda cur: cur.execute(_INSERT, {
            **msg, "network": network, "kind": kind, "target": target,
        }))

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
        return self._run(lambda cur: (
            cur.execute(_SELECT_CHANNEL, (network, channel)),
            cur.fetchall(),
        )[1])  # type: ignore[return-value]

    def get_meta_messages(self, network: str) -> list[Message]:
        return self._run(lambda cur: (
            cur.execute(_SELECT_META, (network,)),
            cur.fetchall(),
        )[1])  # type: ignore[return-value]

    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[Message]:
        return self._run(lambda cur: (
            cur.execute(_SELECT_PRIVATE, (network, nick)),
            cur.fetchall(),
        )[1])  # type: ignore[return-value]

    def list_private_peers(self, network: str) -> list[str]:
        return self._run(lambda cur: (
            cur.execute(
                "SELECT DISTINCT target FROM messages"
                " WHERE network = %s AND kind = 'private'"
                " ORDER BY target",
                (network,),
            ),
            [row["target"] for row in cur.fetchall()],
        )[1])

    def remove_private_peer(
        self, network: str, nick: str,
    ) -> None:
        self._run(lambda cur: cur.execute(
            "DELETE FROM messages"
            " WHERE network = %s AND kind = 'private' AND target = %s",
            (network, nick),
        ))

    def get_session(self) -> Session:
        row = self._run(lambda cur: (
            cur.execute(
                "SELECT current_network, current_channel, current_query"
                " FROM session WHERE id = 1",
            ),
            cur.fetchone(),
        )[1])
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
        self._run(lambda cur: cur.execute(
            "INSERT INTO session (id, current_network,"
            " current_channel, current_query)"
            " VALUES (1, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET"
            " current_network = EXCLUDED.current_network,"
            " current_channel = EXCLUDED.current_channel,"
            " current_query = EXCLUDED.current_query",
            (current_network, current_channel, current_query),
        ))

    def get_pointer(self, network: str, target: str) -> str | None:
        row = self._run(lambda cur: (
            cur.execute(
                "SELECT last_read_id FROM pointers"
                " WHERE network = %s AND target = %s",
                (network, target),
            ),
            cur.fetchone(),
        )[1])
        return row["last_read_id"] if row else None

    def set_pointer(
        self, network: str, target: str, last_read_id: str,
    ) -> None:
        self._run(lambda cur: cur.execute(
            "INSERT INTO pointers (network, target, last_read_id)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (network, target) DO UPDATE"
            " SET last_read_id = EXCLUDED.last_read_id",
            (network, target, last_read_id),
        ))

    def get_all_pointers(self) -> dict[str, str]:
        return self._run(lambda cur: (
            cur.execute("SELECT network, target, last_read_id FROM pointers"),
            {
                f"{r['network']}/{r['target']}": r["last_read_id"]
                for r in cur.fetchall()
            },
        )[1])

    def _decrypt_opt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return decrypt(value, self._passphrase)

    def _encrypt_opt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return encrypt(value, self._passphrase)

    def list_networks(self) -> list[NetworkConfig]:
        def work(cur: psycopg.Cursor) -> list[NetworkConfig]:
            cur.execute(
                "SELECT name, host, port, tls, nick,"
                " server_password, nickserv_password, auto_connect"
                " FROM networks ORDER BY name",
            )
            rows = cur.fetchall()
            configs: list[NetworkConfig] = []
            for row in rows:
                cur.execute(
                    "SELECT channel FROM network_channels"
                    " WHERE network = %s ORDER BY channel",
                    (row["name"],),
                )
                channels = [r["channel"] for r in cur.fetchall()]
                configs.append(NetworkConfig(
                    **{**row,
                       "server_password": self._decrypt_opt(row["server_password"]),
                       "nickserv_password": self._decrypt_opt(row["nickserv_password"]),
                       "channels": channels},
                ))
            return configs

        return self._run(work)

    def save_network(self, config: NetworkConfig) -> None:
        params = {
            **config.model_dump(),
            "server_password": self._encrypt_opt(config.server_password),
            "nickserv_password": self._encrypt_opt(config.nickserv_password),
        }

        def work(cur: psycopg.Cursor) -> None:
            cur.execute(
                "INSERT INTO networks"
                " (name, host, port, tls, nick,"
                "  server_password, nickserv_password, auto_connect)"
                " VALUES (%(name)s, %(host)s, %(port)s, %(tls)s, %(nick)s,"
                "  %(server_password)s, %(nickserv_password)s, %(auto_connect)s)"
                " ON CONFLICT (name) DO UPDATE SET"
                "  host = EXCLUDED.host, port = EXCLUDED.port,"
                "  tls = EXCLUDED.tls, nick = EXCLUDED.nick,"
                "  server_password = EXCLUDED.server_password,"
                "  nickserv_password = EXCLUDED.nickserv_password,"
                "  auto_connect = EXCLUDED.auto_connect",
                params,
            )
            cur.execute(
                "DELETE FROM network_channels WHERE network = %s",
                (config.name,),
            )
            for ch in config.channels:
                cur.execute(
                    "INSERT INTO network_channels (network, channel)"
                    " VALUES (%s, %s)",
                    (config.name, ch),
                )

        self._run(work)

    def delete_network(self, name: str) -> None:
        self._run(lambda cur: cur.execute(
            "DELETE FROM networks WHERE name = %s", (name,),
        ))

    def remove_network_data(self, network: str) -> None:
        def work(cur: psycopg.Cursor) -> None:
            cur.execute("DELETE FROM messages WHERE network = %s", (network,))
            cur.execute("DELETE FROM pointers WHERE network = %s", (network,))

        self._run(work)
