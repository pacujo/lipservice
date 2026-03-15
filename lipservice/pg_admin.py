"""Set up or tear down the lipservice PostgreSQL database.

Usage:
    python -m lipservice.pg_admin setup   --admin-uri URI [--role NAME] [--dbname NAME]
    python -m lipservice.pg_admin teardown --admin-uri URI [--role NAME] [--dbname NAME]

The admin URI connects as a role with CREATEROLE + CREATEDB privileges.
The tool creates (or drops) the specified role and a database of the same
name, then applies the schema.  On setup a random password is generated
and the resulting connection URI is printed.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

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
"""


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


def setup(admin_uri: str, role: str, dbname: str) -> None:
    password = secrets.token_urlsafe(24)
    conn = psycopg.connect(admin_uri, autocommit=True)
    try:
        cur = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,),
        )
        if cur.fetchone():
            print(f"Role {role!r} already exists.", file=sys.stderr)
        else:
            conn.execute(sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password)))
            print(f"Created role {role!r}.", file=sys.stderr)

        cur = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,),
        )
        if cur.fetchone():
            print(f"Database {dbname!r} already exists.", file=sys.stderr)
        else:
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
        print("Schema applied.", file=sys.stderr)
    finally:
        app_conn.close()

    print(app_conn_uri)


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
    parser.add_argument("action", choices=["setup", "teardown"])
    parser.add_argument(
        "--admin-uri", required=True,
        help="admin connection URI (e.g. postgres://postgres@localhost)",
    )
    parser.add_argument(
        "--role", default="lipservice",
        help="application role name (default: lipservice)",
    )
    parser.add_argument(
        "--dbname", default=None,
        help="database name (default: same as --role)",
    )
    args = parser.parse_args()
    dbname: str = args.dbname or args.role

    try:
        if args.action == "setup":
            setup(args.admin_uri, args.role, dbname)
        else:
            teardown(args.admin_uri, args.role, dbname)
    except psycopg.Error as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
