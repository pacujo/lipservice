from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_KEEPALIVE_INTERVAL = 60
_KEEPALIVE_TIMEOUT = 120


@dataclass
class IRCMessage:
    tags: dict[str, str]
    prefix: str | None
    command: str
    params: list[str]


def parse_irc(line: str) -> IRCMessage:
    tags: dict[str, str] = {}
    prefix: str | None = None

    if line.startswith("@"):
        tag_str, line = line[1:].split(" ", 1)
        for tag in tag_str.split(";"):
            if "=" in tag:
                k, v = tag.split("=", 1)
                tags[k] = v
            else:
                tags[tag] = ""

    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)

    if " :" in line:
        head, trailing = line.split(" :", 1)
        parts = head.split()
        command = parts[0]
        params = parts[1:] + [trailing]
    else:
        parts = line.split()
        command = parts[0]
        params = parts[1:]

    return IRCMessage(tags=tags, prefix=prefix, command=command.upper(), params=params)


def format_irc(command: str, *params: str) -> str:
    if not params:
        return command
    *head, tail = params
    if " " in tail or tail.startswith(":"):
        return " ".join([command] + head + [f":{tail}"])
    return " ".join([command] + head + [tail])


EventCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class IRCClient:
    def __init__(
        self,
        network_name: str,
        host: str,
        port: int,
        tls: bool,
        nick: str,
        user: str,
        password: str | None,
        on_event: EventCallback,
    ) -> None:
        self.network_name: str = network_name
        self.host: str = host
        self.port: int = port
        self.tls: bool = tls
        self.nick: str = nick
        self.user: str = user
        self.password: str | None = password
        self.on_event: EventCallback = on_event

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_data: float = 0.0
        self.connected: bool = False
        self.registered: bool = False

    async def connect(self) -> None:
        ssl_ctx: ssl.SSLContext | None = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()

        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx,
        )
        self.connected = True
        self._last_data = time.monotonic()

        if self.password:
            await self.send("PASS", self.password)
        await self.send("NICK", self.nick)
        await self.send("USER", self.user, "0", "*", self.nick)

        self._read_task = asyncio.create_task(self._read_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def disconnect(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        if self._writer:
            try:
                await self.send("QUIT", "Lipservice")
            except Exception:
                pass
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        self.connected = False
        self.registered = False
        self._reader = None
        self._writer = None

    async def send(self, command: str, *params: str) -> None:
        if not self._writer:
            return
        line: str = format_irc(command, *params)
        self._writer.write(f"{line}\r\n".encode("utf-8"))
        await self._writer.drain()

    async def join(self, channel: str, key: str | None = None) -> None:
        if key:
            await self.send("JOIN", channel, key)
        else:
            await self.send("JOIN", channel)

    async def part(self, channel: str, message: str | None = None) -> None:
        if message:
            await self.send("PART", channel, message)
        else:
            await self.send("PART", channel)

    async def privmsg(self, target: str, text: str) -> None:
        await self.send("PRIVMSG", target, text)

    async def notice(self, target: str, text: str) -> None:
        await self.send("NOTICE", target, text)

    async def set_nick(self, new_nick: str) -> None:
        await self.send("NICK", new_nick)

    async def set_topic(self, channel: str, text: str) -> None:
        await self.send("TOPIC", channel, text)

    async def send_raw(self, raw_line: str) -> None:
        if not self._writer:
            return
        self._writer.write(f"{raw_line}\r\n".encode("utf-8"))
        await self._writer.drain()

    # -- keepalive -----------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        try:
            while self.connected:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                if not self.connected:
                    break
                elapsed: float = time.monotonic() - self._last_data
                if elapsed >= _KEEPALIVE_TIMEOUT:
                    if self._writer:
                        self._writer.close()
                    break
                if elapsed >= _KEEPALIVE_INTERVAL:
                    try:
                        await self.send("PING", "lipservice")
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    # -- read loop & handlers ------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.readline()
                if not data:
                    break
                self._last_data = time.monotonic()
                line = data.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = parse_irc(line)
                    await self._handle(msg)
                except Exception:
                    pass
        except (asyncio.CancelledError, ConnectionError, ssl.SSLError):
            pass
        finally:
            self.connected = False
            self.registered = False
            await self.on_event(
                self.network_name, "network_state", {"state": "disconnected"},
            )

    async def _handle(self, msg: IRCMessage) -> None:
        handler = {
            "PING": self._on_ping,
            "001": self._on_welcome,
            "433": self._on_nick_in_use,
            "PRIVMSG": self._on_privmsg,
            "NOTICE": self._on_notice,
            "JOIN": self._on_join,
            "PART": self._on_part,
            "QUIT": self._on_quit,
            "KICK": self._on_kick,
            "TOPIC": self._on_topic,
            "332": self._on_topic_rpl,
            "NICK": self._on_nick,
            "353": self._on_names,
            "366": self._on_names_end,
            "MODE": self._on_mode,
            "403": self._on_join_error,
            "405": self._on_join_error,
            "471": self._on_join_error,
            "473": self._on_join_error,
            "474": self._on_join_error,
            "475": self._on_join_error,
            "476": self._on_join_error,
        }.get(msg.command)

        if handler:
            await handler(msg)

    def _parse_prefix(self, prefix: str) -> tuple[str, str, str]:
        if "!" in prefix:
            nick, rest = prefix.split("!", 1)
            user, host = rest.split("@", 1) if "@" in rest else (rest, "")
            return nick, user, host
        return prefix, "", ""

    async def _on_ping(self, msg: IRCMessage) -> None:
        await self.send("PONG", *msg.params)

    async def _on_welcome(self, msg: IRCMessage) -> None:
        self.registered = True
        if msg.params:
            self.nick = msg.params[0]
        await self.on_event(
            self.network_name, "network_state", {"state": "connected"},
        )

    async def _on_nick_in_use(self, msg: IRCMessage) -> None:
        self.nick = self.nick + "_"
        await self.send("NICK", self.nick)

    async def _handle_ctcp(self, nick: str, ctcp: str) -> None:
        cmd = ctcp.split(" ", 1)[0].upper()
        if cmd == "VERSION":
            await self.send(
                "NOTICE", nick, f"\x01VERSION lipservice 0.2\x01",
            )
        elif cmd == "PING":
            await self.send("NOTICE", nick, f"\x01{ctcp}\x01")

    async def _on_privmsg(self, msg: IRCMessage) -> None:
        nick, user, host = self._parse_prefix(msg.prefix or "")
        target: str = msg.params[0]
        text: str = msg.params[1] if len(msg.params) > 1 else ""

        msg_type: str = "privmsg"
        if text.startswith("\x01") and text.endswith("\x01"):
            ctcp = text[1:-1]
            if ctcp.startswith("ACTION "):
                msg_type = "action"
                text = ctcp[7:]
            else:
                await self._handle_ctcp(nick, ctcp)
                return

        is_channel = target.startswith(("#", "&", "+", "!"))
        data: dict[str, str] = {"from": nick, "type": msg_type, "text": text}
        if is_channel:
            data["channel"] = target
        else:
            data["nick"] = nick
        await self.on_event(self.network_name, "message", data)

    async def _on_notice(self, msg: IRCMessage) -> None:
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        target: str = msg.params[0]
        text: str = msg.params[1] if len(msg.params) > 1 else ""

        if text.startswith("\x01") and text.endswith("\x01"):
            return

        is_channel = target.startswith(("#", "&", "+", "!"))
        data: dict[str, str] = {"from": nick, "type": "notice", "text": text}
        if is_channel:
            data["channel"] = target
        else:
            data["nick"] = nick
        await self.on_event(self.network_name, "message", data)

    async def _on_join(self, msg: IRCMessage) -> None:
        nick, user, host = self._parse_prefix(msg.prefix or "")
        channel: str = msg.params[0]
        await self.on_event(self.network_name, "join", {
            "channel": channel, "nick": nick, "user": user, "host": host,
        })

    async def _on_part(self, msg: IRCMessage) -> None:
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel: str = msg.params[0]
        message: str = msg.params[1] if len(msg.params) > 1 else ""
        await self.on_event(self.network_name, "part", {
            "channel": channel, "nick": nick, "message": message,
        })

    async def _on_quit(self, msg: IRCMessage) -> None:
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        message: str = msg.params[0] if msg.params else ""
        await self.on_event(self.network_name, "quit", {
            "nick": nick, "message": message,
        })

    async def _on_kick(self, msg: IRCMessage) -> None:
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel: str = msg.params[0]
        kicked: str = msg.params[1] if len(msg.params) > 1 else ""
        message: str = msg.params[2] if len(msg.params) > 2 else ""
        await self.on_event(self.network_name, "kick", {
            "channel": channel, "nick": kicked, "by": nick, "message": message,
        })

    async def _on_topic(self, msg: IRCMessage) -> None:
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel: str = msg.params[0]
        text: str = msg.params[1] if len(msg.params) > 1 else ""
        await self.on_event(self.network_name, "topic", {
            "channel": channel, "text": text, "set_by": nick,
        })

    async def _on_topic_rpl(self, msg: IRCMessage) -> None:
        channel: str = msg.params[1] if len(msg.params) > 1 else ""
        text: str = msg.params[2] if len(msg.params) > 2 else ""
        await self.on_event(self.network_name, "topic", {
            "channel": channel, "text": text, "set_by": "",
        })

    async def _on_nick(self, msg: IRCMessage) -> None:
        old_nick, _, _ = self._parse_prefix(msg.prefix or "")
        new_nick: str = msg.params[0] if msg.params else ""
        if old_nick == self.nick:
            self.nick = new_nick
        await self.on_event(self.network_name, "nick", {
            "old_nick": old_nick, "new_nick": new_nick,
        })

    async def _on_names(self, msg: IRCMessage) -> None:
        channel: str = msg.params[2] if len(msg.params) > 2 else ""
        names_str: str = msg.params[3] if len(msg.params) > 3 else ""
        for name in names_str.split():
            prefix = ""
            while name and name[0] in "@+%~&":
                prefix += name[0]
                name = name[1:]
            await self.on_event(self.network_name, "_names", {
                "channel": channel, "nick": name, "prefix": prefix,
            })

    async def _on_names_end(self, msg: IRCMessage) -> None:
        channel: str = msg.params[1] if len(msg.params) > 1 else ""
        await self.on_event(self.network_name, "_names_end", {
            "channel": channel,
        })

    async def _on_mode(self, msg: IRCMessage) -> None:
        target: str = msg.params[0] if msg.params else ""
        modes: str = msg.params[1] if len(msg.params) > 1 else ""
        params: list[str] = msg.params[2:]
        await self.on_event(self.network_name, "mode", {
            "target": target, "modes": modes, "params": params,
        })

    async def _on_join_error(self, msg: IRCMessage) -> None:
        channel: str = msg.params[1] if len(msg.params) > 1 else ""
        reason: str = msg.params[-1] if msg.params else "Cannot join channel"
        await self.on_event(self.network_name, "join_error", {
            "channel": channel, "code": msg.command, "reason": reason,
        })
