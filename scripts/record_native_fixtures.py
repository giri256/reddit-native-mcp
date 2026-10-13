"""Record reddit.com responses for reddit-native-mcp's offline tests.

Runs real tool-level calls through a recording transport and writes every
response (slimmed HTML, status, the headers the client reads) to
tests/native/fixtures/<name>.json, keyed by method + URL + body hash.
tests/native/replay.py serves them back. Re-run when Reddit's markup changes:

    .venv/bin/python scripts/record_native_fixtures.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

from reddit_native_mcp.http import RedditWeb, tls_context
from reddit_native_mcp.listing import feed_page, search_page
from reddit_native_mcp.links import resolve_share_path
from reddit_native_mcp.parse import slim
from reddit_native_mcp.thread import fetch_thread, post_of_comment

OUT = Path(__file__).resolve().parent.parent / "tests" / "native" / "fixtures"
KEEP_HEADERS = ("location", "set-cookie", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after")


def request_key(request: httpx.Request) -> str:
    body = request.content or b""
    digest = hashlib.sha1(body).hexdigest()[:12] if body else "-"
    return f"{request.method} {request.url} {digest}"


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport(http2=True, verify=tls_context())
        self.calls: dict[str, dict] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self.inner.handle_async_request(request)
        body = await resp.aread()
        text = body.decode("utf-8", "replace")
        headers = [
            (k, "loid=fixture; Path=/" if k == "set-cookie" else v)
            for k, v in resp.headers.multi_items()
            if k in KEEP_HEADERS
        ]
        self.calls[request_key(request)] = {
            "status": resp.status_code,
            "headers": headers,
            "body": slim(text) if len(text) > 20_000 else text,
        }
        plain = [(k, v) for k, v in resp.headers.multi_items() if k not in ("content-encoding", "content-length")]
        return httpx.Response(resp.status_code, headers=plain, content=body, request=request)


async def record(name: str, scenario) -> None:
    rec = Recorder()
    web = RedditWeb(transport=rec)
    try:
        await scenario(web)
    finally:
        await web.aclose()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(rec.calls, ensure_ascii=False, indent=0))
    print(f"{name}: {len(rec.calls)} responses, {path.stat().st_size // 1024} KB", file=sys.stderr)


async def main() -> None:
    await record("thread_1wz9oim", lambda w: fetch_thread(w, "1wz9oim", max_requests=100))
    # One permalink page captured on its own: Reddit sometimes answers with an
    # empty shell (kept in thread_1wz9oim to test that path); this is a good copy.
    await record("permalink_pec7ab8", lambda w: w.page("/r/ClaudeAI/comments/1wz9oim/comment/pec7ab8/"))
    await record("thread_focus_pe9yhv8", lambda w: fetch_thread(w, "1wz9oim", focus="pe9yhv8"))

    async def comment_lookup(w):
        pid = await post_of_comment(w, "pefmojb")
        await fetch_thread(w, pid, focus="pefmojb")

    await record("comment_lookup_pefmojb", comment_lookup)
    await record("share_link", lambda w: resolve_share_path("/r/running/s/3TzXiyxaMD", w))

    async def feed(w):
        _, cur = await feed_page(w, "ClaudeAI", "new")
        await feed_page(w, "ClaudeAI", "new", cursor=cur)
        await feed_page(w, "thissubdoesnotexist12345", "new")

    await record("feed_claudeai", feed)

    async def search(w):
        hits, cur = await search_page(w, "startup program", sort="top", time="week")
        await search_page(w, "startup program", sort="top", time="week", cursor=cur)
        await search_page(w, "startup program", sub="Anthropic", time="week")

    await record("search_startup_program", search)


if __name__ == "__main__":
    asyncio.run(main())
