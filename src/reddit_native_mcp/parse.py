"""Extract posts, comments and pagination cursors from reddit.com HTML.

Reddit's web app renders data as attributes of custom elements:
- `<shreddit-post id="t3_.." post-title=.. author=.. score=.. comment-count=..
  created-timestamp=.. content-href=.. permalink=..>`; the body is the
  `#t3_<id>-post-rtjson-content` div inside it.
- `<shreddit-comment thingId="t1_.." author=.. score=.. depth=.. created=..
  [parentId=..] [is-op] [is-comment-deleted] ...>`, nested like the thread;
  the body is the `#t1_<id>-comment-rtjson-content` div.
- `<faceplate-partial src="/svc/shreddit/more-comments/..">` + hidden
  `cursor` input: a "N more replies" placeholder (POST the cursor to src).
- feed pages end with a `community-more-posts` partial (next page); search
  results are `div[data-testid=search-post-unit]` blocks.

Tokenizing is one regex pass over the (slimmed) page feeding start/end/data
handlers: Reddit's markup is generated and well-formed, and this is ~10x
faster than html.parser, whose per-tag work dominated thread fetch time.
Entities are unescaped in attribute values and text. Bodies become light
markdown.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote

_VOID = {"br", "img", "input", "source", "hr", "wbr", "meta", "link", "col", "area"}
_SKIP_TEXT = {"script", "style", "svg", "ac-track", "template", "button", "shreddit-player"}
_FLAIR_RE = re.compile(r"f=flair_name%3A%22(.+?)%22")


@dataclass
class MoreLink:
    """Comments not in the page. kind "cursor": POST `cursor` to `src` (a
    more-comments partial). kind "permalink": GET the page at `src`, the
    parent comment's permalink, which Reddit links instead of rendering
    replies past its depth limit (depths there restart at 0)."""

    src: str
    cursor: str = ""
    parent_id: str | None = None  # comment id the replies belong to; None = top level
    count: int | None = None  # how many comments it stands for, when shown
    kind: str = "cursor"

    @property
    def top_level(self) -> bool:
        return self.parent_id is None


@dataclass
class Page:
    posts: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    more: list[MoreLink] = field(default_factory=list)
    search: list[dict] = field(default_factory=list)
    feed_next: str | None = None  # src of the next feed page
    search_next: str | None = None  # src of the next search page
    total_comments: int | None = None  # comment-tree totalComments


# Inline CSS/JS/icons are 60-85% of every response and carry no data; cutting
# them with one C-level regex pass first makes the pure-Python parse ~3x
# faster (identical output on every captured fixture). <template> is kept:
# Reddit puts the tail of comment trees inside one.
_NOISE_RE = re.compile(
    "|".join(
        rf"<{t}\b[^>]*>[^<]*(?:<(?!/{t}>)[^<]*)*</{t}>" for t in ("script", "style", "svg")
    )
    + r"|<!--[^-]*(?:-(?!->)[^-]*)*-->",
    re.I,
)
_TREE_END = "</shreddit-comment-tree>"


def slim(html: str) -> str:
    html = _NOISE_RE.sub("", html)
    start, end = html.find("<shreddit-post "), html.rfind(_TREE_END)
    if start >= 0 and end > start:  # thread pages: only the post + comment tree
        html = html[start : end + len(_TREE_END)]
    return html


_TOKEN_RE = re.compile(r"<(/?)([a-zA-Z][\w:-]*)((?:[^>\"']+|\"[^\"]*\"|'[^']*')*)>")
_ATTR_RE = re.compile(r"""([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")
# Tags whose attributes the extractor reads outside a body capture.
_ATTR_TAGS = frozenset({
    "shreddit-comment", "shreddit-post", "shreddit-comment-tree", "div",
    "faceplate-partial", "input", "faceplate-number", "a",
    "search-telemetry-tracker", "faceplate-timeago", "faceplate-img",
})


def _attrs(raw: str) -> list[tuple[str, str]]:
    out = []
    for name, dq, sq, bare in _ATTR_RE.findall(raw):
        value = dq or sq or bare
        if "&" in value:
            value = unescape(value)
        out.append((name.lower(), value))
    return out


def _feed(ex: "_Extractor", html: str) -> None:
    pos = 0
    for m in _TOKEN_RE.finditer(html):
        capturing = ex.wants_text
        if capturing and m.start() > pos:
            text = html[pos : m.start()]
            ex.handle_data(unescape(text) if "&" in text else text)
        pos = m.end()
        closing, tag, raw = m.groups()
        tag = tag.lower()
        if closing:
            ex.handle_endtag(tag)
            continue
        attrs = _attrs(raw) if capturing or tag in _ATTR_TAGS else []
        if raw.endswith("/"):
            ex.handle_startendtag(tag, attrs)
        else:
            ex.handle_starttag(tag, attrs)
    if ex.wants_text and pos < len(html):
        ex.handle_data(unescape(html[pos:]))


def parse(html: str) -> Page:
    p = _Extractor()
    _feed(p, slim(html))
    page = p.page
    # Comments with a cursor partial also get a no-JS permalink twin.
    with_cursor = {m.parent_id for m in page.more if m.kind == "cursor"}
    page.more = [m for m in page.more if m.kind == "cursor" or m.parent_id not in with_cursor]
    return page


# --- value helpers ---------------------------------------------------------


def to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def iso_time(value: str | None) -> str | None:
    """'2026-10-06T19:25:01.400000+0000' -> '2026-10-06T19:25:01Z'."""
    ts = epoch(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def strip_kind(fullname: str | None) -> str | None:
    if not fullname:
        return None
    return fullname.split("_", 1)[1] if fullname[:3] in ("t1_", "t3_") else fullname


# --- rich text -> markdown -------------------------------------------------


@dataclass
class _El:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)  # _El | str


_BLOCK = {
    "p", "div", "li", "ul", "ol", "blockquote", "pre", "table", "thead",
    "tbody", "tr", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _raw_text(el: _El) -> str:
    out = []
    for c in el.children:
        if isinstance(c, str):
            out.append(c)
        elif c.tag == "br":
            out.append("\n")
        else:
            out.append(_raw_text(c))
    return "".join(out)


def _inline(el: _El) -> str:
    tag = el.tag
    if tag == "br":
        return "\n"
    if tag == "img":
        src = el.attrs.get("src") or ""
        return f"[image]({src})" if src else ""
    inner = "".join(_render_inline_children(el.children))
    if tag == "a":
        href = el.attrs.get("href") or ""
        text = inner.strip()
        if not href or text.startswith("[image]("):
            return inner
        if href.startswith("/"):
            href = "https://www.reddit.com" + href
        if not text or text.rstrip("/") == href.rstrip("/") or href.endswith(text):
            return href
        return f"[{text}]({href})"
    if not inner.strip():
        return inner
    if tag in ("strong", "b"):
        return f"**{inner.strip()}**"
    if tag in ("em", "i"):
        return f"*{inner.strip()}*"
    if tag in ("s", "del", "strike"):
        return f"~~{inner.strip()}~~"
    if tag == "code":
        return f"`{_raw_text(el).strip()}`"
    if tag == "sup":
        return f"^{inner.strip()}"
    return inner


def _render_inline_children(children: list) -> list[str]:
    out = []
    for c in children:
        if isinstance(c, str):
            out.append(_collapse(c))
        elif c.tag in _BLOCK:
            out.append(" " + "\n".join(_blocks(c)) + " ")
        else:
            out.append(_inline(c))
    return out


def _clean_inline(parts: list[str]) -> str:
    text = "".join(parts)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def _blocks(el: _El) -> list[str]:
    """Render a block element into a list of markdown blocks."""
    tag = el.tag
    if tag == "pre":
        code = _raw_text(el).strip("\n")
        return [f"```\n{code}\n```"] if code.strip() else []
    if tag == "hr":
        return ["---"]
    if tag in ("ul", "ol"):
        items = []
        n = 0
        for c in el.children:
            if isinstance(c, _El) and c.tag == "li":
                n += 1
                body = "\n".join(_child_blocks(c.children)) or ""
                marker = f"{n}. " if tag == "ol" else "- "
                lines = body.split("\n")
                items.append(marker + lines[0] + "".join("\n   " + ln if ln else "\n" for ln in lines[1:]))
        return ["\n".join(items)] if items else []
    if tag == "blockquote":
        inner = "\n\n".join(_child_blocks(el.children))
        return ["\n".join("> " + ln if ln else ">" for ln in inner.split("\n"))] if inner else []
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = _clean_inline(_render_inline_children(el.children))
        return [f"{'#' * int(tag[1])} {text}"] if text else []
    if tag == "table":
        rows = []
        for tr in _find_all(el, "tr"):
            cells = [
                _clean_inline(_render_inline_children(td.children))
                for td in tr.children
                if isinstance(td, _El) and td.tag in ("td", "th")
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        return ["\n".join(rows)] if rows else []
    return _child_blocks(el.children)


def _find_all(el: _El, tag: str) -> list[_El]:
    out = []
    for c in el.children:
        if isinstance(c, _El):
            if c.tag == tag:
                out.append(c)
            else:
                out.extend(_find_all(c, tag))
    return out


def _child_blocks(children: list) -> list[str]:
    blocks: list[str] = []
    run: list[str] = []

    def flush() -> None:
        text = _clean_inline(run)
        if text:
            blocks.append(text)
        run.clear()

    for c in children:
        if isinstance(c, str):
            run.append(_collapse(c))
        elif c.tag in _BLOCK:
            flush()
            blocks.extend(b for b in _blocks(c) if b)
        else:
            run.append(_inline(c))
    flush()
    return blocks


def to_markdown(root: _El) -> str:
    text = "\n\n".join(_blocks(root))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- the extractor ---------------------------------------------------------


class _Extractor:
    def __init__(self) -> None:
        self.page = Page()
        self._comments: list[dict] = []  # open <shreddit-comment> stack
        self._post: dict | None = None  # open <shreddit-post>
        self._capture: tuple[dict, str, list[_El], int] | None = None
        self._skip = 0  # depth inside script/style/svg (captures only)
        self._more: MoreLink | None = None
        self._more_tag = ""  # element that closes self._more
        self._tombstones = 0
        self._unit: dict | None = None  # open search result
        self._unit_divs = 0
        self._in_counter = False

    # body capture: (target record, key, element stack, div depth)

    def handle_starttag(self, tag: str, attrs_list: list) -> None:
        attrs = {k: (v if v is not None else "") for k, v in attrs_list}
        if self._capture is not None:
            self._capture_start(tag, attrs)
            return
        if tag == "shreddit-comment":
            self._start_comment(attrs)
        elif tag == "shreddit-post":
            self._start_post(attrs)
        elif tag == "shreddit-comment-tree" and attrs.get("totalcomments"):
            self.page.total_comments = to_int(attrs.get("totalcomments"))
        elif tag == "div":
            el_id = attrs.get("id", "")
            if el_id.endswith("-comment-rtjson-content") or el_id.endswith("-post-rtjson-content"):
                target = self._target_for(el_id)
                if target is not None:
                    self._capture = (target, "body", [_El("div", attrs)], 1)
                    return
            if attrs.get("data-testid") == "search-post-unit":
                self._unit = {}
                self._unit_divs = 1
                return
            if self._unit is not None:
                self._unit_divs += 1
                if attrs.get("data-testid") == "search-counter-row":
                    self._in_counter = True
        elif tag == "faceplate-partial":
            self._start_partial(attrs)
        elif tag == "input" and self._more is not None and attrs.get("name") == "cursor":
            self._more.cursor = attrs.get("value", "")
        elif tag == "faceplate-number":
            n = to_int(attrs.get("number"))
            if self._more is not None and self._more.count is None:
                self._more.count = n
            elif self._unit is not None and self._in_counter:
                self._unit.setdefault("_numbers", []).append(n)
        elif tag == "a":
            href = attrs.get("href", "")
            if attrs.get("slot") == "more-comments-permalink" and self._comments:
                self._more = MoreLink(
                    src=href.split("?", 1)[0], parent_id=self._comments[-1]["id"], kind="permalink"
                )
                self._more_tag = "a"
                self.page.more.append(self._more)
            if self._post is not None and "flair_name" in href and "flair" not in self._post:
                m = _FLAIR_RE.search(href)
                if m:
                    flair = unquote(m.group(1)).replace("+", " ")
                    self._post["flair"] = re.sub(r":[\w-]+:", "", flair).strip() or flair
            if self._unit is not None and attrs.get("data-testid") == "post-title":
                self._unit.setdefault("permalink", href)
        elif self._unit is not None:
            if tag == "search-telemetry-tracker" and "post" not in self._unit:
                self._unit_tracking(attrs.get("data-faceplate-tracking-context", ""))
            elif tag == "faceplate-timeago":
                self._unit.setdefault("created", attrs.get("ts"))
            elif tag == "faceplate-img" and attrs.get("data-testid") == "search_post_thumbnail":
                self._unit.setdefault("thumbnail", attrs.get("src"))

    def handle_startendtag(self, tag: str, attrs_list: list) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None:
            self._capture_end(tag)
            return
        if tag == "shreddit-comment" and self._comments:
            self._comments.pop()
        elif tag == "shreddit-post":
            self._post = None
        elif tag == self._more_tag and self._more is not None:
            # A permalink link without "N more replies" is the no-JS twin of a
            # cursor partial (or an empty "More replies"); only counted ones
            # stand for replies past Reddit's depth limit.
            if self._more.kind == "permalink" and not self._more.count:
                self.page.more.remove(self._more)
            self._more = None
        elif tag == "div" and self._unit is not None:
            self._unit_divs -= 1
            self._in_counter = False
            if self._unit_divs <= 0:
                self._finish_unit()

    @property
    def wants_text(self) -> bool:
        return self._capture is not None

    def handle_data(self, data: str) -> None:
        if self._capture is not None and not self._skip:
            self._capture[2][-1].children.append(data)

    # --- comments / posts --------------------------------------------------

    def _start_comment(self, a: dict) -> None:
        cid = strip_kind(a.get("thingid"))
        if not cid:
            return
        if cid == "deleted":  # some tombstones carry no real id
            self._tombstones += 1
            cid = f"deleted-{self._tombstones}"
        parent = strip_kind(a.get("parentid")) or (
            self._comments[-1]["id"] if self._comments else None
        )
        rec: dict = {
            "id": cid,
            "parent_id": parent,
            "depth": to_int(a.get("depth")) or 0,
            "author": a.get("author") or None,
            "score": to_int(a.get("score")),
            "created": iso_time(a.get("created")),
            "created_ts": epoch(a.get("created")),
            "body": "",
        }
        if a.get("postid"):
            rec["post_id"] = strip_kind(a["postid"])
        if "is-op" in a:
            rec["op"] = True
        if "is-mod-distinguished" in a:
            rec["distinguished"] = "moderator"
        elif "is-admin-distinguished" in a:
            rec["distinguished"] = "admin"
        if "is-stickied" in a or "stickied" in a:
            rec["stickied"] = True
        if "is-comment-deleted" in a:
            rec["deleted"] = True
        if "is-comment-removed" in a or "is-removed" in a:
            rec["removed"] = True
        if "is-author-deleted" in a:
            rec["author_deleted"] = True
        if "collapsed" in a:
            rec["collapsed"] = True
        if a.get("edited"):
            rec["edited"] = iso_time(a.get("edited")) or True
        self.page.comments.append(rec)
        self._comments.append(rec)

    def _start_post(self, a: dict) -> None:
        pid = strip_kind(a.get("id"))
        if not pid or not a.get("post-title"):
            return
        sub = a.get("subreddit-name") or (a.get("subreddit-prefixed-name") or "").removeprefix("r/")
        permalink = a.get("permalink") or ""
        rec: dict = {
            "id": pid,
            "subreddit": sub or None,
            "author": a.get("author") or None,
            "created": iso_time(a.get("created-timestamp")),
            "created_ts": epoch(a.get("created-timestamp")),
            "title": a.get("post-title"),
            "permalink": permalink,
            "post_type": a.get("post-type") or None,
            "score": to_int(a.get("score")),
            "upvote_ratio": to_float(a.get("upvote-ratio")),
            "num_comments": to_int(a.get("comment-count")),
        }
        link = a.get("content-href") or ""
        if link and f"/comments/{pid}" not in link and f"/gallery/{pid}" not in link:
            rec["link_url"] = link
        for attr, key in (
            ("nsfw", "nsfw"), ("is-nsfw", "nsfw"), ("spoiler", "spoiler"),
            ("is-spoiler", "spoiler"), ("is-locked", "locked"), ("locked", "locked"),
            ("is-stickied", "stickied"), ("stickied", "stickied"),
        ):
            if attr in a:
                rec[key] = True
        if a.get("more-posts-cursor"):
            rec["_cursor"] = a["more-posts-cursor"]
        self.page.posts.append(rec)
        self._post = rec

    def _target_for(self, el_id: str) -> dict | None:
        thing = el_id.split("-", 1)[0]
        cid = strip_kind(thing)
        if thing.startswith("t1_"):
            for rec in reversed(self._comments):
                if rec["id"] == cid:
                    return rec
            for rec in reversed(self.page.comments):
                if rec["id"] == cid:
                    return rec
        elif thing.startswith("t3_"):
            if self._post is not None and self._post["id"] == cid:
                return self._post
            for rec in reversed(self.page.posts):
                if rec["id"] == cid:
                    return rec
        return None

    # --- body capture --------------------------------------------------

    def _capture_start(self, tag: str, attrs: dict) -> None:
        target, key, stack, divs = self._capture
        if tag in _SKIP_TEXT:
            if tag not in _VOID:
                self._skip += 1
            return
        if self._skip:
            return
        el = _El(tag, attrs)
        stack[-1].children.append(el)
        if tag not in _VOID:
            stack.append(el)
        if tag == "div":
            divs += 1
        self._capture = (target, key, stack, divs)

    def _capture_end(self, tag: str) -> None:
        target, key, stack, divs = self._capture
        if tag in _SKIP_TEXT:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        # pop to the matching open element (tolerates unclosed inline tags)
        for i in range(len(stack) - 1, 0, -1):
            if stack[i].tag == tag:
                del stack[i:]
                break
        if tag == "div":
            divs -= 1
            if divs <= 0:
                target[key] = to_markdown(stack[0])
                self._capture = None
                return
        self._capture = (target, key, stack, divs)

    # --- partials / search -------------------------------------------------

    def _start_partial(self, a: dict) -> None:
        src = a.get("src", "")
        if "/more-comments/" in src:
            parent = self._comments[-1]["id"] if self._comments else None
            if "top-level" in src:
                parent = None
            m = re.search(r"comments-remaining=(\d+)", src)
            self._more = MoreLink(src=src, parent_id=parent, count=int(m.group(1)) if m else None)
            self._more_tag = "faceplate-partial"
            self.page.more.append(self._more)
        elif "/community-more-posts/" in src:
            self.page.feed_next = src
        elif "/search" in src and "cursor=" in src:
            self.page.search_next = src

    def _unit_tracking(self, raw: str) -> None:
        try:
            ctx = json.loads(raw)
        except ValueError:
            return
        post = ctx.get("post") or {}
        if not post.get("id"):
            return
        self._unit["post"] = post
        self._unit["author"] = (ctx.get("profile") or {}).get("name")
        self._unit["subreddit"] = (ctx.get("subreddit") or {}).get("name")

    def _finish_unit(self) -> None:
        u, self._unit = self._unit, None
        post = (u or {}).get("post")
        if not post:
            return
        nums = u.get("_numbers") or []
        pid = strip_kind(post.get("id"))
        rec: dict = {
            "id": pid,
            "subreddit": u.get("subreddit"),
            "author": u.get("author"),
            "created": iso_time(u.get("created")),
            "created_ts": epoch(u.get("created")),
            "title": post.get("title"),
            "permalink": u.get("permalink") or "",
            "score": nums[0] if len(nums) > 0 else None,
            "num_comments": nums[1] if len(nums) > 1 else None,
        }
        if post.get("nsfw"):
            rec["nsfw"] = True
        if post.get("spoiler"):
            rec["spoiler"] = True
        if u.get("thumbnail"):
            rec["thumbnail"] = u["thumbnail"]
        self.page.search.append(rec)
