"""Live end-to-end check for reddit-native-mcp: spawn the server over real MCP
stdio and exercise every tool against live reddit.com. Exits non-zero if any
check fails.

    .venv/bin/python scripts/live_check_native.py           # all checks
    .venv/bin/python scripts/live_check_native.py thread    # names containing "thread"

Uses ~100 of Reddit's ~200-per-window anonymous requests; running it twice
in a row can hit the rate limit (the tools then say so; wait a few minutes).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

THREAD_URL = (
    "https://www.reddit.com/r/ClaudeAI/comments/1wz9oim/"
    "anthropic_is_giving_startups_a_free_year_of/"
)
COMMENT_URL = THREAD_URL + "pe9tov3/"
SHARE_LINK = "https://www.reddit.com/r/running/s/3TzXiyxaMD"
HIDDEN_CRITERION = "1wz9rng"  # r/Anthropic "Thank you Anthropic!"
HIDDEN_CREATED = 1791310282  # 2026-10-06T18:11:22Z


class Failed(AssertionError):
    pass


def need(cond: bool, why: str) -> None:
    if not cond:
        raise Failed(why)


async def call(session: ClientSession, tool: str, args: dict) -> tuple[dict, int]:
    res = await session.call_tool(tool, args)
    text = res.content[0].text
    if res.is_error:
        raise Failed(f"tool error: {text}")
    need(res.structured_content is None, "payload duplicated as structuredContent")
    return json.loads(text), len(text)


READ_TOOLS = {"get_thread", "search_posts", "find_posts", "find_similar"}
ACCOUNT_TOOLS = {
    "account_status", "link_account", "get_post_requirements",
    "create_post", "edit_post", "set_post_options", "delete_post",
}


async def check_tools(s: ClientSession) -> str:
    tools = {t.name: t for t in (await s.list_tools()).tools}
    need(set(tools) == READ_TOOLS | ACCOUNT_TOOLS, f"tools: {sorted(tools)}")
    undocumented = [
        f"{t.name}.{p}"
        for t in tools.values()
        for p, schema in t.input_schema["properties"].items()
        if not schema.get("description")
    ]
    need(not undocumented, f"undocumented params: {undocumented}")
    return f"{len(tools)} tools, every parameter documented"


async def check_thread(s: ClientSession) -> str:
    out, size = await call(s, "get_thread", {"post": THREAD_URL})
    meta = out["meta"]
    need(meta["returned_comments"] >= 195, f"only {meta['returned_comments']} comments")
    need(meta["max_depth_returned"] >= 9, f"depth {meta['max_depth_returned']}")
    need(size <= 60_000, f"{size} chars exceeds the default cap")
    need("madrobot.blog" in out["post"].get("link_url", ""), "external link_url missing")
    need(out["post"]["score"] > 100, f"post score {out['post']['score']} looks like a snapshot")
    return (
        f"{meta['returned_comments']}/{meta['known_comments']} comments (Reddit counts "
        f"{meta.get('reddit_comment_count')}), depth {meta['max_depth_returned']}, {size} chars, "
        f"{meta['requests']} requests, server {meta['elapsed_ms']}ms"
    )


async def check_thread_budget_drilldown(s: ClientSession) -> str:
    out, size = await call(s, "get_thread", {"post": THREAD_URL, "max_output_chars": 8000})
    meta = out["meta"]
    need(meta["truncated"] and size <= 8000, f"truncated={meta['truncated']} size={size}")
    hidden = meta.get("omitted_top_level_ids") or []
    need(hidden, "no omitted_top_level_ids to drill into")
    sub, _ = await call(s, "get_thread", {"post": THREAD_URL, "comment_id": hidden[0]})
    need(sub["comments"] and sub["comments"][0]["id"] == hidden[0], "drill-down returned wrong root")
    return (
        f"8000-char cap → {size} chars, {meta['returned_comments']} shown in "
        f"{meta['requests']} request(s); expanded {hidden[0]}"
    )


async def check_thread_comment_url(s: ClientSession) -> str:
    out, _ = await call(s, "get_thread", {"post": COMMENT_URL})
    meta = out["meta"]
    need(meta.get("focus_comment_id") == "pe9tov3", f"focus {meta.get('focus_comment_id')}")
    need(out["comments"][0]["id"] == "pe9tov3", "subtree root is not the linked comment")
    need(out["comments"][0]["depth"] == 0, "focus depth not rebased to 0")
    return f"subtree of pe9tov3: {meta['returned_comments']} comments"


async def check_thread_share_link(s: ClientSession) -> str:
    out, _ = await call(s, "get_thread", {"post": SHARE_LINK})
    need(out["post"]["id"] == "17snc7d", f"resolved to {out['post']['id']}")
    need(out["meta"].get("focus_comment_id") == "k8so54m", "linked comment not focused")
    return f"share link → post 17snc7d, comment k8so54m ({out['meta']['returned_comments']} comments)"


async def check_thread_bare_comment(s: ClientSession) -> str:
    out, _ = await call(s, "get_thread", {"post": "t1_pefmojb"})
    need(out["post"]["id"] == "1wz9oim", f"post {out['post']['id']}")
    need(out["comments"][0]["id"] == "pefmojb", "wrong focus")
    return f"t1_pefmojb → post 1wz9oim, parent {out['meta'].get('focus_parent_id')}"


async def check_search_feed(s: ClientSession) -> str:
    out, _ = await call(s, "search_posts", {"subreddit": "r/ClaudeAI", "limit": 3})
    need(len(out["posts"]) == 3, f"{len(out['posts'])} posts")
    need("next_cursor" in out["meta"], "no pagination cursor")
    age_min = (time.time() - time.mktime(time.strptime(out["posts"][0]["created"], "%Y-%m-%dT%H:%M:%SZ")) + time.timezone) / 60
    return f"newest post {age_min:.0f} min old: {out['posts'][0]['title'][:50]!r}"


async def check_search_query(s: ClientSession) -> str:
    out, _ = await call(
        s, "search_posts", {"query": "startup program", "subreddit": "Anthropic", "time_range": "month", "limit": 20}
    )
    need(out["posts"], "no results")
    subs = {p["subreddit"] for p in out["posts"]}
    need(subs == {"Anthropic"}, f"results outside r/Anthropic: {subs}")
    return f"{len(out['posts'])} hits in r/Anthropic, top: {out['posts'][0]['title'][:50]!r}"


async def check_find(s: ClientSession) -> str:
    age_days = (time.time() - HIDDEN_CREATED) / 86400
    out, _ = await call(
        s,
        "find_posts",
        {
            "keywords": "startup program, free claude team, anthropic startup",
            "days": max(7, round(age_days + 1)),
            "max_results": 40,
        },
    )
    meta = out["meta"]
    ids = [p["id"] for p in out["posts"]]
    need(HIDDEN_CRITERION in ids, f"{HIDDEN_CRITERION} not found; got {ids}")
    subs = sorted({p["subreddit"] for p in out["posts"]})
    return (
        f"{meta['matched_count']} matches across {len(subs)} subreddits, {HIDDEN_CRITERION} at rank "
        f"{ids.index(HIDDEN_CRITERION) + 1}; {meta['requests']} requests, server {meta['elapsed_ms']}ms"
    )


async def check_thread_text(s: ClientSession) -> str:
    res = await s.call_tool("get_thread", {"post": THREAD_URL, "format": "text"})
    text = res.content[0].text
    need(not res.is_error, f"tool error: {text[:200]}")
    need(text.startswith("# Anthropic is giving startups"), "no title line")
    shown = int(text.split("## Comments (", 1)[1].split(" ", 1)[0])
    need(shown >= 195, f"only {shown} comments in text mode")
    need(len(text) <= 60_000, f"{len(text)} chars over the cap")
    return f"{shown} comments in {len(text)} chars (cache made it near-instant after check_thread)"


async def check_similar(s: ClientSession) -> str:
    out, _ = await call(s, "find_similar", {"post": THREAD_URL})
    meta = out["meta"]
    titles = " ".join(p["title"].lower() for p in out["posts"][:10])
    need("startup" in titles, "top results are not about the startup offer")
    need("1wz9oim" not in [p["id"] for p in out["posts"]], "source post returned")
    return (
        f"terms {meta['terms']}: {meta['candidates']} candidates, top: "
        f"{out['posts'][0]['title'][:45]!r}; {meta['requests']} requests, server {meta['elapsed_ms']}ms"
    )


CHECKS = [
    check_tools,
    check_thread,
    check_thread_text,
    check_thread_budget_drilldown,
    check_thread_comment_url,
    check_thread_share_link,
    check_thread_bare_comment,
    check_search_feed,
    check_search_query,
    check_find,
    check_similar,
]


async def main(selector: str) -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "reddit_native_mcp.server"], env=dict(os.environ)
    )
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for check in CHECKS:
                name = check.__name__.removeprefix("check_")
                if selector not in name:
                    continue
                started = time.monotonic()
                try:
                    detail = await check(session)
                    status = "PASS"
                except Failed as e:
                    detail, status = str(e), "FAIL"
                    failures += 1
                print(f"{status} {name:<26} {time.monotonic() - started:5.1f}s  {detail}", flush=True)
    print("OK" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "")))
