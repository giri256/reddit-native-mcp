"""Offline tests: HTML extraction (recorded reddit.com pages + small snippets)."""

import unittest

from native.replay import page_body
from reddit_native_mcp.parse import iso_time, parse, slim

POST_PAGE = "GET https://www.reddit.com/r/ClaudeAI/comments/1wz9oim/anthropic"


class ThreadPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = parse(page_body("thread_1wz9oim", POST_PAGE))

    def test_post_fields(self):
        (post,) = self.page.posts
        self.assertEqual(post["id"], "1wz9oim")
        self.assertEqual(post["subreddit"], "ClaudeAI")
        self.assertEqual(post["author"], "ross2000")
        self.assertTrue(post["title"].startswith("Anthropic is giving startups"))
        self.assertIn("madrobot.blog", post["link_url"])  # external link kept
        self.assertEqual(post["post_type"], "link")
        self.assertEqual(post["flair"], "News")
        self.assertGreater(post["score"], 1000)  # live score, not a 0/1 snapshot
        self.assertGreater(post["num_comments"], 200)
        self.assertRegex(post["created"], r"^2026-10-06T18:08:06Z$")

    def test_first_comments_and_tree_links(self):
        page = self.page
        self.assertEqual(len(page.comments), 25)
        self.assertGreater(page.total_comments, 200)
        ids = {c["id"] for c in page.comments}
        for c in page.comments:  # every parent is either top level or on the page
            self.assertTrue(c["parent_id"] is None or c["parent_id"] in ids, c)
            self.assertTrue(c["body"] or c.get("deleted"), c["id"])
        top = [m for m in page.more if m.top_level]
        self.assertEqual(len(top), 1)
        self.assertGreater(top[0].count, 50)  # comments-remaining
        nested = [m for m in page.more if not m.top_level]
        self.assertTrue(all(m.cursor and m.parent_id in ids for m in nested if m.kind == "cursor"))

    def test_comment_flags(self):
        by_id = {c["id"]: c for c in self.page.comments}
        mod = by_id["pea780s"]
        self.assertEqual(mod["distinguished"], "moderator")
        self.assertEqual(mod["author"], "ClaudeAI-mod-bot")
        self.assertTrue(mod["body"].startswith("**TL;DR"))
        self.assertEqual(by_id["pea489j"]["parent_id"], "pea0g9g")
        self.assertEqual(by_id["pea489j"]["depth"], 1)


class MarkdownTests(unittest.TestCase):
    def body(self, inner: str) -> str:
        html = (
            '<shreddit-comment thingId="t1_abc" author="u" depth="0" score="3" '
            'created="2026-10-06T19:25:01.400000+0000">'
            f'<div id="t1_abc-comment-rtjson-content">{inner}</div></shreddit-comment>'
        )
        return parse(html).comments[0]["body"]

    def test_paragraphs_lists_quotes_code(self):
        md = self.body(
            "<p>Hello <strong>world</strong> &amp; <em>you</em></p>"
            "<ul><li><p>one</p></li><li><p>two <code>x()</code></p></li></ul>"
            "<blockquote><p>quoted</p></blockquote>"
            "<pre><code>a = 1\n  b = 2</code></pre>"
            "<ol><li>first</li><li>second</li></ol>"
        )
        self.assertEqual(
            md,
            "Hello **world** & *you*\n\n- one\n- two `x()`\n\n> quoted\n\n"
            "```\na = 1\n  b = 2\n```\n\n1. first\n2. second",
        )

    def test_links_images_and_nested_divs(self):
        md = self.body(
            '<p>See <a href="https://claude.com/x">the page</a> and '
            '<a href="https://example.com">https://example.com</a></p>'
            '<div><div><p>inner</p></div></div>'
            '<figure><a href="https://i.redd.it/full.png"><img src="https://i.redd.it/a.png"></a></figure>'
            "<p>after</p>"
        )
        self.assertEqual(
            md,
            "See [the page](https://claude.com/x) and https://example.com\n\ninner\n\n"
            "[image](https://i.redd.it/a.png)\n\nafter",
        )

    def test_script_svg_ignored_and_entities(self):
        md = self.body("<p>a<svg><path d='x'/></svg> &lt;b&gt; &#39;c&#39;</p><script>bad()</script>")
        self.assertEqual(md, "a <b> 'c'")


class ContinuationAndTombstoneTests(unittest.TestCase):
    def test_permalink_twin_dropped_counted_continuation_kept(self):
        def comment(cid, inner):
            return (
                f'<shreddit-comment thingId="t1_{cid}" author="u" depth="4" score="1">'
                f'<div id="t1_{cid}-comment-rtjson-content"><p>{cid}</p></div>{inner}</shreddit-comment>'
            )

        partial = (
            '<faceplate-partial src="/svc/shreddit/more-comments/x/t3_p?startingDepth=5" method="post">'
            '<input type="hidden" name="cursor" value="CUR"><faceplate-number number="2"></faceplate-number>'
            "</faceplate-partial>"
        )
        twin = '<a href="/r/x/comments/p/comment/aaa/?force-legacy-sct=1" slot="more-comments-permalink"><faceplate-number number="2"></faceplate-number> more replies</a>'
        deep = '<a href="/r/x/comments/p/comment/bbb/?force-legacy-sct=1" slot="more-comments-permalink"><faceplate-number number="3"></faceplate-number> more replies</a>'
        empty = '<a href="/r/x/comments/p/comment/ccc/" slot="more-comments-permalink">More replies</a>'
        page = parse(comment("aaa", partial + twin) + comment("bbb", deep) + comment("ccc", empty))
        kinds = [(m.kind, m.parent_id, m.count) for m in page.more]
        self.assertEqual(kinds, [("cursor", "aaa", 2), ("permalink", "bbb", 3)])
        self.assertEqual(page.more[1].src, "/r/x/comments/p/comment/bbb/")
        self.assertEqual(page.more[0].cursor, "CUR")

    def test_tombstones_without_ids_keep_their_replies(self):
        html = (
            '<shreddit-comment thingId="t1_deleted" author="[deleted]" depth="0" is-comment-deleted>'
            '<shreddit-comment thingId="t1_kid1" author="a" depth="1" score="2">'
            '<div id="t1_kid1-comment-rtjson-content"><p>hi</p></div></shreddit-comment>'
            "</shreddit-comment>"
            '<shreddit-comment thingId="t1_deleted" author="[deleted]" depth="0" is-comment-deleted></shreddit-comment>'
        )
        c = parse(html).comments
        self.assertEqual([x["id"] for x in c], ["deleted-1", "kid1", "deleted-2"])
        self.assertEqual(c[1]["parent_id"], "deleted-1")
        self.assertTrue(c[0]["deleted"])


class FeedAndSearchTests(unittest.TestCase):
    def test_feed_page(self):
        page = parse(page_body("feed_claudeai", "community-more-posts/new/?name=ClaudeAI -"))
        self.assertGreaterEqual(len(page.posts), 20)
        self.assertIn("after=", page.feed_next)
        self.assertTrue(all(p["subreddit"] == "ClaudeAI" for p in page.posts))
        self.assertTrue(any(p.get("body") for p in page.posts))  # feeds carry selftext
        self.assertTrue(any(p.get("flair") for p in page.posts))

    def test_search_page(self):
        page = parse(page_body("search_startup_program", "svc/shreddit/search/?q=startup"))
        self.assertGreaterEqual(len(page.search), 5)
        hit = page.search[0]
        for key in ("id", "subreddit", "author", "title", "permalink", "created", "score", "num_comments"):
            self.assertIsNotNone(hit.get(key), key)
        self.assertIn("cursor=", page.search_next)


class HelperTests(unittest.TestCase):
    def test_iso_time(self):
        self.assertEqual(iso_time("2026-10-06T19:25:01.400000+0000"), "2026-10-06T19:25:01Z")
        self.assertIsNone(iso_time("garbage"))

    def test_slim_keeps_templates_and_drops_noise(self):
        html = (
            "<style>.a{}</style><script>x<y</script><!-- c - d -->"
            "<template><shreddit-comment thingId='t1_k' depth='0'></shreddit-comment></template>"
            "<svg><path/></svg>"
        )
        out = slim(html)
        self.assertNotIn("script", out)
        self.assertNotIn("style", out)
        self.assertNotIn("svg", out)
        self.assertNotIn("<!--", out)
        self.assertEqual(parse(html).comments[0]["id"], "k")


if __name__ == "__main__":
    unittest.main()
