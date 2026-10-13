"""Linking the account from a browser profile (offline: fake browser
directories, a stubbed yt-dlp extractor, a fake Reddit, a fake MCP client)."""

import http.cookiejar
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from mcp import Client, types

from reddit_native_mcp import auth, browser_cookies, http as rhttp, server
from reddit_native_mcp.browser_cookies import BrowserCookieError, Candidate
from reddit_native_mcp.http import RedditWeb
from reddit_native_mcp.server import mcp

from .test_account import FakeReddit

CHROMIUM_EPOCH_US = 11644473600 * 10**6


def chromium_profile(root: Path, name: str, cookies: list[tuple[str, str, int]]) -> None:
    """A profile dir whose Network/Cookies holds (host, name, expires_utc)."""
    (root / name / "Network").mkdir(parents=True)
    con = sqlite3.connect(root / name / "Network" / "Cookies")
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, expires_utc INTEGER)")
    con.executemany("INSERT INTO cookies VALUES (?, ?, '', x'7631300000', ?)", cookies)
    con.commit()
    con.close()


def future_us(days=30):
    return int((time.time() + days * 86400) * 1e6) + CHROMIUM_EPOCH_US


def jar_cookie(name, value, domain):
    return http.cookiejar.Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."), "/", True, True,
        int(time.time()) + 86400, False, None, None, {},
    )


def candidate(profile="Default", label="Personal", logged_in=True):
    return Candidate("brave", profile, Path("/nowhere") / profile, label, logged_in)


def clear_account_files():
    for p in (auth.session_path(), auth.prefs_path()):
        p.unlink(missing_ok=True)


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="browsers-", dir=os.environ.get("TMPDIR")))
        brave = self.home / "BraveSoftware" / "Brave-Browser"
        chromium_profile(brave, "Default", [(".reddit.com", "reddit_session", future_us()),
                                            (".reddit.com", "token_v2", future_us())])
        chromium_profile(brave, "Profile 1", [(".reddit.com", "token_v2", future_us())])
        chromium_profile(brave, "Profile 2", [(".reddit.com", "reddit_session", CHROMIUM_EPOCH_US + 10**6)])  # expired
        chromium_profile(brave, "Profile 3", [(".notreddit.com", "reddit_session", future_us())])
        (brave / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Personal"}, "Profile 1": {"name": "work"}}}}))
        ff = self.home / "firefox"
        (ff / "ab12.default-release").mkdir(parents=True)
        con = sqlite3.connect(ff / "ab12.default-release" / "cookies.sqlite")
        con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        con.execute("INSERT INTO moz_cookies VALUES ('.reddit.com', 'reddit_session', 'v')")
        con.commit()
        con.close()
        self.patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)}),
            mock.patch.object(browser_cookies, "_platform", return_value="linux"),
            mock.patch.object(browser_cookies, "_firefox_roots", return_value=[ff]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_finds_logged_in_profiles_first(self):
        found = browser_cookies.discover()
        logged = [(c.browser, c.profile, c.label) for c in found if c.logged_in]
        self.assertEqual(logged, [("brave", "Default", "Personal"), ("firefox", "ab12.default-release", "default-release")])
        others = {c.profile for c in found if not c.logged_in}
        self.assertEqual(others, {"Profile 1", "Profile 2", "Profile 3"})  # no login / expired / other site
        self.assertTrue(all(c.logged_in for c in found[:2]))
        self.assertEqual(found[0].describe(), "Brave, profile 'Personal' (Default)")
        self.assertEqual(found[0].source, "brave:Default")

    def test_resolve(self):
        self.assertEqual(browser_cookies.resolve("brave").profile, "Default")
        self.assertEqual(browser_cookies.resolve("Brave:work").profile, "Profile 1")  # by label
        self.assertEqual(browser_cookies.resolve("brave:profile 1").profile, "Profile 1")
        self.assertEqual(browser_cookies.resolve("firefox").browser, "firefox")
        with self.assertRaisesRegex(BrowserCookieError, "No Brave profile 'nope'; found: Default"):
            browser_cookies.resolve("brave:nope")
        with self.assertRaisesRegex(BrowserCookieError, "Unknown browser 'netscape'"):
            browser_cookies.resolve("netscape")
        with self.assertRaisesRegex(BrowserCookieError, "No Chrome profile found"):
            browser_cookies.resolve("chrome")

    def test_several_logged_in_profiles_need_a_choice(self):
        brave = self.home / "BraveSoftware" / "Brave-Browser"
        chromium_profile(brave, "Profile 4", [("www.reddit.com", "reddit_session", 0)])  # session cookie
        with self.assertRaisesRegex(BrowserCookieError, r"pick one: brave:Default \('Personal'\), brave:Profile 4"):
            browser_cookies.resolve("brave")


class ExtractTests(unittest.TestCase):
    def test_keeps_only_reddit_cookies(self):
        jar = http.cookiejar.CookieJar()
        for c in (jar_cookie("reddit_session", "s3cret", ".reddit.com"), jar_cookie("token_v2", "t", ".reddit.com"),
                  jar_cookie("SID", "google", ".google.com"), jar_cookie("x", "y", "notreddit.com")):
            jar.set_cookie(c)
        with mock.patch.object(browser_cookies, "_extract_jar", return_value=jar) as ex:
            s = browser_cookies.extract(candidate())
        ex.assert_called_once_with("brave", Path("/nowhere/Default"))
        self.assertEqual(sorted(c["name"] for c in s.cookies), ["reddit_session", "token_v2"])
        self.assertEqual(s.source, "brave:Default")
        self.assertGreater(s.expires_at(), time.time())

    def test_profile_without_login_is_refused(self):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(jar_cookie("token_v2", "t", ".reddit.com"))
        with mock.patch.object(browser_cookies, "_extract_jar", return_value=jar):
            with self.assertRaisesRegex(BrowserCookieError, "not logged in to reddit.com"):
                browser_cookies.extract(candidate())

    def test_extractor_failure_is_a_clear_error(self):
        def boom(*a, **k):
            raise RuntimeError("Failed to decrypt with DPAPI")

        with mock.patch("yt_dlp.cookies.extract_cookies_from_browser", boom):
            with self.assertRaisesRegex(BrowserCookieError, "could not read Brave's cookies: Failed to decrypt"):
                browser_cookies.extract(candidate())


def fake_web_factory(fake: FakeReddit):
    """RedditWeb(...) built on the fake Reddit, for code that constructs its own."""
    def make(*args, **kw):
        kw.setdefault("transport", httpx.MockTransport(fake))
        return RedditWeb(*args, **kw)
    return make


def linked_session(source="brave:Default"):
    return auth.Session(
        cookies=[{"name": "reddit_session", "value": "s3cret", "domain": ".reddit.com", "path": "/", "expires": 2e9}],
        source=source,
    )


class LinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_account_files()
        self.fake = FakeReddit(me="browser_user")
        p = mock.patch.object(rhttp, "RedditWeb", fake_web_factory(self.fake))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(clear_account_files)

    async def test_link_checks_with_reddit_and_saves(self):
        with mock.patch.object(browser_cookies, "extract", return_value=linked_session()):
            s = await browser_cookies.link(candidate())
        self.assertEqual(s.username, "browser_user")
        saved = auth.load_session()
        self.assertEqual((saved.username, saved.source), ("browser_user", "brave:Default"))
        self.assertEqual(auth.load_prefs(), {"link": "browser", "source": "brave:Default"})
        self.assertEqual(len(self.fake.calls("/api/me.json")), 1)

    async def test_rejected_session_is_not_saved(self):
        self.fake.routes["/api/me.json"] = {"data": {}}
        with mock.patch.object(browser_cookies, "extract", return_value=linked_session()):
            with self.assertRaises(rhttp.NotLoggedIn):
                await browser_cookies.link(candidate())
        self.assertIsNone(auth.load_session())


class LinkToolTests(unittest.IsolatedAsyncioTestCase):
    """The link_account tool and the dialog the MCP client shows."""

    def setUp(self):
        clear_account_files()
        self.addCleanup(clear_account_files)
        self.fake = FakeReddit(me="browser_user", routes={"/api/submit": {"json": {"errors": [], "data": {
            "id": "new1", "name": "t3_new1", "url": "https://www.reddit.com/r/test/comments/new1/x/"}}}})
        self.asked: list = []
        patches = [
            mock.patch.object(rhttp, "RedditWeb", fake_web_factory(self.fake)),
            mock.patch.object(server, "shared_client", self.client_from_saved_session),
            mock.patch.object(browser_cookies, "discover", return_value=[
                candidate(), candidate("Profile 1", "work", logged_in=False),
                Candidate("firefox", "ab.default", Path("/nowhere/ff"), "default", True)]),
            mock.patch.object(browser_cookies, "extract", side_effect=lambda c: linked_session(c.source)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def client_from_saved_session(self):
        # what http.shared_client does: follow session.json
        return rhttp.RedditWeb(session=auth.load_session(), deadline=5.0)

    def answer(self, choice):
        async def elicit(context, params):
            self.asked.append(params)
            if choice in ("decline", "cancel"):
                return types.ElicitResult(action=choice)
            return types.ElicitResult(action="accept", content={"login": choice})
        return elicit

    async def call(self, tool, args, choice=None, mode="auto"):
        kw = {"elicitation_callback": self.answer(choice)} if choice else {}
        async with Client(mcp, mode=mode, **kw) as client:
            result = await client.call_tool(tool, args)
        return result.is_error, result.content[0].text

    async def test_dialog_lists_logged_in_profiles_and_links_the_choice(self):
        err, text = await self.call("link_account", {}, choice="Brave, profile 'Personal' (Default)")
        self.assertFalse(err, text)
        data = json.loads(text)
        self.assertEqual((data["linked"], data["account"], data["source"]), (True, "browser_user", "brave:Default"))
        self.assertNotIn("s3cret", text)
        schema = self.asked[0].requested_schema
        self.assertEqual(schema["properties"]["login"]["enum"], [
            "Brave, profile 'Personal' (Default)", "Firefox, profile 'default' (ab.default)",
            "Don't link: stay anonymous"])
        self.assertIn("yt-dlp", self.asked[0].message)
        self.assertEqual(auth.load_session().source, "brave:Default")

    async def test_dialog_over_the_older_protocol(self):
        # <= 2025-11-25: the dialog is a server-to-client request mid-call
        err, text = await self.call("link_account", {}, choice="Firefox, profile 'default' (ab.default)",
                                    mode="legacy")
        self.assertFalse(err, text)
        self.assertEqual(json.loads(text)["source"], "firefox:ab.default")
        self.assertEqual(len(self.asked), 1)

    async def test_without_dialogs_the_agent_gets_the_choices(self):
        err, text = await self.call("link_account", {})
        data = json.loads(text)
        self.assertFalse(data["linked"])
        self.assertEqual([c["source"] for c in data["choose_one"]], ["brave:Default", "firefox:ab.default"])
        self.assertIn("Ask the user", data["next"])
        self.assertIsNone(auth.load_session())  # nothing read without a choice
        browser_cookies.extract.assert_not_called()

        err, text = await self.call("link_account", {"source": "firefox:ab.default"})
        self.assertFalse(err, text)
        self.assertEqual(json.loads(text)["source"], "firefox:ab.default")

    async def test_decline_is_remembered(self):
        err, text = await self.call("link_account", {}, choice="decline")
        self.assertFalse(json.loads(text)["linked"])
        self.assertEqual(auth.load_prefs()["link"], "declined")
        # posting no longer pops the dialog up; it explains how to link
        err, text = await self.call("create_post", {"subreddit": "test", "title": "x", "body": "y"},
                                    choice="Brave, profile 'Personal' (Default)")
        self.assertTrue(err)
        self.assertIn("link_account", text)
        self.assertEqual(len(self.asked), 1)

    async def test_first_post_asks_then_posts(self):
        err, text = await self.call("create_post", {"subreddit": "test", "title": "x", "body": "y"},
                                    choice="Brave, profile 'Personal' (Default)")
        self.assertFalse(err, text)
        self.assertEqual(json.loads(text)["id"], "new1")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.fake.calls("/api/submit")), 1)

    async def test_env_var_links_without_asking(self):
        with mock.patch.object(browser_cookies, "resolve", return_value=candidate()) as res, \
                mock.patch.dict(os.environ, {server.COOKIES_ENV: "brave"}):
            err, text = await self.call("create_post", {"subreddit": "test", "title": "x", "body": "y"},
                                        choice="decline")
        self.assertFalse(err, text)
        res.assert_called_once_with("brave")
        self.assertEqual(self.asked, [])

    async def test_rejected_browser_session_is_read_again(self):
        auth.save_session(linked_session())
        answers = iter([{"data": {}}, {"data": {"name": "browser_user", "modhash": "m"}}])
        self.fake.routes["/api/me.json"] = lambda req: httpx.Response(200, json=next(answers, {
            "data": {"name": "browser_user", "modhash": "m"}}), request=req)
        with mock.patch.object(browser_cookies, "resolve", return_value=candidate()) as res:
            err, text = await self.call("account_status", {})
        self.assertFalse(err, text)
        self.assertEqual(json.loads(text)["account"], "browser_user")
        res.assert_called_once_with("brave:Default")
        browser_cookies.extract.assert_called_once()

    async def test_account_status_never_opens_the_dialog(self):
        err, text = await self.call("account_status", {}, choice="Brave, profile 'Personal' (Default)")
        self.assertFalse(json.loads(text)["linked"])
        self.assertEqual(self.asked, [])


class CliTests(unittest.TestCase):
    def setUp(self):
        clear_account_files()
        self.addCleanup(clear_account_files)

    def test_login_from_named_browser(self):
        async def link(c):
            s = linked_session(c.source)
            s.username = "browser_user"
            return s

        with mock.patch.object(browser_cookies, "resolve", return_value=candidate()) as res, \
                mock.patch.object(browser_cookies, "link", link), \
                mock.patch("builtins.print") as out:
            self.assertEqual(auth.cli(["login", "brave:Personal"]), 0)
        res.assert_called_once_with("brave:Personal")
        self.assertIn("Linked u/browser_user from brave:Default", out.call_args_list[0].args[0])

    def test_plain_login_offers_the_logged_in_profile(self):
        async def link(c):
            s = linked_session(c.source)
            s.username = "browser_user"
            return s

        with mock.patch.object(browser_cookies, "discover", return_value=[candidate()]), \
                mock.patch.object(browser_cookies, "link", link), \
                mock.patch("builtins.input", return_value="") as ask, \
                mock.patch("builtins.print"):
            self.assertEqual(auth.cli(["login"]), 0)
        self.assertIn("Brave, profile 'Personal' (Default)", ask.call_args.args[0])

    def test_logout_remembers_not_to_offer(self):
        auth.save_session(linked_session())
        with mock.patch("builtins.print"):
            auth.cli(["logout", "--keep-browser"])
        self.assertIsNone(auth.load_session())
        self.assertEqual(auth.load_prefs()["link"], "declined")
