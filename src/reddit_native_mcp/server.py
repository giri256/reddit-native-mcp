"""reddit-native-mcp: personal Reddit MCP server (stdio) reading reddit.com itself.

Read tools (live data from Reddit's own web app, or its JSON API when an
account is linked):
  get_thread   - post + nested comment tree from a URL, share link or id
  search_posts - a subreddit's feed, or Reddit search (sitewide/subreddit/author)
  find_posts   - one-call topic discovery: Reddit search per keyword, sitewide
                 and per subreddit, merged and ranked
  find_similar - posts about the same thing as a given post (same link, title
                 terms), without the agent guessing keywords
Account tools (need `reddit-native-mcp login`; see auth.py):
  account_status, get_post_requirements, create_post, edit_post,
  set_post_options, delete_post
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Annotated, Literal

from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, ElicitationResult
from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, create_model

from . import __version__, browser_cookies, write
from .auth import cli as auth_cli
from .auth import load_prefs, save_prefs
from .browser_cookies import BrowserCookieError
from .http import API, WEB, BlockedError, Forbidden, NotFound, NotLoggedIn, RateLimited, RedditError, shared_client
from .links import LinkError, clean_comment_id, clean_name, parse_reddit_ref, resolve_share_path
from .listing import feed_page, search_page, time_window
from .shape import (
    PREVIEW_CHARS,
    compact_post,
    compact_tree,
    dumps,
    json_size,
    keyword_hits,
    keyword_pattern,
    normalize_keywords,
    render_comments,
    render_meta,
    render_post,
    text_size,
)
from .similar import external_links, post_terms, queries
from .thread import MAX_REQUESTS, fetch_thread, post_of_comment

INSTRUCTIONS = """\
Reddit, live from reddit.com itself. Reads work anonymously; with a linked account \
they use it (higher rate limits), and posting tools work.
- get_thread: post + comment tree from any post/comment URL, share link or id. \
Output is size-capped (max_output_chars); a comment with more_replies has hidden \
replies: call get_thread again with comment_id=<its id>.
- find_posts: find posts on a topic across Reddit (and chosen subreddits) in one call.
- find_similar: given one post, other posts about the same thing (same link or topic).
- search_posts: a subreddit's feed (hot/new/top/...) or one Reddit search, with paging.
- account_status: which account is linked and the rate budget left.
- link_account: link the user's Reddit account from a browser they are logged in to \
(like yt-dlp --cookies-from-browser). Only when the user wants it.
- get_post_requirements -> create_post -> edit_post / set_post_options / delete_post: \
publish as the linked account. Posting is public and permanent under the user's name: \
only post what the user asked for, and check the community's rules first.
Rate limits: ~200 requests per few minutes anonymously; more when logged in. \
Tools report when the budget runs low."""

log = logging.getLogger(__name__)
mcp = MCPServer("reddit-native-mcp", version=__version__, instructions=INSTRUCTIONS)

# structured_output=False: a `-> str` tool would otherwise also send the whole
# payload a second time as structuredContent {"result": "<json string>"}.
_TOOL = dict(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
    structured_output=False,
)

DEFAULT_MAX_OUTPUT_CHARS = 60_000  # ~15-20K tokens: under Claude Code's 25K cap
_META_RESERVE = 1_500
FIND_MAX_REQUESTS = 60  # of Reddit's ~200-per-window anonymous budget
FIND_DEADLINE_S = 40.0
# (scope, sort, pages) per keyword. Relevance and top surface different
# posts; together they found the hidden-criterion post that either alone
# missed (live, 2026-10-07).
SITEWIDE_PLAN = (("relevance", 3), ("top", 2))
SUB_PLAN = (("relevance", 2),)
SCAN_MAX_PAGES = 12  # per subreddit, for find_posts(scan_days>0)


def _reddit_error(e: RedditError) -> ToolError:
    if isinstance(e, RateLimited):
        return ToolError(f"{e}. Wait and retry")
    if isinstance(e, NotFound):
        return ToolError(str(e))
    if isinstance(e, NotLoggedIn):
        return ToolError(
            f"{e}. If the user wants their account linked: link_account (it offers the browser "
            "profiles logged in to Reddit), or the user runs `reddit-native-mcp login` in a terminal; "
            "the running server picks it up, no restart needed"
        )
    if isinstance(e, (Forbidden, write.PostError)):
        return ToolError(str(e))
    if isinstance(e, BlockedError):
        return ToolError(
            f"{e}. Reddit is refusing access from this network right now; "
            "retry later"
        )
    return ToolError(f"reddit.com request failed: {e}")


@mcp.tool(**_TOOL)
async def get_thread(
    post: Annotated[
        str,
        Field(
            description="Reddit post or comment URL (any reddit host), share link "
            "(reddit.com/r/<sub>/s/<code>), redd.it link, or bare post id like 1wz9oim. "
            "A comment URL returns that comment's subtree"
        ),
    ],
    comment_id: Annotated[
        str | None,
        Field(
            description="Return only this comment and its replies (depths then count from "
            "it). Use it to expand a comment that has more_replies, or an id from "
            "meta.omitted_top_level_ids"
        ),
    ] = None,
    sort: Annotated[
        Literal["best", "top", "new", "old", "controversial", "qa"],
        Field(
            description="Comment order (Reddit's own sorts; best = Reddit's default). Also "
            "decides which comments are fetched and kept first when a cap is hit"
        ),
    ] = "best",
    max_depth: Annotated[
        int,
        Field(
            ge=0,
            le=1000,
            description="Deepest reply level to include (top-level comments are depth 0, "
            "so 1 = top-level + direct replies); 0 = no limit",
        ),
    ] = 0,
    max_output_chars: Annotated[
        int,
        Field(
            ge=5_000,
            le=2_000_000,
            description="Size cap for the JSON result (~3.5 chars per token). Comments beyond "
            "it are not fetched or shown and are counted in more_replies/meta. The default "
            "fits Claude Code's 25K-token tool-output cap",
        ),
    ] = DEFAULT_MAX_OUTPUT_CHARS,
    max_requests: Annotated[
        int,
        Field(
            ge=1,
            le=150,
            description="Max reddit.com requests for this call (~10 comments per request "
            "after the first page of ~25). Reddit's anonymous budget is ~200 per few minutes",
        ),
    ] = MAX_REQUESTS,
    format: Annotated[
        Literal["json", "text"],
        Field(
            description="json = nested records; text = an indented outline (one '- id u/author "
            "score time' line per comment, body below), ~20-25% fewer tokens, so more "
            "comments fit the same cap"
        ),
    ] = "json",
) -> str:
    """Fetch a Reddit thread in one call: the post (full selftext, link_url, live
    score) plus its nested comment tree with authors, live scores, ISO times and
    replies, expanded from Reddit's collapsed "more replies" until the thread is
    complete or a cap is hit. Comment URL = post url + comment id + '/'. If
    meta.truncated, see meta.notes for how to get the rest."""
    started = time.monotonic()
    try:
        ref = parse_reddit_ref(post)
        focus = clean_comment_id(comment_id) if comment_id else None
    except LinkError as e:
        raise ToolError(str(e)) from None
    web = shared_client()
    try:
        if ref.share_path:
            ref = await resolve_share_path(ref.share_path, web)
        focus = focus or ref.comment_id
        post_id = ref.post_id or await post_of_comment(web, focus or "")
        room = max_output_chars - _META_RESERVE
        th = await fetch_thread(
            web,
            post_id,
            focus=focus,
            sort=sort,
            # fetch_thread estimates in JSON chars; text needs ~0.77x of that
            char_budget=room if format == "json" else int(room * 1.3),
            max_requests=max_requests,
        )
    except LinkError as e:
        raise ToolError(str(e)) from None
    except RedditError as e:
        raise _reddit_error(e) from None

    post_rec = compact_post(th.post)
    if len(dumps(post_rec)) > room // 2 and th.post.get("body"):
        post_rec = compact_post(th.post, preview_chars=room // 2)
    if format == "text":
        post_size, measure = len("\n".join(render_post(post_rec))), text_size
    else:
        post_size, measure = len(dumps(post_rec)), json_size
    budget = max(0, room - post_size)
    tree, stats = compact_tree(
        th.comments, th.pending, sort=sort, max_depth=max_depth, budget_chars=budget,
        measure=measure,
    )
    meta: dict = {
        **stats,
        "truncated": stats["omitted_comments"] > 0,
        "sort": sort,
        "comment_url_template": f"{post_rec.get('url', '')}{{comment_id}}/",
        "requests": th.requests,
    }
    if th.total_comments is not None and not focus:
        meta["reddit_comment_count"] = th.total_comments
    if focus:
        meta["focus_comment_id"] = focus
        meta["focus_parent_id"] = th.focus_parent_id
    if th.stop_reason:
        meta["stopped"] = th.stop_reason
    notes = []
    if stats["omitted_comments"]:
        notes.append(
            f"{stats['omitted_comments']} known comments are not shown (output cap, request "
            "cap or max_depth). Expand a comment with more_replies via get_thread(post, "
            "comment_id=<id>); omitted_top_level_ids lists hidden top-level comments"
        )
    if th.errors:
        notes.append(f"{len(th.errors)} 'more replies' loads failed: {th.errors[0]}")
    if meta.get("reddit_comment_count") and not th.pending:
        gap = meta["reddit_comment_count"] - stats["known_comments"]
        if gap > 0:
            notes.append(
                f"Reddit counts {gap} more comments than it shows: deleted/removed comments "
                "and their hidden replies"
            )
    if notes:
        meta["notes"] = notes
    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if format == "text":
        lines = render_post(post_rec)
        lines += ["", f"## Comments ({stats['returned_comments']} of {stats['known_comments']} shown, sort {sort})"]
        lines += render_comments(tree) or ["(none)"]
        lines += ["", "## Meta", *render_meta(meta)]
        return "\n".join(lines)
    return dumps({"post": post_rec, "comments": tree, "meta": meta})


@mcp.tool(**_TOOL)
async def search_posts(
    subreddit: Annotated[
        str | None,
        Field(description="Subreddit name (with or without r/). Alone = its feed"),
    ] = None,
    query: Annotated[
        str | None,
        Field(
            description="Reddit search query (Reddit's own engine: title and body, relevance "
            "ranked; supports quotes and operators like title:, flair:, site:). Without "
            "subreddit it searches all of Reddit"
        ),
    ] = None,
    author: Annotated[
        str | None,
        Field(description="Only this user's posts (with or without u/); runs as a search"),
    ] = None,
    sort: Annotated[
        Literal["hot", "new", "top", "rising", "best", "relevance", "comments"] | None,
        Field(
            description="Feeds: hot/new/top/rising/best (default new). Searches: relevance/"
            "new/top/comments (default relevance)"
        ),
    ] = None,
    time_range: Annotated[
        Literal["hour", "day", "week", "month", "year", "all"] | None,
        Field(description="For sort=top feeds and for searches (default all)"),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Max posts")] = 25,
    cursor: Annotated[
        str | None, Field(description="meta.next_cursor from a previous call, for the next page")
    ] = None,
) -> str:
    """List Reddit posts: a subreddit's feed, or a Reddit search (sitewide, in one
    subreddit, and/or by author). Live scores and comment counts. Feed posts
    include a 400-char selftext preview; search results have no selftext (use
    get_thread for the full post and comments)."""
    started = time.monotonic()
    sub = clean_name(subreddit, ("r/",)) if subreddit else None
    user = clean_name(author, ("u/", "user/")) if author else None
    q = " ".join(x for x in ((query or "").strip(), f"author:{user}" if user else "") if x)
    web = shared_client()
    posts: list[dict] = []
    nxt = cursor
    pages = 0
    try:
        if q:
            sort_ = sort or "relevance"
            if sort_ not in ("relevance", "new", "top", "comments"):
                raise ToolError(f"sort={sort_} is for feeds; searches take relevance/new/top/comments")
            while len(posts) < limit and pages < 15:
                hits, nxt = await search_page(
                    web, q, sub=sub, sort=sort_, time=time_range or "all", cursor=nxt
                )
                pages += 1
                posts.extend(hits)
                if not nxt or not hits:
                    break
            if sort_ == "new" and not sub:  # ran as relevance: re-sort by time
                posts.sort(key=lambda p: p.get("created_ts") or 0, reverse=True)
        else:
            if not sub:
                raise ToolError("Give a subreddit (feed), a query (search) or an author")
            sort_ = sort or "new"
            if sort_ not in ("hot", "new", "top", "rising", "best"):
                raise ToolError(f"sort={sort_} needs a query; feeds take hot/new/top/rising/best")
            while len(posts) < limit and pages < 8:
                page, nxt = await feed_page(web, sub, sort_, time=time_range, cursor=nxt)
                pages += 1
                posts.extend(page)
                if not nxt or not page:
                    break
    except RedditError as e:
        raise _reddit_error(e) from None
    if not posts and not q and not cursor:
        raise ToolError(
            f"r/{sub} returned no posts: it may not exist, be private/banned, or be NSFW "
            "(needs a login)"
        )
    seen: set = set()
    unique = [p for p in posts if not (p["id"] in seen or seen.add(p["id"]))]
    meta: dict = {
        "count": min(len(unique), limit),
        "mode": "search" if q else "feed",
        "sort": sort_,
        "requests": pages,
    }
    if q:
        meta["query"] = q
        if sort_ == "new" and not sub:
            meta["notes"] = [
                "Reddit's sitewide search ignores the query when sorted by new, so this ran "
                "by relevance and was re-sorted newest first"
            ]
    if nxt and len(unique) >= limit:
        meta["next_cursor"] = nxt
    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return dumps(
        {"posts": [compact_post(p, preview_chars=PREVIEW_CHARS) for p in unique[:limit]], "meta": meta}
    )


class _Budget:
    def __init__(self, max_requests: int):
        self.left = max_requests

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


async def _search_scope(
    web, kw: str, sub: str | None, sort: str, pages: int, window: str, budget: _Budget,
    out: list, errors: list,
) -> None:
    cursor = None
    pos = 0
    for _ in range(pages):
        if not budget.take():
            return
        try:
            hits, cursor = await search_page(web, kw, sub=sub, sort=sort, time=window, cursor=cursor)
        except RedditError as e:
            errors.append(f"{'r/' + sub if sub else 'sitewide'} {sort} '{kw}': {e}")
            return
        for h in hits:
            out.append((kw, sub, sort, pos, h))
            pos += 1
        if not cursor or not hits:
            return


async def _scan_feed(
    web, sub: str, cutoff: float, budget: _Budget, out: list, errors: list, info: dict
) -> None:
    cursor = None
    for _ in range(SCAN_MAX_PAGES):
        if not budget.take():
            info["complete"] = False
            return
        try:
            posts, cursor = await feed_page(web, sub, "new", cursor=cursor)
        except RedditError as e:
            errors.append(f"r/{sub} feed: {e}")
            info["complete"] = False
            return
        info["scanned"] = info.get("scanned", 0) + len(posts)
        out.extend(p for p in posts if (p.get("created_ts") or 0) >= cutoff)
        if not posts or not cursor or min(p.get("created_ts") or 0 for p in posts) < cutoff:
            info["complete"] = True
            return
    info["complete"] = False


@mcp.tool(**_TOOL)
async def find_posts(
    keywords: Annotated[
        str | list[str],
        Field(
            description="Topic keywords/phrases, OR-combined: one phrase, comma-separated "
            "phrases, or a list (max 6). Each phrase is one Reddit search, so use phrases "
            "people would write (e.g. 'startup program', 'free claude team')"
        ),
    ],
    subreddits: Annotated[
        list[str] | None,
        Field(
            max_length=10,
            description="Also search inside these subreddits (max 10). All of Reddit is "
            "always searched too",
        ),
    ] = None,
    days: Annotated[
        float,
        Field(gt=0, le=3650, description="Only posts from the last N days"),
    ] = 30,
    scan_days: Annotated[
        float,
        Field(
            ge=0,
            le=3,
            description="Also read every new post of the last N days in `subreddits` and "
            "keep those whose title/selftext match a keyword (catches posts search has not "
            "indexed yet or ranks low). ~1 request per 24 posts; 0 = off",
        ),
    ] = 0,
    max_results: Annotated[int, Field(ge=1, le=100, description="Max posts returned")] = 30,
    sort: Annotated[
        Literal["relevance", "recent", "top"],
        Field(
            description="After the number of keywords that found a post: Reddit's relevance "
            "rank, newest first, or highest score"
        ),
    ] = "relevance",
) -> str:
    """Find posts about a topic across Reddit in one call: runs Reddit's own search
    for every keyword phrase, sitewide and inside each given subreddit (plus an
    optional scan of their newest posts), merges duplicates and ranks posts found
    by more keywords (then by more independent searches) first. Per-scope
    coverage and failures are in meta."""
    started = time.monotonic()
    kws = normalize_keywords(keywords)[:6]
    if not kws:
        raise ToolError("keywords is empty")
    subs: list[str] = []
    for s in subreddits or []:
        name = clean_name(s, ("r/",))
        if name and name.lower() not in {x.lower() for x in subs}:
            subs.append(name)
    web = shared_client()
    cutoff = time.time() - days * 86400
    window = time_window(days)
    budget = _Budget(FIND_MAX_REQUESTS)
    hits: list = []
    scanned: list[dict] = []
    errors: list[str] = []
    scan_info: dict[str, dict] = {s: {} for s in subs} if scan_days else {}
    jobs = []
    for kw in kws:
        for sort_, pages in SITEWIDE_PLAN:
            jobs.append(_search_scope(web, kw, None, sort_, pages, window, budget, hits, errors))
        for s in subs:
            for sort_, pages in SUB_PLAN:
                jobs.append(_search_scope(web, kw, s, sort_, pages, window, budget, hits, errors))
    for s in scan_info:
        jobs.append(
            _scan_feed(web, s, time.time() - scan_days * 86400, budget, scanned, errors, scan_info[s])
        )
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(*jobs), FIND_DEADLINE_S)
    except TimeoutError:
        timed_out = True

    patterns = [(k, keyword_pattern(k)) for k in kws]
    found: dict[str, dict] = {}
    for kw, sub, sort_, pos, h in hits:
        if (h.get("created_ts") or 0) < cutoff:
            continue
        e = found.setdefault(
            h["id"], {"raw": h, "kws": set(), "best_pos": pos, "scopes": set(), "votes": set()}
        )
        e["kws"].add(kw)
        e["best_pos"] = min(e["best_pos"], pos)
        e["scopes"].add(f"r/{sub}" if sub else "sitewide")
        e["votes"].add((kw, sub, sort_))
    for p in scanned:
        matched, _ = keyword_hits(p, patterns)
        if not matched:
            continue
        e = found.setdefault(
            p["id"], {"raw": p, "kws": set(), "best_pos": 99, "scopes": set(), "votes": set()}
        )
        e["votes"].add(("feed", p.get("subreddit"), None))
        if p.get("body"):
            e["raw"] = {**e["raw"], **p}
        e["kws"].update(matched)
        e["scopes"].add(f"feed r/{p.get('subreddit')}")
    if not found and errors and len(errors) >= len(jobs):
        raise ToolError("Every search failed: " + "; ".join(errors[:3]))

    def rank(e: dict):
        raw = e["raw"]
        title_hits = sum(1 for _, rx in patterns if rx.search(raw.get("title") or ""))
        # keywords that found it, then how many independent searches did
        primary = (-len(e["kws"]), -len(e["votes"]), -title_hits)
        if sort == "recent":
            return primary + (-(raw.get("created_ts") or 0),)
        if sort == "top":
            return primary + (-(raw.get("score") or 0),)
        return primary + (e["best_pos"], -(raw.get("created_ts") or 0))

    ranked = sorted(found.values(), key=rank)
    out = []
    for e in ranked[:max_results]:
        rec = compact_post(e["raw"], preview_chars=PREVIEW_CHARS)
        rec["matched_keywords"] = [k for k in kws if k in e["kws"]]
        rec["found_in"] = sorted(e["scopes"])
        out.append(rec)
    meta: dict = {
        "keywords": kws,
        "window_days": days,
        "search_window": window,
        "scopes": ["sitewide"] + [f"r/{s}" for s in subs],
        "search_hits": len(hits),
        "matched_count": len(found),
        "returned_count": len(out),
        "requests": FIND_MAX_REQUESTS - budget.left,
    }
    if scan_info:
        meta["feed_scan"] = scan_info
    if budget.left <= 0:
        meta["request_cap_hit"] = True
    if timed_out:
        meta["timed_out"] = True
    if errors:
        meta["errors"] = errors[:10]
    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return dumps({"posts": out, "meta": meta})


SIMILAR_MAX_REQUESTS = 30


@mcp.tool(**_TOOL)
async def find_similar(
    post: Annotated[
        str,
        Field(description="The post to match: any post/comment URL, share link, redd.it link or id"),
    ],
    days: Annotated[
        float,
        Field(
            gt=0,
            le=3650,
            description="Keep posts created within this many days before or after the source post",
        ),
    ] = 30,
    extra_keywords: Annotated[
        list[str] | None,
        Field(
            max_length=4,
            description="Optional extra search phrases (e.g. a product name the title lacks)",
        ),
    ] = None,
    max_results: Annotated[int, Field(ge=1, le=100, description="Max posts returned")] = 25,
) -> str:
    """Find other Reddit posts about the same thing as `post`, in one call: posts
    sharing its external link or links in its text (exact, via Reddit's url:
    search), plus Reddit searches over short combinations of its key title terms
    (topped up from the body when the title says little), sitewide. Ranked by same link, then how many searches found the post, then
    shared title terms. Each result says why it matched (meta.terms lists the
    terms used); refine with find_posts or extra_keywords."""
    started = time.monotonic()
    try:
        ref = parse_reddit_ref(post)
    except LinkError as e:
        raise ToolError(str(e)) from None
    web = shared_client()
    try:
        if ref.share_path:
            ref = await resolve_share_path(ref.share_path, web)
        post_id = ref.post_id or await post_of_comment(web, ref.comment_id or "")
        th = await fetch_thread(web, post_id, char_budget=0, max_requests=1)
    except LinkError as e:
        raise ToolError(str(e)) from None
    except RedditError as e:
        raise _reddit_error(e) from None
    src = th.post
    terms = post_terms(src)
    plan = queries(terms) + [k for k in normalize_keywords(extra_keywords or [])]
    links = external_links(src)
    created = src.get("created_ts") or time.time()
    lo, hi = created - days * 86400, created + days * 86400
    window = time_window((time.time() - lo) / 86400)
    budget = _Budget(SIMILAR_MAX_REQUESTS)
    hits: list = []
    errors: list[str] = []
    jobs = [_search_scope(web, f"url:{u}", None, "relevance", 1, "all", budget, hits, errors) for u in links]
    for i, q in enumerate(plan):
        jobs.append(_search_scope(web, q, None, "relevance", 2, window, budget, hits, errors))
        if i < 2:  # results vary between calls; a second sort steadies the strongest queries
            jobs.append(_search_scope(web, q, None, "top", 1, window, budget, hits, errors))
    try:
        await asyncio.wait_for(asyncio.gather(*jobs), FIND_DEADLINE_S)
    except TimeoutError:
        errors.append("stopped at the call deadline")

    term_rx = [(t, keyword_pattern(t)) for t in terms]
    found: dict[str, dict] = {}
    for q, _sub, sort_, pos, h in hits:
        if h["id"] == src["id"]:
            continue
        ts = h.get("created_ts") or 0
        same_link = q.startswith("url:")
        if not same_link and not (lo <= ts <= hi):
            continue
        e = found.setdefault(h["id"], {"raw": h, "votes": set(), "same_link": False, "pos": pos})
        e["votes"].add((q, sort_))
        e["same_link"] |= same_link
        e["pos"] = min(e["pos"], pos)
    if not found and errors and len(errors) >= len(jobs):
        raise ToolError("Every search failed: " + "; ".join(errors[:3]))

    def shared(e: dict) -> list[str]:
        return [t for t, rx in term_rx if rx.search(e["raw"].get("title") or "")]

    ranked = sorted(
        found.values(),
        key=lambda e: (not e["same_link"], -len(e["votes"]), -len(shared(e)), e["pos"]),
    )
    out = []
    for e in ranked[:max_results]:
        rec = compact_post(e["raw"], preview_chars=PREVIEW_CHARS)
        why: dict = {"searches": len(e["votes"])}
        if e["same_link"]:
            why["same_link"] = True
        if shared(e):
            why["shared_terms"] = shared(e)
        rec["similarity"] = why
        out.append(rec)
    meta: dict = {
        "source": {k: v for k, v in compact_post(src, preview_chars=200).items() if k in ("id", "title", "url", "subreddit", "created", "link_url")},
        "terms": terms,
        "queries": plan,
        "link_queries": links,
        "window": f"±{days:g} days of the source post",
        "candidates": len(found),
        "returned_count": len(out),
        "requests": SIMILAR_MAX_REQUESTS - budget.left + 1,
    }
    if errors:
        meta["errors"] = errors[:10]
    meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return dumps({"posts": out, "meta": meta})


# --- the linked account: status and posting ---------------------------------

_WRITE = dict(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    structured_output=False,
)
_DESTRUCTIVE = dict(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    ),
    structured_output=False,
)
_POST_ID = Field(description="The post: URL, share link, redd.it link or id like 1wz9oim")


async def _post_id(ref: str, web) -> str:
    try:
        r = parse_reddit_ref(ref)
        if r.share_path:
            r = await resolve_share_path(r.share_path, web)
    except LinkError as e:
        raise ToolError(str(e)) from None
    if not r.post_id:
        raise ToolError(f"{ref!r} does not name a post")
    return r.post_id


def _budget(web) -> dict:
    out = {}
    for name, bucket in (("json_api", API), ("web", WEB)):
        left = web.budget_left(bucket)
        if left is not None:
            out[name] = {"left": int(left), "resets_in_s": int(web.budget_reset_s(bucket) or 0)}
    return out


COOKIES_ENV = "REDDIT_NATIVE_MCP_COOKIES_FROM_BROWSER"  # e.g. "brave" or "firefox:abcd.default"
_DONT_LINK = "Don't link: stay anonymous"
_ASK = (
    "reddit-native can use the Reddit account you are logged in to in your browser (it reads "
    "that profile's reddit.com cookies, like yt-dlp --cookies-from-browser). Reads then use "
    "your account's rate limits, and the posting tools post as that account."
)


class LoginChoice(BaseModel):
    login: str


def _can_ask(ctx: Context | None) -> bool:
    """Does the MCP client show the user forms (elicitation)?"""
    try:
        caps = ctx.client_capabilities if ctx is not None else None
    except (AttributeError, LookupError, ValueError):
        return False
    return bool(caps and caps.elicitation and (caps.elicitation.form is not None or caps.elicitation.url is None))


async def _logged_in_profiles() -> list[browser_cookies.Candidate]:
    return [c for c in await asyncio.to_thread(browser_cookies.discover) if c.logged_in]


async def _login_dialog() -> Elicit[LoginChoice] | None:
    """The choice dialog (None: no logged-in browser profile to offer). The
    SDK shows it before the tool body runs, over either protocol generation."""
    cands = await _logged_in_profiles()
    if not cands:
        return None
    labels = [c.describe() for c in cands]
    schema = create_model(
        "RedditLogin",
        __base__=LoginChoice,
        login=(Literal[(*labels, _DONT_LINK)], Field(
            title="Reddit login to use",
            description="The account logged in to reddit.com in that browser profile",
        )),
    )
    return Elicit(_ASK, schema)


async def _ask_to_link(ctx: Context, source: str | None) -> Elicit[LoginChoice] | None:
    """Resolver for link_account: ask, unless a source was given."""
    if source or not _can_ask(ctx):
        return None
    return await _login_dialog()


async def _ask_before_account_use(ctx: Context) -> Elicit[LoginChoice] | None:
    """Resolver for the posting tools: ask once when no account is linked
    (never when the user declined before, or a browser is configured)."""
    if (
        os.environ.get("REDDIT_NATIVE_MCP_ANONYMOUS") or os.environ.get(COOKIES_ENV)
        or load_prefs().get("link") == "declined" or not _can_ask(ctx) or shared_client().logged_in
    ):
        return None
    return await _login_dialog()


LinkDialog = ElicitationResult[LoginChoice]


async def _link(cand: browser_cookies.Candidate) -> dict:
    try:
        session = await browser_cookies.link(cand)
    except BrowserCookieError as e:
        raise ToolError(str(e)) from None
    except NotLoggedIn as e:
        raise ToolError(
            f"{cand.describe()} holds a Reddit login that Reddit no longer accepts ({e}); "
            "log in to reddit.com again in that browser"
        ) from None
    return {"linked": True, "account": session.username, "from": cand.describe(), "source": cand.source}


async def _take_choice(choice: LinkDialog) -> dict | None:
    """Act on the dialog's answer. None: no dialog was shown."""
    if choice is None:
        return None
    if isinstance(choice, AcceptedElicitation):
        if choice.data is None:
            return None
        if choice.data.login != _DONT_LINK:
            by_label = {c.describe(): c for c in await _logged_in_profiles()}
            if choice.data.login not in by_label:
                raise ToolError(f"{choice.data.login} is no longer logged in to reddit.com")
            return await _link(by_label[choice.data.login])
    if isinstance(choice, CancelledElicitation):  # dismissed: may ask again later
        return {"linked": False, "user_choice": "dismissed the dialog"}
    save_prefs(link="declined")
    return {"linked": False, "user_choice": "don't link", "note": "reads stay anonymous; posting "
            "needs a linked account (call link_account if the user changes their mind)"}


async def _account_client():
    """The shared client for a tool that uses the account: links the browser
    named in REDDIT_NATIVE_MCP_COOKIES_FROM_BROWSER when nothing is linked,
    and re-reads a browser-linked session that Reddit stopped accepting
    (logged out or expired), so logging in again in the browser is enough."""
    web = shared_client()
    if os.environ.get("REDDIT_NATIVE_MCP_ANONYMOUS"):
        return web
    if not web.logged_in:
        if source := os.environ.get(COOKIES_ENV):
            try:
                await _link(await asyncio.to_thread(browser_cookies.resolve, source))
            except BrowserCookieError as e:
                raise ToolError(f"{COOKIES_ENV}={source}: {e}") from None
            return shared_client()
        return web
    source = web.session.source if web.session else None
    if source:
        try:
            await web.me(fresh=True)
        except NotLoggedIn:
            log.info("saved session from %s rejected; reading it again", source)
            try:
                await _link(await asyncio.to_thread(browser_cookies.resolve, source))
            except BrowserCookieError as e:
                raise ToolError(f"Reddit no longer accepts the session from {source}, and {e}") from None
            return shared_client()
    return web


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    structured_output=False,
)
async def link_account(
    ctx: Context,
    source: Annotated[
        str | None,
        Field(description="The browser profile the user chose, e.g. 'brave', 'brave:Profile 1', "
              "'firefox', 'chrome:Default'. Leave empty to offer the user a choice"),
    ] = None,
    choice: Annotated[LinkDialog, Resolve(_ask_to_link)] = None,
) -> str:
    """Link the user's Reddit account from a browser they are already logged
    in to (reads that profile's reddit.com cookies, like yt-dlp
    --cookies-from-browser). Only call it when the user wants their account
    linked: the posting tools then post as that account. Without `source`
    the user picks from their logged-in browser profiles in a dialog, or (in
    clients without dialogs) you get the list to ask them about."""
    try:
        if source:
            out = await _link(await asyncio.to_thread(browser_cookies.resolve, source))
        elif (out := await _take_choice(choice)) is None:  # no dialog: let the agent ask
            cands = await _logged_in_profiles()
            if not cands:
                out = {
                    "linked": False,
                    "problem": "no browser profile on this machine is logged in to reddit.com",
                    "how_to_link": "the user logs in to reddit.com in their browser, then call "
                                   "link_account again; or the user runs `reddit-native-mcp login --window`",
                }
            else:
                out = {
                    "linked": False,
                    "choose_one": [{"source": c.source, "browser_profile": c.describe()} for c in cands],
                    "next": "Ask the user which browser profile's Reddit login to use (this server will "
                            "read and post as that account), then call link_account(source=<their choice>)",
                }
    except BrowserCookieError as e:
        raise ToolError(str(e)) from None
    if out.get("linked"):
        out["note"] = "reads and posting now use this account; no restart needed"
    return dumps(out)


@mcp.tool(**_TOOL)
async def account_status() -> str:
    """Which Reddit account is linked (if any), whether Reddit still accepts its
    session, its karma, and the rate budget left. Without an account, reading
    works anonymously and posting tools need link_account first."""
    web = await _account_client()
    if not web.logged_in:
        return dumps({
            "linked": False,
            "reads": "anonymous",
            "how_to_link": "link_account (if the user wants it), or the user runs `reddit-native-mcp login`",
            "rate_budget": _budget(web),
        })
    try:
        me = await web.me(fresh=True)
    except NotLoggedIn as e:
        return dumps({"linked": False, "saved_session": web.session.username, "problem": str(e),
                      "how_to_link": "log in to reddit.com again in the browser, then link_account; "
                                     "or the user runs `reddit-native-mcp login`"})
    except RedditError as e:
        raise _reddit_error(e) from None
    exp = web.session.expires_at()
    return dumps({
        "linked": True,
        "account": me.get("name"),
        "link_karma": me.get("link_karma"),
        "comment_karma": me.get("comment_karma"),
        "account_created": iso_time(me.get("created_utc")),
        "verified_email": me.get("has_verified_email"),
        "session_expires": iso_time(exp) if exp else None,
        "reads": "logged in (JSON API for threads)",
        "rate_budget": _budget(web),
    })


def iso_time(ts) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts))) if ts else None


@mcp.tool(**_TOOL)
async def get_post_requirements(
    subreddit: Annotated[
        str, Field(description="Community name (e.g. 'python' or 'r/python'), or 'profile' for your own profile")
    ],
) -> str:
    """What a community accepts before you post there as the linked account:
    allowed post types, flair choices (ids for create_post), its rules and
    posting guidelines, and whether this account can post. Call it before create_post on a subreddit."""
    web = shared_client()
    try:
        return dumps(await write.post_requirements(web, subreddit))
    except RedditError as e:
        raise _reddit_error(e) from None


@mcp.tool(**_WRITE)
async def create_post(
    subreddit: Annotated[
        str,
        Field(description="Where to post: a community ('python', 'r/python') or 'profile' for "
              "the linked account's own profile"),
    ],
    title: Annotated[str, Field(description="Post title (max 300 chars; Reddit never lets titles be edited later)")],
    kind: Annotated[
        Literal["text", "link", "image", "gallery", "video"],
        Field(description="text = markdown body (may include images); link = a URL; image = one "
              "image; gallery = 2-20 images with captions; video = one MP4/MOV"),
    ] = "text",
    body: Annotated[
        str | None,
        Field(description="Markdown body. Images inside it: ![caption](/local/path.png or "
              "https://...image.jpg); they are uploaded to Reddit and shown inline. With "
              "link/image/gallery/video posts it is optional extra text, if the community allows it"),
    ] = None,
    url: Annotated[str | None, Field(description="kind=link: the URL to share")] = None,
    media: Annotated[
        list[dict] | None,
        Field(description="kind=image/video: [{\"source\": path-or-URL}]; kind=gallery: "
              "[{\"source\": path-or-URL, \"caption\": optional (max 180), \"outbound_url\": "
              "optional link}, ...]. PNG, JPEG, GIF, WEBP images; MP4/MOV video"),
    ] = None,
    video_poster: Annotated[
        str | None,
        Field(description="kind=video: thumbnail image path or URL (default: the first frame, if ffmpeg is installed)"),
    ] = None,
    nsfw: Annotated[bool, Field(description="Mark NSFW")] = False,
    spoiler: Annotated[bool, Field(description="Mark as spoiler")] = False,
    send_replies: Annotated[bool, Field(description="Inbox notifications for replies")] = True,
    flair_id: Annotated[str | None, Field(description="Flair template id from get_post_requirements")] = None,
    flair_text: Annotated[str | None, Field(description="Flair text (only for flairs with text_editable)")] = None,
    allow_repost: Annotated[bool, Field(description="kind=link: post even if the URL was already shared there")] = False,
    link_dialog: Annotated[LinkDialog, Resolve(_ask_before_account_use)] = None,
) -> str:
    """Publish a new post as the linked Reddit account. It is public at once,
    under the user's name: only post what the user asked for, with their
    wording. Check get_post_requirements first (post types, flair, rules).
    Returns the new post's id and URL. Sent once (never retried): on a timeout
    error, look at the profile before trying again."""
    await _take_choice(link_dialog)
    web = await _account_client()
    try:
        out = await write.create_post(
            web, target=subreddit, title=title, kind=kind, body=body, url=url, media=media,
            video_poster=video_poster, nsfw=nsfw, spoiler=spoiler, send_replies=send_replies,
            flair_id=flair_id, flair_text=flair_text, allow_repost=allow_repost,
        )
    except RedditError as e:
        raise _reddit_error(e) from None
    return dumps(out)


@mcp.tool(**_WRITE)
async def edit_post(
    post: Annotated[str, _POST_ID],
    body: Annotated[
        str,
        Field(description="The new body, replacing the old one entirely (markdown; images via "
              "![caption](path-or-URL)). Get the current one with get_thread first to change part of it"),
    ],
    link_dialog: Annotated[LinkDialog, Resolve(_ask_before_account_use)] = None,
) -> str:
    """Replace the body text of one of the linked account's own posts (text
    posts; also the optional text of link/image/gallery posts). Titles can't be
    edited on Reddit; to change the title, delete and post again."""
    await _take_choice(link_dialog)
    web = await _account_client()
    try:
        pid = await _post_id(post, web)
        return dumps(await write.edit_post(web, pid, body))
    except RedditError as e:
        raise _reddit_error(e) from None


@mcp.tool(**_WRITE)
async def set_post_options(
    post: Annotated[str, _POST_ID],
    nsfw: Annotated[bool | None, Field(description="Set/clear the NSFW mark")] = None,
    spoiler: Annotated[bool | None, Field(description="Set/clear the spoiler mark")] = None,
    send_replies: Annotated[bool | None, Field(description="Reply notifications on/off")] = None,
    flair_id: Annotated[str | None, Field(description="New flair template id (get_post_requirements)")] = None,
    flair_text: Annotated[str | None, Field(description="Flair text (editable flairs)")] = None,
    clear_flair: Annotated[bool, Field(description="Remove the flair")] = False,
    link_dialog: Annotated[LinkDialog, Resolve(_ask_before_account_use)] = None,
) -> str:
    """Change the NSFW/spoiler marks, reply notifications or flair of one of the
    linked account's own posts. Only the options given are changed."""
    await _take_choice(link_dialog)
    web = await _account_client()
    try:
        pid = await _post_id(post, web)
        return dumps(await write.set_post_options(
            web, pid, nsfw=nsfw, spoiler=spoiler, send_replies=send_replies,
            flair_id=flair_id, flair_text=flair_text, clear_flair=clear_flair,
        ))
    except RedditError as e:
        raise _reddit_error(e) from None


@mcp.tool(**_DESTRUCTIVE)
async def delete_post(
    post: Annotated[str, _POST_ID],
    link_dialog: Annotated[LinkDialog, Resolve(_ask_before_account_use)] = None,
) -> str:
    """Delete one of the linked account's own posts, permanently (it can't be
    restored). Only do this when the user asked for it."""
    await _take_choice(link_dialog)
    web = await _account_client()
    try:
        pid = await _post_id(post, web)
        return dumps(await write.delete_post(web, pid))
    except RedditError as e:
        raise _reddit_error(e) from None


def main() -> None:
    if len(sys.argv) > 1:  # reddit-native-mcp login | status | logout
        sys.exit(auth_cli(sys.argv[1:]))
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request INFO spam
    mcp.run()


if __name__ == "__main__":
    main()
