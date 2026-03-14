from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class IRCMessage:
    tags: dict[str, str]
    prefix: str | None
    command: str
    params: list[str]


def parse_irc(line: str) -> IRCMessage:
    tags: dict[str, str] = {}
    prefix = None

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


EventCallback = Callable[[str, str, dict], Awaitable[None]]


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
    ):
        self.network_name = network_name
        self.host = host
        self.port = port
        self.tls = tls
        self.nick = nick
        self.user = user
        self.password = password
        self.on_event = on_event

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self.connected = False
        self.registered = False

    async def connect(self):
        ssl_ctx = None
        if self.tls:
            ssl_ctx = ssl.create_default_context()

        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx,
        )
        self.connected = True

        if self.password:
            await self.send("PASS", self.password)
        await self.send("NICK", self.nick)
        await self.send("USER", self.user, "0", "*", self.nick)

        self._read_task = asyncio.create_task(self._read_loop())

    async def disconnect(self):
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
        self.connected = False
        self.registered = False
        self._reader = None
        self._writer = None

    async def send(self, command: str, *params: str):
        if not self._writer:
            return
        line = format_irc(command, *params)
        self._writer.write(f"{line}\r\n".encode("utf-8"))
        await self._writer.drain()

    async def join(self, channel: str, key: str | None = None):
        if key:
            await self.send("JOIN", channel, key)
        else:
            await self.send("JOIN", channel)

    async def part(self, channel: str, message: str | None = None):
        if message:
            await self.send("PART", channel, message)
        else:
            await self.send("PART", channel)

    async def privmsg(self, target: str, text: str):
        await self.send("PRIVMSG", target, text)

    async def notice(self, target: str, text: str):
        await self.send("NOTICE", target, text)

    async def set_nick(self, new_nick: str):
        await self.send("NICK", new_nick)

    async def set_topic(self, channel: str, text: str):
        await self.send("TOPIC", channel, text)

    async def send_raw(self, raw_line: str):
        if not self._writer:
            return
        self._writer.write(f"{raw_line}\r\n".encode("utf-8"))
        await self._writer.drain()

    # -- read loop & handlers ------------------------------------------------

    async def _read_loop(self):
        try:
            while True:
                data = await self._reader.readline()
                if not data:
                    break
                line = data.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = parse_irc(line)
                    await self._handle(msg)
                except Exception:
                    pass
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            self.connected = False
            self.registered = False
            await self.on_event(
                self.network_name, "network_state", {"state": "disconnected"},
            )

    async def _handle(self, msg: IRCMessage):
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
            "MODE": self._on_mode,
        }.get(msg.command)

        if handler:
            await handler(msg)

    def _parse_prefix(self, prefix: str) -> tuple[str, str, str]:
        if "!" in prefix:
            nick, rest = prefix.split("!", 1)
            user, host = rest.split("@", 1) if "@" in rest else (rest, "")
            return nick, user, host
        return prefix, "", ""

    async def _on_ping(self, msg: IRCMessage):
        await self.send("PONG", *msg.params)

    async def _on_welcome(self, msg: IRCMessage):
        self.registered = True
        if msg.params:
            self.nick = msg.params[0]
        await self.on_event(
            self.network_name, "network_state", {"state": "connected"},
        )

    async def _on_nick_in_use(self, msg: IRCMessage):
        self.nick = self.nick + "_"
        await self.send("NICK", self.nick)

    async def _on_privmsg(self, msg: IRCMessage):
        nick, user, host = self._parse_prefix(msg.prefix or "")
        target = msg.params[0]
        text = msg.params[1] if len(msg.params) > 1 else ""

        msg_type = "privmsg"
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            msg_type = "action"
            text = text[8:-1]

        is_channel = target.startswith(("#", "&", "+", "!"))
        data: dict = {"from": nick, "type": msg_type, "text": text}
        if is_channel:
            data["channel"] = target
        else:
            data["nick"] = nick
        await self.on_event(self.network_name, "message", data)

    async def _on_notice(self, msg: IRCMessage):
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        target = msg.params[0]
        text = msg.params[1] if len(msg.params) > 1 else ""

        is_channel = target.startswith(("#", "&", "+", "!"))
        data: dict = {"from": nick, "type": "notice", "text": text}
        if is_channel:
            data["channel"] = target
        else:
            data["nick"] = nick
        await self.on_event(self.network_name, "message", data)

    async def _on_join(self, msg: IRCMessage):
        nick, user, host = self._parse_prefix(msg.prefix or "")
        channel = msg.params[0]
        await self.on_event(self.network_name, "join", {
            "channel": channel, "nick": nick, "user": user, "host": host,
        })

    async def _on_part(self, msg: IRCMessage):
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel = msg.params[0]
        message = msg.params[1] if len(msg.params) > 1 else ""
        await self.on_event(self.network_name, "part", {
            "channel": channel, "nick": nick, "message": message,
        })

    async def _on_quit(self, msg: IRCMessage):
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        message = msg.params[0] if msg.params else ""
        await self.on_event(self.network_name, "quit", {
            "nick": nick, "message": message,
        })

    async def _on_kick(self, msg: IRCMessage):
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel = msg.params[0]
        kicked = msg.params[1] if len(msg.params) > 1 else ""
        message = msg.params[2] if len(msg.params) > 2 else ""
        await self.on_event(self.network_name, "kick", {
            "channel": channel, "nick": kicked, "by": nick, "message": message,
        })

    async def _on_topic(self, msg: IRCMessage):
        nick, _, _ = self._parse_prefix(msg.prefix or "")
        channel = msg.params[0]
        text = msg.params[1] if len(msg.params) > 1 else ""
        await self.on_event(self.network_name, "topic", {
            "channel": channel, "text": text, "set_by": nick,
        })

    async def _on_topic_rpl(self, msg: IRCMessage):
        channel = msg.params[1] if len(msg.params) > 1 else ""
        text = msg.params[2] if len(msg.params) > 2 else ""
        await self.on_event(self.network_name, "topic", {
            "channel": channel, "text": text, "set_by": "",
        })

    async def _on_nick(self, msg: IRCMessage):
        old_nick, _, _ = self._parse_prefix(msg.prefix or "")
        new_nick = msg.params[0] if msg.params else ""
        if old_nick == self.nick:
            self.nick = new_nick
        await self.on_event(self.network_name, "nick", {
            "old_nick": old_nick, "new_nick": new_nick,
        })

    async def _on_names(self, msg: IRCMessage):
        channel = msg.params[2] if len(msg.params) > 2 else ""
        names_str = msg.params[3] if len(msg.params) > 3 else ""
        for name in names_str.split():
            prefix = ""
            while name and name[0] in "@+%~&":
                prefix += name[0]
                name = name[1:]
            await self.on_event(self.network_name, "_names", {
                "channel": channel, "nick": name, "prefix": prefix,
            })

    async def _on_mode(self, msg: IRCMessage):
        target = msg.params[0] if msg.params else ""
        modes = msg.params[1] if len(msg.params) > 1 else ""
        params = msg.params[2:]
        await self.on_event(self.network_name, "mode", {
            "target": target, "modes": modes, "params": params,
        })
