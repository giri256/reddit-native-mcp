"""Posting as the linked account: create, edit, set options, delete, upload media.

Routes (see experiments/2026-10-08_logged-in-session-probe/NOTES.md):
- Classic API with the session cookie + X-Modhash: /api/submit (text, link,
  image, video), /api/submit_gallery_post.json, /api/editusertext, /api/del,
  /api/marknsfw|unmarknsfw, /api/spoiler|unspoiler, /api/sendreplies,
  /r/<sub>/api/selectflair, /api/info.json.
- Media: Reddit's own web app route, the shreddit GraphQL
  CreateMediaUploadLease (the classic /api/media/asset.json is 403 for
  cookie sessions), then a form upload to Reddit's S3 bucket. The S3 key is
  the media id that galleries and inline images reference.
- Inline images in a text body: markdown `![caption](file-or-url)` is uploaded
  and rewritten to `![img](<media id> "caption")`, then converted to Reddit's
  rich-text JSON by its own ConvertMarkdownToRTJSON operation.

Nothing here solves or skips a captcha: these routes ask for none on this
account (/api/needs_captcha.json = false). If Reddit asks for one, the
write fails with Reddit's error. Writes run once (no automatic retry), so a
timeout can't post twice; the error then says to check the profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .http import USER_AGENT, BlockedError, NotFound, RateLimited, RedditError, RedditWeb
from .parse import strip_kind

log = logging.getLogger(__name__)

GRAPHQL_PATH = "/svc/shreddit/graphql"
_COOKIE_PATH = "/svc/shreddit/community-hover-card/reddit"
# file type -> (CreateMediaUploadLease mimetype, content type, kind)
MEDIA_TYPES = {
    "png": ("PNG", "image/png", "image"),
    "jpeg": ("JPEG", "image/jpeg", "image"),
    "gif": ("GIF", "image/gif", "image"),
    "webp": ("WEBP", "image/webp", "image"),
    "mp4": ("MP4", "video/mp4", "video"),
    "mov": ("MOV", "video/quicktime", "video"),
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # Reddit's image limit
MAX_VIDEO_BYTES = 1024 * 1024 * 1024  # Reddit's video limit
GALLERY_MAX = 20
TITLE_MAX = 300
SUBMIT_WAIT_S = 60.0  # image/video posts: wait this long for Reddit to finish
_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)')
_MEDIA_ID = re.compile(r"^[a-z0-9]{10,16}$")


class PostError(RedditError):
    """Reddit rejected a write (its own error codes are in the message)."""


@dataclass
class Media:
    media_id: str
    url: str  # the uploaded file on Reddit's S3 bucket
    kind: str  # image | video
    content_type: str


# --- low-level calls -------------------------------------------------------


def _errors_text(errors: list) -> str:
    parts = []
    for e in errors:
        if isinstance(e, (list, tuple)):
            code, msg, field_ = (list(e) + [None, None, None])[:3]
            parts.append(f"{code}: {msg}" + (f" (field {field_})" if field_ else ""))
        elif isinstance(e, dict):
            parts.append(e.get("message") or json.dumps(e))
        else:
            parts.append(str(e))
    return "; ".join(parts)


async def _refused(web: RedditWeb, path: str, e: Exception) -> RedditError:
    """A 403 on a write: dead session (NotLoggedIn) or a real refusal."""
    await web.me(fresh=True)  # raises NotLoggedIn if the session is dead
    return PostError(f"Reddit refused {path} for u/{web.username} ({e})")


async def api_write(
    web: RedditWeb, path: str, *, data: dict | None = None, json_body: dict | None = None,
    retry: bool = False,
) -> dict:
    """POST to the classic API as the linked account. Raises PostError with
    Reddit's error codes when the JSON reply lists errors."""
    modhash = await web.modhash()
    try:
        resp = await web.request(
            "POST", path, data=data, json_body=json_body,
            headers={"X-Modhash": modhash, "Accept": "application/json"},
            retry=retry, follow=False,
        )
    except BlockedError as e:
        raise await _refused(web, path, e) from None
    except (NotFound, RateLimited):
        raise  # refused outright / never sent
    except RedditError as e:
        if retry:
            raise
        raise RedditError(
            f"{e}. It was sent once and not retried: check the profile before trying "
            "again, it may have gone through"
        ) from None
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        raise PostError(f"reddit.com answered {path} with non-JSON: {resp.text[:200]!r}") from None
    if isinstance(payload, dict):
        errors = (payload.get("json") or {}).get("errors") if isinstance(payload.get("json"), dict) else None
        if errors:
            raise PostError(f"Reddit rejected it: {_errors_text(errors)}")
        if payload.get("success") is False:  # old jQuery-style reply: no error codes
            raise PostError(f"Reddit rejected {path} (no reason given)")
    return payload


async def gql(web: RedditWeb, operation: str, variables: dict, *, retry: bool = True) -> dict:
    """One shreddit GraphQL operation as the linked account (the csrf_token
    cookie doubles as the request's csrf_token). Returns `data`."""
    await web.me()
    csrf = web.cookie("csrf_token")
    if not csrf:
        await web.request("GET", _COOKIE_PATH, partial=True)  # sets csrf_token
        csrf = web.cookie("csrf_token")
    if not csrf:
        raise PostError("reddit.com set no csrf_token cookie; run `reddit-native-mcp login` again")
    try:
        resp = await web.request(
            "POST", GRAPHQL_PATH,
            json_body={"operation": operation, "variables": variables, "csrf_token": csrf},
            headers={"Accept": "application/json"}, retry=retry, follow=False,
        )
    except BlockedError as e:
        raise await _refused(web, operation, e) from None
    try:
        payload = resp.json()
    except ValueError:
        raise PostError(f"Reddit's {operation} answered with non-JSON (HTTP {resp.status_code})") from None
    if payload.get("errors"):
        raise PostError(f"Reddit's {operation} failed: {_errors_text(payload['errors'])}")
    return payload.get("data") or {}


# --- media -------------------------------------------------------------------


def sniff_type(head: bytes, name: str = "") -> str | None:
    """File type from magic bytes, else the file name's extension."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "mov" if head[8:10] == b"qt" else "mp4"
    ext = Path(urlparse(name).path).suffix.lower().lstrip(".")
    return {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp",
            "mp4": "mp4", "mov": "mov"}.get(ext)


def _external_client(web: RedditWeb, timeout: float = 120.0) -> httpx.AsyncClient:
    """A client without Reddit's cookies (for S3 and media downloads)."""
    transport = getattr(web, "transport", None)
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=True, transport=transport,
    )


async def read_source(web: RedditWeb, source: str) -> tuple[bytes, str]:
    """Bytes and a file name for a local path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        async with _external_client(web, 60.0) as c:
            try:
                resp = await c.get(source)
            except httpx.HTTPError as e:
                raise PostError(f"could not download {source}: {type(e).__name__}: {e}") from None
        if resp.status_code != 200:
            raise PostError(f"could not download {source}: HTTP {resp.status_code}")
        name = Path(urlparse(str(resp.url)).path).name or "upload"
        ctype = (resp.headers.get("content-type") or "").split(";")[0]
        if "." not in name and ctype:
            name += mimetypes.guess_extension(ctype) or ""
        return resp.content, name
    path = Path(source).expanduser()
    if not path.is_file():
        raise PostError(f"no such file: {source}")
    size = path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise PostError(f"{source} is {size // 1048576} MB; Reddit's limit is 1 GB")
    return path.read_bytes(), path.name


async def upload_bytes(web: RedditWeb, content: bytes, name: str) -> Media:
    ftype = sniff_type(content[:16], name)
    if ftype is None:
        raise PostError(f"{name}: not a PNG, JPEG, GIF, WEBP, MP4 or MOV file")
    enum, ctype, kind = MEDIA_TYPES[ftype]
    limit = MAX_IMAGE_BYTES if kind == "image" and ftype != "gif" else MAX_VIDEO_BYTES
    if len(content) > limit:
        raise PostError(f"{name} is {len(content) // 1048576} MB; Reddit's limit is {limit // 1048576} MB")
    lease = (await gql(web, "CreateMediaUploadLease", {"input": {"mimetype": enum}})).get(
        "createMediaUploadLease"
    ) or {}
    up = lease.get("uploadLease") or {}
    media_id = lease.get("mediaId")
    if not media_id or not up.get("uploadLeaseUrl"):
        raise PostError(f"Reddit gave no upload lease: {_errors_text(lease.get('errors') or [lease])}")
    fields = {h["header"]: h["value"] for h in up.get("uploadLeaseHeaders") or []}
    async with _external_client(web) as c:
        try:
            resp = await c.post(up["uploadLeaseUrl"], data=fields, files={"file": (name, content, ctype)})
        except httpx.HTTPError as e:
            raise PostError(f"upload of {name} failed: {type(e).__name__}: {e}") from None
    if resp.status_code not in (200, 201, 204):
        raise PostError(f"upload of {name} failed: HTTP {resp.status_code} {resp.text[:200]}")
    m = re.search(r"<Location>(.*?)</Location>", resp.text)
    url = m.group(1) if m else f"{up['uploadLeaseUrl'].rstrip('/')}/{fields.get('key', media_id)}"
    return Media(media_id=media_id, url=url.replace("%2F", "/"), kind=kind, content_type=ctype)


async def upload(web: RedditWeb, source: str) -> Media:
    """Upload a local file or an http(s) URL to Reddit's media store."""
    content, name = await read_source(web, source)
    return await upload_bytes(web, content, name)


def video_frame(content: bytes) -> bytes | None:
    """First frame of a video as PNG, via ffmpeg if it is installed."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        out = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1", "-f", "image2pipe",
             "-vcodec", "png", "pipe:1"],
            input=content, capture_output=True, timeout=60, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout or None


# --- rich text (inline images) ---------------------------------------------


async def markdown_with_media(web: RedditWeb, body: str) -> tuple[str, list[Media]]:
    """Upload every `![caption](file-or-url)` in `body`; return the markdown
    pointing at the media ids, and the uploads. A target that already is a
    Reddit media id is kept."""
    uploads: list[Media] = []
    out, pos = [], 0
    for m in _MD_IMAGE.finditer(body):
        alt, target, title = m.group(1), m.group(2), m.group(3)
        if _MEDIA_ID.match(target):
            media_id = target
        else:
            media = await upload(web, target)
            uploads.append(media)
            media_id = media.media_id
        caption = (title if title is not None else alt).replace('"', "'")
        out.append(body[pos : m.start()])
        out.append(f'![{"gif" if target.lower().endswith(".gif") else "img"}]({media_id} "{caption}")')
        pos = m.end()
    out.append(body[pos:])
    return "".join(out), uploads


def has_media(body: str | None) -> bool:
    return bool(body and _MD_IMAGE.search(body))


async def to_richtext(web: RedditWeb, markdown: str) -> str:
    """Reddit's own markdown -> rich-text JSON conversion (as a JSON string)."""
    data = await gql(web, "ConvertMarkdownToRTJSON", {"markdown": markdown})
    value = data.get("convertedValue")
    for _ in range(3):  # Reddit double-encodes it
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    if not isinstance(value, dict) or "document" not in value:
        raise PostError("Reddit could not convert the body to rich text")
    return json.dumps(value, separators=(",", ":"))


async def body_fields(web: RedditWeb, body: str | None) -> tuple[dict, int]:
    """Submit/edit fields for a body: `text` (markdown) or, with inline
    images, `richtext_json`. Returns (fields, images uploaded)."""
    if not body:
        return {}, 0
    if not has_media(body):
        return {"text": body}, 0
    md, uploads = await markdown_with_media(web, body)
    return {"richtext_json": await to_richtext(web, md)}, len(uploads)


# --- reading posts as the account -------------------------------------------


async def post_info(web: RedditWeb, post_id: str) -> dict:
    """/api/info data for a post (fresh, not cached)."""
    payload = await web.api_json("/api/info.json", params={"id": f"t3_{post_id}"}, fresh=True)
    children = ((payload or {}).get("data") or {}).get("children") or []
    if not children:
        raise NotFound(f"reddit.com shows no post {post_id} to this account (deleted or mistyped?)")
    return children[0]["data"]


async def own_post(web: RedditWeb, post_id: str) -> dict:
    """The post's data, if the linked account wrote it."""
    info = await post_info(web, post_id)
    me = (await web.me())["name"]
    if (info.get("author") or "").lower() != me.lower():
        raise PostError(
            f"Post {post_id} is by u/{info.get('author')}, not the linked account u/{me}; "
            "only your own posts can be changed"
        )
    return info


def post_record(info: dict) -> dict:
    """A short record of a post as Reddit now shows it."""
    rec = {
        "id": info.get("id"),
        "url": f"https://www.reddit.com{info.get('permalink')}" if info.get("permalink") else None,
        "subreddit": info.get("subreddit"),
        "title": info.get("title"),
        "author": info.get("author"),
        "nsfw": bool(info.get("over_18")),
        "spoiler": bool(info.get("spoiler")),
        "send_replies": info.get("send_replies"),
        "flair": info.get("link_flair_text"),
        "flair_id": info.get("link_flair_template_id"),
    }
    if info.get("selftext"):
        rec["body"] = info["selftext"]
    if info.get("is_gallery"):
        rec["gallery_items"] = len(((info.get("gallery_data") or {}).get("items")) or [])
    if not info.get("is_self") and info.get("url") and "/comments/" not in info["url"]:
        rec["link_url"] = info["url"]
    if info.get("removed_by_category"):
        rec["removed_by"] = info["removed_by_category"]
    return {k: v for k, v in rec.items() if v is not None}


# --- creating posts ---------------------------------------------------------


def target_sr(target: str, username: str) -> tuple[str, bool]:
    """('sr' value, is_profile) for 'r/sub', 'sub', 'u/me', 'profile' or 'me'."""
    t = target.strip().strip("/")
    low = t.lower()
    if low in ("me", "profile", "my profile", "u/me", "user/me", f"u/{username.lower()}",
               f"user/{username.lower()}", f"u_{username.lower()}"):
        return f"u_{username}", True
    if low.startswith(("u/", "user/", "u_")):
        raise PostError("Posting to another user's profile is not possible; use 'profile' for your own")
    for prefix in ("r/", "/r/"):
        if low.startswith(prefix):
            t = t[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", t):
        raise PostError(f"Not a subreddit name: {target!r}")
    return t, False


async def _wait_for_post(web: RedditWeb, websocket_url: str | None, title: str, since: float) -> str | None:
    """URL of an image/video post once Reddit has processed it. Reddit's
    reply names a websocket that reports it; else look at the profile's
    newest posts."""
    if websocket_url:
        try:
            url = await asyncio.wait_for(_websocket_result(websocket_url), SUBMIT_WAIT_S)
            if url:
                return url
        except PostError:
            raise
        except Exception as e:  # noqa: BLE001 - fall back to polling
            log.info("websocket wait failed: %s", e)
    me = (await web.me())["name"]
    for attempt in range(6):
        payload = await web.api_json(
            f"/user/{me}/submitted.json", params={"sort": "new", "limit": "10"}, fresh=True
        )
        for child in ((payload or {}).get("data") or {}).get("children") or []:
            d = child.get("data") or {}
            if d.get("title") == title and (d.get("created_utc") or 0) >= since - 120:
                return f"https://www.reddit.com{d.get('permalink')}"
        await asyncio.sleep(2 + 2 * attempt)
    return None


async def _websocket_result(url: str) -> str | None:
    import websockets  # installed with the package (also used by `login`)

    async with websockets.connect(url, user_agent_header=USER_AGENT, open_timeout=10) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "success":
                return (msg.get("payload") or {}).get("redirect")
            if msg.get("type") == "failed":
                raise PostError(f"Reddit could not process the media post: {msg.get('payload')}")
    return None


def _post_id_from_url(url: str | None) -> str | None:
    m = re.search(r"/comments/([a-z0-9]+)", url or "")
    return m.group(1) if m else None


async def create_post(
    web: RedditWeb,
    *,
    target: str,
    title: str,
    kind: str,
    body: str | None = None,
    url: str | None = None,
    media: list[dict] | None = None,
    video_poster: str | None = None,
    nsfw: bool = False,
    spoiler: bool = False,
    send_replies: bool = True,
    flair_id: str | None = None,
    flair_text: str | None = None,
    allow_repost: bool = False,
) -> dict:
    """Publish a post as the linked account. kind: text | link | image |
    gallery | video. `media` items: {"source": path-or-url, "caption",
    "outbound_url"} (captions/links only show on galleries)."""
    me = (await web.me())["name"]
    title = (title or "").strip()
    if not title:
        raise PostError("title is empty")
    if len(title) > TITLE_MAX:
        raise PostError(f"title is {len(title)} characters; Reddit's limit is {TITLE_MAX}")
    sr, profile = target_sr(target, me)
    media = list(media or [])
    if kind in ("image", "video") and len(media) != 1:
        raise PostError(f"a {kind} post takes exactly one media item (use kind=gallery for several images)")
    if kind == "gallery" and not 2 <= len(media) <= GALLERY_MAX:
        raise PostError(f"a gallery takes 2 to {GALLERY_MAX} images")
    if kind == "link" and not (url or "").startswith(("http://", "https://")):
        raise PostError("a link post needs url=http(s)://...")
    if kind not in ("text", "link", "image", "gallery", "video"):
        raise PostError(f"unknown kind {kind!r}")
    if media and kind in ("text", "link"):
        raise PostError(
            f"media goes with kind=image/gallery/video; for images inside a {kind} body use "
            "markdown ![caption](path-or-url)"
        )

    common: dict = {
        "api_type": "json",
        "sr": sr,
        "title": title,
        "nsfw": "true" if nsfw else "false",
        "spoiler": "true" if spoiler else "false",
        "sendreplies": "true" if send_replies else "false",
        "validate_on_submit": "true",
    }
    if flair_id:
        common["flair_id"] = flair_id
    if flair_text:
        common["flair_text"] = flair_text
    started = time.time()
    uploaded = 0
    out: dict = {"kind": kind, "subreddit": sr, "profile_post": profile, "account": me}

    if kind == "gallery":
        items = []
        for item in media:
            m = await upload(web, item["source"])
            if m.kind != "image":
                raise PostError(f"{item['source']}: galleries take images only")
            uploaded += 1
            items.append({
                "media_id": m.media_id,
                "caption": (item.get("caption") or "")[:180],
                "outbound_url": item.get("outbound_url") or "",
            })
        payload = {
            **{k: v for k, v in common.items() if k not in ("nsfw", "spoiler", "sendreplies", "validate_on_submit")},
            "nsfw": nsfw, "spoiler": spoiler, "sendreplies": send_replies,
            "validate_on_submit": True, "show_error_list": True, "items": items,
        }
        if body:
            fields, n = await body_fields(web, body)
            uploaded += n
            payload.update(fields)
        resp = await api_write(web, "/api/submit_gallery_post.json", json_body=payload)
        data = (resp.get("json") or {}).get("data") or {}
        post_url = data.get("url")
        post_id = strip_kind(data.get("id")) or _post_id_from_url(post_url)
    else:
        form = dict(common)
        if kind == "text":
            form["kind"] = "self"
            fields, n = await body_fields(web, body)
            uploaded += n
            form.update(fields or {"text": ""})
        elif kind == "link":
            form.update({"kind": "link", "url": url, "resubmit": "true" if allow_repost else "false"})
            if body:
                fields, n = await body_fields(web, body)
                uploaded += n
                form.update(fields)
        else:
            content, name = await read_source(web, media[0]["source"])
            m = await upload_bytes(web, content, name)
            uploaded += 1
            if kind == "image" and m.kind != "image":
                raise PostError(f"{name} is a video; use kind=video")
            if kind == "video" and m.kind != "video":
                raise PostError(f"{name} is an image; use kind=image")
            form.update({"kind": kind, "url": m.url})
            if kind == "video":
                if video_poster:
                    poster = await upload(web, video_poster)
                else:
                    frame = video_frame(content)
                    if frame is None:
                        raise PostError(
                            "a video post needs a thumbnail: pass video_poster=<image path or URL> "
                            "(or install ffmpeg to take the first frame)"
                        )
                    poster = await upload_bytes(web, frame, "poster.png")
                uploaded += 1
                form["video_poster_url"] = poster.url
            if body:
                fields, n = await body_fields(web, body)
                uploaded += n
                form.update(fields)
        resp = await api_write(web, "/api/submit", data=form)
        data = (resp.get("json") or {}).get("data") or {}
        post_url = data.get("url")
        post_id = strip_kind(data.get("name") or data.get("id")) or _post_id_from_url(post_url)
        if kind in ("image", "video") and not post_id:
            post_url = await _wait_for_post(web, data.get("websocket_url"), title, started)
            post_id = _post_id_from_url(post_url)
            if not post_id:
                out["note"] = (
                    "Reddit accepted the post but had not finished processing the media when this "
                    "call stopped waiting; it should appear on the profile shortly"
                )
    out.update({"id": post_id, "url": post_url, "uploaded_media": uploaded})
    if post_id:
        out.update(await _flags_as_posted(web, post_id, nsfw=nsfw, spoiler=spoiler))
    return {k: v for k, v in out.items() if v is not None}


async def _flags_as_posted(web: RedditWeb, post_id: str, *, nsfw: bool, spoiler: bool) -> dict:
    """The marks Reddit actually gave the new post: an NSFW profile or
    community marks every post NSFW, whatever the request said."""
    try:
        info = await post_info(web, post_id)
    except RedditError as e:  # the post exists; only the read-back failed
        log.info("read-back of %s failed: %s", post_id, e)
        return {}
    out = {"nsfw": bool(info.get("over_18")), "spoiler": bool(info.get("spoiler"))}
    if out["nsfw"] and not nsfw:
        out["note"] = (
            "Reddit marked this post NSFW although it was not requested: the profile or "
            "community is marked NSFW (18+), so all its posts are"
        )
    elif out["spoiler"] != spoiler or out["nsfw"] != nsfw:
        out["note"] = "Reddit did not apply the requested NSFW/spoiler marks; see nsfw/spoiler"
    return out


# --- changing posts ------------------------------------------------------------


async def edit_post(web: RedditWeb, post_id: str, body: str) -> dict:
    """Replace the body of the account's own post (markdown; images via
    ![caption](path-or-url)). Reddit never allows editing titles."""
    info = await own_post(web, post_id)
    fields, uploaded = await body_fields(web, body)
    if not fields:
        fields = {"text": ""}
    await api_write(
        web, "/api/editusertext", data={"api_type": "json", "thing_id": f"t3_{post_id}", **fields}
    )
    after = await post_info(web, post_id)
    rec = post_record(after)
    rec["uploaded_media"] = uploaded
    if info.get("selftext") == after.get("selftext") and body.strip() and not uploaded:
        rec["note"] = "Reddit kept the old body; check that this post type has an editable body"
    return rec


async def set_post_options(
    web: RedditWeb,
    post_id: str,
    *,
    nsfw: bool | None = None,
    spoiler: bool | None = None,
    send_replies: bool | None = None,
    flair_id: str | None = None,
    flair_text: str | None = None,
    clear_flair: bool = False,
) -> dict:
    info = await own_post(web, post_id)
    fullname = f"t3_{post_id}"
    done = []
    if nsfw is not None and bool(info.get("over_18")) != nsfw:
        await api_write(web, "/api/marknsfw" if nsfw else "/api/unmarknsfw", data={"id": fullname})
        done.append("nsfw")
    if spoiler is not None and bool(info.get("spoiler")) != spoiler:
        await api_write(web, "/api/spoiler" if spoiler else "/api/unspoiler", data={"id": fullname})
        done.append("spoiler")
    if send_replies is not None and info.get("send_replies") != send_replies:
        await api_write(
            web, "/api/sendreplies", data={"id": fullname, "state": "true" if send_replies else "false"}
        )
        done.append("send_replies")
    if flair_id or flair_text or clear_flair:
        sub = info.get("subreddit")
        form = {"api_type": "json", "link": fullname}
        if not clear_flair:
            if flair_id:
                form["flair_template_id"] = flair_id
            if flair_text:
                form["text"] = flair_text
        await api_write(web, f"/r/{sub}/api/selectflair", data=form)
        done.append("flair")
    rec = post_record(await post_info(web, post_id))
    rec["changed"] = done
    return rec


async def delete_post(web: RedditWeb, post_id: str) -> dict:
    info = await own_post(web, post_id)
    await api_write(web, "/api/del", data={"id": f"t3_{post_id}"})
    return {"deleted": True, "id": post_id, "title": info.get("title"), "subreddit": info.get("subreddit")}


# --- what a subreddit accepts ---------------------------------------------------


async def post_requirements(web: RedditWeb, sub: str) -> dict:
    """What r/<sub> accepts from the linked account: post types, flair,
    rules and the posting guidelines."""
    me = (await web.me())["name"]
    sr, profile = target_sr(sub, me)
    if profile:
        return {
            "subreddit": sr, "profile": True,
            "post_types": ["text", "link", "image", "gallery", "video"],
            "note": "Your own profile accepts every post type; no flair or rules",
        }
    about = (await web.api_json(f"/r/{sr}/about.json")).get("data") or {}
    if not about.get("display_name"):
        raise NotFound(f"r/{sr} does not exist or is not visible to u/{me}")
    types = {"any": ["text", "link"], "link": ["link"], "self": ["text"]}.get(
        about.get("submission_type") or "any", ["text", "link"]
    )
    if about.get("submission_type") in ("any", "link"):
        if about.get("allow_images"):
            types.append("image")
        if about.get("allow_galleries"):
            types.append("gallery")
        if about.get("allow_videos"):
            types.append("video")
    rec: dict = {
        "subreddit": about.get("display_name"),
        "type": about.get("subreddit_type"),
        "post_types": types,
        "nsfw_community": bool(about.get("over18")),
        "spoilers_enabled": about.get("spoilers_enabled"),
        "can_post": not (about.get("user_is_banned") or (
            about.get("subreddit_type") == "restricted" and not about.get("user_is_contributor")
        )),
        "inline_media_in_text": bool(about.get("allow_images")),
        "link_flair_enabled": about.get("link_flair_enabled"),
        "can_set_own_flair": about.get("can_assign_link_flair"),
    }
    if about.get("user_is_banned"):
        rec["banned"] = True
    if about.get("submit_text"):
        rec["posting_guidelines"] = about["submit_text"][:3000]
    try:
        rules = (await web.api_json(f"/r/{sr}/about/rules.json")).get("rules") or []
        rec["rules"] = [
            {"name": r.get("short_name"), "applies_to": r.get("kind"), "description": (r.get("description") or "")[:500]}
            for r in rules
        ]
    except RedditError as e:
        rec["rules_error"] = str(e)
    if not about.get("link_flair_enabled"):
        rec["flairs"] = []
        return rec
    try:
        flairs = await web.api_json(f"/r/{sr}/api/link_flair_v2.json")
        rec["flairs"] = [
            {"id": f.get("id"), "text": f.get("text"), "text_editable": f.get("text_editable"),
             "mod_only": f.get("mod_only")}
            for f in (flairs if isinstance(flairs, list) else [])
        ]
    except RedditError:  # 403/404: Reddit lists no flair this account may pick
        rec["flairs"] = []
        rec["flairs_note"] = (
            "this community does not let posters pick flair (moderators set it)"
            if not about.get("can_assign_link_flair") else "Reddit did not list its flair"
        )
    return rec
