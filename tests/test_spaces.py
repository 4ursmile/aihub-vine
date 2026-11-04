import io
import json
import os
import re
import unittest
import zipfile

from tests.test_hub_first import admin
from tests.test_robust import client

API = "/api/v1"
DECK = ('<html><head><title>Q3</title><style>.slide{width:100vw}</style><meta name="aihub-slide-size" content="1280x720"></head><body>'
        '<section class="slide" data-transition="zoom"><h1>One</h1><script>alert(1)</script><aside class="notes">say hi</aside></section>'
        '<section class="slide"><h1 onclick="x()">Two</h1><img src="data:image/png;base64,AA"></section></body></html>')


def pptx():
    ns = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    sl = ('<p:sld %s><p:cSld><p:spTree><p:sp><p:spPr><a:xfrm><a:off x="952500" y="952500"/><a:ext cx="9525000" cy="952500"/></a:xfrm></p:spPr>'
          '<p:txBody><a:p><a:r><a:rPr sz="3200" b="1"/><a:t>Hello &lt;b&gt;</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>'
          '<p:transition><p:push dir="l"/></p:transition></p:sld>' % ns)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("ppt/presentation.xml", '<p:presentation %s><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>' % ns)
        z.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="slides/slide1.xml" Type="x"/></Relationships>')
        z.writestr("ppt/slides/slide1.xml", sl)
    return b.getvalue()


def docx():
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="%s"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Title &lt;script&gt;</w:t></w:r></w:p>'
                   '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r></w:p></w:body></w:document>' % w)
    return b.getvalue()


def site():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.html", "<h1>report</h1><script src=app.js></script>")
        z.writestr("app.js", "1")
    return b.getvalue()


class Spaces(unittest.TestCase):
    def setUp(self):
        self.c = client()
        self.h = admin(self.c)

    def pub(self, data, filename, h=None, **q):
        q["filename"] = filename
        return self.c.post(API + "/spaces", params=q, content=data, headers=h or self.h)

    def test_slides_from_html_strip_scripts_and_keep_transitions(self):
        r = self.pub(DECK.encode(), "q3.html")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["space"]["kind"], "slides")
        self.assertEqual(r.json()["space"]["ref"], "root@q3")
        d = self.c.get(API + "/spaces/root@q3/data", headers=self.h).json()
        self.assertEqual((d["w"], d["h"], len(d["slides"])), (1280, 720, 2))
        self.assertEqual(d["slides"][0]["tr"], "zoom")
        self.assertEqual(d["slides"][0]["notes"], "say hi")
        blob = json.dumps(d)
        self.assertNotIn("alert(1)", blob)
        self.assertNotIn("onclick", blob)

    def test_pptx_becomes_a_deck_with_transition_and_escaped_text(self):
        r = self.pub(pptx(), "deck.pptx", view="fit", transition="fade")
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()["space"]
        self.assertEqual((s["kind"], s["settings"]["view"], s["settings"]["transition"]), ("slides", "fit", "fade"))
        d = self.c.get(API + "/spaces/root@deck/data", headers=self.h).json()
        self.assertEqual((d["w"], d["h"]), (1280, 720))
        self.assertEqual(d["slides"][0]["tr"], "slide")
        self.assertIn("Hello &lt;b&gt;", d["slides"][0]["html"])

    def test_docx_and_markdown_are_escaped_html(self):
        self.pub(docx(), "notes.docx")
        h = self.c.get(API + "/spaces/root@notes/data", headers=self.h).json()["html"]
        self.assertIn("<h1>Title &lt;script&gt;</h1>", h)
        self.pub(b"# Hi\n<script>x</script>", "readme.md")
        self.assertNotIn("<script>", self.c.get(API + "/spaces/root@readme/data", headers=self.h).json()["html"])

    def test_sheet_csv(self):
        self.pub(b"a,b\n1,2\n", "t.csv")
        d = self.c.get(API + "/spaces/root@t/data", headers=self.h).json()
        self.assertEqual(d["sheets"][0]["rows"], [["a", "b"], ["1", "2"]])

    def test_html_site_is_served_sandboxed_through_a_signed_link(self):
        self.assertEqual(self.pub(site(), "rep.zip", kind="dashboard").json()["space"]["kind"], "dashboard")
        url = self.c.get(API + "/spaces/root@rep/viewurl", headers=self.h).json()["url"]
        r = self.c.get(url)                                               # no sign-in header: the signature is the key
        self.assertEqual(r.status_code, 200)
        self.assertIn("sandbox", r.headers["content-security-policy"])
        self.assertEqual(self.c.get(url.replace("index.html", "app.js")).status_code, 200)
        self.assertEqual(self.c.get(url.replace("index.html", "../../../x")).status_code, 404)
        bad = re.sub(r"/v/\d+\.", "/v/1.", url)
        self.assertEqual(self.c.get(bad).status_code, 403)

    def test_only_a_hub_admin_lets_an_html_space_load_other_sites(self):
        self.c.post(API + "/auth/register", json={"username": "nina", "password": "secret1"})
        nina = {"Authorization": "Bearer " + self.c.post(API + "/auth/login", json={"username": "nina", "password": "secret1"}).json()["token"]}
        self.assertEqual(self.pub(site(), "own.zip", h=nina, kind="html").status_code, 200)
        csp = lambda: self.c.get(self.c.get(API + "/spaces/nina@own/viewurl", headers=nina).json()["url"]).headers["content-security-policy"]
        self.assertIn("frame-src 'none'", csp())
        self.assertFalse(self.c.get(API + "/spaces/nina@own", headers=nina).json()["can_grant_net"])
        r = self.c.patch(API + "/spaces/nina@own", json={"net_origins": ["https://x.example"]}, headers=nina)   # the owner may not
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.patch(API + "/spaces/nina@own", json={"net_origins": ["https://x.example/path"]}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.patch(API + "/spaces/nina@own", json={"net_origins": ["javascript:alert(1)"]}, headers=self.h).status_code, 400)
        r = self.c.patch(API + "/spaces/nina@own", json={"net_origins": "https://Grafana.Example.com/, https://*.corp.example"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["net_origins"], ["https://grafana.example.com", "https://*.corp.example"])
        self.assertTrue(r.json()["can_grant_net"])
        c = csp()
        self.assertIn("frame-src https://grafana.example.com https://*.corp.example", c)
        self.assertIn("wss://grafana.example.com", c)
        self.assertIn("form-action https://grafana.example.com", c)
        self.assertIn("sandbox allow-scripts", c)
        self.assertNotIn("allow-same-origin", c)
        self.assertEqual(self.c.patch(API + "/spaces/nina@own", json={"title": "t"}, headers=nina).status_code, 200)   # editing without the field is fine
        self.assertEqual(self.c.patch(API + "/spaces/nina@own", json={"net_origins": []}, headers=self.h).json()["net_origins"], [])
        self.assertIn("connect-src 'none'", csp())

    def test_private_space_is_hidden_until_shared_and_edit_can_republish(self):
        self.pub(b"x,y\n", "s.csv", visibility="private")
        self.assertEqual(self.c.get(API + "/spaces/root@s").status_code, 404)
        self.assertEqual(self.c.get(API + "/spaces").json()["total"], 0)
        self.assertEqual(self.c.get(API + "/spaces/root@s/open").status_code, 404)
        self.c.post(API + "/auth/register", json={"username": "bob", "password": "secret1"})
        bob = {"Authorization": "Bearer " + self.c.post(API + "/auth/login", json={"username": "bob", "password": "secret1"}).json()["token"]}
        self.assertEqual(self.c.get(API + "/spaces/root@s", headers=bob).status_code, 404)
        self.assertEqual(self.c.put(API + "/spaces/root@s/access", json={"type": "user", "name": "bob", "access": "view"}, headers=self.h).status_code, 200)
        self.assertEqual(self.c.get(API + "/spaces/root@s", headers=bob).json()["level"], "view")
        self.assertEqual(self.c.put(API + "/spaces/root@s/content", content=b"a\n", params={"filename": "s.csv"}, headers=bob).status_code, 403)
        self.c.put(API + "/spaces/root@s/access", json={"type": "user", "name": "bob", "access": "edit"}, headers=self.h)
        r = self.c.put(API + "/spaces/root@s/content", content=b"a\n", params={"filename": "s.csv"}, headers=bob)
        self.assertEqual((r.status_code, r.json()["space"]["rev"]), (200, 2))
        self.assertEqual(self.c.patch(API + "/spaces/root@s", json={"visibility": "public"}, headers=bob).status_code, 403)
        self.assertEqual(self.c.delete(API + "/spaces/root@s", headers=bob).status_code, 403)
        self.c.delete(API + "/spaces/root@s/access/user/bob", headers=self.h)
        self.assertEqual(self.c.get(API + "/spaces/root@s", headers=bob).status_code, 404)

    def test_bulk_share_is_all_or_none_and_the_single_form_still_works(self):
        self.pub(b"x,y\n", "s.csv", visibility="private")
        for n in ("bob", "carl"):
            self.c.post(API + "/auth/register", json={"username": n, "password": "secret1"})
        url = API + "/spaces/root@s/access"
        bad = self.c.put(url, json={"items": [{"type": "user", "name": "bob"}, {"type": "user", "name": "nobody"}], "access": "view"}, headers=self.h)
        self.assertEqual(bad.status_code, 404)
        self.assertEqual(self.c.get(url, headers=self.h).json()["access"], [])
        ok = self.c.put(url, json={"items": [{"type": "user", "name": "bob"}, {"type": "user", "name": "carl"}], "access": "view"}, headers=self.h)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(sorted(x["name"] for x in self.c.get(url, headers=self.h).json()["access"]), ["bob", "carl"])
        self.assertEqual(self.c.put(url, json={"type": "user", "name": "bob", "access": "edit"}, headers=self.h).status_code, 200)
        self.assertEqual(self.c.put(url, json={"type": "team", "name": "bob", "access": "view"}, headers=self.h).status_code, 400)
        self.assertEqual(self.c.get(API + "/groups/lookup?limit=500", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get(API + "/groups/lookup", headers=self.h).status_code, 200)

    def test_overview_search_views_and_validation(self):
        self.pub(DECK.encode(), "q3.html", visibility="public", title="Quarterly")
        self.c.post(API + "/spaces/root@q3/view")
        self.assertEqual(self.c.get(API + "/spaces/root@q3").json()["views"], 1)
        self.c.post(API + "/spaces/root@q3/view")
        self.assertEqual(self.c.get(API + "/spaces/root@q3").json()["views"], 1)      # same visitor, same half hour
        o = self.c.get(API + "/spaces/overview").json()
        self.assertEqual((o["carousel"][0]["ref"], o["kinds"]), ("root@q3", {"slides": 1}))
        self.assertEqual(self.c.get(API + "/spaces", params={"q": "quarter"}).json()["total"], 1)
        self.assertEqual(self.c.get(API + "/spaces", params={"q": "nope"}).json()["total"], 0)
        self.assertEqual(self.pub(b"x", "a.exe").status_code, 400)
        self.assertEqual(self.pub(b"x,y", "a.csv", view="huge").status_code, 400)
        self.assertEqual(self.pub(b"x,y", "a.csv", h={"X-Anon": "1"}).status_code, 401)
        self.assertEqual(self.pub(b"x,y", "a.csv", kind="slides").status_code, 400)


    def test_since_lists_changes_oldest_first_and_a_write_is_seen_at_once(self):
        for n in ("a1", "a2"):
            self.assertEqual(self.pub(b"# hi", n + ".md").status_code, 200)
        t0 = self.c.get(API + "/spaces", params={"since": 0}, headers=self.h).json()["items"]
        self.assertEqual([x["ref"] for x in t0[-2:]], ["root@a1", "root@a2"])            # oldest change first
        cur = t0[-1]["updated"]
        self.assertEqual([x["ref"] for x in self.c.get(API + "/spaces", params={"since": cur}, headers=self.h).json()["items"]], ["root@a2"])
        self.assertEqual(self.c.patch(API + "/spaces/root@a1", json={"title": "New"}, headers=self.h).status_code, 200)
        got = self.c.get(API + "/spaces", params={"since": cur}, headers=self.h).json()["items"]   # same key, cached: the write dropped it
        self.assertEqual([x["ref"] for x in got], ["root@a2", "root@a1"])


def login(c, name):
    c.post(API + "/auth/register", json={"username": name, "password": "secret1"})
    return {"Authorization": "Bearer " + c.post(API + "/auth/login", json={"username": name, "password": "secret1"}).json()["token"]}


class PgRows:
    """A connection whose rows behave like the Postgres layer's `db.Row`: iterating a row gives its KEYS (indexing works as usual)."""
    def __init__(self, c):
        self.c = c

    def execute(self, sql, args=()):
        from aihub.server.db import Row
        res = self.c.execute(sql, args)
        wrap = lambda r: Row(dict(r)) if r is not None and hasattr(r, "keys") else r

        class Res:
            lastrowid = res.lastrowid
            rowcount = getattr(res, "rowcount", -1)

            def fetchone(_):
                return wrap(res.fetchone())

            def fetchall(_):
                return [wrap(r) for r in res.fetchall()]

            def __iter__(_):
                return iter([wrap(r) for r in res.fetchall()])
        return Res()

    def __getattr__(self, n):
        return getattr(self.c, n)


class Browse(unittest.TestCase):
    """The Spaces browser: exact tag filter, shared-with-me scope, grouping, stable pages and the filter-rail counts."""
    def setUp(self):
        self.c = client()
        self.h = admin(self.c)
        self.bob = login(self.c, "bob")

    def pub(self, name, h=None, data=b"# hi", ext="md", **q):
        q["filename"] = name + "." + ext
        r = self.c.post(API + "/spaces", params=q, content=data, headers=h or self.h)
        self.assertEqual(r.status_code, 200, r.text)

    def get(self, path, h=None, **q):
        r = self.c.get(API + path, params=q, headers=h or self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_tag_is_exact_and_group_keeps_rows_together_and_pages_do_not_overlap(self):
        self.pub("a", tags="q3,finance")
        self.pub("b", tags="q3-draft", ext="csv", data=b"x,y\n1,2")
        self.pub("c", tags="a_b", h=self.bob)
        self.pub("d", tags="axb", h=self.bob)
        self.assertEqual([x["slug"] for x in self.get("/spaces", tag="q3")["items"]], ["a"])            # not q3-draft
        self.assertEqual([x["slug"] for x in self.get("/spaces", tag="a_b")["items"]], ["c"])           # _ is not a wildcard
        kinds = [x["kind"] for x in self.get("/spaces", group="kind", sort="name")["items"]]
        self.assertEqual(kinds, sorted(kinds))
        owners = [x["owner"] for x in self.get("/spaces", group="owner")["items"]]
        self.assertEqual(owners, sorted(owners))
        seen = []
        for off in range(0, 4):
            seen += [x["ref"] for x in self.get("/spaces", sort="new", limit=1, offset=off)["items"]]
        self.assertEqual(len(set(seen)), 4)
        self.assertEqual(self.c.get(API + "/spaces", params={"group": "nope"}).status_code, 400)

    def test_shared_scope_lists_only_what_others_shared_with_me(self):
        self.pub("pub")
        self.pub("priv", visibility="private")
        self.pub("mine", h=self.bob)
        self.assertEqual(self.get("/spaces", h=self.bob, shared="true")["total"], 0)               # public spaces are not 'shared'
        self.c.put(API + "/spaces/root@priv/access", json={"type": "user", "name": "bob", "access": "view"}, headers=self.h)
        self.assertEqual([x["ref"] for x in self.get("/spaces", h=self.bob, shared="true")["items"]], ["root@priv"])
        self.assertEqual([x["ref"] for x in self.get("/spaces", h=self.bob, mine="true")["items"]], ["bob@mine"])
        self.assertEqual(self.c.get(API + "/spaces", params={"shared": "true"}).status_code, 401)
        f = self.get("/spaces/facets", h=self.bob)
        self.assertEqual(f["scopes"], {"all": 3, "mine": 1, "shared": 1})

    def test_facets_count_each_dimension_under_the_other_filters(self):
        self.pub("a", tags="x,y")
        self.pub("b", tags="x", ext="csv", data=b"p,q\n1,2")
        self.pub("c", tags="y", h=self.bob)
        f = self.get("/spaces/facets")
        self.assertEqual((f["total"], f["kinds"]), (3, {"doc": 2, "sheet": 1}))
        self.assertEqual({t["tag"]: t["n"] for t in f["tags"]}, {"x": 2, "y": 2})
        self.assertEqual({o["username"]: o["n"] for o in f["owners"]}, {"root": 2, "bob": 1})
        g = self.get("/spaces/facets", tag="x")                                                    # its own facet ignores the tag, others obey it
        self.assertEqual((g["total"], g["kinds"], {t["tag"]: t["n"] for t in g["tags"]}), (2, {"doc": 1, "sheet": 1}, {"x": 2, "y": 2}))
        h = self.get("/spaces/facets", kind="sheet")
        self.assertEqual((h["kinds"], {t["tag"]: t["n"] for t in h["tags"]}), ({"doc": 2, "sheet": 1}, {"x": 1}))
        self.assertEqual(self.c.get(API + "/spaces/facets").json()["scopes"], {})                           # signed out: no scope counts
        self.pub("d", tags="z")
        self.assertIn("z", {t["tag"] for t in self.get("/spaces/facets")["tags"]})                 # a publish drops the cached counts


class Comments(unittest.TestCase):
    """view < comment < edit < admin. A public space: everybody views, nobody comments until its public_access says so (signed in only)."""

    def setUp(self):
        self.c = client()
        self.h = admin(self.c)
        self.u = {n: login(self.c, n) for n in ("bob", "carl", "dave", "erin")}
        r = self.c.post(API + "/spaces", params={"filename": "deck.html"}, content=DECK.encode(), headers=self.u["bob"])
        self.assertEqual(r.status_code, 200, r.text)                    # bob owns it (not a site admin)
        for n, a in (("carl", "comment"), ("dave", "view"), ("erin", "edit")):
            self.assertEqual(self.c.put(API + "/spaces/bob@deck/access", json={"type": "user", "name": n, "access": a}, headers=self.u["bob"]).status_code, 200)
        self.url = API + "/spaces/bob@deck/comments"

    def post(self, who, text="hi", **kw):
        return self.c.post(self.url, json=dict(kw, body=text), headers=self.u.get(who, {}) if who != "root" else self.h)

    def test_permission_matrix_public_default_is_view_only(self):
        sp = self.c.get(API + "/spaces/bob@deck").json()
        self.assertEqual((sp["visibility"], sp["public_access"], sp["level"], sp["can_comment"]), ("public", "view", "view", False))
        self.assertEqual(self.c.get(self.url).status_code, 200)                       # anonymous reads
        self.assertEqual(self.post(None).status_code, 401)                            # ...never writes
        self.assertEqual(self.post("dave").status_code, 403)                          # shared as view
        ann = login(self.c, "ann")
        self.assertEqual(self.c.post(self.url, json={"body": "x"}, headers=ann).status_code, 403)   # signed in, public space, default view only
        for who in ("carl", "erin", "bob", "root"):                                   # comment, edit, owner, site admin
            self.assertEqual(self.post(who, "from " + who).status_code, 200, who)
        self.assertEqual(self.c.get(API + "/spaces/bob@deck", headers=self.u["carl"]).json()["level"], "comment")
        self.assertEqual(self.c.get(API + "/spaces/bob@deck", headers=self.u["dave"]).json()["level"], "view")
        # only the space admin may open comments to the public
        self.assertEqual(self.c.patch(API + "/spaces/bob@deck", json={"public_access": "comment"}, headers=self.u["erin"]).status_code, 403)
        self.assertEqual(self.c.patch(API + "/spaces/bob@deck", json={"public_access": "edit"}, headers=self.u["bob"]).status_code, 400)
        self.assertEqual(self.c.patch(API + "/spaces/bob@deck", json={"public_access": "comment"}, headers=self.u["bob"]).json()["public_access"], "comment")
        self.assertEqual(self.c.post(self.url, json={"body": "now I can"}, headers=ann).status_code, 200)
        self.assertEqual(self.post(None).status_code, 401)                            # anonymous: still never
        self.assertFalse(self.c.get(self.url).json()["can_comment"])
        self.assertEqual(self.post("dave").status_code, 200)                          # a view share does not lower the public level
        # resolve / edit
        first = self.c.get(self.url).json()["items"][0]
        self.assertEqual(self.c.patch(self.url + "/%d" % first["id"], json={"body": "mine now"}, headers=self.u["erin"]).status_code, 403)
        self.c.patch(API + "/spaces/bob@deck", json={"public_access": "view"}, headers=self.u["bob"])
        self.assertEqual(self.c.patch(self.url + "/%d" % first["id"], json={"resolved": True}, headers=self.u["dave"]).status_code, 403)
        r = self.c.patch(self.url + "/%d" % first["id"], json={"resolved": True}, headers=self.u["carl"])
        self.assertTrue(r.json()["resolved"])
        r = self.c.patch(self.url + "/%d" % first["id"], json={"body": "edited"}, headers=self.u["carl"])
        self.assertEqual((r.status_code, r.json()["body"]), (200, "edited"))
        # private space: no access = 404 for comments too
        self.c.patch(API + "/spaces/bob@deck", json={"visibility": "private"}, headers=self.u["bob"])
        self.assertEqual(self.c.get(self.url).status_code, 404)
        self.assertEqual(self.c.get(self.url, headers=ann).status_code, 404)
        self.assertEqual(self.c.post(self.url, json={"body": "x"}, headers=ann).status_code, 404)
        self.assertEqual(self.post("carl").status_code, 200)
        self.assertEqual(self.post("dave").status_code, 403)

    def test_share_accepts_comment_and_rejects_unknown_levels(self):
        url = API + "/spaces/bob@deck/access"
        self.assertEqual(self.c.put(url, json={"type": "user", "name": "dave", "access": "owner"}, headers=self.u["bob"]).status_code, 400)
        got = {x["name"]: x["access"] for x in self.c.get(url, headers=self.u["bob"]).json()["access"]}
        self.assertEqual(got, {"carl": "comment", "dave": "view", "erin": "edit"})

    def test_threads_soft_delete_and_validation(self):
        a = self.post("carl", "top", anchor={"slide": 2}).json()
        self.assertEqual((a["anchor"], a["rev"], a["author"]["username"]), ({"slide": 2}, 1, "carl"))
        b = self.post("erin", "reply", parent_id=a["id"]).json()
        c = self.post("carl", "reply to reply", parent_id=b["id"]).json()
        self.assertEqual(self.c.delete(self.url + "/%d" % a["id"], headers=self.u["erin"]).status_code, 403)   # not the author, not the space admin
        self.assertEqual(self.c.delete(self.url + "/%d" % a["id"], headers=self.u["carl"]).status_code, 200)
        t = self.c.get(self.url).json()
        self.assertEqual(t["total"], 2)
        top = t["items"][0]
        self.assertTrue(top["deleted"])
        self.assertEqual((top["body"], top["author"]), ("", None))                    # the text is gone, the thread keeps its shape
        self.assertEqual(top["replies"][0]["id"], b["id"])
        self.assertEqual(top["replies"][0]["replies"][0]["id"], c["id"])
        self.assertEqual(self.post("carl", "x", parent_id=a["id"]).status_code, 404)  # cannot reply to a deleted comment
        self.assertEqual(self.c.delete(self.url + "/%d" % b["id"], headers=self.u["bob"]).status_code, 200)   # the space owner may
        self.assertEqual(len(self.c.get(self.url).json()["items"]), 1)                # c still holds the thread
        self.assertEqual(self.c.delete(self.url + "/%d" % c["id"], headers=self.u["carl"]).status_code, 200)
        self.assertEqual(self.c.get(self.url).json()["items"], [])                    # nothing live left: the empty thread disappears
        self.assertEqual(self.c.delete(self.url + "/%d" % c["id"], headers=self.u["carl"]).status_code, 404)
        self.assertEqual(self.c.get(API + "/spaces/bob@deck").json()["comments"], 0)
        # validation
        self.assertEqual(self.post("carl", "   ").status_code, 400)
        self.assertEqual(self.post("carl", "x" * 5001).status_code, 400)
        self.assertEqual(self.post("carl", "x", anchor="slide 2").status_code, 400)
        self.assertEqual(self.post("carl", "x", anchor={"Bad-Key": 1}).status_code, 400)
        raw = json.dumps({"body": "line one\r\nline two\udc8d\x00\u202e"})            # a lone surrogate can only arrive as a JSON escape
        r = self.c.post(self.url, content=raw, headers=dict(self.u["carl"], **{"Content-Type": "application/json"}))
        self.assertEqual(r.json()["body"], "line one\nline two\ufffd")
        self.assertEqual(self.c.get(API + "/spaces/bob@deck").json()["comments"], 1)
        self.assertEqual(self.c.get(API + "/spaces", params={"q": "deck"}).json()["items"][0]["comments"], 1)

    def test_deleting_the_space_deletes_its_comments(self):
        self.post("carl", "bye")
        sid = self.c.get(API + "/spaces/bob@deck").json()
        repos = self.c.app.state.repos
        self.assertEqual(len(repos.space_comments(repos.space_get("bob", "deck")["id"])), 1)
        spid = repos.space_get("bob", "deck")["id"]
        self.assertEqual(self.c.delete(API + "/spaces/bob@deck", headers=self.u["bob"]).status_code, 200)
        self.assertEqual(repos.space_comments(spid), [])
        self.assertEqual(repos.space_versions(spid), [])
        self.assertTrue(sid)


class Versions(unittest.TestCase):
    def setUp(self):
        self.c = client()
        self.h = admin(self.c)
        self.repos = self.c.app.state.repos

    def pub(self, data, filename="notes.md", h=None, **q):
        q["filename"] = filename
        r = self.c.post(API + "/spaces", params=q, content=data, headers=h or self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["space"]

    def html(self, rev=None):
        return self.c.get(API + "/spaces/root@notes/data", params={"rev": rev} if rev else {}, headers=self.h)

    def test_every_publish_is_kept_and_old_revisions_open(self):
        for i in (1, 2, 3):
            self.assertEqual(self.pub(b"# version %d" % i, note="v%d" % i)["rev"], i)
        v = self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()
        self.assertEqual([(x["rev"], x["note"], x["current"], x["available"]) for x in v["items"]],
                         [(3, "v3", True, True), (2, "v2", False, True), (1, "v1", False, True)])
        self.assertEqual((v["current"], v["keep"], v["items"][0]["author"]), (3, 20, "root"))
        self.assertIn("version 1", self.html(1).json()["html"])
        self.assertIn("version 3", self.html().json()["html"])
        f = self.c.get(API + "/spaces/root@notes/file", params={"rev": 2}, headers=self.h)
        self.assertEqual((f.content, f.headers["x-space-rev"]), (b"# version 2", "2"))
        self.assertEqual(self.html(9).status_code, 404)
        # an html revision: a pinned link keeps showing THAT revision after a republish
        self.pub(site(), "rep.zip", kind="dashboard")
        url = self.c.get(API + "/spaces/root@rep/viewurl", headers=self.h).json()["url"]
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("index.html", "<h1>second</h1>")
        self.pub(b.getvalue(), "rep.zip", kind="dashboard")
        self.assertEqual(self.c.get(url).status_code, 403)                            # the current-revision link is bound to rev 1
        old = self.c.get(API + "/spaces/root@rep/viewurl", params={"rev": 1}, headers=self.h).json()
        self.assertEqual(old["rev"], 1)
        self.assertIn(b"report", self.c.get(old["url"]).content)
        self.assertIn(b"second", self.c.get(self.c.get(API + "/spaces/root@rep/viewurl", headers=self.h).json()["url"]).content)
        bad = re.sub(r"\.1/index", ".2/index", old["url"])                          # the pinned revision is signed too
        self.assertEqual(self.c.get(bad).status_code, 403)

    def test_restore_publishes_a_new_revision(self):
        self.pub(b"# one")
        self.pub(b"# two")
        bob = login(self.c, "bob")
        self.c.put(API + "/spaces/root@notes/access", json={"type": "user", "name": "bob", "access": "comment"}, headers=self.h)
        self.assertEqual(self.c.post(API + "/spaces/root@notes/versions/1/restore", headers=bob).status_code, 403)
        self.assertEqual(self.c.post(API + "/spaces/root@notes/versions/2/restore", headers=self.h).status_code, 400)   # already current
        self.assertEqual(self.c.post(API + "/spaces/root@notes/versions/7/restore", headers=self.h).status_code, 404)
        self.c.put(API + "/spaces/root@notes/access", json={"type": "user", "name": "bob", "access": "edit"}, headers=self.h)
        r = self.c.post(API + "/spaces/root@notes/versions/1/restore", headers=bob)
        self.assertEqual((r.status_code, r.json()["rev"], r.json()["restored"]), (200, 3, 1))
        self.assertIn("one", self.html().json()["html"])
        v = self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()["items"]
        self.assertEqual([(x["rev"], x["note"], x["author"]) for x in v], [(3, "restored revision 1", "bob"), (2, "", "root"), (1, "", "root")])
        self.assertIn("two", self.html(2).json()["html"])                             # history is not rewritten

    def test_pruning_keeps_the_cap_and_the_current_revision(self):
        self.assertEqual(self.c.put(API + "/admin/sync", json={"space_versions_keep": "2"}, headers=self.h).status_code, 200)
        self.assertEqual(self.c.put(API + "/admin/sync", json={"space_versions_keep": "0"}, headers=self.h).status_code, 400)
        for i in range(1, 5):
            self.pub(b"# v%d" % i)
        sp = self.repos.space_get("root", "notes")
        st = self.c.app.state.spaces
        self.assertEqual([x["rev"] for x in self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()["items"]], [4, 3])
        self.assertEqual(sorted(os.listdir(st.vbase(sp["id"]))), ["3"])               # files of 1 and 2 are gone, 4 is the live folder
        self.assertEqual(self.html(1).status_code, 404)
        self.assertEqual(self.c.delete(API + "/spaces/root@notes/versions/4", headers=self.h).status_code, 400)   # never the current one
        self.assertEqual(self.c.delete(API + "/spaces/root@notes/versions/3", headers=self.h).status_code, 200)
        self.assertFalse(os.path.exists(st.vdir(sp["id"], 3)))
        self.assertEqual(self.pub(b"# v5")["rev"], 5)
        self.assertEqual([x["rev"] for x in self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()["items"]], [5, 4])
        self.c.put(API + "/admin/sync", json={"space_versions_keep": "1"}, headers=self.h)
        self.pub(b"# v6")
        self.assertEqual([x["rev"] for x in self.repos.space_versions(sp["id"])], [6])
        self.assertEqual(os.listdir(st.vbase(sp["id"])), [])
        self.assertIn("v6", self.html().json()["html"])

    def test_a_space_from_before_version_history(self):
        """No version rows and no .rev file in its folder: listing works, the backfill adds the row once, a republish keeps the old files."""
        self.pub(b"# old")
        sp = self.repos.space_get("root", "notes")
        st = self.c.app.state.spaces
        c = self.repos.db.conn()
        c.execute("DELETE FROM space_versions"); c.execute("DELETE FROM app_settings WHERE key='space_versions_v1'"); c.commit()
        os.remove(os.path.join(st.dir(sp["id"]), ".rev"))
        self.assertEqual(self.repos.space_versions_backfill(), 1)
        self.assertEqual(self.repos.space_versions_backfill(), 0)                     # once
        c.execute("DELETE FROM space_versions"); c.commit()
        self.assertEqual([x["rev"] for x in self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()["items"]], [1])
        c.execute("DELETE FROM space_versions"); c.commit()
        self.pub(b"# new")
        self.assertEqual([(x["rev"], x["available"]) for x in self.c.get(API + "/spaces/root@notes/versions", headers=self.h).json()["items"]],
                         [(2, True), (1, True)])
        self.assertIn("old", self.html(1).json()["html"])

    def test_space_level_and_history_with_postgres_like_rows(self):
        bob, carl = login(self.c, "bob"), login(self.c, "carl")
        self.pub(b"# a", visibility="private")
        self.pub(b"# b")
        self.c.put(API + "/spaces/root@notes/access", json={"type": "user", "name": "bob", "access": "edit"}, headers=self.h)
        self.c.put(API + "/spaces/root@notes/access", json={"type": "user", "name": "carl", "access": "comment"}, headers=self.h)
        self.assertEqual(self.c.post(API + "/spaces/root@notes/comments", json={"body": "hey"}, headers=carl).status_code, 200)
        db, raw = self.repos.db, self.repos.db.conn
        db.conn = lambda: PgRows(raw())
        try:
            sp = self.repos.space_get("root", "notes")
            lv = lambda n: self.repos.space_level(sp, {"id": self.repos.user_by_name(n)["id"], "bypass": False})
            self.assertEqual((lv("bob"), lv("carl")), ("edit", "comment"))      # `for (a,) in rows` read the column NAME on Postgres
            self.assertEqual(self.c.get(API + "/spaces/root@notes", headers=bob).json()["level"], "edit")
            self.assertEqual([x["rev"] for x in self.c.get(API + "/spaces/root@notes/versions", headers=bob).json()["items"]], [2, 1])
            self.assertEqual(self.c.get(API + "/spaces/root@notes/comments", headers=bob).json()["items"][0]["body"], "hey")
            self.assertEqual(self.pub(b"# c")["rev"], 3)
            self.assertEqual(self.repos.space_versions_prune(sp["id"], 1), [2, 1])
        finally:
            db.conn = raw


class Mentions(unittest.TestCase):
    """`@name` in a comment, alerts on the account, and which people / groups hear about what. bob owns a PRIVATE deck shared with carl (comment),
    erin (edit) and the group `crew` (view: dave + fay)."""

    def setUp(self):
        self.c = client()
        self.h = admin(self.c)
        self.u = {n: login(self.c, n) for n in ("bob", "carl", "dave", "erin", "fay", "gus")}
        r = self.c.post(API + "/spaces", params={"filename": "deck.html", "visibility": "private"}, content=DECK.encode(), headers=self.u["bob"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.post(API + "/groups", json={"name": "crew"}, headers=self.h).status_code, 200)
        self.c.post(API + "/groups/crew/members", json={"usernames": ["dave", "fay"]}, headers=self.h)      # + root, who made the group
        for body in ({"type": "user", "name": "carl", "access": "comment"}, {"type": "user", "name": "erin", "access": "edit"},
                     {"type": "group", "name": "crew", "access": "comment"}):
            self.assertEqual(self.c.put(API + "/spaces/bob@deck/access", json=body, headers=self.u["bob"]).status_code, 200)
        for n in self.u:                                    # the share alerts are not what these tests are about
            self.c.post(API + "/notifications/read", json={"all": True}, headers=self.u[n])
        self.url = API + "/spaces/bob@deck/comments"

    def say(self, who, text, **kw):
        return self.c.post(self.url, json=dict(kw, body=text), headers=self.u[who])

    def inbox(self, who, **q):
        return self.c.get(API + "/notifications", params=q, headers=self.u[who]).json()

    def count(self, who):
        return self.c.get(API + "/notifications/count", headers=self.u[who]).json()["unread"]

    def test_mentions_people_and_groups_are_stored_and_alerted_once_each(self):
        r = self.say("carl", "ping @erin and @crew, also @erin again, mail me bob@corp.com or @nobody", rev=1)
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual([(m["type"], m["name"]) for m in j["mentions"]], [("user", "erin"), ("group", "crew")])      # unknown / e-mail text stays plain
        self.assertEqual({(m["name"], m["notified"]) for m in j["mention_report"]}, {("erin", 1), ("crew", 3)})
        for who in ("erin", "dave", "fay", "bob"):
            self.assertEqual(self.count(who), 1, who)
        self.assertEqual(self.count("carl"), 0)                                   # never the author
        self.assertEqual(self.count("gus"), 0)
        dave = self.inbox("dave")["items"][0]
        self.assertEqual((dave["kind"], dave["via"], dave["ref"], dave["rev"], dave["comment_id"], dave["actor"]["username"]), ("mention", "crew", "bob@deck", 1, j["id"], "carl"))
        self.assertIn("@erin", dave["body"])
        self.assertEqual(self.inbox("erin")["items"][0]["kind"], "mention")      # the strongest reason: erin is also told only once
        self.assertEqual(self.inbox("bob")["items"][0]["kind"], "comment")        # the owner hears about every comment

    def test_mention_card_says_who_they_are_and_whether_they_can_open_the_space(self):
        url = API + "/spaces/bob@deck/mention-card/"
        self.c.patch(API + "/auth/me", json={"display_name": "Erin Ng", "title": "Designer"}, headers=self.u["erin"])
        erin = self.c.get(url + "user/erin", headers=self.u["carl"])
        self.assertEqual(erin.status_code, 200, erin.text)
        e = erin.json()
        self.assertEqual((e["type"], e["name"], e["access"]), ("user", "erin", True))
        self.assertNotIn("password_hash", e)
        self.assertEqual((e["display_name"], e["title"]), ("Erin Ng", "Designer"))
        gus = self.c.get(url + "user/gus", headers=self.u["carl"]).json()
        self.assertFalse(gus["access"])                                           # gus cannot open the private deck: no alert for them
        crew = self.c.get(url + "group/crew", headers=self.u["carl"]).json()
        self.assertEqual((crew["type"], crew["access"]), ("group", True))
        self.assertGreaterEqual(crew["members"], 2)
        self.assertEqual(self.c.get(url + "user/nobody", headers=self.u["carl"]).status_code, 404)
        self.assertEqual(self.c.get(url + "user/erin", headers=self.u["gus"]).status_code, 404)    # no view access to the space: no card either

    def test_date_and_space_chips_are_plain_text_and_read_as_words_in_alerts(self):
        j = self.say("carl", "@erin due [date:2026-11-05], see [space:bob@q3-review|Q3 review] and [space:bob@plain]", rev=1).json()
        self.assertIn("[date:2026-11-05]", j["body"])                              # stored as typed: the page draws the chips
        self.assertEqual([m["name"] for m in j["mentions"]], ["erin"])             # `bob@q3-review` inside a chip is not a mention
        body = self.inbox("erin")["items"][0]["body"]
        self.assertEqual(body, "@erin due 2026-11-05, see Q3 review and bob@plain")

    def test_a_mention_of_someone_without_access_is_not_delivered_and_is_reported(self):
        j = self.say("carl", "what do you think @gus?").json()
        self.assertEqual(j["mention_report"], [{"type": "user", "name": "gus", "notified": 0, "no_access": ["gus"]}])
        self.assertEqual(self.count("gus"), 0)
        self.assertEqual(self.c.get(API + "/spaces/bob@deck/mentionable", params={"q": "gu"}, headers=self.u["carl"]).json()["users"][0]["access"], False)
        self.assertEqual(self.c.get(API + "/spaces/bob@deck/mentionable", params={"q": "er"}, headers=self.u["carl"]).json()["users"][0]["access"], True)
        self.assertEqual(self.c.get(API + "/spaces/bob@deck/mentionable", params={"q": "cr"}, headers=self.u["carl"]).json()["groups"][0],
                         {"name": "crew", "members": 3, "access": True})
        self.assertEqual(self.c.get(API + "/spaces/bob@deck/mentionable", headers=self.u["dave"]).status_code, 200)   # crew is comment level
        self.assertEqual(self.c.get(API + "/spaces/bob@deck/mentionable", headers=self.u["gus"]).status_code, 404)

    def test_reply_resolve_edit_and_delete(self):
        first = self.say("carl", "no mention here", rev=1).json()
        self.assertEqual(self.inbox("erin", unread=True)["items"], [])
        rep = self.say("erin", "agreed", parent_id=first["id"]).json()
        self.assertEqual(self.inbox("carl")["items"][0]["kind"], "reply")
        self.assertEqual(self.count("bob"), 2)                                    # both comments; the reply alert is carl's
        # editing adds NEW names only
        url = self.url + "/%d" % first["id"]
        e = self.c.patch(url, json={"body": "now @dave and @erin"}, headers=self.u["carl"]).json()
        self.assertEqual({m["name"] for m in e["mentions"]}, {"dave", "erin"})
        self.assertEqual(self.count("dave"), 1)
        again = self.c.patch(url, json={"body": "now @dave and @erin!!"}, headers=self.u["carl"]).json()
        self.assertEqual(self.count("dave"), 1)                                   # same names: nobody is pinged twice
        self.assertEqual(self.count("bob"), 2)                                    # an edit is not a new comment
        # resolve tells the author
        self.assertEqual(self.c.patch(url, json={"resolved": True}, headers=self.u["erin"]).status_code, 200)
        self.assertEqual(self.inbox("carl")["items"][0]["kind"], "resolved")
        # deleting the comment takes its alerts away
        self.assertEqual(self.c.delete(url, headers=self.u["carl"]).status_code, 200)
        self.assertNotIn(first["id"], [n["comment_id"] for n in self.inbox("dave")["items"]])
        self.assertTrue(rep["id"])

    def test_revision_travels_with_the_alert_and_new_revisions_are_announced(self):
        r = self.c.put(API + "/spaces/bob@deck/content", content=DECK.encode(), params={"filename": "deck.html", "note": "more slides"}, headers=self.u["erin"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["space"]["rev"], 2)
        n = self.inbox("bob")["items"][0]
        self.assertEqual((n["kind"], n["rev"], n["body"], n["actor"]["username"]), ("version", 2, "more slides", "erin"))
        self.assertEqual(self.count("erin"), 0)                                   # the publisher is not told
        self.say("carl", "@dave look at version one", rev=1)
        d = self.inbox("dave")["items"][0]
        self.assertEqual((d["rev"], d["current_rev"]), (1, 2))                    # the link can open revision 1 even though 2 is current
        self.assertEqual(self.c.put(API + "/spaces/bob@deck/content", content=DECK.encode(), params={"filename": "deck.html"}, headers=self.u["bob"]).status_code, 200)
        self.assertEqual({n["kind"] for n in self.inbox("carl")["items"] if n["kind"] == "version"}, {"version"})   # commenters hear about it too

    def test_share_alert_read_state_filters_and_privacy(self):
        self.c.put(API + "/spaces/bob@deck/access", json={"type": "user", "name": "gus", "access": "view"}, headers=self.u["bob"])
        self.c.put(API + "/spaces/bob@deck/access", json={"type": "user", "name": "gus", "access": "view"}, headers=self.u["bob"])
        g = self.inbox("gus")
        self.assertEqual([(n["kind"], n["body"]) for n in g["items"]], [("share", "view")])                         # the same grant twice is one alert
        self.assertEqual(g["unread"], 1)
        nid = g["items"][0]["id"]
        self.assertEqual(self.c.post(API + "/notifications/read", json={"ids": [nid]}, headers=self.u["carl"]).json()["unread"], 0)     # carl cannot read gus's
        self.assertEqual(self.count("gus"), 1)
        self.assertEqual(self.c.post(API + "/notifications/read", json={"ids": [nid]}, headers=self.u["gus"]).json()["unread"], 0)
        self.assertEqual(self.inbox("gus", unread=True)["items"], [])
        self.assertEqual(len(self.inbox("gus")["items"]), 1)
        self.assertEqual(self.c.post(API + "/notifications/read", json={}, headers=self.u["gus"]).status_code, 400)
        self.say("carl", "@gus hi")                                               # gus can view: mention delivered
        self.assertEqual({n["kind"] for n in self.inbox("gus", kind="mention")["items"]}, {"mention"})
        self.assertEqual(self.c.get(API + "/notifications").status_code, 401)
        # access taken away: the alerts about the space disappear from the list and the count
        self.c.delete(API + "/spaces/bob@deck/access/user/gus", headers=self.u["bob"])
        self.assertEqual((self.count("gus"), self.inbox("gus")["items"]), (0, []))
        # deleting the space removes its alerts
        self.assertEqual(self.c.delete(API + "/spaces/bob@deck", headers=self.u["bob"]).status_code, 200)
        self.assertEqual(self.inbox("bob")["items"], [])


class UpgradeFromBeforeSpaces(unittest.TestCase):
    """A database made before Spaces existed (schema without the spaces tables) opens with the new code, keeps its rows and gains the tables."""

    def test_old_database_gains_the_tables_and_keeps_its_rows(self):
        import sqlite3
        import tempfile
        from aihub.server import db as dbm
        from aihub.server.config import Settings
        from aihub.server.db import DB
        from aihub.server.repos import Repos
        d = tempfile.mkdtemp()
        s = Settings.load(); s.data_dir = d; s.sqlite_path = d + "/hub.db"
        old = re.sub(r"CREATE TABLE IF NOT EXISTS spaces\(.*?space_access\([^\n]*\n", "", dbm.TABLES, flags=re.S)   # the schema as it was before Spaces
        self.assertNotIn("space", old.lower())
        raw = sqlite3.connect(s.sqlite_path)
        raw.executescript(old.format(pk="INTEGER PRIMARY KEY", real="REAL", blob="BLOB", bigint="INTEGER"))
        raw.execute("INSERT INTO app_settings VALUES('marker','keep')"); raw.commit(); raw.close()
        db = DB(s); r = Repos(db); c = db.conn()
        names = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"spaces", "space_access"} <= names)
        self.assertEqual(c.execute("SELECT value FROM app_settings WHERE key='marker'").fetchone()[0], "keep")
        self.assertEqual(r.space_access_list(1), [])
