"""Offline tests: find_similar query planning."""

import unittest

from reddit_native_mcp.similar import external_links, post_terms, queries, singular, title_terms


class TermTests(unittest.TestCase):
    def test_title_terms_prefer_proper_nouns_and_singularise(self):
        t = "Anthropic is giving startups a free year of Claude Team, $1,000 in credits and up to $45,000 in perks"
        self.assertEqual(title_terms(t), ["claude team", "anthropic", "startup", "credit"])

    def test_phrase_absorbs_its_words(self):
        t = "I asked Claude to make music, then asked Claude Code to make a music video"
        terms = title_terms(t)
        self.assertIn("claude code", terms)
        self.assertNotIn("claude", terms)
        self.assertNotIn("asked", terms)

    def test_body_tops_up_weak_titles(self):
        post = {"title": "Thank you Anthropic!", "body": "Just applied to their newly announced Startup Program https://x.com/a"}
        terms = post_terms(post)
        self.assertEqual(terms[:2], ["anthropic", "thank"])
        self.assertIn("startup program", terms)

    def test_singular(self):
        self.assertEqual([singular(w) for w in ["startups", "class", "status", "basis", "cats"]],
                         ["startup", "class", "status", "basis", "cats"])

    def test_queries_are_pairs_plus_top_three(self):
        q = queries(["a1", "b2", "c3", "d4"])
        self.assertEqual(len(q), 7)
        self.assertEqual(q[0], "a1 b2")
        self.assertEqual(q[-1], "a1 b2 c3")
        self.assertEqual(queries(["solo"]), ["solo"])

    def test_external_links(self):
        post = {
            "link_url": "https://www.madrobot.blog/2026/x/?utm_source=a",
            "body": "see https://claude.com/programs/startups. and https://i.redd.it/a.png and https://www.reddit.com/r/x",
        }
        self.assertEqual(external_links(post), ["madrobot.blog/2026/x", "claude.com/programs/startups"])


if __name__ == "__main__":
    unittest.main()
