"""Offline tests: link parsing and share-link resolution (replayed)."""

import unittest

from native.replay import replay_web
from reddit_native_mcp.links import (
    LinkError,
    RedditRef,
    clean_comment_id,
    clean_name,
    parse_reddit_ref,
    resolve_share_path,
)


class ParseRefTests(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "https://www.reddit.com/r/ClaudeAI/comments/1wz9oim/anthropic_is/": RedditRef("1wz9oim"),
            "https://old.reddit.com/r/x/comments/1wz9oim/slug/pea0g9g/": RedditRef("1wz9oim", "pea0g9g"),
            "https://reddit.com/r/x/comments/1wz9oim/comment/pea0g9g/?context=3": RedditRef("1wz9oim", "pea0g9g"),
            "https://redd.it/1wz9oim": RedditRef("1wz9oim"),
            "https://www.reddit.com/gallery/1wz9rng": RedditRef("1wz9rng"),
            "t3_1wz9oim": RedditRef("1wz9oim"),
            "t1_pefmojb": RedditRef(comment_id="pefmojb"),
            "1WZ9OIM": RedditRef("1wz9oim"),
            "https://www.reddit.com/r/running/s/3TzXiyxaMD": RedditRef(share_path="/r/running/s/3TzXiyxaMD"),
        }
        for text, ref in cases.items():
            self.assertEqual(parse_reddit_ref(text), ref, text)

    def test_rejects_non_reddit(self):
        for bad in ("https://example.com/foo", "hello world", "https://v.redd.it/abc123"):
            with self.assertRaises(LinkError):
                parse_reddit_ref(bad)

    def test_clean_helpers(self):
        self.assertEqual(clean_comment_id("t1_PEA0G9G"), "pea0g9g")
        self.assertEqual(clean_comment_id("https://reddit.com/r/x/comments/abc12/comment/pea0g9g/"), "pea0g9g")
        with self.assertRaises(LinkError):
            clean_comment_id("not an id!")
        self.assertEqual(clean_name("r/ClaudeAI/", ("r/",)), "ClaudeAI")
        self.assertEqual(clean_name("u/spez", ("u/", "user/")), "spez")


class ShareLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_link_resolves_via_reddit_redirect(self):
        web, transport = replay_web("share_link")
        ref = await resolve_share_path("/r/running/s/3TzXiyxaMD", web)
        self.assertEqual(ref, RedditRef("17snc7d", "k8so54m"))
        self.assertEqual(transport.misses, [])

    async def test_unresolvable_share_link(self):
        web, _ = replay_web("share_link", overrides={"/s/nope": (200, "<html></html>")})
        with self.assertRaisesRegex(LinkError, "did not redirect"):
            await resolve_share_path("/r/x/s/nope", web)


if __name__ == "__main__":
    unittest.main()
