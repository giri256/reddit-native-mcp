"""Reddit's classic JSON API, readable only with a logged-in session.

Anonymous clients get 403 here, which is why the anonymous path parses the
web app's HTML partials instead. Logged in, one `/comments/<id>.json` call
returns up to 500 comments, where the HTML path returns ~25 plus ~10 per extra
request. These helpers turn JSON things into the same raw post/comment
dicts that parse.py produces, so shape.py and thread.py treat both alike.

Hidden replies: a `more` thing lists comment ids to load through
`/api/morechildren.json` (100 per request, link kind "json_more"). Its
"continue this thread" form (id "_", no children) means the replies sit past
Reddit's depth limit; they load from `/comments/<post>/_/<comment>.json`
(link kind "json_thread").
"""

from __future__ import annotations

from datetime import datetime, timezone

from .parse import MoreLink, strip_kind

MORECHILDREN_PATH = "/api/morechildren.json"
MORECHILDREN_MAX = 100  # ids per morechildren request (Reddit's limit)
THREAD_LIMIT = 500  # comments per /comments/<id>.json request (Reddit's cap)


def iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def post_type(d: dict) -> str:
    if d.get("is_self"):
        return "text"
    if d.get("is_gallery"):
        return "gallery"
    if d.get("is_video") or d.get("post_hint") in ("hosted:video", "rich:video"):
        return "video"
    if d.get("post_hint") == "image":
        return "image"
    if d.get("poll_data"):
        return "poll"
    return "link"


def post_from_json(d: dict) -> dict:
    """A t3 `data` dict as a parse.py-style raw post."""
    pid = d.get("id")
    created = d.get("created_utc")
    rec: dict = {
        "id": pid,
        "subreddit": d.get("subreddit"),
        "author": d.get("author"),
        "created": iso(created),
        "created_ts": float(created) if created else None,
        "title": d.get("title"),
        "permalink": d.get("permalink") or "",
        "post_type": post_type(d),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
    }
    url = d.get("url_overridden_by_dest") or d.get("url") or ""
    if url and not d.get("is_self") and f"/comments/{pid}" not in url and f"/gallery/{pid}" not in url:
        rec["link_url"] = url
    if d.get("link_flair_text"):
        rec["flair"] = d["link_flair_text"]
    for src, key in (("over_18", "nsfw"), ("spoiler", "spoiler"), ("locked", "locked"), ("stickied", "stickied")):
        if d.get(src):
            rec[key] = True
    if d.get("selftext"):
        rec["body"] = d["selftext"]
    return rec


def comment_from_json(d: dict, depth: int) -> dict:
    """A t1 `data` dict as a parse.py-style raw comment (`depth` given by the
    caller: it counts from the tree being built, not Reddit's view)."""
    created = d.get("created_utc")
    parent = d.get("parent_id") or ""
    body = d.get("body") or ""
    rec: dict = {
        "id": d.get("id"),
        "parent_id": strip_kind(parent) if parent.startswith("t1_") else None,
        "depth": depth,
        "author": None if d.get("author") == "[deleted]" else d.get("author"),
        "score": d.get("score"),
        "created": iso(created),
        "created_ts": float(created) if created else None,
        "body": body,
        "post_id": strip_kind(d.get("link_id")),
    }
    if d.get("is_submitter"):
        rec["op"] = True
    if d.get("distinguished"):
        rec["distinguished"] = d["distinguished"]
    if d.get("stickied"):
        rec["stickied"] = True
    if body == "[deleted]" and d.get("author") == "[deleted]":
        rec["deleted"] = True
        rec["body"] = ""
    elif body == "[removed]":
        rec["removed"] = True
    if d.get("collapsed"):
        rec["collapsed"] = True
    if d.get("edited"):
        rec["edited"] = iso(d["edited"]) if isinstance(d["edited"], (int, float)) and not isinstance(d["edited"], bool) else True
    return rec


def more_links(d: dict, post_id: str) -> list[MoreLink]:
    """MoreLinks for one `more` thing (parent_id None = top level)."""
    parent = d.get("parent_id") or ""
    parent_id = strip_kind(parent) if parent.startswith("t1_") else None
    children = [c for c in d.get("children") or [] if c]
    if not children:
        if parent_id is None:
            return []
        # "continue this thread": replies are past Reddit's depth limit
        return [MoreLink(
            src=f"/comments/{post_id}/_/{parent_id}.json", parent_id=parent_id,
            count=d.get("count") or None, kind="json_thread",
        )]
    total = d.get("count") or len(children)
    out = []
    for i in range(0, len(children), MORECHILDREN_MAX):
        chunk = children[i : i + MORECHILDREN_MAX]
        out.append(MoreLink(
            src=MORECHILDREN_PATH, cursor=",".join(chunk), parent_id=parent_id,
            count=max(len(chunk), round(total * len(chunk) / len(children))), kind="json_more",
        ))
    return out


def walk(children: list, post_id: str, depths: dict[str, int], base_depth: int = 0):
    """Comments (document order) and MoreLinks from a nested Listing's
    children. `depths` maps known comment ids to their depth; new comments
    hang one below their parent, or at `base_depth` when the parent is
    unknown (top level, or the root of a loaded subtree)."""
    comments: list[dict] = []
    more: list[MoreLink] = []
    stack = list(reversed(children or []))
    while stack:
        thing = stack.pop()
        kind, d = thing.get("kind"), thing.get("data") or {}
        if kind == "more":
            more.extend(more_links(d, post_id))
            continue
        if kind != "t1":
            continue
        parent = d.get("parent_id") or ""
        pid = strip_kind(parent) if parent.startswith("t1_") else None
        depth = depths[pid] + 1 if pid in depths else base_depth
        rec = comment_from_json(d, depth)
        depths[rec["id"]] = depth
        comments.append(rec)
        replies = d.get("replies")
        if isinstance(replies, dict):
            stack.extend(reversed((replies.get("data") or {}).get("children") or []))
    return comments, more


def morechildren_things(payload: dict) -> list:
    """The flat thing list of a morechildren response."""
    return (((payload or {}).get("json") or {}).get("data") or {}).get("things") or []


def morechildren_errors(payload: dict) -> list:
    return ((payload or {}).get("json") or {}).get("errors") or []
