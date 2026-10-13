"""HTTP client for reddit.com's own web app (anonymous, or as the linked account).

Routes used (see experiments/2026-10-07_native-shreddit-access/NOTES.md):
- `/svc/shreddit/...` partials: comment trees, more-comments (POST cursor),
  subreddit feeds, subreddit search. No cookies or CSRF needed.
- `/comments/<id>/` and comment permalinks: full pages; they need the session
  cookies any svc response sets, else Reddit serves a JS check page.

Client identity: an honest User-Agent naming this tool. Python's default TLS
cipher list is refused at Reddit's edge (403 whatever the UA), so the SSL
context uses OpenSSL's own defaults, the same ones curl sends. Nothing here
imitates a browser or solves Reddit's bot check; if Reddit serves that check
the request fails with BlockedError.

Rate limit: Reddit reports a budget in x-ratelimit-remaining /
x-ratelimit-reset. The client tracks it across calls and refuses to start
requests that would exhaust it (RateLimited), keeping a reserve so one big
thread can't lock out the next tool call. Anonymous: ~200 requests per
window. Logged in (a saved auth.Session): two separate budgets, ~600 for
the web app and ~100 for the classic JSON API (`*.json`, `/api/...`).

Logged in, the client sends the account's reddit.com cookies, and classic
API writes carry the account's modhash (from /api/me.json) as X-Modhash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import ssl
import time
from collections import OrderedDict

import anyio
import httpx

from . import __version__
from .auth import Session, load_session, save_session, session_path

log = logging.getLogger(__name__)

BASE_URL = "https://www.reddit.com"
USER_AGENT = f"reddit-native-mcp/{__version__} (personal agent tool)"
# Partials without the app shell: 40-60% smaller than text/html.
PARTIAL_ACCEPT = "text/vnd.reddit.partial+html, text/html;q=0.9"
MAX_CONCURRENCY = 4
MAX_ATTEMPTS = 3
REQUEST_DEADLINE_S = 30.0  # one request, all retries included
RATE_RESERVE = 8  # requests left untouched in each rate-limit window
# Successful svc/page results are reused for a few minutes: an agent that caps
# a thread and then drills into it, or finds posts and then opens one, would
# otherwise spend Reddit's rate budget twice on the same pages.
CACHE_TTL_S = 180.0
CACHE_MAX_CHARS = 40_000_000
# Any svc partial sets the anonymous session cookies; this one is cheap.
_COOKIE_PATH = "/svc/shreddit/community-hover-card/reddit"
# Rate-limit buckets: the classic JSON API is metered apart from the web app
API, WEB = "api", "web"


_sleep = asyncio.sleep  # module hook: tests patch this, never asyncio itself


class RedditError(RuntimeError):
    """Reddit could not serve the request (after retries)."""


class NotFound(RedditError):
    pass


class BlockedError(RedditError):
    """Reddit refused this client (403 or its JS bot-check page)."""


class RateLimited(RedditError):
    def __init__(self, reset_s: float, who: str = "anonymous"):
        self.reset_s = reset_s
        super().__init__(f"Reddit's {who} rate limit is used up; it resets in ~{int(reset_s) + 1}s")


class Forbidden(RedditError):
    """The linked account may not see this (private/quarantined/banned)."""


class NotLoggedIn(RedditError):
    """No working account session (none saved, or Reddit no longer accepts it)."""


def bucket_of(path: str) -> str:
    p = path.split("?", 1)[0]
    if p.endswith(".json") or p.startswith("/api/") or "/api/" in p:
        return API
    return WEB


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")  # OpenSSL defaults, as curl uses
    return ctx


def is_challenge(text: str) -> bool:
    """Reddit's JS proof-of-work interstitial (~8KB, a form with `solution`)."""
    return len(text) < 20_000 and 'name="solution"' in text


class RedditWeb:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline: float = REQUEST_DEADLINE_S,
        concurrency: int = MAX_CONCURRENCY,
        session: Session | None = None,
        persist_session: bool = False,
    ) -> None:
        self._deadline = deadline
        self.transport = transport  # tests: write.py's S3/download client reuses it
        self.session = session
        self._persist = persist_session  # write rotated cookies back to session.json
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            transport=transport,
            **({} if transport else {"verify": tls_context(), "http2": True}),
        )
        if session:
            for c in session.cookies:
                self._client.cookies.set(
                    c["name"], c["value"], domain=c.get("domain") or ".reddit.com", path=c.get("path") or "/"
                )
        self._sem = asyncio.Semaphore(concurrency)
        self._cookie_lock = asyncio.Lock()
        self._me_lock = asyncio.Lock()
        self.me_data: dict | None = None  # /api/me.json data, once fetched
        self.session_stamp: tuple | None = None  # session.json (mtime, size) it was loaded from
        self.session_error: str | None = None  # why the saved session stopped working
        self._rate: dict[str, tuple[float, float]] = {}  # bucket -> (remaining, reset_at)
        self.requests_made = 0
        self.cache_hits = 0
        self._cache: OrderedDict[str, tuple[float, tuple[str, str]]] = OrderedDict()
        self._cache_chars = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def logged_in(self) -> bool:
        """A session is loaded and Reddit has not rejected it."""
        return self.session is not None and self.session_error is None

    # --- rate budget -----------------------------------------------------

    def _bucket(self, path: str) -> str:
        # anonymous requests share one budget
        return bucket_of(path) if self.session is not None else WEB

    def budget_left(self, bucket: str = WEB) -> float | None:
        """Requests left in the current window, or None if unknown."""
        if self.session is None:
            bucket = WEB
        rate = self._rate.get(bucket)
        if rate is None or time.monotonic() >= rate[1]:
            return None
        return rate[0]

    def budget_reset_s(self, bucket: str = WEB) -> float | None:
        rate = self._rate.get(bucket if self.session is not None else WEB)
        return max(0.0, rate[1] - time.monotonic()) if rate else None

    def _note_rate(self, resp: httpx.Response, bucket: str) -> None:
        try:
            remaining = float(resp.headers["x-ratelimit-remaining"])
            reset = float(resp.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return
        self._rate[bucket] = (remaining, time.monotonic() + reset)

    def _check_budget(self, bucket: str) -> None:
        left = self.budget_left(bucket)
        if left is not None and left <= RATE_RESERVE:
            raise RateLimited(self._rate[bucket][1] - time.monotonic(), self._who(bucket))

    def _who(self, bucket: str) -> str:
        if self.session is None:
            return "anonymous"
        return "account (JSON API)" if bucket == API else "account"

    # --- requests --------------------------------------------------------

    async def ensure_cookies(self) -> None:
        if self._has_cookie("loid"):
            return
        async with self._cookie_lock:
            if not self._has_cookie("loid"):
                await self.request("GET", _COOKIE_PATH, partial=True)

    def _has_cookie(self, name: str) -> bool:
        return any(c.name == name for c in self._client.cookies.jar)

    def cookie(self, name: str) -> str | None:
        for c in self._client.cookies.jar:
            if c.name == name:
                return c.value
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
        partial: bool = False,
        follow: bool = True,
        headers: dict | None = None,
        retry: bool = True,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        """One request with retries for transient failures (`retry=False`:
        one attempt, for writes that must not run twice). Follows same-site
        redirects when `follow`; returns the final response. Raises
        NotFound, BlockedError, RateLimited or RedditError, except for
        statuses in `ok_statuses`, which are returned."""
        hdrs = {"Accept": PARTIAL_ACCEPT} if partial else {}
        hdrs.update(headers or {})
        try:
            with anyio.fail_after(self._deadline):
                return await self._attempts(
                    method, path, params, data, json_body, hdrs, follow, retry, ok_statuses
                )
        except TimeoutError:
            raise RedditError(
                f"reddit.com did not answer {path} within {self._deadline:.0f}s"
            ) from None

    async def _attempts(self, method, path, params, data, json_body, headers, follow, retry, ok_statuses):
        last = ""
        bucket = self._bucket(path)
        for attempt in range(MAX_ATTEMPTS if retry else 1):
            if attempt:
                await _sleep(1.5 * 2 ** (attempt - 1) + random.uniform(0, 0.5))
            self._check_budget(bucket)
            try:
                async with self._sem:
                    self.requests_made += 1
                    resp = await self._client.request(
                        method, path, params=params, data=data, json=json_body, headers=headers
                    )
                    hops = 0
                    while follow and resp.is_redirect and hops < 3:
                        loc = resp.headers.get("location", "")
                        if not _same_site(loc):
                            break
                        hops += 1
                        self.requests_made += 1
                        resp = await self._client.get(loc, headers=headers)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                log.info("reddit %s %s failed: %s", method, path, last)
                continue
            self._note_rate(resp, bucket)
            status = resp.status_code
            if status in ok_statuses:
                return resp
            if status == 429:
                wait = float(resp.headers.get("retry-after") or 0) or (self.budget_reset_s(bucket) or 0)
                if wait > 10 or not retry:
                    raise RateLimited(wait, self._who(bucket))
                await _sleep(max(wait, 1.0))
                last = "HTTP 429"
                continue
            if status == 404:
                raise NotFound(f"reddit.com has no {path}")
            if status == 403 or (status == 200 and is_challenge(resp.text)):
                raise BlockedError(
                    f"reddit.com refused {path} (HTTP {status}"
                    + (", bot-check page" if is_challenge(resp.text) else "")
                    + ")"
                )
            if status >= 500 or status in (408, 425):
                last = f"HTTP {status}"
                continue
            if status >= 400:
                raise RedditError(f"reddit.com answered HTTP {status} for {path}")
            return resp
        if not retry:
            raise RedditError(f"reddit.com failed for {path} ({last})")
        raise RedditError(f"reddit.com failed for {path} after {MAX_ATTEMPTS} tries ({last})")

    # --- cached text results -----------------------------------------------

    def _cache_get(self, key: str) -> tuple[str, str] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] > CACHE_TTL_S:
            self._drop(key)
            return None
        self._cache.move_to_end(key)
        self.cache_hits += 1
        return hit[1]

    def _drop(self, key: str) -> None:
        _, (url, text) = self._cache.pop(key)
        self._cache_chars -= len(text)

    def _cache_put(self, key: str, value: tuple[str, str]) -> None:
        if key in self._cache:
            self._drop(key)
        self._cache[key] = (time.monotonic(), value)
        self._cache_chars += len(value[1])
        while self._cache_chars > CACHE_MAX_CHARS and self._cache:
            self._drop(next(iter(self._cache)))

    async def svc(
        self, path: str, *, params: dict | None = None, data: dict | None = None, fresh: bool = False
    ) -> str:
        """A /svc/shreddit partial (GET, or POST when `data`)."""
        key = json.dumps(["svc", path, params, data], sort_keys=True)
        hit = None if fresh else self._cache_get(key)
        if hit is not None:
            return hit[1]
        resp = await self.request(
            "POST" if data is not None else "GET", path, params=params, data=data, partial=True
        )
        self._cache_put(key, (str(resp.url), resp.text))
        return resp.text

    async def page(
        self, path: str, *, params: dict | None = None, fresh: bool = False
    ) -> tuple[str, str]:
        """A full page (needs session cookies). Returns (final_url, html)."""
        key = json.dumps(["page", path, params], sort_keys=True)
        hit = None if fresh else self._cache_get(key)
        if hit is not None:
            return hit
        await self.ensure_cookies()
        resp = await self.request("GET", path, params=params)
        self._cache_put(key, (str(resp.url), resp.text))
        return str(resp.url), resp.text

    async def redirect_target(self, path: str) -> str | None:
        """Location of a redirect at `path` (share links), without following."""
        resp = await self.request("GET", path, follow=False)
        return resp.headers.get("location") if resp.is_redirect else None

    # --- the linked account ------------------------------------------------

    async def api_json(self, path: str, *, params: dict | None = None, fresh: bool = False):
        """GET a classic JSON API path (logged in only), cached like svc().
        A 403 here means Reddit no longer accepts the session."""
        self.require_login()
        params = {"raw_json": "1", **(params or {})}
        key = json.dumps(["json", path, params], sort_keys=True)
        hit = None if fresh else self._cache_get(key)
        if hit is not None:
            return json.loads(hit[1])
        try:
            resp = await self.request("GET", path, params=params)
        except BlockedError as e:
            # a dead session, or a community this account may not see?
            await self.me(fresh=True)  # raises NotLoggedIn for the former
            raise Forbidden(
                f"reddit.com refused {path} for u/{self.username} ({e}): a private, "
                "quarantined or banned community, or content this account can't see"
            ) from None
        try:
            data = resp.json()
        except ValueError:
            raise RedditError(f"reddit.com answered {path} with non-JSON") from None
        self._cache_put(key, (str(resp.url), resp.text))
        return data

    def require_login(self) -> None:
        if self.session is None:
            raise NotLoggedIn("No Reddit account is linked")
        if self.session_error:
            raise NotLoggedIn(self.session_error)

    def _session_rejected(self, e: Exception) -> NotLoggedIn:
        self.session_error = (
            f"Reddit no longer accepts the saved login for u/{self.username or '?'} ({e}); "
            "log in again"
        )
        return NotLoggedIn(self.session_error)

    @property
    def username(self) -> str | None:
        if self.me_data:
            return self.me_data.get("name")
        return self.session.username if self.session else None

    async def me(self, fresh: bool = False) -> dict:
        """/api/me.json data for the linked account (name, karma, modhash...).
        Raises NotLoggedIn if there is no session or Reddit rejects it."""
        self.require_login()
        async with self._me_lock:
            if self.me_data is not None and not fresh:
                return self.me_data
            try:
                resp = await self.request("GET", "/api/me.json", params={"raw_json": "1"})
                data = (resp.json() or {}).get("data") or {}
            except BlockedError as e:
                raise self._session_rejected(e) from None
            except ValueError:
                data = {}
            if not data.get("name"):
                raise self._session_rejected(RuntimeError("/api/me.json shows no logged-in user"))
            self.me_data = data
            self._persist_cookies()
            return data

    def _persist_cookies(self) -> None:
        """Write refreshed cookies (Reddit rotates token_v2 etc.) back to the
        saved session, so a restart starts from the newest ones."""
        if self.session is None or not self._persist:
            return
        old = {c["name"]: c for c in self.session.cookies}
        fresh = {}
        for c in self._client.cookies.jar:
            if c.domain.lstrip(".") == "reddit.com" or c.domain.endswith(".reddit.com"):
                prev = old.get(c.name) or {}
                # cookies loaded from session.json carry no expiry in the jar
                expires = c.expires or (prev.get("expires") if prev.get("value") == c.value else None)
                fresh[c.name] = {
                    "name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path or "/", "expires": expires or -1,
                }
        if not fresh.get("reddit_session") or all(
            old.get(n, {}).get("value") == c["value"] for n, c in fresh.items()
        ):
            return
        merged = {**old, **fresh}
        self.session.cookies = list(merged.values())
        if self.me_data and self.me_data.get("name"):
            self.session.username = self.me_data["name"]
        try:
            save_session(self.session)
            self.session_stamp = _session_stamp()  # our own write: no reload
        except OSError as e:
            log.info("could not save refreshed cookies: %s", e)

    async def modhash(self) -> str:
        return (await self.me()).get("modhash") or ""


def _same_site(location: str) -> bool:
    return location.startswith("/") or location.startswith("https://www.reddit.com/")


_shared: tuple[asyncio.AbstractEventLoop, RedditWeb] | None = None


def shared_client() -> RedditWeb:
    """One client per event loop: tool calls share cookies, TLS connections
    and the rate-limit budget. The stdio server runs a single loop. Uses the
    saved account session, if any (REDDIT_NATIVE_MCP_ANONYMOUS=1 ignores it)."""
    global _shared
    loop = asyncio.get_running_loop()
    stamp = None if os.environ.get("REDDIT_NATIVE_MCP_ANONYMOUS") else _session_stamp()
    if _shared is None or _shared[0] is not loop or _shared[1].session_stamp != stamp:
        # first use, or `login`/`logout` changed session.json since
        session = load_session() if stamp else None
        web = RedditWeb(session=session, persist_session=True)
        web.session_stamp = stamp
        _shared = (loop, web)
    return _shared[1]


def _session_stamp() -> tuple | None:
    try:
        st = session_path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
