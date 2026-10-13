"""Token-lean records, comment-tree building/budgeting, keyword ranking.

Output is read by agents, often under a hard tool-output cap (Claude Code's
default is 25K tokens), so records carry no per-comment permalinks (comment
URL = post url + comment id + '/'), flags only when true, and ISO-8601 UTC
times. Scores come live from reddit.com, so there are no stale-stat flags.
"""

from __future__ import annotations

import heapq
import itertools
import json
import re
from dataclasses import dataclass, field

from .parse import MoreLink

PREVIEW_CHARS = 400
_NODE_OVERHEAD = 32  # nesting punctuation + possible "more_replies" per comment
_MAX_LISTED_IDS = 25
_POST_FLAGS = ("nsfw", "spoiler", "locked", "stickied")


def dumps(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}… [+{len(text) - limit} chars]"


def compact_post(raw: dict, *, preview_chars: int | None = None) -> dict:
    """Agent-facing post record. `url` is the Reddit thread; `link_url` the
    external/media target. `preview_chars` truncates selftext (listings);
    None keeps the full text (get_thread)."""
    pid, sub = raw.get("id"), raw.get("subreddit")
    permalink = raw.get("permalink") or (f"/r/{sub}/comments/{pid}/" if sub and pid else "")
    out: dict = {
        "id": pid,
        "subreddit": sub,
        "author": raw.get("author"),
        "created": raw.get("created"),
        "title": raw.get("title"),
        "url": f"https://www.reddit.com{permalink}" if permalink else None,
        "link_url": raw.get("link_url"),
        "type": raw.get("post_type"),
        "flair": raw.get("flair"),
        "score": raw.get("score"),
        "upvote_ratio": round(raw["upvote_ratio"], 3) if raw.get("upvote_ratio") else None,
        "num_comments": raw.get("num_comments"),
    }
    for flag in _POST_FLAGS:
        if raw.get(flag):
            out[flag] = True
    if raw.get("thumbnail"):
        out["thumbnail"] = raw["thumbnail"]
    text = raw.get("body") or ""
    if text:
        out["selftext"] = preview(text, preview_chars) if preview_chars else text
    return {k: v for k, v in out.items() if v is not None}


def compact_comment(raw: dict, depth: int) -> dict:
    out: dict = {
        "id": raw.get("id"),
        "author": raw.get("author"),
        "depth": depth,
        "score": raw.get("score"),
        "created": raw.get("created"),
    }
    for flag in ("op", "stickied", "deleted", "removed", "collapsed"):
        if raw.get(flag):
            out[flag] = True
    if raw.get("distinguished"):
        out["distinguished"] = raw["distinguished"]
    if raw.get("edited"):
        out["edited"] = raw["edited"]
    out["body"] = raw.get("body") or ("[deleted]" if raw.get("deleted") else "")
    return {k: v for k, v in out.items() if v is not None}


# --- comment trees -------------------------------------------------------


@dataclass(eq=False)
class _Node:
    raw: dict
    record: dict
    size: int
    order: int  # position in Reddit's own ordering (document order)
    children: list[_Node] = field(default_factory=list)
    hidden_more: int = 0  # comments behind unexpanded "more" links right below
    more_links: int = 0
    subtree: int = 1  # self + every descendant known or counted


def _order_key(sort: str):
    if sort == "new":
        return lambda n: (-(n.raw.get("created_ts") or 0), n.order)
    if sort == "old":
        return lambda n: (n.raw.get("created_ts") or 0, n.order)
    if sort == "top":
        return lambda n: (-(n.raw.get("score") or 0), n.order)
    return lambda n: (n.order,)  # Reddit's order (best/controversial/qa/...)


def json_size(rec: dict) -> int:
    return len(dumps(rec)) + _NODE_OVERHEAD


def build_tree(
    comments: list[dict], more: list[MoreLink], root_depth: int = 0, measure=json_size
) -> tuple[list[_Node], int, int]:
    """Nodes from flat parsed comments (document order, parent_id links).
    Returns (roots, hidden top-level comments, unexpanded top-level links).
    Comments whose parent is unknown become roots (e.g. a drill-down)."""
    nodes: dict[str, _Node] = {}
    roots: list[_Node] = []
    for i, c in enumerate(comments):
        if c["id"] in nodes:
            continue
        depth = max(0, (c.get("depth") or 0) - root_depth)
        rec = compact_comment(c, depth)
        node = _Node(c, rec, measure(rec), i)
        nodes[c["id"]] = node
        parent = nodes.get(c.get("parent_id") or "")
        (parent.children if parent else roots).append(node)
    root_hidden = root_links = 0
    for m in more:
        parent = nodes.get(m.parent_id or "")
        n = m.count if m.count is not None else 1
        if parent is None:
            root_hidden += n
            root_links += 1
        else:
            parent.hidden_more += n
            parent.more_links += 1

    def size(node: _Node) -> int:
        node.subtree = 1 + node.hidden_more + sum(size(c) for c in node.children)
        return node.subtree

    for r in roots:
        size(r)
    return roots, root_hidden, root_links


def estimate_chars(comments: list[dict]) -> int:
    return sum(len(dumps(compact_comment(c, c.get("depth") or 0))) + _NODE_OVERHEAD for c in comments)


def compact_tree(
    comments: list[dict],
    more: list[MoreLink],
    *,
    sort: str = "best",
    max_depth: int = 0,
    budget_chars: int | None = None,
    root_depth: int = 0,
    measure=json_size,
) -> tuple[list[dict], dict]:
    """Nested comment records, chosen best-first in `sort` order (a reply is
    eligible once its parent is in) until `budget_chars` runs out;
    `max_depth` > 0 drops deeper replies. Each returned comment with hidden
    replies (not fetched, depth-cut or over budget) gets `more_replies`."""
    roots, root_hidden, root_links = build_tree(comments, more, root_depth, measure)
    key = _order_key(sort)
    tie = itertools.count()
    heap = [(key(n), next(tie), n) for n in roots]
    heapq.heapify(heap)
    included: set[int] = set()
    used = 0
    max_seen = 0
    while heap:
        _, _, node = heapq.heappop(heap)
        if budget_chars is not None and used + node.size > budget_chars:
            break
        used += node.size
        included.add(id(node))
        max_seen = max(max_seen, node.record["depth"])
        if not max_depth or node.record["depth"] < max_depth:
            for child in node.children:
                heapq.heappush(heap, (key(child), next(tie), child))

    def render(level: list[_Node]) -> list[dict]:
        out = []
        for node in sorted((n for n in level if id(n) in included), key=key):
            rec = dict(node.record)
            hidden = node.hidden_more + sum(c.subtree for c in node.children if id(c) not in included)
            if hidden:
                rec["more_replies"] = hidden
            replies = render(node.children)
            if replies:
                rec["replies"] = replies
            out.append(rec)
        return out

    tree = render(roots)
    known = root_hidden + sum(r.subtree for r in roots)
    omitted_roots = sorted((r for r in roots if id(r) not in included), key=key)
    stats: dict = {
        "returned_comments": len(included),
        "known_comments": known,
        "omitted_comments": known - len(included),
        "max_depth_returned": max_seen,
        "top_level_count": len(tree),
    }
    if root_hidden:
        stats["unloaded_top_level"] = root_hidden
    if omitted_roots:
        stats["omitted_top_level_ids"] = [r.record["id"] for r in omitted_roots][:_MAX_LISTED_IDS]
    return tree, stats


# --- plain-text rendering --------------------------------------------------
# ~22% smaller than JSON on the example thread (structure overhead halves)
# and reads like a forum outline. Budgeting uses text_size, which matches
# _comment_lines exactly up to the "(+N more replies)" line it reserves.

_FLAGS = ("op", "stickied", "deleted", "removed", "collapsed")


def _comment_head(rec: dict) -> str:
    parts = [f"- {rec['id']}", f"u/{rec.get('author') or '?'}"]
    if rec.get("score") is not None:
        parts.append(f"{rec['score']}pt")
    if rec.get("created"):
        parts.append(rec["created"][:16].replace("T", " "))
    parts += [f"[{f}]" for f in _FLAGS if rec.get(f)]
    if rec.get("distinguished"):
        parts.append(f"[{rec['distinguished']}]")
    if rec.get("edited"):
        parts.append("[edited]")
    return " ".join(parts)


def _comment_lines(rec: dict) -> list[str]:
    pad = "  " * rec.get("depth", 0)
    lines = [pad + _comment_head(rec)]
    lines += [f"{pad}  {ln}" if ln else "" for ln in (rec.get("body") or "").split("\n")]
    if rec.get("more_replies"):
        lines.append(f"{pad}  (+{rec['more_replies']} more replies)")
    return lines


def text_size(rec: dict) -> int:
    return sum(len(ln) + 1 for ln in _comment_lines(rec)) + 2 * rec.get("depth", 0) + 24


def render_comments(tree: list[dict]) -> list[str]:
    out: list[str] = []
    for rec in tree:
        out += _comment_lines(rec)
        out += render_comments(rec.get("replies") or [])
    return out


def render_post(post: dict) -> list[str]:
    facts = [f"r/{post.get('subreddit')}", f"u/{post.get('author')}"]
    if post.get("score") is not None:
        ratio = f" ({post['upvote_ratio']:.0%} up)" if post.get("upvote_ratio") else ""
        facts.append(f"{post['score']} points{ratio}")
    if post.get("num_comments") is not None:
        facts.append(f"{post['num_comments']} comments")
    if post.get("created"):
        facts.append(post["created"])
    for key in ("type", "flair"):
        if post.get(key):
            facts.append(f"{key}: {post[key]}")
    facts += [f for f in _POST_FLAGS if post.get(f)]
    lines = [f"# {post.get('title')}", " · ".join(facts), f"url: {post.get('url')}"]
    if post.get("link_url"):
        lines.append(f"link: {post['link_url']}")
    if post.get("selftext"):
        lines += ["", post["selftext"]]
    return lines


def render_meta(meta: dict) -> list[str]:
    return [f"{k}: {v if isinstance(v, (str, int, float)) else dumps(v)}" for k, v in meta.items()]


# --- keyword matching ----------------------------------------------------


def normalize_keywords(keywords: str | list[str]) -> list[str]:
    """A plain string is one phrase; a comma-separated string or list is
    several (OR-matched), all case-folded."""
    parts = keywords.split(",") if isinstance(keywords, str) else [str(k) for k in keywords]
    seen: dict[str, None] = {}
    for p in parts:
        p = " ".join(p.split()).lower()
        if p:
            seen.setdefault(p)
    return list(seen)


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Match at a word start ('team' skips 'steam'; 'startup' hits
    'startups'); words may be joined by space, '-', '_' or nothing. Short
    single tokens (<= 3 chars, e.g. 'ai') must be whole words."""
    words = [re.escape(w) for w in re.split(r"[\s_-]+", keyword) if w]
    body = r"[\s_-]*".join(words)
    if len(words) == 1 and len(keyword) <= 3 and keyword.isalnum():
        return re.compile(rf"(?<!\w){body}s?(?!\w)", re.I)
    return re.compile(rf"(?<!\w){body}", re.I)


def keyword_hits(post: dict, patterns: list[tuple[str, re.Pattern[str]]]) -> tuple[list[str], int]:
    """(keywords matched in title or body, how many of them in the title)."""
    title, body = post.get("title") or "", post.get("body") or ""
    in_title = {k for k, rx in patterns if rx.search(title)}
    matched = [k for k, rx in patterns if k in in_title or rx.search(body)]
    return matched, len(in_title)
