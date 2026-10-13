"""Subreddit feeds and Reddit's own search, with opaque paging cursors.

- Feed: `/svc/shreddit/community-more-posts/<sort>/?name=<sub>[&t=][&after=]`
  -> ~24 posts per page (full selftext, flair, live score). `after` is the
  page's cursor (base64 of the last post's fullname).
- Search: `/svc/shreddit[/r/<sub>]/search/?q=&type=posts&sort=&t=[&cursor=]`
  -> ~7 results per page, no selftext. Sitewide search ignores the query
  when sort=new (it returns the newest posts of all of Reddit), so a
  sitewide "new" search runs as relevance and is re-sorted by time here.
  `author:<name>` inside q scopes results to a user.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, urlsplit

from .http import RedditWeb
from .parse import parse

FEED_SORTS = ("hot", "new", "top", "rising", "best")
SEARCH_SORTS = ("relevance", "new", "top", "comments")
TIMES = ("hour", "day", "week", "month", "year", "all")


def _query_param(src: str | None, key: str) -> str | None:
    if not src:
        return None
    values = parse_qs(urlsplit(src).query).get(key)
    return values[0] if values else None


def feed_cursor_post(cursor: str) -> str | None:
    """The post id a feed cursor points at (for diagnostics/tests)."""
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    return raw.removeprefix("t3_") if raw.startswith("t3_") else None


async def feed_page(
    web: RedditWeb, sub: str, sort: str = "new", time: str | None = None, cursor: str | None = None
) -> tuple[list[dict], str | None]:
    """One feed page: (posts, next cursor or None)."""
    params: dict = {"name": sub}
    if sort == "top" and time:
        params["t"] = time.upper()
    if cursor:
        params["after"] = cursor
    page = parse(await web.svc(f"/svc/shreddit/community-more-posts/{sort}/", params=params))
    posts = [p for p in page.posts if (p.get("subreddit") or "").lower() == sub.lower()]
    nxt = _query_param(page.feed_next, "after") if posts else None
    return posts, nxt


async def search_page(
    web: RedditWeb,
    query: str,
    *,
    sub: str | None = None,
    sort: str = "relevance",
    time: str = "all",
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    """One search results page: (posts without selftext, next cursor)."""
    if sort == "new" and not sub:
        sort = "relevance"  # sitewide "new" ignores q; caller re-sorts
    params: dict = {"q": query, "type": "posts", "sort": sort, "t": time}
    if cursor:
        params["cursor"] = cursor
    path = f"/svc/shreddit/r/{sub}/search/" if sub else "/svc/shreddit/search/"
    page = parse(await web.svc(path, params=params))
    return page.search, _query_param(page.search_next, "cursor")


def time_window(days: float) -> str:
    """Smallest Reddit search window covering `days`."""
    for name, span in (("hour", 1 / 24), ("day", 1), ("week", 7), ("month", 31), ("year", 365)):
        if days <= span:
            return name
    return "all"
