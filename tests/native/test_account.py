"""Offline tests: the linked account (session storage, logged-in reads over
the JSON API) and posting, against a fake reddit.com (httpx.MockTransport)."""

import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx
from mcp import Client

from reddit_native_mcp import auth, http, write
from reddit_native_mcp.http import API, WEB, Forbidden, NotLoggedIn, RedditError, RedditWeb, bucket_of
from reddit_native_mcp.server import mcp
from reddit_native_mcp.thread import fetch_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
MODHASH = "m" * 50


def session(name="tester"):
    return auth.Session(
        cookies=[{"name": "reddit_session", "value": "secret", "domain": ".reddit.com", "path": "/", "expires": 2e9}],
        username=name,
    )


def t1(cid, parent, body="hi", replies=None):
    return {"kind": "t1", "data": {
        "id": cid, "parent_id": parent, "link_id": "t3_p1", "author": "a", "body": body,
        "score": 1, "created_utc": 1.7e9,
        "replies": {"kind": "Listing", "data": {"children": replies}} if replies else "",
    }}


def more(parent, children, count=None):
    return {"kind": "more", "data": {"parent_id": parent, "children": children, "count": count or len(children)}}


POST = {"id": "p1", "title": "T", "subreddit": "test", "author": "tester", "permalink": "/r/test/comments/p1/t/",
        "num_comments": 9, "is_self": True, "selftext": "body", "created_utc": 1.7e9, "score": 5}


class FakeReddit:
    """Routes requests by path; records them. `routes` maps a path (or
    'METHOD path') to a dict/list (JSON 200), an (status, body) tuple, or a
    callable(request) -> httpx.Response."""

    def __init__(self, routes=None, me="tester"):
        self.requests: list[httpx.Request] = []
        self.routes = {
            "/api/me.json": {"data": {"name": me, "modhash": MODHASH, "link_karma": 3}},
            **(routes or {}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        route = self.routes.get(f"{request.method} {path}", self.routes.get(path))
        if route is None:
            return httpx.Response(404, text="no route", request=request)
        if callable(route):
            return route(request)
        if isinstance(route, tuple):
            return httpx.Response(route[0], text=route[1], request=request)
        return httpx.Response(200, json=route, request=request, headers={
            "x-ratelimit-remaining": "90", "x-ratelimit-reset": "300"})

    def calls(self, path):
        return [r for r in self.requests if r.url.path == path]

    def web(self, sess=None, **kw):
        return RedditWeb(transport=httpx.MockTransport(self), deadline=5.0,
                         session=session() if sess is None else sess, **kw)


def form(request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class SessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(os.environ["REDDIT_NATIVE_MCP_HOME"]) / "case" / "session.json"

    def test_round_trip_private_file(self):
        auth.save_session(session("bob"), self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        loaded = auth.load_session(self.path)
        self.assertEqual(loaded.username, "bob")
        self.assertEqual(loaded.cookie("reddit_session"), "secret")
        self.assertEqual(loaded.expires_at(), 2e9)
        self.assertTrue(auth.delete_session(self.path))
        self.assertIsNone(auth.load_session(self.path))
        self.assertFalse(auth.delete_session(self.path))

    def test_session_without_login_cookie_is_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"cookies": [{"name": "loid", "value": "x"}]}))
        self.assertIsNone(auth.load_session(self.path))

    def test_only_reddit_cookies_kept(self):
        raw = [{"name": "a", "value": "1", "domain": ".reddit.com"},
               {"name": "b", "value": "2", "domain": "www.reddit.com"},
               {"name": "c", "value": "3", "domain": "evilreddit.com"},
               {"name": "d", "value": "4", "domain": ".google.com"}]
        self.assertEqual([c["name"] for c in auth.reddit_cookies(raw)], ["a", "b"])

    def test_config_dir_comes_from_env_in_tests(self):
        self.assertTrue(str(auth.session_path()).startswith(os.environ["REDDIT_NATIVE_MCP_HOME"]))


class AccountClientTests(unittest.IsolatedAsyncioTestCase):
    def test_buckets(self):
        self.assertEqual(bucket_of("/comments/x.json"), API)
        self.assertEqual(bucket_of("/api/submit"), API)
        self.assertEqual(bucket_of("/r/x/api/selectflair"), API)
        self.assertEqual(bucket_of("/svc/shreddit/graphql"), WEB)
        self.assertEqual(bucket_of("/r/x/comments/y/"), WEB)

    async def test_cookie_sent_and_me_cached(self):
        fake = FakeReddit()
        web = fake.web()
        self.assertTrue(web.logged_in)
        me = await web.me()
        await web.me()
        self.assertEqual(me["name"], "tester")
        self.assertEqual(await web.modhash(), MODHASH)
        self.assertEqual(len(fake.calls("/api/me.json")), 1)
        self.assertIn("reddit_session=secret", fake.requests[0].headers["cookie"])
        self.assertEqual(web.budget_left(API), 90)
        self.assertIsNone(web.budget_left(WEB))

    async def test_anonymous_client_refuses_account_calls(self):
        web = RedditWeb(transport=httpx.MockTransport(FakeReddit()), deadline=5.0)
        self.assertFalse(web.logged_in)
        with self.assertRaises(NotLoggedIn):
            await web.me()
        with self.assertRaises(NotLoggedIn):
            await web.api_json("/api/info.json")

    async def test_rejected_session_vs_forbidden_community(self):
        fake = FakeReddit({"/r/secret/about.json": (403, "{}")})
        web = fake.web()
        with self.assertRaises(Forbidden):
            await web.api_json("/r/secret/about.json")
        self.assertTrue(web.logged_in)
        fake.routes["/api/me.json"] = (403, "{}")
        with self.assertRaises(NotLoggedIn):
            await web.api_json("/r/secret/about.json", fresh=True)
        self.assertFalse(web.logged_in)

    async def test_api_json_adds_raw_json(self):
        fake = FakeReddit({"/api/info.json": {"data": {"children": []}}})
        await fake.web().api_json("/api/info.json", params={"id": "t3_x"})
        self.assertEqual(fake.requests[-1].url.params["raw_json"], "1")

    async def test_rotated_cookies_saved_only_when_persisting(self):
        def me(request):
            return httpx.Response(200, json={"data": {"name": "tester"}}, request=request,
                                  headers={"set-cookie": "token_v2=new; Domain=.reddit.com; Path=/"})
        path = auth.session_path()
        auth.delete_session(path)
        await FakeReddit({"/api/me.json": me}).web().me()
        self.assertFalse(path.exists())
        await FakeReddit({"/api/me.json": me}).web(persist_session=True).me()
        saved = auth.load_session(path)
        self.assertEqual(saved.cookie("token_v2"), "new")
        self.assertEqual(saved.cookie("reddit_session"), "secret")
        self.assertEqual(saved.expires_at(), 2e9)  # expiry kept for unchanged cookies
        auth.delete_session(path)

    async def test_shared_client_follows_login_and_logout(self):
        path = auth.session_path()
        auth.delete_session(path)
        http._shared = None
        self.assertFalse(http.shared_client().logged_in)
        auth.save_session(session("carol"), path)
        web = http.shared_client()
        self.assertTrue(web.logged_in)
        self.assertIs(http.shared_client(), web)
        auth.delete_session(path)
        self.assertFalse(http.shared_client().logged_in)
        http._shared = None


class JsonThreadTests(unittest.IsolatedAsyncioTestCase):
    def thread_routes(self):
        listing = [
            {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": POST}]}},
            {"kind": "Listing", "data": {"children": [
                t1("c1", "t3_p1", replies=[t1("c2", "t1_c1"), more("t1_c1", ["c3"])]),
                t1("c4", "t3_p1", replies=[more("t1_c4", [], count=0)]),
                more("t3_p1", ["c5", "c6"]),
            ]}},
        ]
        thread_c4 = [listing[0], {"kind": "Listing", "data": {"children": [
            t1("c4", "t3_p1", replies=[t1("c7", "t1_c4", replies=[t1("c8", "t1_c7")])])]}}]

        def morechildren(request):
            ids = request.url.params["children"].split(",")
            things = []
            for i in ids:
                parent = "t1_c1" if i == "c3" else "t3_p1"
                things.append(t1(i, parent))
                if i == "c5":
                    things.append(t1("c9", "t1_c5"))
            return httpx.Response(200, json={"json": {"errors": [], "data": {"things": things}}}, request=request)

        return {"/comments/p1.json": listing, "/comments/p1/_/c4.json": thread_c4,
                "/api/morechildren.json": morechildren}

    async def test_logged_in_thread_uses_json_and_batches_morechildren(self):
        fake = FakeReddit(self.thread_routes())
        th = await fetch_thread(fake.web(), "p1", char_budget=None, max_requests=20)
        self.assertEqual(th.source, "json")
        got = {c["id"]: (c["depth"], c["parent_id"]) for c in th.comments}
        self.assertEqual(got, {
            "c1": (0, None), "c2": (1, "c1"), "c3": (1, "c1"), "c4": (0, None),
            "c7": (1, "c4"), "c8": (2, "c7"), "c5": (0, None), "c6": (0, None), "c9": (1, "c5"),
        })
        self.assertEqual(th.pending, [])
        mc = fake.calls("/api/morechildren.json")
        self.assertEqual(len(mc), 1)  # c3 and c5,c6 in one request
        self.assertEqual(set(mc[0].url.params["children"].split(",")), {"c3", "c5", "c6"})
        self.assertEqual(mc[0].url.params["link_id"], "t3_p1")
        self.assertEqual(th.post["body"], "body")

    async def test_focus_comment(self):
        fake = FakeReddit({"/comments/p1/_/c2.json": [
            {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": POST}]}},
            {"kind": "Listing", "data": {"children": [t1("c2", "t1_c1", replies=[t1("c10", "t1_c2")])]}},
        ]})
        th = await fetch_thread(fake.web(), "p1", focus="c2", char_budget=None, max_requests=5)
        self.assertEqual(th.focus_parent_id, "c1")
        self.assertEqual({c["id"]: c["depth"] for c in th.comments}, {"c2": 0, "c10": 1})
        self.assertEqual(fake.requests[-1].url.params["context"], "0")

    async def test_rejected_session_falls_back_to_web_path(self):
        fake = FakeReddit({"/comments/p1.json": (403, "{}"), "/api/me.json": (403, "{}")})
        web = fake.web()
        with self.assertRaises(RedditError):  # the fake has no web pages
            await fetch_thread(web, "p1", char_budget=None, max_requests=5)
        self.assertFalse(web.logged_in)
        self.assertTrue(any("/svc/" in r.url.path or "/comments/p1/" in r.url.path for r in fake.requests[2:]))


class UploadRoutes:
    """Lease + S3 + markdown conversion, as the composer does it."""

    def __init__(self):
        self.n = 0

    def gql(self, request):
        body = json.loads(request.content)
        assert body["csrf_token"] == "tok"
        if body["operation"] == "CreateMediaUploadLease":
            self.n += 1
            mid = f"media{self.n:05d}xx"
            return httpx.Response(200, request=request, json={"data": {"createMediaUploadLease": {
                "mediaId": mid, "uploadLease": {"uploadLeaseUrl": "https://s3.example/up",
                    "uploadLeaseHeaders": [{"header": "key", "value": mid}]}}}})
        if body["operation"] == "ConvertMarkdownToRTJSON":
            md = body["variables"]["markdown"]
            doc = {"document": [{"e": "par", "t": md}]}
            return httpx.Response(200, request=request, json={"data": {"convertedValue": json.dumps(json.dumps(doc))}})
        return httpx.Response(200, request=request, json={"errors": [{"message": "unknown op"}]})

    def s3(self, request):
        key = request.content.split(b'name="key"\r\n\r\n')[1].split(b"\r\n")[0].decode()
        return httpx.Response(201, request=request,
                              text=f"<PostResponse><Location>https://s3.example/{key}</Location></PostResponse>")

    def routes(self):
        return {"/svc/shreddit/graphql": self.gql, "/up": self.s3}


class WriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(os.environ["REDDIT_NATIVE_MCP_HOME"]) / "media"
        self.dir.mkdir(exist_ok=True)
        self.png = self.dir / "pic.png"
        self.png.write_bytes(PNG)

    def make(self, routes):
        up = UploadRoutes()
        fake = FakeReddit({**up.routes(), **routes})
        web = fake.web()
        web._client.cookies.set("csrf_token", "tok", domain=".reddit.com")
        return fake, web

    def test_target_and_types(self):
        self.assertEqual(write.target_sr("r/Python", "me"), ("Python", False))
        self.assertEqual(write.target_sr("profile", "Me"), ("u_Me", True))
        self.assertEqual(write.target_sr("u/me", "me"), ("u_me", True))
        with self.assertRaises(write.PostError):
            write.target_sr("u/someoneelse", "me")
        with self.assertRaises(write.PostError):
            write.target_sr("not a sub!", "me")
        self.assertEqual(write.sniff_type(PNG), "png")
        self.assertEqual(write.sniff_type(b"\xff\xd8\xff\xe0"), "jpeg")
        self.assertEqual(write.sniff_type(b"\0\0\0\x18ftypmp42"), "mp4")
        self.assertEqual(write.sniff_type(b"????", "x.webp"), "webp")
        self.assertIsNone(write.sniff_type(b"%PDF", "x.pdf"))

    async def test_text_post(self):
        fake, web = self.make({"/api/submit": {"json": {"errors": [], "data": {
            "url": "https://www.reddit.com/r/test/comments/abc/t/", "name": "t3_abc"}}}})
        out = await write.create_post(web, target="r/test", title="Hello", kind="text", body="plain *md*",
                                      flair_id="f1", spoiler=True)
        self.assertEqual(out["id"], "abc")
        req = fake.calls("/api/submit")[0]
        self.assertEqual(req.headers["x-modhash"], MODHASH)
        f = form(req)
        self.assertEqual((f["sr"], f["kind"], f["title"], f["text"], f["flair_id"], f["spoiler"]),
                         ("test", "self", "Hello", "plain *md*", "f1", "true"))

    async def test_reports_marks_reddit_forced(self):
        # an NSFW (18+) profile or community marks every post NSFW
        fake, web = self.make({
            "/api/submit": {"json": {"errors": [], "data": {"name": "t3_abc"}}},
            "/api/info.json": {"data": {"children": [{"kind": "t3", "data": {
                **POST, "id": "abc", "over_18": True, "spoiler": False}}]}},
        })
        out = await write.create_post(web, target="profile", title="Hi", kind="text", body="x")
        self.assertEqual((out["nsfw"], out["spoiler"]), (True, False))
        self.assertIn("marked NSFW (18+)", out["note"])
        # a failed read-back leaves the result without marks, not an error
        fake.routes["/api/info.json"] = (404, "gone")
        out = await write.create_post(web, target="profile", title="Hi", kind="text", body="x")
        self.assertEqual(out["id"], "abc")
        self.assertNotIn("nsfw", out)

    async def test_text_post_with_inline_images_goes_as_richtext(self):
        fake, web = self.make({"/api/submit": {"json": {"errors": [], "data": {"name": "t3_abc"}}}})
        out = await write.create_post(
            web, target="profile", title="Pics", kind="text",
            body=f'look ![a cat]({self.png}) and ![b](media00009xx "kept")',
        )
        self.assertEqual(out["uploaded_media"], 1)
        f = form(fake.calls("/api/submit")[0])
        self.assertEqual(f["sr"], "u_tester")
        self.assertNotIn("text", f)
        rt = json.loads(f["richtext_json"])
        self.assertEqual(rt["document"][0]["t"], 'look ![img](media00001xx "a cat") and ![img](media00009xx "kept")')
        s3 = fake.calls("/up")[0]
        self.assertNotIn("cookie", s3.headers)  # Reddit's cookies never go to S3

    async def test_gallery(self):
        fake, web = self.make({"/api/submit_gallery_post.json": {"json": {"errors": [], "data": {
            "id": "t3_gal", "url": "https://www.reddit.com/gallery/gal"}}}})
        out = await write.create_post(web, target="test", title="G", kind="gallery", media=[
            {"source": str(self.png), "caption": "one"},
            {"source": str(self.png), "outbound_url": "https://x.example"},
        ])
        self.assertEqual(out["id"], "gal")
        body = json.loads(fake.calls("/api/submit_gallery_post.json")[0].content)
        self.assertEqual([i["media_id"] for i in body["items"]], ["media00001xx", "media00002xx"])
        self.assertEqual(body["items"][0]["caption"], "one")
        self.assertEqual(body["items"][1]["outbound_url"], "https://x.example")
        self.assertIs(body["spoiler"], False)

    async def test_image_post_found_on_profile(self):
        submitted = {"data": {"children": [{"kind": "t3", "data": {
            "title": "Img", "created_utc": 4e9, "permalink": "/user/tester/comments/img1/img/"}}]}}
        fake, web = self.make({
            "/api/submit": {"json": {"errors": [], "data": {"user_submitted_page": "x"}}},
            "/user/tester/submitted.json": submitted,
        })
        out = await write.create_post(web, target="profile", title="Img", kind="image",
                                      media=[{"source": str(self.png)}])
        self.assertEqual(out["id"], "img1")
        f = form(fake.calls("/api/submit")[0])
        self.assertEqual((f["kind"], f["url"]), ("image", "https://s3.example/media00001xx"))

    async def test_link_post_and_validation(self):
        fake, web = self.make({"/api/submit": {"json": {"errors": [], "data": {"name": "t3_l"}}}})
        await write.create_post(web, target="test", title="L", kind="link", url="https://e.example", allow_repost=True)
        f = form(fake.calls("/api/submit")[0])
        self.assertEqual((f["kind"], f["url"], f["resubmit"]), ("link", "https://e.example", "true"))
        for kw in ({"kind": "link", "url": "ftp://x"}, {"kind": "gallery", "media": [{"source": str(self.png)}]},
                   {"kind": "image", "media": []}, {"kind": "text", "media": [{"source": "x"}]}):
            with self.assertRaises(write.PostError):
                await write.create_post(web, target="test", title="x", **kw)
        with self.assertRaises(write.PostError):
            await write.create_post(web, target="test", title="x" * 301, kind="text")
        self.assertEqual(len(fake.calls("/api/submit")), 1)

    async def test_reddit_errors_surface_and_writes_are_not_retried(self):
        fake, web = self.make({"/api/submit": {"json": {"errors": [["SUBREDDIT_NOEXIST", "that community doesn't exist", "sr"]]}}})
        with self.assertRaisesRegex(write.PostError, "SUBREDDIT_NOEXIST"):
            await write.create_post(web, target="nope", title="x", kind="text", body="b")
        fake.routes["/api/submit"] = (502, "bad gateway")
        with self.assertRaisesRegex(RedditError, "may have gone through"):
            await write.create_post(web, target="test", title="x", kind="text", body="b")
        self.assertEqual(len(fake.calls("/api/submit")), 2)

    async def test_edit_options_delete_own_post_only(self):
        info = {"id": "p1", "author": "tester", "subreddit": "test", "title": "T", "selftext": "old",
                "over_18": False, "spoiler": False, "send_replies": True, "permalink": "/r/test/comments/p1/t/"}
        ok = {"json": {"errors": []}}
        fake, web = self.make({"/api/info.json": {"data": {"children": [{"kind": "t3", "data": info}]}},
                               "/api/editusertext": ok, "/api/marknsfw": {}, "/api/sendreplies": {},
                               "/r/test/api/selectflair": ok, "/api/del": {}})
        await write.edit_post(web, "p1", "new body")
        self.assertEqual(form(fake.calls("/api/editusertext")[0]),
                         {"api_type": "json", "thing_id": "t3_p1", "text": "new body"})
        rec = await write.set_post_options(web, "p1", nsfw=True, spoiler=False, send_replies=False, flair_id="f9")
        self.assertEqual(rec["changed"], ["nsfw", "send_replies", "flair"])
        self.assertEqual(form(fake.calls("/api/sendreplies")[0])["state"], "false")
        self.assertEqual(form(fake.calls("/r/test/api/selectflair")[0])["flair_template_id"], "f9")
        out = await write.delete_post(web, "p1")
        self.assertTrue(out["deleted"])
        self.assertEqual(form(fake.calls("/api/del")[0]), {"id": "t3_p1"})
        info["author"] = "someone"
        with self.assertRaisesRegex(write.PostError, "not the linked account"):
            await write.delete_post(web, "p1")
        self.assertEqual(len(fake.calls("/api/del")), 1)

    async def test_requirements(self):
        fake, web = self.make({
            "/r/test/about.json": {"data": {"display_name": "test", "subreddit_type": "public",
                "submission_type": "any", "allow_images": True, "allow_galleries": True,
                "allow_videos": False, "submit_text": "Be nice", "link_flair_enabled": True,
                "can_assign_link_flair": True}},
            "/r/test/about/rules.json": {"rules": [{"short_name": "No spam", "kind": "all", "description": "d"}]},
            "/r/test/api/link_flair_v2.json": [{"id": "f1", "text": "News", "text_editable": False, "mod_only": False}],
        })
        rec = await write.post_requirements(web, "r/test")
        self.assertEqual(rec["post_types"], ["text", "link", "image", "gallery"])
        self.assertTrue(rec["can_post"])
        self.assertEqual(rec["rules"][0]["name"], "No spam")
        self.assertEqual(rec["flairs"][0]["id"], "f1")
        self.assertEqual(rec["posting_guidelines"], "Be nice")


class AccountToolTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, web, tool, args):
        with mock.patch("reddit_native_mcp.server.shared_client", return_value=web):
            async with Client(mcp) as client:
                result = await client.call_tool(tool, args)
        return result.is_error, result.content[0].text

    async def test_write_tool_annotations(self):
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("create_post", "edit_post", "set_post_options", "delete_post"):
            self.assertFalse(tools[name].annotations.read_only_hint, name)
        self.assertTrue(tools["delete_post"].annotations.destructive_hint)
        self.assertTrue(tools["account_status"].annotations.read_only_hint)

    async def test_posting_without_account_explains_login(self):
        web = RedditWeb(transport=httpx.MockTransport(FakeReddit()), deadline=5.0)
        err, text = await self.call(web, "create_post", {"subreddit": "profile", "title": "x", "body": "y"})
        self.assertTrue(err)
        self.assertIn("reddit-native-mcp login", text)
        err, text = await self.call(web, "account_status", {})
        self.assertFalse(err)
        self.assertFalse(json.loads(text)["linked"])

    async def test_account_status_linked(self):
        err, text = await self.call(FakeReddit().web(), "account_status", {})
        data = json.loads(text)
        self.assertEqual((data["linked"], data["account"]), (True, "tester"))
        self.assertIn("json_api", data["rate_budget"])
        self.assertNotIn("secret", text)
