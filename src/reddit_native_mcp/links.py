"""Reddit link parsing: post/comment permalinks, short links, share links.

Share links (`reddit.com/r/<sub>/s/<code>`, what the Reddit app's Share button
copies) carry no post id; reddit.com answers them with a 301 whose Location
is the full permalink, so they are resolved with one request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .http import RedditError, RedditWeb

# /comments/<post>[/<slug>[/<comment>]]: <slug> is the title slug, "_", or
# the literal "comment" of new-style links (/comments/<post>/comment/<id>/).
_COMMENTS_RE = re.compile(
    r"/comments/([a-z0-9]{2,12})(?:/[^/?#\s]*(?:/([a-z0-9]{2,12}))?)?", re.I
)
_GALLERY_RE = re.compile(r"reddit\.com/gallery/([a-z0-9]{2,12})", re.I)
_SHORT_RE = re.compile(r"(?<![\w.])redd\.it/([a-z0-9]{2,12})", re.I)
_SHARE_RE = re.compile(r"(?:reddit\.com|^)(/(?:r|u|user)/[\w-]+/s/[A-Za-z0-9]+)", re.I)
_FULLNAME_RE = re.compile(r"^(t1|t3)_([a-z0-9]{2,12})$", re.I)
_BARE_ID_RE = re.compile(r"^[a-z0-9]{5,10}$", re.I)
_COMMENT_ID_RE = re.compile(r"^[a-z0-9]{2,12}$")


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class RedditRef:
    """What a link points at: post_id (+ optional comment_id), comment_id
    alone (t1_ fullname), or share_path (needs `resolve_share_path`)."""

    post_id: str | None = None
    comment_id: str | None = None
    share_path: str | None = None


def parse_reddit_ref(text: str) -> RedditRef:
    s = text.strip().strip("<>\"'")
    m = _SHARE_RE.search(s)
    if m:
        return RedditRef(share_path=m.group(1))
    m = _COMMENTS_RE.search(s)
    if m and ("reddit" in s.lower() or s.startswith("/")):
        comment = m.group(2)
        return RedditRef(
            post_id=m.group(1).lower(), comment_id=comment.lower() if comment else None
        )
    for rx in (_SHORT_RE, _GALLERY_RE):
        m = rx.search(s)
        if m:
            return RedditRef(post_id=m.group(1).lower())
    m = _FULLNAME_RE.match(s)
    if m:
        kind, thing = m.group(1).lower(), m.group(2).lower()
        return RedditRef(post_id=thing) if kind == "t3" else RedditRef(comment_id=thing)
    if _BARE_ID_RE.match(s):
        return RedditRef(post_id=s.lower())
    if "v.redd.it" in s.lower():
        raise LinkError(f"{text!r} is a video file link, not a post link; pass the post URL")
    raise LinkError(
        f"Not a Reddit post/comment URL, share link or id: {text!r}. Accepted: "
        "reddit.com/r/<sub>/comments/<id>/..., redd.it/<id>, reddit.com/r/<sub>/s/<code>, "
        "a bare post id like 1wz9oim, or t3_/t1_ fullnames"
    )


def clean_comment_id(value: str) -> str:
    """A comment id from an id, t1_ fullname or comment permalink."""
    value = value.strip()
    if "/" in value:
        ref = parse_reddit_ref(value)
        if not ref.comment_id:
            raise LinkError(f"No comment id in {value!r}")
        return ref.comment_id
    cid = value.lower().removeprefix("t1_")
    if not _COMMENT_ID_RE.match(cid):
        raise LinkError(f"Not a comment id: {value!r} (expected e.g. pe9tov3 or t1_pe9tov3)")
    return cid


def clean_name(name: str, prefixes: tuple[str, ...]) -> str:
    name = name.strip().strip("/")
    for p in prefixes:
        if name.lower().startswith(p):
            return name[len(p) :]
    return name


async def resolve_share_path(share_path: str, web: RedditWeb) -> RedditRef:
    """Turn `/r/<sub>/s/<code>` into a post/comment ref via reddit.com's 301."""
    try:
        location = await web.redirect_target(share_path)
    except RedditError as e:
        raise LinkError(f"Could not resolve share link {share_path}: {e}") from None
    if not location or "/comments/" not in location:
        raise LinkError(
            f"Could not resolve share link {share_path}: reddit.com did not redirect "
            "to a post. Open it in a browser and pass the resulting /comments/ URL"
        )
    m = _COMMENTS_RE.search(location)
    if not m:
        raise LinkError(f"Share link resolved to a non-post URL: {location}")
    comment = m.group(2)
    return RedditRef(post_id=m.group(1).lower(), comment_id=comment.lower() if comment else None)
