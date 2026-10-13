"""Linking the account from a browser the owner is already logged in to,
the way `yt-dlp --cookies-from-browser` does (it is yt-dlp's extractor).

discover() only checks which browser profiles hold a reddit.com login cookie:
it reads cookie *names* from the browser's cookie database, never values.
extract() decrypts that profile's reddit.com cookies (yt-dlp handles the
OS keyring, Chromium's cookie encryption and Firefox), and runs only after the
owner picked the profile: `reddit-native-mcp login`, the link_account tool,
the MCP client's choice dialog, or REDDIT_NATIVE_MCP_COOKIES_FROM_BROWSER.

A session linked this way remembers its source ("brave:Default"), so when
Reddit stops accepting it the server re-reads the same profile instead of
asking again: log in again in the browser and the server follows.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .auth import LOGIN_COOKIE, Session, reddit_cookies

log = logging.getLogger(__name__)

# browser -> its user-data dir under the platform's base (Linux: ~/.config)
_CHROMIUM_DIRS = {
    "linux": {
        "brave": "BraveSoftware/Brave-Browser", "chrome": "google-chrome", "chromium": "chromium",
        "edge": "microsoft-edge", "opera": "opera", "vivaldi": "vivaldi", "whale": "naver-whale",
    },
    "darwin": {
        "brave": "BraveSoftware/Brave-Browser", "chrome": "Google/Chrome", "chromium": "Chromium",
        "edge": "Microsoft Edge", "opera": "com.operasoftware.Opera", "vivaldi": "Vivaldi",
        "whale": "Naver/Whale",
    },
    "win32": {
        "brave": "BraveSoftware/Brave-Browser/User Data", "chrome": "Google/Chrome/User Data",
        "chromium": "Chromium/User Data", "edge": "Microsoft/Edge/User Data",
        "opera": "Opera Software/Opera Stable", "vivaldi": "Vivaldi/User Data",
        "whale": "Naver/Naver Whale/User Data",
    },
}
BROWSER_NAMES = {
    "brave": "Brave", "chrome": "Chrome", "chromium": "Chromium", "edge": "Edge", "opera": "Opera",
    "vivaldi": "Vivaldi", "whale": "Whale", "firefox": "Firefox",
}
_CHROMIUM_EPOCH_S = 11644473600  # 1601-01-01 -> 1970-01-01


class BrowserCookieError(RuntimeError):
    pass


@dataclass
class Candidate:
    browser: str  # yt-dlp's browser name
    profile: str  # profile directory name ("Default", "Profile 1", "abcd.default-release")
    path: Path  # the profile directory
    label: str  # the browser's own name for the profile
    logged_in: bool  # holds a reddit_session cookie

    @property
    def source(self) -> str:
        return f"{self.browser}:{self.profile}"

    def describe(self) -> str:
        name = BROWSER_NAMES.get(self.browser, self.browser)
        same = self.label in ("", self.profile)
        return f"{name}, profile '{self.label}'" + ("" if same else f" ({self.profile})")


def _platform() -> str:
    return "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")


def _chromium_base() -> Path:
    plat = _platform()
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support"
    if plat == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _firefox_roots() -> list[Path]:
    home = Path.home()
    plat = _platform()
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    if plat == "win32":
        return [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Mozilla" / "Firefox" / "Profiles"]
    return [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def _readonly(db: Path) -> sqlite3.Connection:
    # immutable: the browser may hold the database open (and locked)
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True, timeout=2)


def _chromium_logged_in(cookies_db: Path) -> bool:
    now = int((time.time() + _CHROMIUM_EPOCH_S) * 1e6)
    with _readonly(cookies_db) as con:
        row = con.execute(
            "SELECT 1 FROM cookies WHERE name = ? AND (host_key = 'reddit.com' OR host_key LIKE '%.reddit.com') "
            "AND (expires_utc = 0 OR expires_utc > ?) LIMIT 1",
            (LOGIN_COOKIE, now),
        ).fetchone()
    return row is not None


def _firefox_logged_in(cookies_db: Path) -> bool:
    with _readonly(cookies_db) as con:
        row = con.execute(
            "SELECT 1 FROM moz_cookies WHERE name = ? AND (host = 'reddit.com' OR host LIKE '%.reddit.com') LIMIT 1",
            (LOGIN_COOKIE,),
        ).fetchone()
    return row is not None


def _chromium_candidates(browser: str, root: Path) -> list[Candidate]:
    try:
        info = json.loads((root / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError):
        info = {}
    dirs = [root] if browser == "opera" else sorted(p for p in root.iterdir() if p.is_dir())
    out = []
    for d in dirs:
        db = next((p for p in (d / "Network" / "Cookies", d / "Cookies") if p.is_file()), None)
        if db is None:
            continue
        try:
            logged_in = _chromium_logged_in(db)
        except sqlite3.Error as e:
            log.info("can't read %s: %s", db, e)
            continue
        name = d.name if d != root else ""
        out.append(Candidate(browser, name, d, (info.get(name) or {}).get("name") or name, logged_in))
    return out


def discover(browsers: list[str] | None = None) -> list[Candidate]:
    """Browser profiles on this machine, and which hold a Reddit login (names
    only: no cookie value is read). Logged-in profiles first."""
    found: list[Candidate] = []
    plat = _platform()
    for browser, rel in _CHROMIUM_DIRS[plat].items():
        if browsers and browser not in browsers:
            continue
        base = _chromium_base()
        if plat == "win32" and browser == "opera":  # Roaming, not Local
            base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        root = base / rel
        if root.is_dir():
            found += _chromium_candidates(browser, root)
    if not browsers or "firefox" in browsers:
        for root in _firefox_roots():
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir()):
                db = d / "cookies.sqlite"
                if not db.is_file():
                    continue
                try:
                    logged_in = _firefox_logged_in(db)
                except sqlite3.Error as e:
                    log.info("can't read %s: %s", db, e)
                    continue
                label = d.name.split(".", 1)[1] if "." in d.name else d.name
                found.append(Candidate("firefox", d.name, d, label, logged_in))
    found.sort(key=lambda c: not c.logged_in)  # stable: keeps browser order otherwise
    return found


def parse_source(source: str) -> tuple[str, str | None]:
    """'brave' / 'brave:Profile 1' / 'firefox:abcd.default' -> (browser, profile)."""
    browser, _, profile = source.strip().partition(":")
    browser = browser.strip().lower()
    if browser not in BROWSER_NAMES:
        raise BrowserCookieError(
            f"Unknown browser {browser!r}; use one of {', '.join(sorted(BROWSER_NAMES))} "
            "(optionally with :<profile>)"
        )
    return browser, (profile.strip() or None)


def resolve(source: str) -> Candidate:
    """The profile a source names. Without a profile: the browser's profile
    that is logged in to Reddit (or its only profile)."""
    browser, profile = parse_source(source)
    cands = discover([browser])
    if not cands:
        raise BrowserCookieError(f"No {BROWSER_NAMES[browser]} profile found on this machine")
    if profile:
        for c in cands:
            if profile in (c.profile, c.label) or profile.lower() in (c.profile.lower(), c.label.lower()):
                return c
        raise BrowserCookieError(
            f"No {BROWSER_NAMES[browser]} profile {profile!r}; found: "
            + ", ".join(f"{c.profile} ('{c.label}')" for c in cands)
        )
    logged = [c for c in cands if c.logged_in]
    if len(logged) == 1:
        return logged[0]
    if not logged:
        raise BrowserCookieError(f"No {BROWSER_NAMES[browser]} profile is logged in to reddit.com")
    raise BrowserCookieError(
        f"Several {BROWSER_NAMES[browser]} profiles are logged in to reddit.com; pick one: "
        + ", ".join(f"{browser}:{c.profile} ('{c.label}')" for c in logged)
    )


class _Log:
    """yt-dlp's logger interface, routed to logging (stdout is the MCP channel)."""

    def debug(self, message, only_once=False):
        log.debug("yt-dlp: %s", message)

    def info(self, message):
        log.debug("yt-dlp: %s", message)

    def warning(self, message, only_once=False):
        log.info("yt-dlp: %s", message)

    def error(self, message):
        log.info("yt-dlp: %s", message)

    def progress_bar(self):
        return None


def _extract_jar(browser: str, profile_dir: Path):
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise BrowserCookieError("yt-dlp is not installed (pip install yt-dlp)") from None
    try:
        return extract_cookies_from_browser(browser, str(profile_dir), _Log())
    except Exception as e:  # noqa: BLE001 - yt-dlp raises many kinds
        raise BrowserCookieError(f"could not read {BROWSER_NAMES[browser]}'s cookies: {e}") from None


def extract(candidate: Candidate) -> Session:
    """The profile's reddit.com cookies as a Session (blocking: decrypts the
    browser's cookie store; may ask the OS keyring)."""
    jar = _extract_jar(candidate.browser, candidate.path)
    raw = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires or -1}
        for c in jar
        if c.value is not None
    ]
    cookies = reddit_cookies(raw)
    if not any(c["name"] == LOGIN_COOKIE and c["value"] for c in cookies):
        raise BrowserCookieError(f"{candidate.describe()} is not logged in to reddit.com")
    return Session(cookies=cookies, source=candidate.source)


async def link(candidate: Candidate) -> Session:
    """Read the profile's Reddit login, check it with Reddit, save it as the
    linked session. Returns the session (username filled in)."""
    import asyncio

    from .auth import save_prefs, save_session
    from .http import RedditWeb  # late: http imports auth

    session = await asyncio.to_thread(extract, candidate)
    web = RedditWeb(session=session)
    try:
        me = await web.me()
    finally:
        await web.aclose()
    session.username = me["name"]
    save_session(session)
    save_prefs(link="browser", source=candidate.source)
    return session
