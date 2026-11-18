"""aihub-ui Data Studio: MongoDB statements, the built-in sample database, agentic Magic SQL and the @data: chat mention."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import magic, mongorunner, sampledb, sqlstudio, store  # noqa: E402


class Box:
    def __init__(self):
        self.d = {}

    def put(self, k, v):
        self.d[k] = v

    def remove(self, k):
        self.d.pop(k, None)

    def get_all(self):
        return dict(self.d)


def have_sqlalchemy():
    import importlib.util
    return importlib.util.find_spec("sqlalchemy") is not None


def fake_call(box, cid, req, timeout=120, rid=None):
    """Stands in for the runner process (which needs SQLAlchemy): the same answers, straight from sqlite3 on the sample file."""
    db = sqlite3.connect(sqlstudio.sample_path())
    try:
        if req["action"] == "digest":
            want = str(req.get("table") or "").lower()
            tabs = []
            for n, kind in db.execute("select name, type from sqlite_master where type in ('table','view') and name not like 'sqlite_%' order by name"):
                if want and want not in n.lower():
                    continue
                cols = db.execute("pragma table_info(%s)" % n).fetchall()
                tabs.append({"name": n, "kind": kind, "columns": [{"name": c[1], "type": c[2], "pk": bool(c[5])} for c in cols], "more_columns": 0, "fks": []})
            return {"ok": True, "dialect": "sqlite", "schemas": ["main"], "schema": "main", "tables": tabs, "total": len(tabs)}
        if req["action"] == "query":
            sql = req["sql"].strip()
            if req.get("readonly") and not sql.lower().startswith(("select", "with", "pragma")):
                return {"ok": False, "error": "This connection is read-only: only SELECT / WITH / SHOW / EXPLAIN style statements run."}
            try:
                cur = db.execute(sql)
            except sqlite3.Error as e:
                return {"ok": False, "error": str(e)}
            rows = cur.fetchmany(int(req.get("limit") or 1000) + 1)
            n = int(req.get("limit") or 1000)
            return {"ok": True, "columns": [d[0] for d in cur.description], "rows": [list(r) for r in rows[:n]], "truncated": len(rows) > n, "ms": 1}
    finally:
        db.close()
    return {"ok": False, "error": "unsupported"}


class MongoStatementTest(unittest.TestCase):
    def test_relaxed_shell_notation_becomes_json(self):
        t = mongorunner.relaxed("{status: 'paid', total: {$gt: 10,}, d: ISODate('2024-01-31'), id: ObjectId(\"5f\"), n: NumberLong(5), r: /^ab/i, x: undefined}")
        v = json.loads(t)
        self.assertEqual(v["status"], "paid")
        self.assertEqual(v["total"], {"$gt": 10})
        self.assertEqual(v["d"], {"$date": "2024-01-31"})
        self.assertEqual(v["id"], {"$oid": "5f"})
        self.assertEqual(v["n"], {"$numberLong": "5"})
        self.assertEqual(v["r"], {"$regex": "^ab", "$options": "i"})
        self.assertIsNone(v["x"])

    def test_unknown_bare_word_is_explained(self):
        with self.assertRaises(ValueError) as cm:
            mongorunner.relaxed("{a: paid}")
        self.assertIn("Quote", str(cm.exception))

    def test_parse_chain_and_json_form(self):
        p = mongorunner.parse('db.orders.find({"a": 1}, {"b": 1}).sort({"b": -1}).limit(5);  // note')
        self.assertEqual(p["coll"], "orders")
        self.assertEqual([c[0] for c in p["calls"]], ["find", "sort", "limit"])
        self.assertEqual(p["calls"][0][1], ['{"a": 1}', '{"b": 1}'])
        p = mongorunner.parse("db.getCollection('my coll').aggregate([{$group: {_id: '$x', n: {$sum: 1}}}])")
        self.assertEqual(p["coll"], "my coll")
        self.assertEqual(p["calls"][0][0], "aggregate")
        p = mongorunner.parse('{"collection": "orders", "find": {"a": 1}, "limit": 3}')
        self.assertEqual([c[0] for c in p["calls"]], ["find", "limit"])
        self.assertEqual(mongorunner.parse("show collections"), {"show": "collections"})
        with self.assertRaises(ValueError):
            mongorunner.parse("select * from orders")

    def test_documents_become_a_flat_table(self):
        cols, rows = mongorunner.table([{"_id": "1", "a": {"b": 1, "c": {"d": 2}}, "tags": ["x"]}, {"_id": "2", "z": None}])
        self.assertEqual(cols[0], "_id")
        self.assertIn("a.b", cols)
        self.assertIn("a.c.d", cols)
        self.assertEqual(rows[0][cols.index("a.c.d")], 2)
        self.assertIsNone(rows[1][cols.index("a.b")])
        self.assertEqual(mongorunner.uri({"host": "h", "port": "27017", "user": "u@x", "password": "p:w", "options": "authSource=admin"}),
                         "mongodb://u%40x:p%3Aw@h:27017/?authSource=admin")

    def test_connection_saves_with_optional_connection_string(self):
        tmp = tempfile.mkdtemp()
        old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(tmp, *a)
        try:
            box = Box()
            c = sqlstudio.save(box, {"name": "mongo", "driver": "mongodb", "host": "h", "database": "shop", "url": "mongodb+srv://u:s3cret@cluster/"})
            self.assertEqual(c["driver"], "mongodb")
            self.assertNotIn("s3cret", json.dumps(sqlstudio.connections()))
            self.assertEqual(sqlstudio.conn_fields(box, sqlstudio.get(c["id"]))["url"], "mongodb+srv://u:s3cret@cluster/")
            sqlstudio.save(box, {"id": c["id"], "name": "mongo", "driver": "mongodb", "host": "h", "database": "shop"})          # blank keeps it
            self.assertIn("url", sqlstudio.conn_fields(box, sqlstudio.get(c["id"])))
        finally:
            store.ui_dir = old


class SampleDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)
        self.box = Box()

    def tearDown(self):
        store.ui_dir = self.old

    def test_every_chart_kind_has_data(self):
        p = sampledb.ensure(os.path.join(self.tmp, "s.db"))
        db = sqlite3.connect(p)
        names = {r[0] for r in db.execute("select name from sqlite_master where type in ('table','view')")}
        for need in ("orders", "daily_metrics", "web_traffic", "exam_scores", "employees", "survey_responses", "products", "customers", "events", "v_revenue_by_month", "v_category_share", "sample_guide"):
            self.assertIn(need, names)
            self.assertGreater(db.execute("select count(*) from " + need).fetchone()[0], 5, need)
        self.assertEqual(db.execute("select count(*) from web_traffic").fetchone()[0], 168)           # 7 weekdays x 24 hours: a heatmap
        self.assertGreater(db.execute("select count(distinct class) from exam_scores").fetchone()[0], 2)  # box plot groups
        self.assertEqual(db.execute("select count(*) from sample_guide").fetchone()[0], len(sampledb.GUIDE))
        self.assertEqual(sampledb.ensure(p), p)                                                         # current version: reused, not rebuilt

    def test_sample_connection_is_added_once_and_stays_deleted(self):
        self.assertEqual(sqlstudio.connections(), [])                                                   # library code never seeds on its own
        self.assertEqual(sqlstudio.ensure_sample(), sqlstudio.SAMPLE_ID)
        cons = sqlstudio.connections()
        self.assertEqual([c["id"] for c in cons], [sqlstudio.SAMPLE_ID])
        self.assertTrue(cons[0]["sample"] and cons[0]["readonly"] and cons[0]["driver"] == "sqlite")
        sqlstudio.ensure_sample()
        self.assertEqual(len(sqlstudio.connections()), 1)
        self.assertTrue(sqlstudio.delete(self.box, sqlstudio.SAMPLE_ID))
        self.assertIsNone(sqlstudio.ensure_sample())                                                    # a deleted sample is not forced back
        self.assertEqual(sqlstudio.connections(), [])
        sqlstudio.restore_sample()
        self.assertEqual(len(sqlstudio.connections()), 1)

    @unittest.skipUnless(have_sqlalchemy(), "needs sqlalchemy")
    def test_sample_can_be_queried_and_read_only(self):
        sqlstudio.ensure_sample()
        r = sqlstudio.ai_query(self.box, "sample", "select region_id, count(*) n from customers group by 1 order by 1", 10)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["rows"]), 6)
        w = sqlstudio.call(self.box, sqlstudio.SAMPLE_ID, {"action": "query", "sql": "delete from orders"})
        self.assertFalse(w["ok"])
        self.assertIn("read-only", w["error"])


class AgenticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)
        self.box = Box()
        sqlstudio.ensure_sample()
        sqlstudio._digests.clear()
        self.p = mock.patch.object(sqlstudio, "call", fake_call)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        store.ui_dir = self.old

    def test_it_explores_then_writes_and_checks_the_query(self):
        replies = [
            '{"action": "tables", "filter": "order revenue"}',
            '```json\n{"action": "describe", "tables": ["orders"]}\n```',
            '{"action": "values", "table": "orders", "column": "status"}',
            '{"action": "final", "sql": "select channel, round(sum(total)) as revenue from orders where status = \'delivered\' group by channel", "note": "Delivered orders only."}',
        ]
        seen = []

        def fake(system, prompt, model="", extra_env=None, engine="auto", max_tokens=0, quick=False):
            seen.append(prompt)
            return replies[len(seen) - 1], "m", "api"
        with mock.patch.object(magic, "ask_model", fake):
            r = sqlstudio.ask_sql(self.box, "revenue by channel", "sample", mode="agentic")
        self.assertEqual([s["kind"] for s in r["steps"]], ["tables", "describe", "values", "check", "final"])
        self.assertIn("delivered", r["steps"][2]["detail"])                     # it saw the real status codes before filtering on them
        self.assertTrue(all(s["ok"] for s in r["steps"]))
        self.assertIn("group by channel", r["sql"])
        self.assertEqual(r["conn"], sqlstudio.SAMPLE_ID)
        self.assertIn("Tables of Sample data (SQLite)", seen[0])
        self.assertNotIn("discount", seen[0])                                  # first prompt: table names only, no columns yet
        self.assertIn("discount", seen[2])                                     # after describe the columns of just that table are in
        self.assertNotIn("regions(", seen[2])

    def test_a_failing_step_and_a_failing_final_query_are_worked_around(self):
        replies = ['{"action": "describe", "tables": ["nope"]}',
                   '{"action": "final", "sql": "select nothing from orders"}',
                   '{"action": "final", "sql": "select count(*) as n from orders"}']
        seen = []

        def fake(system, prompt, *a, **k):
            seen.append(prompt)
            return replies[len(seen) - 1], "m", "api"
        with mock.patch.object(magic, "ask_model", fake):
            r = sqlstudio.ask_sql(self.box, "how many orders", "sample", mode="agentic")
        self.assertEqual(r["sql"], "select count(*) as n from orders")
        self.assertIn("FAILED", seen[2])                                       # the model was shown why the first final query failed
        self.assertEqual([s["ok"] for s in r["steps"]][:2], [True, False])

    def test_statement_builders_follow_the_dialect(self):
        pg, my, ms, mo = ({"driver": d} for d in ("postgresql", "mysql", "mssql", "mongodb"))
        self.assertEqual(sqlstudio.sample_stmt(pg, "t", 3), 'SELECT * FROM "t" LIMIT 3')
        self.assertEqual(sqlstudio.sample_stmt(my, "t", 3), "SELECT * FROM `t` LIMIT 3")
        self.assertEqual(sqlstudio.sample_stmt(ms, "t", 3), "SELECT TOP 3 * FROM [t]")
        self.assertEqual(sqlstudio.sample_stmt(mo, "t", 3), "db.t.find({}).limit(3)")
        self.assertIn("LIKE '%o''x%'", sqlstudio.values_stmt(pg, "t", "c", "o'x"))
        self.assertIn("$group", sqlstudio.values_stmt(mo, "t", "c"))


class MentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)
        self.box = Box()
        sqlstudio.ensure_sample()
        sqlstudio._digests.clear()
        self.p = mock.patch.object(sqlstudio, "call", fake_call)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        store.ui_dir = self.old

    def test_nothing_is_added_without_a_mention(self):
        self.assertEqual(sqlstudio.chat_brief(self.box, "draw me a chart of sales"), "")
        self.assertEqual(sqlstudio.mentioned("mail me@data.com about it"), [])

    def test_mention_resolves_by_slug_id_and_name(self):
        for tok in ("sample-data-sqlite", sqlstudio.SAMPLE_ID, "Sample"):
            if tok == "Sample":
                continue                                                       # a bare word is not a name
            self.assertEqual([c["id"] for c in sqlstudio.mentioned("look at @data:%s please" % tok)], [sqlstudio.SAMPLE_ID])
        self.assertEqual(sqlstudio.mentioned("@data:unknown"), [])
        self.assertEqual(sqlstudio.mention_name(sqlstudio.get(sqlstudio.SAMPLE_ID)), "sample-data-sqlite")

    def test_brief_has_schema_and_how_to_query_and_draw_per_engine(self):
        t = "@data:sample-data-sqlite revenue by month as a line chart"
        claude = sqlstudio.chat_brief(self.box, t, "claude")
        self.assertIn("orders", claude)
        self.assertIn("daily_metrics", claude)
        self.assertIn("sqlcli.py", claude)
        self.assertIn("```echarts", claude)
        self.assertIn("-- conn: Sample data (SQLite)", claude)
        self.assertIn('"action":"query"', sqlstudio.chat_brief(self.box, t, "harness"))
        free = sqlstudio.chat_brief(self.box, t, "free")
        self.assertIn("cannot run queries", free)
        self.assertNotIn("sqlcli.py", free)

    def test_harness_offers_the_sql_tool_only_after_a_mention(self):
        from aihubui import harnesstools as HT
        with mock.patch.object(store, "get_session", return_value={"id": "x"}):
            t = HT.Tools(self.tmp, "x", "auto", {})
            self.assertNotIn("sql", [s["name"] for s in t.specs()])
        with mock.patch.object(store, "get_session", return_value={"id": "x", "data": [sqlstudio.SAMPLE_ID]}):
            t = HT.Tools(self.tmp, "x", "auto", {})
            self.assertIn("sql", [s["name"] for s in t.specs()])


if __name__ == "__main__":
    unittest.main()
