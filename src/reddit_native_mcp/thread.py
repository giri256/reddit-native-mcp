"""Fetch a thread from reddit.com and expand its collapsed comments.

The post page carries the post and the first ~25 comments. The rest sit
behind "more" links (parse.MoreLink): cursor partials (POST, ~10 comments
each) and, past Reddit's render depth (~4), permalink pages that restart
depths at 0. Expansion runs on a small worker pool, in Reddit's own order, until every
link is expanded or a limit is hit: the output budget (fetching more than
can be shown is wasted rate limit), a per-call request cap, the deadline, or
Reddit's rate budget. What stays unexpanded is reported as more_replies.

Logged in, threads come from the classic JSON API instead (jsonapi.py): up
to 500 comments in the first request, then 100 per morechildren request. If
Reddit rejects the session, the HTML path above takes over.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from . import jsonapi
from .http import API, WEB, NotFound, NotLoggedIn, RateLimited, RedditError, RedditWeb
from .parse import MoreLink, Page, parse
from .shape import estimate_chars

WORKERS = 4  # = RedditWeb's connection concurrency
MAX_REQUESTS = 60  # per call; Reddit's anonymous window is ~200
DEADLINE_S = 40.0  # stay under 60s client request timeouts
OVERFETCH = 1.2  # fetch a bit past the budget so selection has choice

# get_thread sort -> the JSON API's ?sort= value
JSON_SORTS = {"best": "confidence", "top": "top", "new": "new", "old": "old",
              "controversial": "controversial", "qa": "qa"}
# get_thread sort -> Reddit's ?sort= value (None = Reddit's default, "best")
SORTS = {
    "best": None,
    "top": "top",
    "new": "new",
    "old": "old",
    "controversial": "controversial",
    "qa": "qa",
}


SHELL_RETRIES = 2
_sleep = asyncio.sleep  # module hook: tests patch this, never asyncio itself


async def content_page(
    web: RedditWeb, path: str, params: dict | None = None, *, need: str = "<shreddit-post "
) -> str:
    """A thread/permalink page. Reddit now and then answers a valid one with
    a shell that lacks the post or the comment tree (rendered lazily for
    browsers); fetch again when `need` is missing."""
    for attempt in range(SHELL_RETRIES + 1):
        _, html = await web.page(path, params=params, fresh=attempt > 0)
        if need in html or attempt == SHELL_RETRIES:
            return html
        await _sleep(0.5 * (attempt + 1))
    return html


async def post_of_comment(web: RedditWeb, comment_id: str) -> str:
    """The post a bare comment id belongs to (one small svc request)."""
    try:
        page = parse(await web.svc(f"/svc/shreddit/comment/t1_{comment_id}"))
    except NotFound:
        page = Page()
    for c in page.comments:
        if c["id"] == comment_id and c.get("post_id"):
            return c["post_id"]
    raise NotFound(f"reddit.com shows no comment {comment_id} (deleted, removed or mistyped?)")


@dataclass
class Thread:
    post: dict
    comments: list[dict]
    pending: list[MoreLink]  # links not expanded (limits or errors)
    total_comments: int | None = None  # Reddit's count, deleted ones included
    focus_parent_id: str | None = None
    requests: int = 0
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    source: str = "web"  # "json" = classic JSON API (logged in)


def _descendants(page: Page, root_id: str) -> list[dict]:
    children: dict[str | None, list[dict]] = {}
    for c in page.comments:
        children.setdefault(c["parent_id"], []).append(c)
    out, stack = [], list(reversed(children.get(root_id, [])))
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(reversed(children.get(c["id"], [])))
    return out


class _Expander:
    def __init__(
        self, web: RedditWeb, char_budget: int | None, max_requests: int, deadline: float,
        *, post_id: str = "", sort: str = "best", bucket: str = WEB,
    ):
        self.web = web
        self.post_id = post_id
        self.sort = sort
        self.bucket = bucket
        self.depths: dict[str, int] = {}
        # Reddit answers concurrent morechildren calls from one user with 429
        self._morechildren = asyncio.Lock()
        self.char_budget = char_budget
        self.max_requests = max_requests
        self.deadline = time.monotonic() + deadline
        self.comments: dict[str, dict] = {}
        self.order: list[dict] = []
        self.chars = 0
        self.requests = 0
        self.errors: list[str] = []
        self.offset: dict[int, int] = {}  # id(MoreLink) -> depth offset
        self.failed: list[MoreLink] = []

    def add(self, comments: list[dict], offset: int, default_parent: str | None) -> None:
        """Record new comments; roots of a loaded link hang under its parent."""
        for c in comments:
            if c["id"] in self.comments:
                continue
            c = dict(c, depth=(c.get("depth") or 0) + offset)
            if c.get("parent_id") is None:
                c["parent_id"] = default_parent
            self.comments[c["id"]] = c
            self.depths[c["id"]] = c["depth"]
            self.order.append(c)
            self.chars += estimate_chars([c])

    def depth_of(self, cid: str | None) -> int:
        c = self.comments.get(cid or "")
        return c["depth"] if c else -1

    def stop_reason(self) -> str | None:
        if self.char_budget is not None and self.chars >= self.char_budget * OVERFETCH:
            return "output budget reached"
        if self.requests >= self.max_requests:
            return f"request cap ({self.max_requests}) reached"
        if time.monotonic() > self.deadline - 3:
            return "call deadline reached"
        left = self.web.budget_left(self.bucket)
        if left is not None and left <= 12:
            return "Reddit rate budget nearly used up"
        return None

    async def expand(self, link: MoreLink) -> list[MoreLink]:
        """Load one link; returns the new links found inside it."""
        offset = self.offset.get(id(link), 0)
        self.requests += 1
        if link.kind in ("json_more", "json_thread"):
            return await self._expand_json(link)
        if link.kind == "cursor":
            html = await self.web.svc(link.src, data={"cursor": link.cursor})
            page = parse(html)
            self.add(page.comments, offset, link.parent_id)
            new = page.more
            child_offset = offset
        else:
            html = await content_page(self.web, link.src, need="<shreddit-comment ")
            page = parse(html)
            focus = next((c for c in page.comments if c["id"] == link.parent_id), None)
            if focus is None:
                raise RedditError(f"permalink page {link.src} lacks comment {link.parent_id}")
            child_offset = self.depth_of(link.parent_id) - (focus.get("depth") or 0)
            desc = _descendants(page, link.parent_id)
            self.add(desc, child_offset, link.parent_id)
            keep = {link.parent_id} | {c["id"] for c in desc}
            new = [m for m in page.more if m.parent_id in keep]
        for m in new:
            self.offset[id(m)] = child_offset
        return new

    async def _expand_json(self, link: MoreLink) -> list[MoreLink]:
        depths = dict(self.depths)  # walk() fills in the new ones
        base = self.depth_of(link.parent_id) + 1
        if link.kind == "json_more":
            base = 0  # a top-level comment; replies find their parent's depth
            async with self._morechildren:
                payload = await self.web.api_json(
                    link.src,
                    params={
                        "api_type": "json", "link_id": f"t3_{self.post_id}", "children": link.cursor,
                        "sort": JSON_SORTS[self.sort], "limit_children": "false",
                    },
                )
            errs = jsonapi.morechildren_errors(payload)
            if errs:
                raise RedditError(f"morechildren: {errs}")
            comments, new = jsonapi.walk(jsonapi.morechildren_things(payload), self.post_id, depths, base)
            self.add(comments, 0, None)
            return new
        else:
            payload = await self.web.api_json(link.src, params={"sort": JSON_SORTS[self.sort], "limit": jsonapi.THREAD_LIMIT})
            listing = payload[1]["data"]["children"] if isinstance(payload, list) and len(payload) > 1 else []
            # the listing's root is the parent comment itself (already known)
            comments, new = jsonapi.walk(listing, self.post_id, depths, base - 1)
            comments = [c for c in comments if c["id"] != link.parent_id]
        self.add(comments, 0, link.parent_id)
        return new

    async def run(self, links: list[MoreLink]) -> tuple[list[MoreLink], str | None]:
        """Expand links with WORKERS concurrent workers on one shared queue.
        Links found inside a loaded link go to the front: they continue
        conversations already shown."""
        queue = list(links)
        state: dict = {"reason": None, "busy": 0}
        wake = asyncio.Event()

        async def worker() -> None:
            while True:
                if state["reason"] is None:
                    state["reason"] = self.stop_reason() if queue else None
                if state["reason"] or not queue:
                    if state["busy"] == 0 or state["reason"]:
                        wake.set()
                        return
                    wake.clear()
                    await wake.wait()  # others may still find new links
                    continue
                link = queue.pop(0)
                batch = _take_batch(link, queue)
                if len(batch) > 1:
                    link = MoreLink(
                        src=link.src, cursor=",".join(m.cursor for m in batch),
                        count=sum(m.count or 1 for m in batch), kind="json_more",
                    )
                state["busy"] += 1
                try:
                    found = await self.expand(link)
                except RateLimited as e:
                    state["reason"] = str(e)
                    self.failed.extend(batch)
                except RedditError as e:
                    self.errors.append(f"{link.src}: {e}")
                    self.failed.extend(batch)  # reported as hidden, not retried
                else:
                    queue[:0] = found
                finally:
                    state["busy"] -= 1
                    wake.set()

        await asyncio.gather(*(worker() for _ in range(WORKERS)))
        return queue + self.failed, state["reason"]


def _take_batch(link: MoreLink, queue: list[MoreLink]) -> list[MoreLink]:
    """`link` plus queued morechildren links that fit in the same request:
    /api/morechildren takes up to 100 ids from anywhere in the thread."""
    batch = [link]
    if link.kind != "json_more":
        return batch
    n = link.cursor.count(",") + 1
    i = 0
    while i < len(queue) and n < jsonapi.MORECHILDREN_MAX:
        m = queue[i]
        k = m.cursor.count(",") + 1 if m.kind == "json_more" else 0
        if k and n + k <= jsonapi.MORECHILDREN_MAX:
            batch.append(queue.pop(i))
            n += k
        else:
            i += 1
    return batch


async def fetch_thread(
    web: RedditWeb,
    post_id: str,
    *,
    focus: str | None = None,
    sort: str = "best",
    char_budget: int | None = None,
    max_requests: int = MAX_REQUESTS,
    deadline: float = DEADLINE_S,
) -> Thread:
    """Post + comments (absolute depths; with `focus`, depths count from the
    focus comment = 0 and only its subtree is kept)."""
    if web.logged_in:
        try:
            return await _fetch_thread_json(
                web, post_id, focus=focus, sort=sort, char_budget=char_budget,
                max_requests=max_requests, deadline=deadline,
            )
        except NotLoggedIn:
            pass  # session rejected: read the web app like an anonymous client
    path = f"/comments/{post_id}/comment/{focus}/" if focus else f"/comments/{post_id}/"
    params = {"sort": SORTS[sort]} if SORTS.get(sort) else None
    exp = _Expander(web, char_budget, max_requests, deadline)
    exp.requests = 1
    try:
        html = await content_page(
            web, path, params, need="<shreddit-comment " if focus else "<shreddit-post "
        )
    except NotFound:
        what = f"comment {focus} under post {post_id}" if focus else f"post {post_id}"
        raise NotFound(f"reddit.com has no {what} (deleted, private or mistyped?)") from None
    page = parse(html)
    post = next((p for p in page.posts if p["id"] == post_id), None)
    if post is None:
        raise NotFound(
            f"reddit.com showed no post {post_id}: it may be removed, in a private or "
            "quarantined subreddit, or need a login (NSFW)"
        )
    focus_parent = None
    links = page.more
    if focus:
        node = next((c for c in page.comments if c["id"] == focus), None)
        if node is None:
            raise NotFound(f"Comment {focus} is not shown under post {post_id} (deleted or removed?)")
        focus_parent = node.get("parent_id")
        desc = _descendants(page, focus)
        root = dict(node, parent_id=None)
        base = node.get("depth") or 0
        exp.add([root] + desc, -base, None)
        keep = {focus} | {c["id"] for c in desc}
        links = [m for m in page.more if m.parent_id in keep]
        for m in links:
            exp.offset[id(m)] = -base
    else:
        exp.add(page.comments, 0, None)
    pending, reason = await exp.run(links)
    return Thread(
        post=post,
        comments=exp.order,
        pending=pending,
        total_comments=page.total_comments,
        focus_parent_id=focus_parent,
        requests=exp.requests,
        stop_reason=reason,
        errors=exp.errors,
    )


async def _fetch_thread_json(
    web: RedditWeb,
    post_id: str,
    *,
    focus: str | None,
    sort: str,
    char_budget: int | None,
    max_requests: int,
    deadline: float,
) -> Thread:
    """fetch_thread over the classic JSON API (logged in)."""
    path = f"/comments/{post_id}/_/{focus}.json" if focus else f"/comments/{post_id}.json"
    params: dict = {"sort": JSON_SORTS[sort], "limit": jsonapi.THREAD_LIMIT}
    if focus:
        params["context"] = 0
    exp = _Expander(web, char_budget, max_requests, deadline, post_id=post_id, sort=sort, bucket=API)
    exp.requests = 1
    try:
        payload = await web.api_json(path, params=params)
    except NotFound:
        what = f"comment {focus} under post {post_id}" if focus else f"post {post_id}"
        raise NotFound(f"reddit.com has no {what} (deleted, private or mistyped?)") from None
    try:
        post_data = payload[0]["data"]["children"][0]["data"]
        children = payload[1]["data"]["children"]
    except (KeyError, IndexError, TypeError):
        raise NotFound(f"reddit.com showed no post {post_id}") from None
    post = jsonapi.post_from_json(post_data)
    comments, links = jsonapi.walk(children, post_id, {}, 0)
    focus_parent = None
    if focus:
        node = next((c for c in comments if c["id"] == focus), None)
        if node is None:
            raise NotFound(f"Comment {focus} is not shown under post {post_id} (deleted or removed?)")
        focus_parent = node["parent_id"]
        node["parent_id"] = None
    exp.add(comments, 0, None)
    pending, reason = await exp.run(links)
    return Thread(
        post=post,
        comments=exp.order,
        pending=pending,
        total_comments=post.get("num_comments"),
        focus_parent_id=focus_parent,
        requests=exp.requests,
        stop_reason=reason,
        errors=exp.errors,
        source="json",
    )
