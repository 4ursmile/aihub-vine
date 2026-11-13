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


if __name__ == "__main__":
    unittest.main()
