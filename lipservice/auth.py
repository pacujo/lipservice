from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from lipservice.config import settings


@dataclass
class TokenEntry:
    token: str
    username: str
    created_at: float
    expires_at: float


class TokenStore:
    def __init__(self):
        self._tokens: dict[str, TokenEntry] = {}

    def create(self, username: str) -> TokenEntry:
        token = "ls_" + secrets.token_hex(24)
        now = time.time()
        entry = TokenEntry(
            token=token,
            username=username,
            created_at=now,
            expires_at=now + settings.token_lifetime_seconds,
        )
        self._tokens[token] = entry
        return entry

    def validate(self, token: str) -> TokenEntry | None:
        entry = self._tokens.get(token)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            del self._tokens[token]
            return None
        return entry

    def revoke(self, token: str):
        self._tokens.pop(token, None)


token_store = TokenStore()


def _extract_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "message": "Missing or malformed Authorization header.",
        })
    return auth[7:]


async def require_auth(request: Request) -> TokenEntry:
    raw = _extract_token(request)
    entry = token_store.validate(raw)
    if entry is None:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "message": "Invalid or expired token.",
        })
    return entry
