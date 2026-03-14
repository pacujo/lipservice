from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from lipservice.auth import TokenEntry, require_auth, token_store
from lipservice.config import settings
from lipservice.irc import IRCClient
from lipservice.models import (
    ChannelJoin,
    ChannelResponse,
    ErrorResponse,
    MemberResponse,
    MessageSend,
    NetworkCreate,
    NetworkResponse,
    NetworkUpdate,
    NickChange,
    RawCommand,
    StatusResponse,
    TokenRequest,
    TokenResponse,
    TopicUpdate,
)
from lipservice.state import NetworkState, ProxyState

router = APIRouter()
proxy = ProxyState(max_backlog=settings.max_backlog)


def _net_response(net: NetworkState) -> dict:
    return {
        "name": net.name,
        "host": net.host,
        "port": net.port,
        "tls": net.tls,
        "nick": net.nick,
        "state": net.state,
        "channels": list(net.channels.keys()),
    }


def _get_network(name: str) -> NetworkState:
    net = proxy.networks.get(name)
    if not net:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Network "{name}" does not exist.',
        })
    return net


def _get_connected_network(name: str) -> NetworkState:
    net = _get_network(name)
    if net.state != "connected" or not net.client:
        raise HTTPException(502, detail={
            "error": "upstream",
            "message": f'Network "{name}" is not connected.',
        })
    return net


# -- Auth -----------------------------------------------------------------

@router.post("/auth/token")
async def create_token(body: TokenRequest):
    if body.username != settings.username or body.password != settings.password:
        raise HTTPException(401, detail={
            "error": "unauthorized", "message": "Bad credentials.",
        })
    entry = token_store.create(body.username)
    expires = datetime.fromtimestamp(entry.expires_at, tz=timezone.utc).isoformat()
    return {"token": entry.token, "expires_at": expires}


@router.delete("/auth/token", status_code=204)
async def revoke_token(request: Request, _auth: TokenEntry = Depends(require_auth)):
    raw = request.headers.get("Authorization", "")[7:]
    token_store.revoke(raw)


# -- Networks -------------------------------------------------------------

@router.get("/networks")
async def list_networks(_auth: TokenEntry = Depends(require_auth)):
    return [_net_response(n) for n in proxy.networks.values()]


@router.get("/networks/{name}")
async def get_network(name: str, _auth: TokenEntry = Depends(require_auth)):
    return _net_response(_get_network(name))


@router.post("/networks", status_code=201)
async def create_network(body: NetworkCreate, _auth: TokenEntry = Depends(require_auth)):
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
    )
    proxy.networks[body.name] = net
    return _net_response(net)


_CONNECTION_FIELDS = {"host", "port", "tls", "nick", "server_password"}


@router.patch("/networks/{name}")
async def update_network(name: str, body: NetworkUpdate, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(name)
    updates = body.model_dump(exclude_unset=True)

    if net.client and updates.keys() & _CONNECTION_FIELDS:
        await net.client.disconnect()
        net.client = None
        net.state = "disconnected"

    for field, value in updates.items():
        setattr(net, field, value)
    return _net_response(net)


@router.delete("/networks/{name}", status_code=204)
async def delete_network(name: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(name)
    if net.client:
        await net.client.disconnect()
    del proxy.networks[name]


@router.post("/networks/{name}/connect")
async def connect_network(name: str, _auth: TokenEntry = Depends(require_auth)):
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
    try:
        await client.connect()
    except Exception as exc:
        net.state = "disconnected"
        net.client = None
        raise HTTPException(502, detail={
            "error": "upstream",
            "message": f"Failed to connect: {exc}",
        })
    return _net_response(net)


@router.post("/networks/{name}/disconnect")
async def disconnect_network(name: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(name)
    if net.client:
        await net.client.disconnect()
        net.client = None
    net.state = "disconnected"
    return _net_response(net)


# -- Channels -------------------------------------------------------------

@router.get("/networks/{network}/channels")
async def list_channels(network: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(network)
    result = []
    for ch in net.channels.values():
        result.append({
            "name": ch.name,
            "topic": ch.topic,
            "joined": True,
            "members_count": len(ch.members),
            "unread_count": 0,
        })
    return result


@router.get("/networks/{network}/channels/{channel}")
async def get_channel(network: str, channel: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    return {
        "name": ch.name,
        "topic": ch.topic,
        "topic_set_by": ch.topic_set_by,
        "joined": True,
        "members_count": len(ch.members),
        "unread_count": 0,
    }


@router.post("/networks/{network}/channels", status_code=201)
async def join_channel(network: str, body: ChannelJoin, _auth: TokenEntry = Depends(require_auth)):
    net = _get_connected_network(network)
    if body.name in net.channels:
        raise HTTPException(409, detail={
            "error": "conflict",
            "message": f'Already joined "{body.name}".',
        })
    await net.client.join(body.name, body.key)
    await asyncio.sleep(0.5)
    ch = net.channels.get(body.name)
    return {
        "name": body.name,
        "topic": ch.topic if ch else "",
        "joined": True,
        "members_count": len(ch.members) if ch else 0,
        "unread_count": 0,
    }


@router.delete("/networks/{network}/channels/{channel}", status_code=204)
async def part_channel(network: str, channel: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_connected_network(network)
    await net.client.part(channel)


@router.put("/networks/{network}/channels/{channel}/topic")
async def set_topic(network: str, channel: str, body: TopicUpdate, _auth: TokenEntry = Depends(require_auth)):
    net = _get_connected_network(network)
    await net.client.set_topic(channel, body.text)
    await asyncio.sleep(0.3)
    ch = net.channels.get(channel)
    return {
        "name": channel,
        "topic": ch.topic if ch else body.text,
        "joined": True,
        "members_count": len(ch.members) if ch else 0,
        "unread_count": 0,
    }


# -- Members --------------------------------------------------------------

@router.get("/networks/{network}/channels/{channel}/members")
async def list_members(network: str, channel: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    return [
        {"nick": m.nick, "prefix": m.prefix, "user": m.user, "host": m.host}
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
):
    net = _get_network(network)
    ch = net.channels.get(channel)
    if not ch:
        raise HTTPException(404, detail={
            "error": "not_found",
            "message": f'Channel "{channel}" not found.',
        })
    return _paginate_messages(ch.messages, limit, before, after)


@router.post("/networks/{network}/channels/{channel}/messages", status_code=201)
async def send_channel_message(
    network: str,
    channel: str,
    body: MessageSend,
    _auth: TokenEntry = Depends(require_auth),
):
    net = _get_connected_network(network)
    if body.type == "notice":
        await net.client.notice(channel, body.text)
    elif body.type == "action":
        await net.client.privmsg(channel, f"\x01ACTION {body.text}\x01")
    else:
        await net.client.privmsg(channel, body.text)

    now = datetime.now(timezone.utc).isoformat()
    msg_id = proxy.next_message_id()
    msg = {"id": msg_id, "time": now, "from": net.nick, "type": body.type, "text": body.text}

    if channel not in net.channels:
        from lipservice.state import ChannelState
        net.channels[channel] = ChannelState(name=channel)
    net.channels[channel].messages.append(msg)

    return msg


@router.get("/networks/{network}/messages/{nick}")
async def list_private_messages(
    network: str,
    nick: str,
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None),
    after: str | None = Query(None),
    _auth: TokenEntry = Depends(require_auth),
):
    net = _get_network(network)
    msgs = net.private_messages.get(nick, deque())
    return _paginate_messages(msgs, limit, before, after)


@router.post("/networks/{network}/messages/{nick}", status_code=201)
async def send_private_message(
    network: str,
    nick: str,
    body: MessageSend,
    _auth: TokenEntry = Depends(require_auth),
):
    net = _get_connected_network(network)
    if body.type == "notice":
        await net.client.notice(nick, body.text)
    elif body.type == "action":
        await net.client.privmsg(nick, f"\x01ACTION {body.text}\x01")
    else:
        await net.client.privmsg(nick, body.text)

    now = datetime.now(timezone.utc).isoformat()
    msg_id = proxy.next_message_id()
    msg = {"id": msg_id, "time": now, "from": net.nick, "type": body.type, "text": body.text}

    if nick not in net.private_messages:
        net.private_messages[nick] = deque(maxlen=proxy.max_backlog)
    net.private_messages[nick].append(msg)

    return msg


def _paginate_messages(
    buf: deque, limit: int, before: str | None, after: str | None,
) -> dict:
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
    return {"messages": msgs, "has_more": has_more}


# -- User -----------------------------------------------------------------

@router.get("/user")
async def get_user(_auth: TokenEntry = Depends(require_auth)):
    return {
        "username": _auth.username,
        "networks": list(proxy.networks.keys()),
    }


@router.get("/networks/{network}/user")
async def get_network_user(network: str, _auth: TokenEntry = Depends(require_auth)):
    net = _get_network(network)
    return {
        "nick": net.nick,
        "user": net.irc_user or net.nick,
        "host": net.irc_host,
        "modes": net.modes,
    }


@router.put("/networks/{network}/user/nick")
async def change_nick(network: str, body: NickChange, _auth: TokenEntry = Depends(require_auth)):
    net = _get_connected_network(network)
    await net.client.set_nick(body.nick)
    await asyncio.sleep(0.3)
    return {
        "nick": net.nick,
        "user": net.irc_user or net.nick,
        "host": net.irc_host,
        "modes": net.modes,
    }


# -- Raw ------------------------------------------------------------------

@router.post("/networks/{network}/raw", status_code=202)
async def send_raw(network: str, body: RawCommand, _auth: TokenEntry = Depends(require_auth)):
    net = _get_connected_network(network)
    await net.client.send_raw(body.command)
    return {"status": "accepted"}


# -- Events (SSE) ---------------------------------------------------------

@router.get("/events")
async def events(
    request: Request,
    networks: str | None = Query(None),
    channels: str | None = Query(None),
    _auth: TokenEntry = Depends(require_auth),
):
    net_filter = set(networks.split(",")) if networks else None
    ch_filter = set(channels.split(",")) if channels else None

    queue = proxy.event_bus.subscribe()

    async def generate():
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


# -- Status ---------------------------------------------------------------

@router.get("/status")
async def status(_auth: TokenEntry = Depends(require_auth)):
    connected = sum(1 for n in proxy.networks.values() if n.state == "connected")
    disconnected = len(proxy.networks) - connected
    return {
        "version": "0.1.0",
        "uptime_seconds": int(time.time() - proxy.start_time),
        "networks_connected": connected,
        "networks_disconnected": disconnected,
    }
