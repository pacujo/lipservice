from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from lipservice.auth import TokenEntry, require_auth, token_store
from lipservice.config import settings
from lipservice.irc import IRCClient
from lipservice.models import (
    ChannelJoin,
    ChannelResponse,
    MemberResponse,
    Message,
    MessagePage,
    MessageSend,
    NetworkConfig,
    NetworkCreate,
    NetworkResponse,
    NetworkUpdate,
    NetworkUserResponse,
    NickChange,
    QueryPeer,
    RawCommand,
    Session,
    SessionUpdate,
    StatusResponse,
    TokenRequest,
    TokenResponse,
    TopicUpdate,
    UserResponse,
)
from lipservice.state import ChannelState, NetworkState, ProxyState
from lipservice.storage import MemoryBackend, StorageBackend


def _make_storage() -> StorageBackend:
    backend = settings.storage_backend
    if backend == "postgres":
        from lipservice.pg_backend import PostgresBackend
        if not settings.database_uri:
            raise RuntimeError(
                "LIPSERVICE_STORAGE=postgres requires LIPSERVICE_DATABASE_URI",
            )
        return PostgresBackend(settings.database_uri, settings.password)
    if backend == "memory":
        return MemoryBackend(max_backlog=settings.max_backlog)
    raise RuntimeError(f"Unknown storage backend: {backend!r}")


router: APIRouter = APIRouter()
proxy: ProxyState = ProxyState(storage=_make_storage())


def _net_config(net: NetworkState) -> NetworkConfig:
    return NetworkConfig(
        name=net.name, host=net.host, port=net.port, tls=net.tls,
        nick=net.nick, server_password=net.server_password,
        nickserv_password=net.nickserv_password,
        auto_connect=net.state in ("connected", "connecting"),
    )


def _net_response(net: NetworkState) -> NetworkResponse:
    return NetworkResponse(
        name=net.name,
        host=net.host,
        port=net.port,
        tls=net.tls,
        nick=net.nick,
        state=net.state,
        channels=[n for n, ch in net.channels.items() if ch.joined],
    )


def _get_network(name: str) -> NetworkState:
    net = proxy.networks.get(name)
    if not net:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Network "{name}" does not exist.',
        })
    return net


def _get_connected_network(name: str) -> tuple[NetworkState, IRCClient]:
    net = _get_network(name)
    if net.state != "connected" or net.client is None:
        raise HTTPException(502, detail={
            "error": "upstream",
            "message": f'Network "{name}" is not connected.',
        })
    return net, net.client


# -- Auth -----------------------------------------------------------------

@router.post("/auth/token")
async def create_token(body: TokenRequest) -> TokenResponse:
    if body.username != settings.username or body.password != settings.password:
        raise HTTPException(401, detail={
            "error": "unauthorized", "message": "Bad credentials.",
        })
    entry = token_store.create(body.username)
    expires = datetime.fromtimestamp(entry.expires_at, tz=timezone.utc).isoformat()
    return TokenResponse(token=entry.token, expires_at=expires)


@router.delete("/auth/token", status_code=204)
async def revoke_token(request: Request, _auth: TokenEntry = Depends(require_auth)) -> None:
    raw: str = request.headers.get("Authorization", "")[7:]
    token_store.revoke(raw)


# -- Networks -------------------------------------------------------------

@router.get("/networks")
async def list_networks(
    _auth: TokenEntry = Depends(require_auth),
) -> list[NetworkResponse]:
    return [_net_response(n) for n in proxy.networks.values()]


@router.get("/networks/{name}")
async def get_network(
    name: str, _auth: TokenEntry = Depends(require_auth),
) -> NetworkResponse:
    return _net_response(_get_network(name))


@router.post("/networks", status_code=201)
async def create_network(
    body: NetworkCreate, _auth: TokenEntry = Depends(require_auth),
) -> NetworkResponse:
    if body.name in proxy.networks:
        raise HTTPException(409, detail={
            "error": "conflict",
            "message": f'Network "{body.name}" already exists.',
        })
    net = NetworkState(
        name=body.name,
        host=body.host,
        port=body.port,
        tls=body.tls,
        nick=body.nick,
        server_password=body.server_password,
        nickserv_password=body.nickserv_password,
    )
    proxy.networks[body.name] = net
    proxy.storage.save_network(_net_config(net))
    return _net_response(net)


_CONNECTION_FIELDS: set[str] = {"host", "port", "tls", "nick", "server_password"}


@router.patch("/networks/{name}")
async def update_network(
    name: str, body: NetworkUpdate, _auth: TokenEntry = Depends(require_auth),
) -> NetworkResponse:
    net = _get_network(name)
    updates = body.model_dump(exclude_unset=True)

    if net.client and updates.keys() & _CONNECTION_FIELDS:
        proxy.cancel_reconnect(net)
        await net.client.disconnect()
        net.client = None
        net.state = "disconnected"

    for field, value in updates.items():
        setattr(net, field, value)
    proxy.storage.save_network(_net_config(net))
    return _net_response(net)


@router.delete("/networks/{name}", status_code=204)
async def delete_network(
    name: str, _auth: TokenEntry = Depends(require_auth),
) -> None:
    net = _get_network(name)
    proxy.cancel_reconnect(net)
    if net.client:
        await net.client.disconnect()
    del proxy.networks[name]
    proxy.storage.delete_network(name)
    proxy.storage.remove_network_data(name)


@router.post("/networks/{name}/connect")
async def connect_network(
    name: str, _auth: TokenEntry = Depends(require_auth),
) -> NetworkResponse:
    net = _get_network(name)
    if net.state in ("connected", "connecting") and net.client:
        return _net_response(net)

    if net.client:
        await net.client.disconnect()
        net.client = None

    client = IRCClient(
        network_name=net.name,
        host=net.host,
        port=net.port,
        tls=net.tls,
        nick=net.nick,
        user=net.nick,
        password=net.server_password,
        on_event=proxy.handle_irc_event,
    )
    net.client = client
    net.state = "connecting"
    net.auto_reconnect = True
    net.reconnect_delay = 1.0
    try:
        await client.connect()
    except Exception as exc:
        net.state = "disconnected"
        net.client = None
        raise HTTPException(502, detail={
            "error": "upstream",
            "message": f"Failed to connect: {exc}",
        })
    proxy.storage.save_network(_net_config(net))
    return _net_response(net)


@router.post("/networks/{name}/disconnect")
async def disconnect_network(
    name: str, _auth: TokenEntry = Depends(require_auth),
) -> NetworkResponse:
    net = _get_network(name)
    proxy.cancel_reconnect(net)
    if net.client:
        await net.client.disconnect()
        net.client = None
    net.state = "disconnected"
    proxy.storage.save_network(_net_config(net))
    return _net_response(net)


# -- Channels -------------------------------------------------------------

@router.get("/networks/{network}/channels")
async def list_channels(
    network: str, _auth: TokenEntry = Depends(require_auth),
) -> list[ChannelResponse]:
    net = _get_network(network)
    return [
        ChannelResponse(
            name=ch.name, topic=ch.topic,
            joined=ch.joined, members_count=len(ch.members),
        )
        for ch in net.channels.values()
    ]


@router.get("/networks/{network}/channels/{channel}")
async def get_channel(
    network: str, channel: str, _auth: TokenEntry = Depends(require_auth),
) -> ChannelResponse:
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    return ChannelResponse(
        name=ch.name, topic=ch.topic, topic_set_by=ch.topic_set_by,
        joined=ch.joined, members_count=len(ch.members),
    )


@router.post("/networks/{network}/channels", status_code=201)
async def join_channel(
    network: str, body: ChannelJoin, _auth: TokenEntry = Depends(require_auth),
) -> ChannelResponse:
    net, client = _get_connected_network(network)
    ch_existing = net.channels.get(body.name)
    if ch_existing and ch_existing.joined:
        raise HTTPException(409, detail={
            "error": "conflict",
            "message": f'Already joined "{body.name}".',
        })
    fut = proxy.expect_join(network, body.name)
    await client.join(body.name, body.key)
    try:
        await asyncio.wait_for(fut, timeout=5.0)
    except TimeoutError:
        raise HTTPException(504, detail={
            "error": "timeout",
            "message": f'Server did not confirm join for "{body.name}".',
        })
    except RuntimeError as exc:
        raise HTTPException(502, detail={
            "error": "join_rejected",
            "message": str(exc),
        })
    ch = net.channels.get(body.name)
    return ChannelResponse(
        name=body.name,
        topic=ch.topic if ch else "",
        joined=ch.joined if ch else False,
        members_count=len(ch.members) if ch else 0,
    )


@router.delete("/networks/{network}/channels/{channel}", status_code=204)
async def part_channel(
    network: str, channel: str, _auth: TokenEntry = Depends(require_auth),
) -> None:
    _net, client = _get_connected_network(network)
    await client.part(channel)


@router.put("/networks/{network}/channels/{channel}/topic")
async def set_topic(
    network: str, channel: str, body: TopicUpdate,
    _auth: TokenEntry = Depends(require_auth),
) -> ChannelResponse:
    net, client = _get_connected_network(network)
    await client.set_topic(channel, body.text)
    await asyncio.sleep(0.3)
    ch = net.channels.get(channel)
    return ChannelResponse(
        name=channel,
        topic=ch.topic if ch else body.text,
        joined=True,
        members_count=len(ch.members) if ch else 0,
    )


# -- Members --------------------------------------------------------------

@router.get("/networks/{network}/channels/{channel}/members")
async def list_members(
    network: str, channel: str, _auth: TokenEntry = Depends(require_auth),
) -> list[MemberResponse]:
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    return [
        MemberResponse(nick=m.nick, prefix=m.prefix, user=m.user, host=m.host)
        for m in ch.members.values()
    ]


# -- Messages -------------------------------------------------------------

@router.get("/networks/{network}/channels/{channel}/messages")
async def list_channel_messages(
    network: str,
    channel: str,
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None),
    after: str | None = Query(None),
    _auth: TokenEntry = Depends(require_auth),
) -> MessagePage:
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    ch_msgs = proxy.storage.get_channel_messages(network, channel)
    meta = proxy.storage.get_meta_messages(network)
    merged = sorted(ch_msgs + meta, key=lambda m: m["id"])
    return _paginate_messages(merged, limit, before, after)


@router.post("/networks/{network}/channels/{channel}/messages", status_code=201)
async def send_channel_message(
    network: str,
    channel: str,
    body: MessageSend,
    _auth: TokenEntry = Depends(require_auth),
) -> Message:
    net, client = _get_connected_network(network)
    if body.type == "notice":
        await client.notice(channel, body.text)
    elif body.type == "action":
        await client.privmsg(channel, f"\x01ACTION {body.text}\x01")
    else:
        await client.privmsg(channel, body.text)

    now = datetime.now(timezone.utc).isoformat()
    msg_id = proxy.storage.next_message_id()
    msg: Message = {
        "id": msg_id, "time": now, "from": net.nick, "type": body.type, "text": body.text,
    }

    if channel not in net.channels:
        net.channels[channel] = ChannelState(name=channel)
    proxy.storage.append_channel_message(network, channel, msg)
    await proxy.event_bus.publish("message", {
        "network": network, "channel": channel, **msg,
    })

    return msg


@router.get("/networks/{network}/queries")
async def list_queries(
    network: str, _auth: TokenEntry = Depends(require_auth),
) -> list[QueryPeer]:
    _get_network(network)
    peers = proxy.storage.list_private_peers(network)
    return [QueryPeer(nick=p) for p in peers]


@router.get("/networks/{network}/messages/{nick}")
async def list_private_messages(
    network: str,
    nick: str,
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None),
    after: str | None = Query(None),
    _auth: TokenEntry = Depends(require_auth),
) -> MessagePage:
    _get_network(network)
    msgs = proxy.storage.get_private_messages(network, nick)
    meta = proxy.storage.get_meta_messages(network)
    merged = sorted(msgs + meta, key=lambda m: m["id"])
    return _paginate_messages(merged, limit, before, after)


@router.delete("/networks/{network}/messages/{nick}", status_code=204)
async def close_query(
    network: str,
    nick: str,
    _auth: TokenEntry = Depends(require_auth),
) -> None:
    _get_network(network)
    proxy.storage.remove_private_peer(network, nick)


@router.post("/networks/{network}/messages/{nick}", status_code=201)
async def send_private_message(
    network: str,
    nick: str,
    body: MessageSend,
    _auth: TokenEntry = Depends(require_auth),
) -> Message:
    net, client = _get_connected_network(network)
    if body.type == "notice":
        await client.notice(nick, body.text)
    elif body.type == "action":
        await client.privmsg(nick, f"\x01ACTION {body.text}\x01")
    else:
        await client.privmsg(nick, body.text)

    now = datetime.now(timezone.utc).isoformat()
    msg_id = proxy.storage.next_message_id()
    msg: Message = {
        "id": msg_id, "time": now, "from": net.nick, "type": body.type, "text": body.text,
    }

    proxy.storage.append_private_message(network, nick, msg)
    await proxy.event_bus.publish("message", {
        "network": network, "nick": nick, **msg,
    })

    return msg


def _paginate_messages(
    buf: list[Message], limit: int,
    before: str | None, after: str | None,
) -> MessagePage:
    msgs = list(buf)
    if before:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == before), None)
        if idx is not None:
            msgs = msgs[:idx]
    if after:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == after), None)
        if idx is not None:
            msgs = msgs[idx + 1:]
    has_more = len(msgs) > limit
    msgs = msgs[-limit:]
    return MessagePage(messages=msgs, has_more=has_more)


# -- User -----------------------------------------------------------------

@router.get("/user")
async def get_user(
    _auth: TokenEntry = Depends(require_auth),
) -> UserResponse:
    return UserResponse(
        username=_auth.username,
        networks=list(proxy.networks.keys()),
    )


def _network_user_response(net: NetworkState) -> NetworkUserResponse:
    return NetworkUserResponse(
        nick=net.nick,
        user=net.irc_user or net.nick,
        host=net.irc_host,
        modes=net.modes,
    )


@router.get("/networks/{network}/user")
async def get_network_user(
    network: str, _auth: TokenEntry = Depends(require_auth),
) -> NetworkUserResponse:
    return _network_user_response(_get_network(network))


@router.put("/networks/{network}/user/nick")
async def change_nick(
    network: str, body: NickChange, _auth: TokenEntry = Depends(require_auth),
) -> NetworkUserResponse:
    net, client = _get_connected_network(network)
    await client.set_nick(body.nick)
    await asyncio.sleep(0.3)
    return _network_user_response(net)


# -- Raw ------------------------------------------------------------------

@router.post("/networks/{network}/raw", status_code=202)
async def send_raw(
    network: str, body: RawCommand, _auth: TokenEntry = Depends(require_auth),
) -> dict[str, str]:
    _net, client = _get_connected_network(network)
    await client.send_raw(body.command)
    return {"status": "accepted"}


# -- Events (SSE) ---------------------------------------------------------

@router.get("/events")
async def events(
    request: Request,
    networks: str | None = Query(None),
    channels: str | None = Query(None),
    _auth: TokenEntry = Depends(require_auth),
) -> EventSourceResponse:
    net_filter: set[str] | None = set(networks.split(",")) if networks else None
    ch_filter: set[str] | None = set(channels.split(",")) if channels else None

    queue = proxy.event_bus.subscribe()

    async def generate() -> AsyncGenerator[dict[str, str], None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
                    continue

                data = event["data"]
                if net_filter and data.get("network") not in net_filter:
                    continue
                if ch_filter and data.get("channel") not in ch_filter:
                    continue

                yield {
                    "event": event["event"],
                    "id": event["id"],
                    "data": json.dumps(data),
                }
        finally:
            proxy.event_bus.unsubscribe(queue)

    return EventSourceResponse(generate())


# -- Session --------------------------------------------------------------

@router.get("/session")
async def get_session(
    _auth: TokenEntry = Depends(require_auth),
) -> Session:
    return proxy.storage.get_session()


@router.put("/session", status_code=204)
async def set_session(
    body: SessionUpdate, _auth: TokenEntry = Depends(require_auth),
) -> None:
    proxy.storage.set_session(
        body.current_network, body.current_channel, body.current_query,
    )
    if body.pointers:
        for key, last_read_id in body.pointers.items():
            parts = key.split("/", 1)
            if len(parts) == 2:
                proxy.storage.set_pointer(parts[0], parts[1], last_read_id)


# -- Status ---------------------------------------------------------------

@router.get("/status")
async def status(
    _auth: TokenEntry = Depends(require_auth),
) -> StatusResponse:
    connected = sum(1 for n in proxy.networks.values() if n.state == "connected")
    return StatusResponse(
        version="0.2.0",
        uptime_seconds=int(time.time() - proxy.start_time),
        networks_connected=connected,
        networks_disconnected=len(proxy.networks) - connected,
    )
