"""Offline tests: the MCP surface through an in-memory MCP client, on replayed
reddit.com responses (and canned search hits for find_posts ranking)."""

import json
import time
import unittest
from unittest import mock

from mcp import Client

from native.replay import replay_web
from reddit_native_mcp.http import RedditError
from reddit_native_mcp.server import mcp

URL = "https://www.reddit.com/r/ClaudeAI/comments/1wz9oim/anthropic_is_giving_startups_a_free_year_of/"


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, tool, args, *fixtures, **kw):
        web, self.transport = replay_web(*fixtures, **kw) if fixtures else replay_web("share_link")
        with mock.patch("reddit_native_mcp.server.shared_client", return_value=web):
            async with Client(mcp) as client:
                result = await client.call_tool(tool, args)
        text = result.content[0].text
        self.assertIsNone(result.structured_content)  # payload is sent once
        if result.is_error or args.get("format") == "text":
            return result.is_error, text
        return result.is_error, json.loads(text)

    async def test_tool_schemas_are_documented_and_read_only(self):
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        reads = {"get_thread", "search_posts", "find_posts", "find_similar", "account_status",
                 "get_post_requirements"}
        writes = {"create_post", "edit_post", "set_post_options", "delete_post", "link_account"}
        self.assertEqual(set(tools), reads | writes)
        for tool in tools.values():
            for name, prop in tool.input_schema["properties"].items():
                self.assertTrue(prop.get("description"), f"{tool.name}.{name}")
            self.assertEqual(tool.annotations.read_only_hint, tool.name in reads, tool.name)

    async def test_get_thread_full(self):
        err, data = await self.call(
            "get_thread", {"post": URL, "max_output_chars": 200_000, "max_requests": 100},
            "thread_1wz9oim", "permalink_pec7ab8",
        )
        self.assertFalse(err, data)
        meta = data["meta"]
        self.assertEqual(data["post"]["id"], "1wz9oim")
        self.assertIn("madrobot.blog", data["post"]["link_url"])
        self.assertFalse(meta["truncated"])
        self.assertEqual(meta["max_depth_returned"], 9)
        self.assertGreaterEqual(meta["returned_comments"], 200)
        self.assertTrue(meta["comment_url_template"].endswith("{comment_id}/"))
        self.assertEqual(self.transport.misses, [])

    async def test_get_thread_text_format(self):
        args = {"post": "1wz9oim", "max_output_chars": 200_000, "max_requests": 100}
        _, data = await self.call("get_thread", args, "thread_1wz9oim", "permalink_pec7ab8")
        err, text = await self.call("get_thread", {**args, "format": "text"}, "thread_1wz9oim", "permalink_pec7ab8")
        self.assertFalse(err, text)
        self.assertTrue(text.startswith("# Anthropic is giving startups"))
        self.assertIn("link: https://madrobot.blog", text)
        self.assertIn(f"## Comments ({data['meta']['returned_comments']} of", text)
        self.assertIn("- pefmojb u/", text)  # deep comment present, indented
        self.assertIn("\n" + "  " * 5 + "- pefmojb", text)
        self.assertLess(len(text), 0.85 * len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))))

    async def test_text_format_respects_cap(self):
        err, text = await self.call(
            "get_thread", {"post": "1wz9oim", "format": "text", "max_output_chars": 8_000}, "thread_1wz9oim"
        )
        self.assertFalse(err, text)
        self.assertLessEqual(len(text), 8_000)
        self.assertIn("truncated: True", text)
        self.assertIn("more replies)", text)

    async def test_get_thread_output_cap_and_hints(self):
        err, data = await self.call("get_thread", {"post": "1wz9oim", "max_output_chars": 8_000}, "thread_1wz9oim")
        self.assertFalse(err, data)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))), 8_000)
        meta = data["meta"]
        self.assertTrue(meta["truncated"])
        self.assertTrue(meta["omitted_top_level_ids"])
        self.assertIn("comment_id", meta["notes"][0])
        self.assertTrue(any("more_replies" in c for c in data["comments"]))

    async def test_get_thread_focus_and_bare_comment(self):
        err, data = await self.call("get_thread", {"post": "1wz9oim", "comment_id": "t1_pe9yhv8"}, "thread_focus_pe9yhv8")
        self.assertFalse(err, data)
        self.assertEqual(data["comments"][0]["id"], "pe9yhv8")
        self.assertEqual(data["comments"][0]["depth"], 0)
        self.assertEqual(data["meta"]["focus_parent_id"], "pe9u3io")
        err, data = await self.call("get_thread", {"post": "t1_pefmojb"}, "comment_lookup_pefmojb")
        self.assertFalse(err, data)
        self.assertEqual((data["post"]["id"], data["comments"][0]["id"]), ("1wz9oim", "pefmojb"))

    async def test_get_thread_errors_are_readable(self):
        err, text = await self.call("get_thread", {"post": "https://example.com/x"})
        self.assertTrue(err)
        self.assertIn("Not a Reddit post", text)
        err, text = await self.call(
            "get_thread", {"post": "zzzzzzz"}, "thread_1wz9oim", overrides={"/comments/zzzzzzz/": (404, "")}
        )
        self.assertTrue(err)
        self.assertIn("no post zzzzzzz", text)
        err, text = await self.call(
            "get_thread", {"post": "1wz9oim"}, "thread_1wz9oim",
            overrides={"/comments/1wz9oim/": (403, "blocked")},
        )
        self.assertTrue(err)
        self.assertIn("refused", text)

    async def test_search_posts_feed_and_paging(self):
        err, data = await self.call("search_posts", {"subreddit": "r/ClaudeAI", "limit": 30}, "feed_claudeai")
        self.assertFalse(err, data)
        self.assertEqual(data["meta"]["mode"], "feed")
        self.assertEqual(len(data["posts"]), 30)
        self.assertEqual(len({p["id"] for p in data["posts"]}), 30)
        self.assertIn("next_cursor", data["meta"])
        self.assertTrue(all(len(p.get("selftext", "")) <= 430 for p in data["posts"]))

    async def test_search_posts_validation(self):
        err, text = await self.call("search_posts", {"subreddit": "thissubdoesnotexist12345"}, "feed_claudeai")
        self.assertTrue(err)
        self.assertIn("returned no posts", text)
        err, text = await self.call("search_posts", {"subreddit": "x", "sort": "relevance"})
        self.assertTrue(err)
        self.assertIn("needs a query", text)
        err, text = await self.call("search_posts", {})
        self.assertTrue(err)

    async def test_search_posts_search_mode(self):
        err, data = await self.call(
            "search_posts", {"query": "startup program", "sort": "top", "time_range": "week", "limit": 10},
            "search_startup_program",
        )
        self.assertFalse(err, data)
        self.assertEqual(data["meta"]["mode"], "search")
        self.assertGreaterEqual(len(data["posts"]), 8)  # two pages of ~7


NOW = time.time()


def hit(pid, title, sub="x", age_days=1, score=1):
    return {
        "id": pid, "subreddit": sub, "author": "a", "title": title,
        "permalink": f"/r/{sub}/comments/{pid}/t/", "created": "t",
        "created_ts": NOW - age_days * 86400, "score": score, "num_comments": 0,
    }


class FindPostsTests(unittest.IsolatedAsyncioTestCase):
    async def run_find(self, args, results):
        calls = []

        async def fake_search(web, q, *, sub=None, sort="relevance", time="all", cursor=None):
            calls.append((q, sub, sort, cursor))
            value = results(q, sub, sort)
            if isinstance(value, Exception):
                raise value
            return value, None

        with mock.patch("reddit_native_mcp.server.search_page", fake_search), mock.patch(
            "reddit_native_mcp.server.shared_client", return_value=object()
        ):
            async with Client(mcp) as client:
                result = await client.call_tool("find_posts", args)
        text = result.content[0].text
        return result.is_error, (text if result.is_error else json.loads(text)), calls

    async def test_merge_rank_and_window(self):
        def results(q, sub, sort):
            if q == "startup program":
                return [hit("p1", "Startup program news"), hit("old", "startup program", age_days=60)] + (
                    [hit("p2", "Thank you!", sub="Anthropic")] if sub == "Anthropic" else []
                )
            return [hit("p2", "Thank you!", sub="Anthropic"), hit("p3", "free claude team plan")]

        err, data, calls = await self.run_find(
            {"keywords": "startup program, free claude team", "subreddits": ["r/Anthropic"], "days": 30},
            results,
        )
        self.assertFalse(err, data)
        ids = [p["id"] for p in data["posts"]]
        self.assertEqual(ids[0], "p2")  # found by both keywords
        self.assertNotIn("old", ids)  # outside the window
        self.assertEqual(data["posts"][0]["matched_keywords"], ["startup program", "free claude team"])
        self.assertIn("r/Anthropic", data["posts"][0]["found_in"])
        # sitewide relevance + top for each keyword, plus the subreddit
        self.assertEqual({(q, s, so) for q, s, so, _ in calls}, {
            ("startup program", None, "relevance"), ("startup program", None, "top"),
            ("startup program", "Anthropic", "relevance"), ("free claude team", None, "relevance"),
            ("free claude team", None, "top"), ("free claude team", "Anthropic", "relevance"),
        })

    async def test_partial_and_total_failure(self):
        def some_fail(q, sub, sort):
            return RedditError("boom") if sort == "top" else [hit("p1", "x")]

        err, data, _ = await self.run_find({"keywords": "k"}, some_fail)
        self.assertFalse(err, data)
        self.assertTrue(data["meta"]["errors"])
        err, text, _ = await self.run_find({"keywords": "k"}, lambda q, s, so: RedditError("down"))
        self.assertTrue(err)
        self.assertIn("Every search failed", text)


class FindSimilarTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranks_same_link_then_agreement(self):
        calls = []

        async def fake_search(web, q, *, sub=None, sort="relevance", time="all", cursor=None):
            calls.append((q, sort, time))
            if q.startswith("url:"):
                return [hit("same", "Totally different title"), hit("1wz9oim", "source itself")], None
            if "anthropic" in q:
                return [hit("multi", "Anthropic startup credit"), hit("far", "startup", age_days=400)], None
            return [hit("multi", "Anthropic startup credit"), hit("once", "claude team chat")], None

        web, _ = replay_web("thread_1wz9oim")
        with mock.patch("reddit_native_mcp.server.search_page", fake_search), mock.patch(
            "reddit_native_mcp.server.shared_client", return_value=web
        ):
            async with Client(mcp) as client:
                result = await client.call_tool("find_similar", {"post": URL, "days": 30})
        data = json.loads(result.content[0].text)
        ids = [p["id"] for p in data["posts"]]
        self.assertEqual(ids[:2], ["same", "multi"])  # same link first, then most searches
        self.assertNotIn("1wz9oim", ids)  # never the source
        self.assertNotIn("far", ids)  # outside ±30 days of the source
        self.assertTrue(data["posts"][0]["similarity"]["same_link"])
        self.assertEqual(data["meta"]["terms"], ["claude team", "anthropic", "startup", "credit"])
        self.assertTrue(any(q.startswith("url:madrobot.blog") for q, _, _ in calls))
        self.assertEqual(data["meta"]["requests"], len(calls) + 1)


if __name__ == "__main__":
    unittest.main()
