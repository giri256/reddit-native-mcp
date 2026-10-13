"""Linking the owner's Reddit account: a saved browser login session.

Reddit closed self-serve API apps in Nov 2025, so instead of OAuth the
server uses the owner's reddit.com login cookies, saved to `session.json`
(mode 0600) in the config dir. Three ways to get them; nothing here types or
stores a password:

- from a browser already logged in to Reddit, like `yt-dlp
  --cookies-from-browser` (browser_cookies.py). The default: the MCP client
  shows a choice of the logged-in browser profiles on first use, or run
  `reddit-native-mcp login`.
- a login window: a dedicated Brave/Chromium profile this module launches;
  the owner logs in by hand and the cookies are read over the DevTools
  protocol (localhost only).
- pasting the `reddit_session` cookie.

    reddit-native-mcp login                     # pick a logged-in browser profile
    reddit-native-mcp login brave[:Profile 1]   # that browser/profile, no questions
    reddit-native-mcp login --window            # log in in a new browser window
    reddit-native-mcp login --paste             # paste the reddit_session cookie
    reddit-native-mcp status                    # who is linked, is the session alive
    reddit-native-mcp logout                    # forget the session

Config dir: $REDDIT_NATIVE_MCP_HOME, else ~/.config/reddit-native-mcp.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

LOGIN_COOKIE = "reddit_session"
LOGIN_URL = "https://www.reddit.com/login/"
LOGIN_TIMEOUT_S = 600.0
BROWSERS = ("brave", "brave-browser", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def config_dir() -> Path:
    return Path(os.environ.get("REDDIT_NATIVE_MCP_HOME") or Path.home() / ".config" / "reddit-native-mcp")


def session_path() -> Path:
    return config_dir() / "session.json"


@dataclass
class Session:
    cookies: list[dict]  # {name, value, domain, path, expires}
    username: str | None = None
    saved_at: float = field(default_factory=time.time)
    source: str | None = None  # "brave:Default" when read from a browser profile

    def cookie(self, name: str) -> str | None:
        for c in self.cookies:
            if c["name"] == name:
                return c["value"]
        return None

    def expires_at(self) -> float | None:
        for c in self.cookies:
            if c["name"] == LOGIN_COOKIE:
                exp = c.get("expires")
                return float(exp) if exp and exp > 0 else None
        return None

    def to_json(self) -> dict:
        out = {"username": self.username, "saved_at": self.saved_at, "cookies": self.cookies}
        if self.source:
            out["source"] = self.source
        return out


def reddit_cookies(raw: list[dict]) -> list[dict]:
    """reddit.com cookies from a CDP cookie list, in the stored shape."""
    out = []
    for c in raw:
        domain = c.get("domain", "")
        if domain.lstrip(".") == "reddit.com" or domain.endswith(".reddit.com"):
            out.append(
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": domain,
                    "path": c.get("path") or "/",
                    "expires": c.get("expires") or -1,
                }
            )
    return out


def load_session(path: Path | None = None) -> Session | None:
    path = path or session_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    cookies = data.get("cookies") or []
    if not any(c.get("name") == LOGIN_COOKIE and c.get("value") for c in cookies):
        return None
    return Session(
        cookies=cookies, username=data.get("username"), saved_at=data.get("saved_at") or 0,
        source=data.get("source"),
    )


def save_session(session: Session, path: Path | None = None) -> Path:
    path = path or session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(session.to_json(), f, indent=1)
    os.replace(tmp, path)
    return path


# --- preferences (what the owner chose in the link dialog) -------------------


def prefs_path() -> Path:
    return config_dir() / "prefs.json"


def load_prefs() -> dict:
    try:
        data = json.loads(prefs_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(**changes) -> None:
    prefs = {**load_prefs(), **changes}
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, indent=1))
    os.replace(tmp, path)


def delete_session(path: Path | None = None) -> bool:
    path = path or session_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# --- browser login over the DevTools protocol ------------------------------


def find_browser() -> str | None:
    env = os.environ.get("REDDIT_NATIVE_MCP_BROWSER")
    if env:
        return env
    for name in BROWSERS:
        if path := shutil.which(name):
            return path
    return None


async def _cdp(ws_url: str, method: str, params: dict | None = None) -> dict:
    import websockets  # only the login command needs it

    async with websockets.connect(ws_url, max_size=None, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"DevTools {method}: {msg['error']}")
                return msg.get("result") or {}


async def _browser_ws(profile: Path, proc: subprocess.Popen, timeout: float = 30.0) -> str:
    """The browser's DevTools websocket URL (it writes its port to the profile)."""
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("the browser exited before it was ready")
        try:
            port = int(port_file.read_text().split()[0])
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            return r.json()["webSocketDebuggerUrl"]
        except (OSError, ValueError, IndexError, KeyError, httpx.HTTPError):
            await asyncio.sleep(0.5)
    raise RuntimeError("the browser did not open its DevTools port")


async def browser_login(timeout: float = LOGIN_TIMEOUT_S, say=print) -> Session:
    """Open reddit.com/login in a dedicated profile; wait until the owner has
    logged in; return the reddit.com cookies. The profile is kept, so a later
    `login` usually finds the owner still logged in."""
    exe = find_browser()
    if not exe:
        raise RuntimeError(
            "no Brave/Chromium found; set REDDIT_NATIVE_MCP_BROWSER or use `login --paste`"
        )
    profile = config_dir() / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir(), 0o700)
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            exe,
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ws = await _browser_ws(profile, proc)
        say("Log in to Reddit in the browser window that just opened (waiting up to "
            f"{int(timeout // 60)} min)...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("the browser was closed before the login finished")
            raw = (await _cdp(ws, "Storage.getCookies")).get("cookies", [])
            cookies = reddit_cookies(raw)
            if any(c["name"] == LOGIN_COOKIE and c["value"] for c in cookies):
                # let the page finish its post-login redirects (token_v2 refresh)
                await asyncio.sleep(3)
                raw = (await _cdp(ws, "Storage.getCookies")).get("cookies", [])
                return Session(cookies=reddit_cookies(raw))
            await asyncio.sleep(2)
        raise RuntimeError("timed out waiting for the Reddit login")
    finally:
        if proc.poll() is None:
            try:
                await _cdp(ws, "Browser.close")  # type: ignore[possibly-undefined]
            except Exception:
                proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def paste_login() -> Session:
    print(
        "In a browser where you are logged in to reddit.com, open DevTools > Application >\n"
        "Cookies > https://www.reddit.com and copy the value of `reddit_session`."
    )
    value = getpass.getpass("reddit_session (input hidden): ").strip()
    if not value:
        raise RuntimeError("no cookie given")
    return Session(
        cookies=[
            {"name": LOGIN_COOKIE, "value": value, "domain": ".reddit.com", "path": "/", "expires": -1}
        ]
    )


# --- CLI -------------------------------------------------------------------


async def _whoami(session: Session) -> str:
    from .http import RedditWeb  # late: http imports this module

    web = RedditWeb(session=session)
    try:
        me = await web.me()
    finally:
        await web.aclose()
    return me["name"]


def _pick_browser_profile() -> "Candidate | None":
    from .browser_cookies import discover

    logged = [c for c in discover() if c.logged_in]
    if not logged:
        return None
    if len(logged) == 1:
        c = logged[0]
        answer = input(f"Use the Reddit login from {c.describe()}? [Y/n] ").strip().lower()
        return c if answer in ("", "y", "yes") else None
    print("Browser profiles logged in to Reddit:")
    for i, c in enumerate(logged, 1):
        print(f"  {i}. {c.describe()}")
    answer = input(f"Pick one [1-{len(logged)}], or Enter to log in in a new window instead: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(logged):
        return logged[int(answer) - 1]
    return None


def _login(argv: list[str]) -> Session:
    from .browser_cookies import link, resolve

    args = [a for a in argv[1:] if not a.startswith("--")]
    if "--paste" in argv:
        session = paste_login()
    elif args:  # login brave[:profile]
        return asyncio.run(link(resolve(" ".join(args))))
    elif "--window" not in argv and (cand := _pick_browser_profile()) is not None:
        return asyncio.run(link(cand))
    else:
        session = asyncio.run(browser_login())
    session.username = asyncio.run(_whoami(session))
    save_session(session)
    save_prefs(link="window" if "--paste" not in argv else "paste")
    return session


def cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "login":
        try:
            session = _login(argv)
        except Exception as e:  # noqa: BLE001 - CLI boundary
            print(f"Login failed: {e}", file=sys.stderr)
            return 1
        how = f" from {session.source}" if session.source else ""
        print(f"Linked u/{session.username}{how}. Session saved to {session_path()} (mode 0600).")
        print("A running reddit-native server picks it up on its next tool call.")
        return 0
    if cmd == "logout":
        gone = delete_session()
        save_prefs(link="declined")  # don't offer the browser login again
        if "--keep-browser" not in argv:
            shutil.rmtree(config_dir() / "browser-profile", ignore_errors=True)
        print("Session removed." if gone else "No session was saved.")
        return 0
    if cmd == "status":
        session = load_session()
        if not session:
            print("Not linked: reading anonymously. Run `reddit-native-mcp login`.")
            return 1
        try:
            name = asyncio.run(_whoami(session))
        except Exception as e:  # noqa: BLE001
            print(f"Saved session for u/{session.username} does not work: {e}", file=sys.stderr)
            return 1
        exp = session.expires_at()
        print(f"Linked: u/{name}" + (f" (from {session.source})" if session.source else "")
              + (f", login cookie expires {time.strftime('%Y-%m-%d', time.localtime(exp))}" if exp else ""))
        return 0
    print(
        "usage: reddit-native-mcp [login [BROWSER[:PROFILE]] [--window | --paste] | status | "
        "logout [--keep-browser]]",
        file=sys.stderr,
    )
    return 2
