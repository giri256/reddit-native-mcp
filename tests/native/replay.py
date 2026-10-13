"""Serve recorded reddit.com responses (tests/native/fixtures/*.json, written
by scripts/record_native_fixtures.py) to RedditWeb through an httpx transport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from reddit_native_mcp.http import RedditWeb

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def request_key(request: httpx.Request) -> str:
    body = request.content or b""
    digest = hashlib.sha1(body).hexdigest()[:12] if body else "-"
    return f"{request.method} {request.url} {digest}"


class Replay(httpx.AsyncBaseTransport):
    def __init__(self, *names: str, overrides: dict | None = None) -> None:
        self.calls: dict[str, dict] = {}
        for name in names:
            self.calls.update(json.loads((FIXTURES / f"{name}.json").read_text()))
        self.overrides = overrides or {}  # key substring -> (status, body)
        self.misses: list[str] = []
        self.served: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        for needle, (status, body) in self.overrides.items():
            if needle in key:
                return httpx.Response(status, text=body, request=request)
        rec = self.calls.get(key)
        if rec is None:
            self.misses.append(key)
            return httpx.Response(599, text="not recorded", request=request)
        self.served.append(key)
        return httpx.Response(
            rec["status"], headers=rec["headers"], text=rec["body"], request=request
        )


def replay_web(*names: str, **kw) -> tuple[RedditWeb, Replay]:
    transport = Replay(*names, **kw)
    return RedditWeb(transport=transport, deadline=5.0), transport


def page_body(name: str, needle: str) -> str:
    """Body of the first recorded response whose key contains `needle`."""
    calls = json.loads((FIXTURES / f"{name}.json").read_text())
    for key, rec in calls.items():
        if needle in key:
            return rec["body"]
    raise KeyError(needle)
