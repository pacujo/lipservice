"""Set up, migrate, or tear down the lipservice PostgreSQL database.

Usage:
    python -m lipservice.pg_admin setup    --admin-uri URI [--role NAME] [--dbname NAME]
    python -m lipservice.pg_admin migrate  --database-uri URI
    python -m lipservice.pg_admin teardown --admin-uri URI [--role NAME] [--dbname NAME]

setup    -- Create the role, database, and schema from scratch.
            Fails if the role or database already exists.
            Generates a random password and prints the app URI to stdout.

migrate  -- Apply the latest schema to an existing database.
            Safe to run repeatedly (uses IF NOT EXISTS throughout).

teardown -- Drop the database and role.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from typing import Any
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from lipservice.crypto import decrypt, encrypt

_SCHEMA = """\
CREATE SEQUENCE IF NOT EXISTS message_id_seq;

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT        PRIMARY KEY,
    network     TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    target      TEXT        NOT NULL DEFAULT '',
    time        TEXT        NOT NULL,
    from_nick   TEXT        NOT NULL DEFAULT '',
    type        TEXT        NOT NULL DEFAULT 'privmsg',
    text        TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_messages_channel
    ON messages (network, kind, target, id)
    WHERE kind = 'channel';

CREATE INDEX IF NOT EXISTS idx_messages_meta
    ON messages (network, id)
    WHERE kind = 'meta';

CREATE INDEX IF NOT EXISTS idx_messages_private
    ON messages (network, kind, target, id)
    WHERE kind = 'private';

CREATE TABLE IF NOT EXISTS networks (
    name              TEXT PRIMARY KEY,
    host              TEXT NOT NULL,
    port              INTEGER NOT NULL DEFAULT 6697,
    tls               BOOLEAN NOT NULL DEFAULT TRUE,
    nick              TEXT NOT NULL,
    server_password   TEXT,
    nickserv_password  TEXT,
    auto_connect      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS session (
    id               INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    current_network  TEXT,
    current_channel  TEXT,
    current_query    TEXT
);

CREATE TABLE IF NOT EXISTS pointers (
    network       TEXT NOT NULL,
    target        TEXT NOT NULL,
    last_read_id  TEXT NOT NULL,
    PRIMARY KEY (network, target)
);

CREATE TABLE IF NOT EXISTS passphrase_probe (
    id    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    token TEXT NOT NULL
);
"""

_PROBE_PLAINTEXT = "lipservice-probe"


def _app_uri(admin_uri: str, role: str, password: str, dbname: str) -> str:
    """Build an application connection URI reusing the admin URI's host."""
    parsed = urlparse(admin_uri)
    return urlunparse((
        parsed.scheme or "postgres",
        f"{role}:{password}@{parsed.hostname}"
        + (f":{parsed.port}" if parsed.port else ""),
        f"/{dbname}",
        "", "", "",
    ))


def _write_probe(conn: psycopg.Connection[Any], passphrase: str) -> None:
    token = encrypt(_PROBE_PLAINTEXT, passphrase)
    conn.execute(
        "INSERT INTO passphrase_probe (id, token) VALUES (1, %s)"
        " ON CONFLICT (id) DO UPDATE SET token = EXCLUDED.token",
        (token,),
    )


def setup(
    admin_uri: str, role: str, dbname: str, passphrase: str,
) -> None:
    password = secrets.token_urlsafe(24)
    conn = psycopg.connect(admin_uri, autocommit=True)
    try:
        cur = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,),
        )
        if cur.fetchone():
            print(f"Error: role {role!r} already exists.", file=sys.stderr)
            sys.exit(1)
        conn.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password)))
        print(f"Created role {role!r}.", file=sys.stderr)

        cur = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,),
        )
        if cur.fetchone():
            print(f"Error: database {dbname!r} already exists.",
                  file=sys.stderr)
            sys.exit(1)
        conn.execute(sql.SQL(
            "CREATE DATABASE {} OWNER {}"
        ).format(sql.Identifier(dbname), sql.Identifier(role)))
        print(f"Created database {dbname!r}.", file=sys.stderr)
    finally:
        conn.close()

    app_conn_uri = _app_uri(admin_uri, role, password, dbname)
    app_conn = psycopg.connect(app_conn_uri, autocommit=True)
    try:
        app_conn.execute(_SCHEMA)
        _write_probe(app_conn, passphrase)
        print("Schema applied.", file=sys.stderr)
    finally:
        app_conn.close()

    print(app_conn_uri)


def migrate(database_uri: str) -> None:
    conn = psycopg.connect(database_uri, autocommit=True)
    try:
        conn.execute(_SCHEMA)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM passphrase_probe WHERE id = 1")
            has_probe = cur.fetchone() is not None
        if not has_probe:
            passphrase = getpass.getpass("LIPSERVICE_PASS: ")
            if not passphrase:
                print("Error: passphrase must not be empty.", file=sys.stderr)
                sys.exit(1)
            _write_probe(conn, passphrase)
            print("Passphrase probe written.", file=sys.stderr)
        print("Schema up to date.", file=sys.stderr)
    finally:
        conn.close()


def passwd(database_uri: str, old_pass: str, new_pass: str) -> None:
    conn = psycopg.connect(database_uri, row_factory=dict_row)
    try:
        # Verify the old passphrase against the stored probe first.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token FROM passphrase_probe WHERE id = 1",
            )
            probe_row = cur.fetchone()
        if probe_row:
            try:
                decrypt(probe_row["token"], old_pass)
            except Exception:
                print("Error: old passphrase is incorrect.",
                      file=sys.stderr)
                sys.exit(1)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, server_password, nickserv_password"
                " FROM networks"
                " WHERE server_password IS NOT NULL"
                "    OR nickserv_password IS NOT NULL",
            )
            rows = cur.fetchall()

        # Re-encrypt in memory first; if the old password is wrong,
        # decrypt() raises before any writes happen.
        updates: list[tuple[str | None, str | None, str]] = []
        for row in rows:
            sp = row["server_password"]
            np = row["nickserv_password"]
            new_sp = encrypt(decrypt(sp, old_pass), new_pass) if sp else None
            new_np = encrypt(decrypt(np, old_pass), new_pass) if np else None
            updates.append((new_sp, new_np, row["name"]))

        with conn.cursor() as cur:
            for new_sp, new_np, name in updates:
                cur.execute(
                    "UPDATE networks"
                    " SET server_password = %s, nickserv_password = %s"
                    " WHERE name = %s",
                    (new_sp, new_np, name),
                )
        _write_probe(conn, new_pass)
        conn.commit()
        print(f"Re-keyed probe and {len(rows)} network(s).",
              file=sys.stderr)
    finally:
        conn.close()


def teardown(admin_uri: str, role: str, dbname: str) -> None:
    conn = psycopg.connect(admin_uri, autocommit=True)
    try:
        cur = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,),
        )
        if cur.fetchone():
            conn.execute(sql.SQL(
                "DROP DATABASE {} WITH (FORCE)"
            ).format(sql.Identifier(dbname)))
            print(f"Dropped database {dbname!r}.", file=sys.stderr)
        else:
            print(f"Database {dbname!r} does not exist.", file=sys.stderr)

        cur = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,),
        )
        if cur.fetchone():
            conn.execute(sql.SQL(
                "DROP ROLE {}"
            ).format(sql.Identifier(role)))
            print(f"Dropped role {role!r}.", file=sys.stderr)
        else:
            print(f"Role {role!r} does not exist.", file=sys.stderr)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage lipservice PostgreSQL database",
    )
    sub = parser.add_subparsers(dest="action")
    sub.required = True

    sp_setup = sub.add_parser("setup", help="create role, database, schema")
    sp_setup.add_argument(
        "--admin-uri", required=True,
        help="admin connection URI (e.g. postgres://postgres@localhost)",
    )
    sp_setup.add_argument(
        "--role", default="lipservice",
        help="application role name (default: lipservice)",
    )
    sp_setup.add_argument(
        "--dbname", default=None,
        help="database name (default: same as --role)",
    )

    sp_migrate = sub.add_parser(
        "migrate", help="apply latest schema to existing database",
    )
    sp_migrate.add_argument(
        "--database-uri", required=True,
        help="application connection URI (as printed by setup)",
    )

    sp_passwd = sub.add_parser(
        "passwd", help="re-encrypt passwords after changing LIPSERVICE_PASS",
    )
    sp_passwd.add_argument(
        "--database-uri", required=True,
        help="application connection URI (as printed by setup)",
    )

    sp_teardown = sub.add_parser("teardown", help="drop database and role")
    sp_teardown.add_argument(
        "--admin-uri", required=True,
        help="admin connection URI",
    )
    sp_teardown.add_argument(
        "--role", default="lipservice",
        help="application role name (default: lipservice)",
    )
    sp_teardown.add_argument(
        "--dbname", default=None,
        help="database name (default: same as --role)",
    )

    args = parser.parse_args()

    try:
        if args.action == "setup":
            passphrase = getpass.getpass("LIPSERVICE_PASS: ")
            if not passphrase:
                print("Error: passphrase must not be empty.", file=sys.stderr)
                sys.exit(1)
            dbname: str = args.dbname or args.role
            setup(args.admin_uri, args.role, dbname, passphrase)
        elif args.action == "migrate":
            migrate(args.database_uri)
        elif args.action == "passwd":
            old_pass = getpass.getpass("Old LIPSERVICE_PASS: ")
            new_pass = getpass.getpass("New LIPSERVICE_PASS: ")
            confirm = getpass.getpass("Confirm new LIPSERVICE_PASS: ")
            if new_pass != confirm:
                print("Error: new passwords do not match.", file=sys.stderr)
                sys.exit(1)
            passwd(args.database_uri, old_pass, new_pass)
        else:
            dbname = args.dbname or args.role
            teardown(args.admin_uri, args.role, dbname)
    except psycopg.Error as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
