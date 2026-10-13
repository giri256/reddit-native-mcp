"""Offline tests: RedditWeb retry, rate-limit, block and redirect handling."""

import unittest
from unittest import mock

import httpx

from reddit_native_mcp import http as rhttp
from reddit_native_mcp.http import BlockedError, NotFound, RateLimited, RedditError, RedditWeb


def web_for(handler) -> RedditWeb:
    return RedditWeb(transport=httpx.MockTransport(handler), deadline=5.0)


async def no_sleep(_s):
    return None


@mock.patch("reddit_native_mcp.http._sleep", no_sleep)
class RedditWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_retried_then_succeed(self):
        codes = iter([503, 502, 200])
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(next(codes), text="<p>ok</p>")

        web = web_for(handler)
        self.assertEqual(await web.svc("/svc/shreddit/x"), "<p>ok</p>")
        self.assertEqual(len(calls), 3)

    async def test_permanent_errors_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(404)

        with self.assertRaises(NotFound):
            await web_for(handler).svc("/svc/shreddit/x")
        self.assertEqual(len(calls), 1)

    async def test_403_and_bot_check_are_blocked(self):
        web = web_for(lambda r: httpx.Response(403, text="blocked"))
        with self.assertRaises(BlockedError):
            await web.svc("/x")
        challenge = '<form><input name="solution"></form>'
        web = web_for(lambda r: httpx.Response(200, text=challenge))
        with self.assertRaisesRegex(BlockedError, "bot-check"):
            await web.svc("/x")

    async def test_partial_accept_header_and_user_agent(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="x")

        await web_for(handler).svc("/svc/shreddit/x")
        self.assertIn("vnd.reddit.partial", seen["accept"])
        self.assertTrue(seen["user-agent"].startswith("reddit-native-mcp/"))

    async def test_rate_budget_tracked_and_enforced(self):
        def handler(request):
            return httpx.Response(
                200, text="x", headers={"x-ratelimit-remaining": "5", "x-ratelimit-reset": "120"}
            )

        web = web_for(handler)
        await web.svc("/a")
        self.assertEqual(web.budget_left(), 5)
        with self.assertRaises(RateLimited) as ctx:
            await web.svc("/b")  # 5 <= reserve: refused without a request
        self.assertGreater(ctx.exception.reset_s, 100)

    async def test_429_short_wait_retried_long_wait_raises(self):
        codes = iter([429, 200])
        web = web_for(lambda r: httpx.Response(next(codes), text="x", headers={"retry-after": "2"}))
        self.assertEqual(await web.svc("/a"), "x")
        web = web_for(lambda r: httpx.Response(429, headers={"retry-after": "300"}))
        with self.assertRaises(RateLimited):
            await web.svc("/a")

    async def test_follows_same_site_redirects_only(self):
        def handler(request):
            if request.url.path == "/comments/abc/":
                return httpx.Response(301, headers={"location": "/r/x/comments/abc/slug/"})
            if request.url.path == "/away":
                return httpx.Response(302, headers={"location": "https://evil.example/"})
            return httpx.Response(200, text="page")

        web = web_for(handler)
        web._client.cookies.set("loid", "1", domain="www.reddit.com")
        url, html = await web.page("/comments/abc/")
        self.assertTrue(url.endswith("/r/x/comments/abc/slug/"))
        resp = await web.request("GET", "/away")
        self.assertEqual(resp.status_code, 302)  # not followed off-site
        self.assertEqual(await web.redirect_target("/comments/abc/"), "/r/x/comments/abc/slug/")

    async def test_cookie_bootstrap_before_pages(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == rhttp._COOKIE_PATH:
                return httpx.Response(200, text="x", headers={"set-cookie": "loid=abc; Path=/"})
            return httpx.Response(200, text="page")

        web = web_for(handler)
        await web.page("/r/x/")
        await web.page("/r/y/")
        self.assertEqual(paths, [rhttp._COOKIE_PATH, "/r/x/", "/r/y/"])

    async def test_results_cached_briefly_and_bypassable(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, text=f"v{len(calls)}")

        web = web_for(handler)
        self.assertEqual(await web.svc("/a", params={"q": "1"}), "v1")
        self.assertEqual(await web.svc("/a", params={"q": "1"}), "v1")  # cached
        self.assertEqual(await web.svc("/a", params={"q": "2"}), "v2")  # different key
        self.assertEqual(await web.svc("/a", params={"q": "1"}, fresh=True), "v3")
        self.assertEqual(web.cache_hits, 1)
        with mock.patch("reddit_native_mcp.http.time.monotonic", return_value=10**9):
            self.assertEqual(await web.svc("/a", params={"q": "1"}), "v4")  # expired

    async def test_failures_are_not_cached(self):
        codes = iter([404, 200])
        web = web_for(lambda r: httpx.Response(next(codes), text="ok"))
        with self.assertRaises(NotFound):
            await web.svc("/a")
        self.assertEqual(await web.svc("/a"), "ok")

    async def test_network_errors_exhaust_to_reddit_error(self):
        def handler(request):
            raise httpx.ConnectError("down")

        with self.assertRaisesRegex(RedditError, "after 3 tries"):
            await web_for(handler).svc("/x")


if __name__ == "__main__":
    unittest.main()
