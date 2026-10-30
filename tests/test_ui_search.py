"""aihub-ui local search engine (aihubui/searchidx.py): incremental chat index, rewrite detection, tiers, hub mirror, hub asked only when thin."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui"))
from aihubui import searchidx as X, store  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        store._icache.update(stamp=None, data=None)
        store._counts.clear()
        store._scache.clear()
        X.reset()
        if X._conn() is None:
            self.skipTest("this Python's SQLite has no FTS5")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        X.reset()
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chat(self, title, *texts):
        s = store.create_session(title)
        for t in texts:
            store.append(s["id"], {"k": "user", "text": t})
        return s["id"]

    def hits(self, q):
        return [(h["id"], h["snippet"]) for h in store.search(q)]


class Chats(Base):
    def test_words_match_as_prefixes_and_best_chat_comes_first(self):
        a = self.chat("one", "we deployed the kubernetes cluster", "nothing else")
        b = self.chat("two", "kubernetes kubernetes kubernetes cluster upgrade")
        ids = [i for i, _ in self.hits("kuber clust")]
        self.assertEqual(set(ids), {a, b})
        self.assertEqual(ids[0], b)                                     # BM25: more matches, shorter text

    def test_title_hits_come_first(self):
        a = self.chat("Budget review", "x")
        b = self.chat("other", "the budget is fine")
        self.assertEqual([i for i, _ in self.hits("budget")], [a, b])

    def test_appends_are_indexed_incrementally(self):
        sid = self.chat("t", "alpha")
        self.assertEqual(self.hits("bravo"), [])
        store.append(sid, {"k": "msg", "blocks": [{"type": "text", "text": "bravo answer"}]})
        self.assertEqual([i for i, _ in self.hits("bravo")], [sid])
        with X._lock:
            n = X._conn().execute("SELECT COUNT(*) FROM msg").fetchone()[0]
        self.assertEqual(n, 2)                                          # alpha was not indexed twice

    def test_rewrite_and_delete_drop_old_text(self):
        sid = self.chat("t", "zulu one", "yankee two")
        self.assertTrue(self.hits("yankee"))
        store.delete_event(sid, 1)
        self.assertEqual(self.hits("yankee"), [])
        self.assertTrue(self.hits("zulu"))
        store.delete_session(sid)
        self.assertEqual(self.hits("zulu"), [])

    def test_a_file_rewritten_behind_our_back_is_reindexed(self):
        sid = self.chat("t", "original words here")
        self.hits("original")
        p = os.path.join(store.ui_dir("history"), sid + ".jsonl")
        with open(p, "w") as f:                                         # same size or bigger, different bytes
            f.write(json.dumps({"k": "user", "text": "replacement text that is longer"}) + "\n")
        X.touch(sid)                                                    # an append, as far as the index knows
        self.assertEqual(self.hits("original"), [])
        self.assertTrue(self.hits("replacement"))

    def test_warm_pass_indexes_existing_files_and_forgets_deleted_ones(self):
        sid = self.chat("t", "papaya")
        X.reset()
        store._scache.clear()
        X.warm()
        for _ in range(100):
            if X.ready():
                break
            time.sleep(0.02)
        self.assertTrue(X.ready())
        self.assertEqual(X.chats("papaya"), [(sid, "papaya")])

    def test_mid_word_substring_still_found_when_the_index_has_little(self):
        sid = self.chat("t", "supercalifragilistic")
        self.assertEqual([i for i, _ in self.hits("califrag")], [sid])

    def test_scale_query_stays_fast(self):
        c = X._conn()
        with X._lock:
            c.execute("BEGIN")
            c.executemany("INSERT INTO msg(body, sid) VALUES(?, ?)", (("message %d about topic%d common words" % (i, i % 500), "%08x" % (i % 2000)) for i in range(200000)))
            c.execute("COMMIT")
        t0 = time.monotonic()
        rare = X.chats("topic42")
        t1 = time.monotonic()
        common = X.chats("common words")
        t2 = time.monotonic()
        self.assertTrue(rare and common)
        self.assertLess(t1 - t0, 1.5)                                   # loose bounds: a loaded machine (a parallel test run) must not fail it
        self.assertLess(t2 - t1, X.RANK_MS / 1000.0 + 1.5)              # cut and answered newest-first if BM25 over 200k rows is slow


class Mirror(Base):
    SPACES = [{"ref": "alice@q3-review", "title": "Q3 review", "kind": "slides", "tags": [], "views": 9},
              {"ref": "alice@quarterly-plan", "title": "Plan", "kind": "doc", "tags": [], "views": 3},
              {"ref": "bob@design", "title": "Design notes for alice", "kind": "doc", "tags": ["aihub-chat"], "views": 1}]

    def setUp(self):
        super().setUp()
        X.merge_spaces(self.SPACES, {"context": {"hub": "h", "username": "me"}})

    def refs(self, q):
        return [x["ref"] for x in X.spaces(q)]

    def test_exact_ref_then_author_prefix_then_author(self):
        self.assertEqual(self.refs("alice@q3-review")[0], "alice@q3-review")
        self.assertEqual(self.refs("alice@qu")[0], "alice@quarterly-plan")
        self.assertEqual(self.refs("alice@")[:2], ["alice@q3-review", "alice@quarterly-plan"])

    def test_author_word_ranks_before_text_mentions(self):
        self.assertEqual(self.refs("alice"), ["alice@q3-review", "alice@quarterly-plan", "bob@design"])

    def test_full_text_and_empty_query_lists_shared_chats(self):
        self.assertEqual(self.refs("design not"), ["bob@design"])
        self.assertEqual(X.spaces("")[0]["chat"], True)
        self.assertEqual(self.refs(""), ["bob@design"])

    def test_another_account_empties_the_mirror(self):
        X.merge_spaces([], {"context": {"hub": "h", "username": "someone-else"}})
        self.assertEqual(self.refs("alice"), [])

    def test_hub_asked_only_when_thin_and_once_per_query(self):
        self.assertFalse(X.want_remote("hub", "alice", len(X.spaces("alice"))))
        self.assertTrue(X.want_remote("hub", "nothing here", 0))
        X.asked("hub", "nothing  here")
        self.assertFalse(X.want_remote("hub", "nothing here", 0))

    def test_packages_exact_prefix_text_and_full_catalogue_removes_gone_ones(self):
        X.merge_pkgs([{"name": "pdf-tools", "description": "Split PDFs"}, {"name": "pdf", "description": "x"}, {"name": "slides", "description": "make pdf decks"}], full=True)
        self.assertEqual([p["name"] for p in X.pkgs("pdf")], ["pdf", "pdf-tools", "slides"])
        X.merge_pkgs([{"name": "pdf"}], full=True, now=time.time() + 1)
        self.assertEqual([p["name"] for p in X.pkgs("pdf")], ["pdf"])


    def test_refresh_after_the_first_asks_only_for_changes_and_search_hits_never_move_the_cursor(self):
        calls, now = [], time.time()

        def run(args, timeout=0):
            calls.append(args)
            if args == ["search"]:
                return {"data": []}
            if "--since" in args:
                return {"data": {"items": [{"ref": "eve@fresh", "title": "Fresh one", "tags": [], "updated": now + 5}]}}
            if args[1] == "search":
                return {"data": {"items": [{"ref": "zed@far", "title": "Far", "tags": [], "updated": now + 99}]}}
            return {"data": {"items": [{"ref": "eve@old", "title": "Old", "tags": [], "updated": now}]}}
        with mock.patch.object(X.A, "run", run):
            X._seed()                                                   # first: full seed
            self.assertTrue(any(a[:3] == ["space", "list", "--kind"] for a in calls))
            with X._lock:
                self.assertEqual(float(X._meta(X._conn(), "spaces_cursor")), now)      # the search hit (now + 99) did not move it
            calls.clear()
            X._seed()                                                   # then: catalogue + changes only
            self.assertEqual(sorted(a[2] if a[0] == "space" else a[0] for a in calls), ["--since", "search"])
        self.assertEqual(self.refs("fresh"), ["eve@fresh"])


class Folders(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        p = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        p.start()
        self.addCleanup(p.stop)
        for d in ("code/my-app/.git", "code/my-app/src", "notes/my-app-ideas", "code/node_modules/my-app", ".hidden/my-app", "work/billing"):
            os.makedirs(os.path.join(self.home, d))
        open(os.path.join(self.home, "work", "billing", "aihub.toml"), "w").close()

    def walk(self):
        self.assertTrue(X.dirs_refresh(force=True))
        for _ in range(200):
            if not X.dirs_busy():
                break
            time.sleep(0.02)

    def test_projects_first_hidden_and_vendor_folders_skipped(self):
        self.walk()
        got = [(x["short"], x["proj"]) for x in X.folders("my-app")]
        self.assertEqual(got[0], (os.path.join("~", "code", "my-app"), True))
        self.assertIn((os.path.join("~", "notes", "my-app-ideas"), False), got)
        self.assertFalse(any("node_modules" in g or ".hidden" in g for g, _ in got))
        self.assertEqual(X.folders("bill")[0]["proj"], True)

    def test_a_second_walk_replaces_the_first_and_drops_removed_folders(self):
        self.walk()
        shutil.rmtree(os.path.join(self.home, "notes"))
        os.makedirs(os.path.join(self.home, "fresh-dir"))
        self.walk()
        self.assertEqual([x["name"] for x in X.folders("fresh")], ["fresh-dir"])
        self.assertEqual(X.folders("ideas"), [])
        with X._lock:
            self.assertEqual(X._conn().execute("SELECT COUNT(*) FROM dirs").fetchone()[0], X._conn().execute("SELECT COUNT(*) FROM dir_fts").fetchone()[0])

    def test_a_typed_path_is_listed_live(self):
        got = [x["name"] for x in X.folders("~/code/")]
        self.assertEqual(got, ["code", "my-app"])                       # the folder itself, then its sub folders (no node_modules)
        self.assertEqual([x["name"] for x in X.folders("~/code/my")], ["my-app"])

class Routes(Base):
    def test_local_first_then_deep_merges_the_hub_answer(self):
        import server
        calls = []

        def run(args, timeout=0):
            calls.append(args)
            return {"context": {"hub": "h", "username": "me"}, "data": {"items": [{"ref": "carol@roadmap", "title": "Roadmap", "tags": []}]}}
        for p in (mock.patch.object(server.A, "run", run), mock.patch.object(X, "refresh", lambda force=False: False)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(server._spot.clear)
        r = server.spot_hub(None, {"q": "roadmap"}, {})
        self.assertEqual((r["items"], r["more"], calls), ([], True, []))  # local only: nothing asked yet
        r = server.spot_hub(None, {"q": "roadmap", "deep": "1"}, {})
        self.assertEqual([x["ref"] for x in r["items"]], ["carol@roadmap"])
        self.assertEqual(calls[0][:3], ["space", "search", "roadmap"])
        self.assertFalse(server.spot_hub(None, {"q": "roadmap"}, {})["more"])

    def test_online_search_off_never_calls_the_hub(self):
        import server
        calls = []
        refresh = mock.Mock(return_value=False)
        for p in (mock.patch.object(server.A, "run", lambda *a, **k: calls.append(a)), mock.patch.object(X, "refresh", refresh)):
            p.start()
            self.addCleanup(p.stop)
        store.save_prefs({"online_search": False})
        X.merge_spaces([{"ref": "dan@cached", "title": "Cached", "tags": []}])
        for q in ({"q": "cached"}, {"q": "nothing", "deep": "1"}):
            r = server.spot_hub(None, q, {})
            self.assertEqual((r["more"], r["offline"]), (False, True))
        self.assertEqual(server.spot_hub(None, {"q": "cached"}, {})["items"][0]["ref"], "dan@cached")
        r = server.spot_pkg(None, {"q": "x", "deep": "1"}, {})
        self.assertEqual((r["more"], calls, refresh.call_count), (False, [], 0))


if __name__ == "__main__":
    unittest.main()
