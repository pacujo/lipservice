from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lipservice.irc import IRCClient
    from lipservice.models import Message, NetworkConfig
    from lipservice.storage import StorageBackend

log: logging.Logger = logging.getLogger(__name__)

_RECONNECT_DELAY_MAX: float = 300.0


@dataclass
class MemberInfo:
    nick: str
    prefix: str = ""
    user: str = ""
    host: str = ""


class ChannelState:
    __slots__ = ("name", "topic", "topic_set_by", "members", "joined")

    def __init__(
        self, name: str, *, topic: str = "", topic_set_by: str = "",
        joined: bool = True,
    ) -> None:
        self.name = name
        self.topic = topic
        self.topic_set_by = topic_set_by
        self.members: dict[str, MemberInfo] = {}
        self.joined: bool = joined


@dataclass
class NetworkState:
    name: str
    host: str
    port: int
    tls: bool
    nick: str
    server_password: str | None = None
    nickserv_password: str | None = None
    state: str = "disconnected"
    channels: dict[str, ChannelState] = field(default_factory=dict)
    irc_user: str = ""
    irc_host: str = ""
    modes: str = ""
    client: IRCClient | None = None
    read_task: asyncio.Task[None] | None = None
    auto_reconnect: bool = False
    reconnect_delay: float = 1.0
    reconnect_task: asyncio.Task[None] | None = None


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._counter: int = 0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        self._counter += 1
        event: dict[str, Any] = {
            "id": f"evt_{self._counter:06d}",
            "event": event_type,
            "data": data,
        }
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class ProxyState:
    def __init__(self, storage: StorageBackend) -> None:
        self.networks: dict[str, NetworkState] = {}
        self.event_bus: EventBus = EventBus()
        self.start_time: float = time.time()
        self.storage: StorageBackend = storage
        self._join_waiters: dict[
            tuple[str, str], asyncio.Future[None]
        ] = {}

    def expect_join(
        self, network: str, channel: str,
    ) -> asyncio.Future[None]:
        key = (network, channel)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._join_waiters[key] = fut
        return fut

    def _resolve_join(
        self, network: str, channel: str, error: str | None = None,
    ) -> None:
        key = (network, channel)
        fut = self._join_waiters.pop(key, None)
        if fut and not fut.done():
            if error:
                fut.set_exception(RuntimeError(error))
            else:
                fut.set_result(None)

    def _inject_meta(self, net: NetworkState, text: str) -> None:
        msg: Message = {
            "id": self.storage.next_message_id(),
            "time": datetime.now(timezone.utc).isoformat(),
            "from": "",
            "type": "meta",
            "text": text,
        }
        self.storage.append_meta_message(net.name, msg)

    def _persist_network(self, net: NetworkState) -> None:
        from lipservice.models import NetworkConfig
        self.storage.save_network(NetworkConfig(
            name=net.name, host=net.host, port=net.port, tls=net.tls,
            nick=net.nick, server_password=net.server_password,
            nickserv_password=net.nickserv_password,
            auto_connect=net.state in ("connected", "connecting"),
            channels=[
                name for name, ch in net.channels.items() if ch.joined
            ],
        ))

    async def _inject_channel_meta(
        self, network_name: str, channel: str, text: str,
    ) -> None:
        msg: Message = {
            "id": self.storage.next_message_id(),
            "time": datetime.now(timezone.utc).isoformat(),
            "from": "",
            "type": "meta",
            "text": text,
        }
        self.storage.append_channel_message(network_name, channel, msg)
        await self.event_bus.publish("message", {
            "network": network_name, "channel": channel, **msg,
        })

    async def handle_irc_event(
        self, network_name: str, event_type: str, data: dict[str, Any],
    ) -> None:
        net = self.networks.get(network_name)
        if not net:
            return

        now: str = datetime.now(timezone.utc).isoformat()

        if event_type == "network_state":
            prev_state = net.state
            net.state = data["state"]
            if data["state"] == "connected":
                net.reconnect_delay = 1.0
                if prev_state == "connecting":
                    self._inject_meta(net, f"Connected to {net.host}")
                if net.nickserv_password and net.client:
                    try:
                        await net.client.privmsg(
                            "NickServ", f"IDENTIFY {net.nickserv_password}",
                        )
                    except Exception:
                        log.warning("NickServ IDENTIFY failed for %s",
                                    network_name)
                channels_to_rejoin = [
                    name for name, ch in net.channels.items() if ch.joined
                ]
                if channels_to_rejoin and net.client:
                    for ch_name in channels_to_rejoin:
                        try:
                            await net.client.join(ch_name)
                        except Exception:
                            pass
            elif data["state"] == "disconnected":
                if prev_state in ("connected", "connecting"):
                    self._inject_meta(net, f"Disconnected from {net.host}")
                for ch in net.channels.values():
                    ch.members.clear()
                if net.auto_reconnect:
                    self._start_reconnect(network_name)
            await self.event_bus.publish("network_state", {
                "network": network_name,
                "state": data["state"],
            })

        elif event_type == "message":
            msg_id = self.storage.next_message_id()
            msg: Message = {
                "id": msg_id,
                "time": now,
                "from": data["from"],
                "type": data["type"],
                "text": data["text"],
            }
            if "channel" in data:
                channel: str = data["channel"]
                if channel not in net.channels:
                    net.channels[channel] = ChannelState(name=channel)
                self.storage.append_channel_message(
                    network_name, channel, msg,
                )
                await self.event_bus.publish("message", {
                    "network": network_name, "channel": channel, **msg,
                })
            else:
                nick: str = data.get("nick", data["from"])
                self.storage.append_private_message(
                    network_name, nick, msg,
                )
                await self.event_bus.publish("message", {
                    "network": network_name, "nick": nick, **msg,
                })

        elif event_type == "join":
            channel = data["channel"]
            if channel not in net.channels:
                net.channels[channel] = ChannelState(name=channel)
            if data["nick"] == net.nick:
                net.channels[channel].joined = True
            net.channels[channel].members[data["nick"]] = MemberInfo(
                nick=data["nick"],
                user=data.get("user", ""),
                host=data.get("host", ""),
            )
            if data["nick"] == net.nick:
                self._resolve_join(network_name, channel)
                self._persist_network(net)
            await self._inject_channel_meta(
                network_name, channel,
                f"{data['nick']} has joined",
            )
            await self.event_bus.publish("join", {
                "network": network_name, **data,
            })

        elif event_type == "join_error":
            channel = data["channel"]
            self._resolve_join(
                network_name, channel, data.get("reason", "Cannot join"),
            )

        elif event_type == "part":
            channel = data["channel"]
            part_text = f"{data['nick']} has left"
            if data.get("message"):
                part_text += f" ({data['message']})"
            await self._inject_channel_meta(
                network_name, channel, part_text,
            )
            if channel in net.channels:
                ch = net.channels[channel]
                ch.members.pop(data["nick"], None)
                if data["nick"] == net.nick:
                    ch.joined = False
                    ch.members.clear()
                    self._persist_network(net)
            await self.event_bus.publish("part", {
                "network": network_name, **data,
            })

        elif event_type == "quit":
            quit_text = f"{data['nick']} has quit"
            if data.get("message"):
                quit_text += f" ({data['message']})"
            for ch in net.channels.values():
                if data["nick"] in ch.members:
                    ch.members.pop(data["nick"])
                    await self._inject_channel_meta(
                        network_name, ch.name, quit_text,
                    )
            await self.event_bus.publish("quit", {
                "network": network_name, **data,
            })

        elif event_type == "kick":
            channel = data["channel"]
            kick_text = f"{data['nick']} was kicked by {data.get('by', '?')}"
            if data.get("message"):
                kick_text += f" ({data['message']})"
            await self._inject_channel_meta(
                network_name, channel, kick_text,
            )
            if channel in net.channels:
                net.channels[channel].members.pop(data["nick"], None)
                if data["nick"] == net.nick:
                    del net.channels[channel]
                    self._persist_network(net)
            await self.event_bus.publish("kick", {
                "network": network_name, **data,
            })

        elif event_type == "topic":
            channel = data["channel"]
            if channel in net.channels:
                net.channels[channel].topic = data.get("text", "")
                net.channels[channel].topic_set_by = data.get("set_by", "")
            await self.event_bus.publish("topic", {
                "network": network_name, **data,
            })

        elif event_type == "nick":
            if data["old_nick"] == net.nick:
                net.nick = data["new_nick"]
            for ch in net.channels.values():
                if data["old_nick"] in ch.members:
                    member = ch.members.pop(data["old_nick"])
                    member.nick = data["new_nick"]
                    ch.members[data["new_nick"]] = member
            await self.event_bus.publish("nick", {
                "network": network_name, **data,
            })

        elif event_type == "_names":
            channel = data["channel"]
            if channel not in net.channels:
                net.channels[channel] = ChannelState(name=channel)
            net.channels[channel].members[data["nick"]] = MemberInfo(
                nick=data["nick"], prefix=data.get("prefix", ""),
            )

        elif event_type == "_names_end":
            channel = data["channel"]
            if channel in net.channels:
                members = net.channels[channel].members
                nicks = sorted(
                    (m.prefix or "") + m.nick for m in members.values()
                )
                await self._inject_channel_meta(
                    network_name, channel,
                    f"Users ({len(nicks)}): {', '.join(nicks)}",
                )

        elif event_type == "mode":
            await self.event_bus.publish("mode", {
                "network": network_name, **data,
            })

        elif event_type == "error":
            await self.event_bus.publish("error", {
                "network": network_name, **data,
            })

        elif event_type == "raw":
            await self.event_bus.publish("raw", {
                "network": network_name, **data,
            })

    # -- auto-reconnect ------------------------------------------------------

    def _start_reconnect(self, network_name: str) -> None:
        net = self.networks.get(network_name)
        if not net:
            return
        if net.reconnect_task and not net.reconnect_task.done():
            net.reconnect_task.cancel()
        net.reconnect_task = asyncio.create_task(
            self._reconnect_loop(network_name),
        )

    async def _reconnect_loop(self, network_name: str) -> None:
        from lipservice.irc import IRCClient

        while True:
            net = self.networks.get(network_name)
            if not net or not net.auto_reconnect:
                return

            delay = net.reconnect_delay
            log.info(
                "Reconnecting to %s in %.0f s", network_name, delay,
            )
            await asyncio.sleep(delay)

            net = self.networks.get(network_name)
            if not net or not net.auto_reconnect:
                return

            self._inject_meta(
                net, f"Reconnecting to {net.host} (attempt after {delay:.0f} s)",
            )
            client = IRCClient(
                network_name=net.name,
                host=net.host,
                port=net.port,
                tls=net.tls,
                nick=net.nick,
                user=net.nick,
                password=net.server_password,
                on_event=self.handle_irc_event,
            )
            net.client = client
            net.state = "connecting"
            try:
                await client.connect()
                log.info("Reconnected to %s (TCP up)", network_name)
                return
            except Exception:
                log.warning(
                    "Reconnect to %s failed, backing off", network_name,
                )
                net.state = "disconnected"
                net.client = None
                net.reconnect_delay = min(
                    net.reconnect_delay * 2, _RECONNECT_DELAY_MAX,
                )

    def cancel_reconnect(self, net: NetworkState) -> None:
        net.auto_reconnect = False
        if net.reconnect_task and not net.reconnect_task.done():
            net.reconnect_task.cancel()
            net.reconnect_task = None

    async def restore_networks(self) -> None:
        from lipservice.irc import IRCClient

        configs = self.storage.list_networks()
        for cfg in configs:
            net = NetworkState(
                name=cfg.name, host=cfg.host, port=cfg.port,
                tls=cfg.tls, nick=cfg.nick,
                server_password=cfg.server_password,
                nickserv_password=cfg.nickserv_password,
            )
            self.networks[cfg.name] = net
            for ch_name in cfg.channels:
                net.channels[ch_name] = ChannelState(
                    name=ch_name, joined=True,
                )
            self._inject_meta(net, "lipservice started")
            if cfg.auto_connect:
                client = IRCClient(
                    network_name=net.name, host=net.host, port=net.port,
                    tls=net.tls, nick=net.nick, user=net.nick,
                    password=net.server_password,
                    on_event=self.handle_irc_event,
                )
                net.client = client
                net.state = "connecting"
                net.auto_reconnect = True
                try:
                    await client.connect()
                    log.info("Restored connection to %s", cfg.name)
                except Exception:
                    log.warning("Failed to restore %s, will reconnect",
                                cfg.name)
                    net.state = "disconnected"
                    net.client = None
                    self._start_reconnect(cfg.name)

    async def shutdown(self) -> None:
        for net in self.networks.values():
            self._inject_meta(net, "lipservice stopping")
            self.cancel_reconnect(net)
            if net.client:
                await net.client.disconnect()
                net.client = None
