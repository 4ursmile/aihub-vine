import io
import json
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


if __name__ == "__main__":
    unittest.main()


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
