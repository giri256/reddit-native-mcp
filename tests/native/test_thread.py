"""Offline tests: thread fetch + expansion against recorded reddit.com responses."""

import unittest
from unittest import mock

from native.replay import replay_web
from reddit_native_mcp.http import NotFound
from reddit_native_mcp.thread import fetch_thread, post_of_comment


async def no_sleep(_s):
    return None


class ThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_expansion_reaches_every_level(self):
        # permalink_pec7ab8 replaces the empty-shell copy recorded in the thread run
        web, transport = replay_web("thread_1wz9oim", "permalink_pec7ab8")
        th = await fetch_thread(web, "1wz9oim", max_requests=100)
        self.assertEqual(transport.misses, [])
        self.assertEqual(th.pending, [])
        self.assertIsNone(th.stop_reason)
        self.assertGreaterEqual(len(th.comments), 200)
        depths = {c["depth"] for c in th.comments}
        self.assertEqual(max(depths), 9)  # past Reddit's render depth via permalink pages
        ids = [c["id"] for c in th.comments]
        self.assertEqual(len(ids), len(set(ids)))
        known = set(ids)
        for c in th.comments:  # a connected tree with consistent depths
            if c["parent_id"] is not None:
                self.assertIn(c["parent_id"], known)
        by = {c["id"]: c for c in th.comments}
        for c in th.comments:
            if c["parent_id"]:
                self.assertEqual(c["depth"], by[c["parent_id"]]["depth"] + 1, c["id"])
        self.assertEqual(by["pefmojb"]["parent_id"], "pef4faf")  # depth-limited reply recovered
        self.assertEqual(th.requests, len(transport.served) - 2)  # minus cookie + 301 hop

    @mock.patch("reddit_native_mcp.thread._sleep", no_sleep)
    async def test_empty_shell_page_retried_then_reported(self):
        web, transport = replay_web("thread_1wz9oim")  # its pec7ab8 page is an empty shell
        th = await fetch_thread(web, "1wz9oim", max_requests=100)
        shell = [k for k in transport.served if "comment/pec7ab8/" in k]
        self.assertEqual(len(shell), 3)  # first try + 2 retries
        self.assertEqual([m.parent_id for m in th.pending], ["pec7ab8"])
        self.assertIn("lacks comment pec7ab8", th.errors[0])

    async def test_budget_and_request_caps_stop_early(self):
        web, _ = replay_web("thread_1wz9oim")
        th = await fetch_thread(web, "1wz9oim", char_budget=8_000)
        self.assertLessEqual(th.requests, 5)  # a wave in flight may finish
        self.assertEqual(th.stop_reason, "output budget reached")
        self.assertTrue(th.pending)
        web, _ = replay_web("thread_1wz9oim")
        th = await fetch_thread(web, "1wz9oim", max_requests=5)
        self.assertEqual(th.requests, 5)
        self.assertIn("request cap", th.stop_reason)

    async def test_focus_subtree_rebased(self):
        web, transport = replay_web("thread_focus_pe9yhv8")
        th = await fetch_thread(web, "1wz9oim", focus="pe9yhv8")
        self.assertEqual(transport.misses, [])
        root = th.comments[0]
        self.assertEqual((root["id"], root["depth"], root["parent_id"]), ("pe9yhv8", 0, None))
        self.assertEqual(th.focus_parent_id, "pe9u3io")
        self.assertGreaterEqual(len(th.comments), 5)
        self.assertTrue(all(c["depth"] >= 0 for c in th.comments))

    async def test_bare_comment_lookup(self):
        web, transport = replay_web("comment_lookup_pefmojb")
        self.assertEqual(await post_of_comment(web, "pefmojb"), "1wz9oim")
        th = await fetch_thread(web, "1wz9oim", focus="pefmojb")
        self.assertEqual(th.comments[0]["id"], "pefmojb")
        self.assertEqual(transport.misses, [])

    @mock.patch("reddit_native_mcp.http._sleep", no_sleep)
    async def test_failed_link_is_reported_not_fatal(self):
        web, _ = replay_web(
            "thread_1wz9oim", "permalink_pec7ab8",
            overrides={"commentParentPositions=0 ": (500, "boom")},
        )
        th = await fetch_thread(web, "1wz9oim", max_requests=100)
        self.assertGreaterEqual(len(th.errors), 1)
        self.assertEqual(len(th.pending), len(th.errors))
        self.assertIn("HTTP 500", th.errors[0])
        self.assertGreater(len(th.comments), 150)

    async def test_missing_post(self):
        web, _ = replay_web("thread_1wz9oim", overrides={"/comments/zzzzzzz/": (404, "")})
        with self.assertRaisesRegex(NotFound, "no post zzzzzzz"):
            await fetch_thread(web, "zzzzzzz")


if __name__ == "__main__":
    unittest.main()
