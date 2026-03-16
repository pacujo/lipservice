from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


# "from" is a Python keyword, so we use the functional form.
Message = TypedDict("Message", {
    "id": str,
    "time": str,
    "from": str,
    "type": str,
    "text": str,
})


class MessagePage(BaseModel):
    messages: list[Message]
    has_more: bool


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: str


class NetworkConfig(BaseModel):
    name: str
    host: str
    port: int = 6697
    tls: bool = True
    nick: str
    server_password: str | None = None
    nickserv_password: str | None = None
    auto_connect: bool = False


class NetworkCreate(BaseModel):
    name: str
    host: str
    port: int = 6697
    tls: bool = True
    nick: str
    server_password: str | None = None
    nickserv_password: str | None = None


class NetworkUpdate(BaseModel):
    host: str | None = None
    port: int | None = None
    tls: bool | None = None
    nick: str | None = None
    server_password: str | None = None
    nickserv_password: str | None = None


class NetworkResponse(BaseModel):
    name: str
    host: str
    port: int
    tls: bool
    nick: str
    state: str
    channels: list[str]


class ChannelJoin(BaseModel):
    name: str
    key: str | None = None


class ChannelResponse(BaseModel):
    name: str
    topic: str
    topic_set_by: str = ""
    joined: bool
    members_count: int


class TopicUpdate(BaseModel):
    text: str


class MemberResponse(BaseModel):
    nick: str
    prefix: str
    user: str
    host: str


class MessageSend(BaseModel):
    text: str
    type: str = "privmsg"


class NickChange(BaseModel):
    nick: str


class RawCommand(BaseModel):
    command: str


class QueryPeer(BaseModel):
    nick: str


class UserResponse(BaseModel):
    username: str
    networks: list[str]


class NetworkUserResponse(BaseModel):
    nick: str
    user: str
    host: str
    modes: str


class StatusResponse(BaseModel):
    version: str
    uptime_seconds: int
    networks_connected: int
    networks_disconnected: int


class Session(BaseModel):
    current_network: str | None = None
    current_channel: str | None = None
    current_query: str | None = None
    pointers: dict[str, str] = {}


class SessionUpdate(BaseModel):
    current_network: str | None = None
    current_channel: str | None = None
    current_query: str | None = None
    pointers: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    error: str
    message: str
