"""Offline tests: records, comment-tree budgeting, keyword helpers."""

import unittest

from reddit_native_mcp.parse import MoreLink
from reddit_native_mcp.shape import (
    compact_comment,
    compact_post,
    compact_tree,
    dumps,
    keyword_hits,
    keyword_pattern,
    normalize_keywords,
)


def c(cid, parent=None, depth=0, score=1, ts=0, body="text " * 10, **flags):
    return {
        "id": cid, "parent_id": parent, "depth": depth, "author": "u", "score": score,
        "created": "2026-10-06T00:00:00Z", "created_ts": ts, "body": body, **flags,
    }


COMMENTS = [
    c("a", score=5, ts=3),
    c("a1", "a", 1, ts=4),
    c("a2", "a1", 2, ts=5),
    c("b", score=9, ts=1),
    c("d", score=1, ts=2, deleted=True, body=""),
]


class TreeTests(unittest.TestCase):
    def test_full_tree_reddit_order(self):
        tree, stats = compact_tree(COMMENTS, [])
        self.assertEqual([n["id"] for n in tree], ["a", "b", "d"])  # document order
        self.assertEqual(tree[0]["replies"][0]["replies"][0]["id"], "a2")
        self.assertEqual(stats["returned_comments"], 5)
        self.assertEqual(stats["omitted_comments"], 0)
        self.assertEqual(stats["max_depth_returned"], 2)
        self.assertEqual(tree[2]["body"], "[deleted]")
        self.assertTrue(tree[2]["deleted"])

    def test_sorts(self):
        top, _ = compact_tree(COMMENTS, [], sort="top")
        self.assertEqual([n["id"] for n in top], ["b", "a", "d"])
        new, _ = compact_tree(COMMENTS, [], sort="new")
        self.assertEqual([n["id"] for n in new], ["a", "d", "b"])  # created_ts 3, 2, 1

    def test_more_links_become_more_replies(self):
        more = [MoreLink("/m1", "c1", "a", 4), MoreLink("/top", "c2", None, 50), MoreLink("/m2", "", "b", None, kind="permalink")]
        tree, stats = compact_tree(COMMENTS, more)
        by = {n["id"]: n for n in tree}
        self.assertEqual(by["a"]["more_replies"], 4)
        self.assertEqual(by["b"]["more_replies"], 1)  # unknown count counts as 1
        self.assertEqual(stats["unloaded_top_level"], 50)
        self.assertEqual(stats["known_comments"], 5 + 4 + 1 + 50)

    def test_budget_keeps_best_first_and_counts_hidden(self):
        node = len(dumps(compact_comment(COMMENTS[0], 0))) + 32  # record + nesting overhead
        tree, stats = compact_tree(COMMENTS, [], budget_chars=node * 2 + 5, sort="top")
        self.assertEqual([n["id"] for n in tree], ["b", "a"])
        self.assertEqual(tree[1]["more_replies"], 2)  # a1 + a2 hidden
        self.assertEqual(stats["omitted_top_level_ids"], ["d"])
        self.assertLessEqual(len(dumps(tree)), node * 2 + 5)

    def test_max_depth_and_rebased_focus(self):
        tree, stats = compact_tree(COMMENTS, [], max_depth=1)
        self.assertEqual(tree[0]["replies"][0]["more_replies"], 1)
        self.assertEqual(stats["max_depth_returned"], 1)
        focus = [c("a1", None, 1), c("a2", "a1", 2)]
        tree, _ = compact_tree(focus, [], root_depth=1)
        self.assertEqual(tree[0]["depth"], 0)
        self.assertEqual(tree[0]["replies"][0]["depth"], 1)


class PostTests(unittest.TestCase):
    def test_compact_post(self):
        raw = {
            "id": "p1", "subreddit": "x", "author": "op", "created": "t", "title": "T",
            "permalink": "/r/x/comments/p1/t/", "link_url": "https://ex.com", "post_type": "link",
            "score": 3, "upvote_ratio": 0.987654, "num_comments": 2, "body": "b" * 500, "nsfw": True,
        }
        full = compact_post(raw)
        self.assertEqual(full["url"], "https://www.reddit.com/r/x/comments/p1/t/")
        self.assertEqual(full["upvote_ratio"], 0.988)
        self.assertEqual(len(full["selftext"]), 500)
        self.assertTrue(full["nsfw"])
        short = compact_post(raw, preview_chars=100)
        self.assertTrue(short["selftext"].endswith("… [+400 chars]"))
        self.assertNotIn("spoiler", short)


class KeywordTests(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(keyword_pattern("startup").search("Startups rejoice"))
        self.assertFalse(keyword_pattern("team").search("steam deck"))
        self.assertFalse(keyword_pattern("ai").search("aim high"))
        self.assertTrue(keyword_pattern("claude team").search("Claude-Team plan"))
        self.assertEqual(normalize_keywords(" A b, a  B ,c"), ["a b", "c"])
        pats = [(k, keyword_pattern(k)) for k in ["startup program", "free"]]
        self.assertEqual(
            keyword_hits({"title": "Free stuff", "body": "the startup program"}, pats),
            (["startup program", "free"], 1),
        )


if __name__ == "__main__":
    unittest.main()
