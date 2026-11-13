"""aihub-ui SQL Studio: saved connections (secrets kept out of the JSON file), the SQLAlchemy runner and the driver catalog."""
import json
import os
import sys
import tempfile
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import sqlstudio, store  # noqa: E402


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


class SqlStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.ui_dir
        store.ui_dir = lambda *a: os.path.join(self.tmp, *a)
        self.box = Box()

    def tearDown(self):
        store.ui_dir = self.old

    def test_password_never_in_the_json_file(self):
        c = sqlstudio.save(self.box, {"name": "pg", "driver": "postgresql", "host": "h", "user": "u", "password": "s3cret"})
        self.assertNotIn("s3cret", json.dumps(sqlstudio.connections()))
        with open(os.path.join(self.tmp, "sqlstudio.json")) as f:
            self.assertNotIn("s3cret", f.read())
        self.assertTrue(c["has_secret"])
        sqlstudio.save(self.box, {"id": c["id"], "name": "pg2", "driver": "postgresql", "host": "h"})         # blank password keeps it
        self.assertEqual(sqlstudio.conn_fields(self.box, sqlstudio.get(c["id"]))["password"], "s3cret")
        self.assertTrue(sqlstudio.delete(self.box, c["id"]))
        self.assertEqual(self.box.d, {})

    def test_url_is_masked_and_validated(self):
        with self.assertRaises(ValueError):
            sqlstudio.save(self.box, {"name": "x", "driver": "other", "url": "nonsense"})
        c = sqlstudio.save(self.box, {"name": "x", "driver": "other", "url": "postgresql://u:pw@h/db"})
        self.assertNotIn("pw", c["url_masked"])

    def test_catalog_lists_extra_drivers(self):
        keys = {d["key"] for d in sqlstudio.catalog()}
        self.assertTrue({"postgresql", "sqlite", "snowflake", "clickhouse"} <= keys)

    @unittest.skipUnless(have_sqlalchemy(), "sqlalchemy not installed for this Python")
    def test_query_and_readonly(self):
        import sqlite3
        db = os.path.join(self.tmp, "t.db")
        con = sqlite3.connect(db)
        con.execute("create table t(a integer, b text)")
        con.execute("insert into t values (1,'x'),(2,null)")
        con.commit()
        con.close()
        c = sqlstudio.save(self.box, {"name": "t", "driver": "sqlite", "database": db})
        r = sqlstudio.call(self.box, c["id"], {"action": "query", "sql": "select * from t order by a", "limit": 1})
        self.assertTrue(r["ok"] and r["truncated"] and r["rows"] == [[1, "x"]])
        self.assertEqual(sqlstudio.call(self.box, c["id"], {"action": "tables"})["tables"][0]["name"], "t")
        ro = sqlstudio.save(self.box, {"id": c["id"], "name": "t", "driver": "sqlite", "database": db, "readonly": True})
        r = sqlstudio.call(self.box, ro["id"], {"action": "query", "sql": "delete from t"})
        self.assertFalse(r["ok"])

    def test_resolve_by_name_id_and_prefix(self):
        a = sqlstudio.save(self.box, {"name": "Sales DB", "driver": "sqlite"})
        b = sqlstudio.save(self.box, {"name": "Stock", "driver": "sqlite"})
        self.assertEqual(sqlstudio.resolve("sales db")["id"], a["id"])
        self.assertEqual(sqlstudio.resolve(b["id"])["id"], b["id"])
        self.assertEqual(sqlstudio.resolve("sto")["id"], b["id"])
        with self.assertRaises(ValueError):
            sqlstudio.resolve("s")                                   # ambiguous: lists what exists
        with self.assertRaises(ValueError):
            sqlstudio.resolve("")

    @unittest.skipUnless(have_sqlalchemy(), "sqlalchemy not installed for this Python")
    def test_ai_context_across_connections_and_readonly_query(self):
        import sqlite3
        ids = []
        for n, ddl in (("one", "create table cust(id integer primary key, name text)"), ("two", "create table orders(id integer, cust_id integer references cust(id))")):
            db = os.path.join(self.tmp, n + ".db")
            con = sqlite3.connect(db)
            con.execute(ddl)
            con.execute("insert into %s values (1, 1)" % ("cust" if n == "one" else "orders"))
            con.commit()
            con.close()
            ids.append(sqlstudio.save(self.box, {"name": n, "driver": "sqlite", "database": db})["id"])
        text = sqlstudio.ai_context(self.box)
        self.assertIn("## one", text)
        self.assertIn("## two", text)
        self.assertIn("cust", text)
        self.assertIn("orders", text)
        r = sqlstudio.ai_query(self.box, "one", "select * from cust")
        self.assertTrue(r["ok"])
        self.assertIn("id | name", r["text"])
        r = sqlstudio.ai_query(self.box, "one", "delete from cust")          # the AI never writes, even on a writable connection
        self.assertFalse(r["ok"])
        self.assertTrue(r["text"].startswith("ERROR"))
        self.assertTrue(sqlstudio.ai_query(self.box, "one", "select count(*) from cust")["ok"])

    def test_ask_sql_parses_the_model_answer(self):
        from aihubui import magic
        sqlstudio.save(self.box, {"name": "one", "driver": "sqlite"})
        sqlstudio.save(self.box, {"name": "two", "driver": "sqlite"})
        old, oldc = magic.ask_model, sqlstudio.ai_context
        magic.ask_model = lambda *a, **k: ("```sql\n-- conn: two\nselect 1\n```\nAssumes two.", "m", "direct")
        sqlstudio.ai_context = lambda *a, **k: "schema"
        try:
            r = sqlstudio.ask_sql(self.box, "count", conn="one")
        finally:
            magic.ask_model, sqlstudio.ai_context = old, oldc
        self.assertEqual(r["sql"], "select 1")
        self.assertEqual(r["conn"], sqlstudio.resolve("two")["id"])
        self.assertIn("Assumes", r["note"])


if __name__ == "__main__":
    unittest.main()
