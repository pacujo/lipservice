"""Utilities for working with IRC formatting control codes."""

from __future__ import annotations

import re

_STRIP_RE = re.compile(
    r"[\x02\x1D\x1F\x1E\x11\x16\x0F]"
    r"|\x03(?:\d{1,2}(?:,\d{1,2})?)?"
    r"|\x04(?:[0-9A-Fa-f]{6}(?:,[0-9A-Fa-f]{6})?)?"
)


def strip(text: str) -> str:
    """Remove all IRC formatting control codes from *text*."""
    return _STRIP_RE.sub("", text)
