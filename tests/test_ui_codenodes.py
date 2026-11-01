"""aihub-ui code nodes: Python environments (nodeenv: what a node needs, checks in THAT python, install jobs, managed envs), the SQL
(SQLAlchemy) node, aihub_data lazy imports, Magic ask (prompt never holds secrets, answers checked against the fields), Run from the
code editor (works/try) and sharing (requires travel, a machine path does not, setup reports what to install)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import magic, nodeenv, secretbox, store, workpkg, works as W  # noqa: E402

LIB = os.path.join(UI, "lib")
sys.path.insert(0, LIB)
import aihub_data  # noqa: E402

HAS_SA = aihub_data.has("sqlalchemy")
VENV = None


def setUpModule():
    """One bare venv (no pip, nothing installed) shared by the tests as the managed env `data`."""
    global VENV
    VENV = tempfile.mkdtemp()
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(VENV, "env")], check=True, capture_output=True, timeout=120)


def tearDownModule():
    shutil.rmtree(VENV, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "ui", "envs"), exist_ok=True)
        os.symlink(os.path.join(VENV, "env"), os.path.join(self.tmp, "ui", "envs", "data"))
        self.box = secretbox.Box(os.path.join(self.tmp, "ui"))
        W.A.run = lambda args, **k: {"data": []}
        W._cat.update(t=0.0, v={})
        nodeenv.forget()
        self.cat = W.catalogue()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)


class Requirements(Base):
    def mods(self, t, cfg, extra=None):
        return [r["module"] for r in nodeenv.requirements(self.cat[t], cfg, extra)]

    def test_sql_needs_sqlalchemy_the_driver_and_the_frame_library(self):
        self.assertEqual(self.mods("db.sql", {"driver": "postgresql"}), ["sqlalchemy", "psycopg"])
        self.assertEqual(self.mods("db.sql", {"driver": "mssql", "frame": "pandas"}), ["sqlalchemy", "pymssql", "pandas"])
        self.assertEqual(self.mods("db.sql", {"driver": "oracle", "frame": "polars", "write_table": "t"}), ["sqlalchemy", "oracledb", "polars", "pandas", "pyarrow"])
        self.assertEqual(self.mods("db.sql", {"driver": "sqlite"}), ["sqlalchemy"])
        self.assertEqual(self.mods("db.sql", {"url": "trino://me@host:443/hive"}), ["sqlalchemy", "trino"])
        pips = {r["module"]: r["pip"] for r in nodeenv.requirements(self.cat["db.sql"], {"driver": "postgresql"})}
        self.assertEqual(pips, {"sqlalchemy": "sqlalchemy>=2", "psycopg": "psycopg[binary]"})

    def test_code_imports_skip_the_stdlib_and_our_helpers(self):
        code = "import json, os\nimport pandas as pd\nfrom sklearn.linear_model import X\nimport aihub_vault, aihub_data\nfrom . import local\n"
        self.assertEqual(self.mods("code.python", {"code": code}), ["pandas", "sklearn"])
        self.assertEqual({r["module"]: r["pip"] for r in nodeenv.requirements(self.cat["code.python"], {"code": code})}["sklearn"], "scikit-learn")
        self.assertEqual(nodeenv.code_imports("import numpy\nthis is not python ("), ["numpy"])         # line scan when it does not parse
        self.assertEqual(self.mods("etl.spark", {"code": "from pyspark.sql import functions as F"}), ["pyspark"])
        self.assertEqual(self.mods("code.python", {"code": "x = 1"}, ["polars>=1", "psycopg[binary]"]), ["polars", "psycopg"])

    def test_package_specs_only(self):
        for bad in ("-r reqs.txt", "--index-url=http://x", "http://evil/pkg.whl", "../pkg", "pkg; rm -rf /", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                nodeenv._valid([bad])
        self.assertEqual(nodeenv._valid(["polars", "psycopg[binary]>=3,<4", " sqlalchemy>=2 "]), ["polars", "psycopg[binary]>=3,<4", "sqlalchemy>=2"])


class Checks(Base):
    def test_resolve_names_paths_and_the_ui_python(self):
        self.assertEqual(nodeenv.resolve("")[0], sys.executable)
        py, name = nodeenv.resolve("data")
        self.assertEqual((name, os.path.isfile(py)), ("data", True))
        self.assertEqual(nodeenv.resolve(os.path.join(VENV, "env"))[0], nodeenv._venv_python(os.path.join(VENV, "env")))
        py, name = nodeenv.resolve("later")
        self.assertEqual((name, os.path.exists(py)), ("later", False))
        self.assertFalse(os.path.exists(nodeenv.envs_dir("later")))                       # looking never creates it
        with self.assertRaisesRegex(ValueError, "not created yet"):
            nodeenv.interpreter("later")
        with self.assertRaisesRegex(ValueError, "does not look like a Python"):
            nodeenv.interpreter(shutil.which("ls") or "/bin/ls")
        self.assertEqual([e["name"] for e in nodeenv.envs()], ["data"])

    def test_check_runs_in_the_nodes_python(self):
        c = nodeenv.check(self.cat["db.sql"], {"python": "data", "driver": "mysql"})
        self.assertTrue(c["exists"] and c["venv"])
        self.assertEqual(c["python"], nodeenv._venv_python(nodeenv.envs_dir("data")))
        self.assertEqual(c["missing"], ["sqlalchemy>=2", "pymysql"])                  # a bare venv has none of them
        self.assertFalse(c["ok"])
        self.assertIn("-m pip install", c["command"])
        self.assertIn("pymysql", c["command"])
        new = nodeenv.check(self.cat["db.sql"], {"python": "fresh"})
        self.assertFalse(new["exists"])
        self.assertIn("-m venv", new["command"])                                     # the one command makes the env first
        ok = nodeenv.check(self.cat["code.python"], {"python": "data", "code": "import json\ndef main(a: dict) -> dict:\n    return a\n"})
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["packages"], [])

    def test_install_job_creates_the_env_streams_and_reports(self):
        calls = []

        def fake(job, argv):
            calls.append(argv)
            nodeenv._say(job, "line from %s" % os.path.basename(argv[0]))
            if "venv" in argv:
                os.makedirs(os.path.dirname(nodeenv._venv_python(argv[-1])), exist_ok=True)
                shutil.copy(sys.executable, nodeenv._venv_python(argv[-1]))
            return 0
        old = nodeenv._stream
        nodeenv._stream = fake
        try:
            job = nodeenv.install("brandnew", ["polars"])
            v = nodeenv.wait(job["id"], timeout=60)
        finally:
            nodeenv._stream = old
        self.assertEqual(v["state"], "done", v)
        self.assertEqual(calls[0][1:3], ["-m", "venv"])
        self.assertIn("polars", calls[-1])
        self.assertEqual(v["lines"][-1], "done")
        self.assertEqual(nodeenv.job_view(job["id"], v["next"])["lines"], [])
        with self.assertRaises(ValueError):
            nodeenv.install("data", ["--pre"])
        with self.assertRaises(ValueError):
            nodeenv.install("data", [])

    def test_a_node_runs_with_its_environment(self):
        spec = self.cat["code.python"]
        code = "import sys\ndef main(args: dict) -> dict:\n    return {'py': sys.executable}\n"
        r = W.run_single(spec, {"code": code, "python": "data"}, "", store.ui_dir())
        self.assertEqual(os.path.realpath(r["data"]["py"]), os.path.realpath(nodeenv._venv_python(nodeenv.envs_dir("data"))))
        with self.assertRaisesRegex(RuntimeError, "not created yet"):
            W.run_single(spec, {"code": code, "python": "nope"}, "", store.ui_dir())


class SqlNode(Base):
    def run_node(self, cfg, inputs=None, python=None):
        d = {"config": cfg, "inputs": inputs or [], "input": "", "workdir": self.tmp}
        p = subprocess.run([python or sys.executable, os.path.join(UI, "nodes", "sql_query.py")], input=json.dumps(d), capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PYTHONPATH=LIB, AIHUB_NODE_DIR=os.path.join(self.tmp, "node")))
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_a_missing_package_says_what_to_install(self):
        venv_py = nodeenv._venv_python(nodeenv.envs_dir("data"))
        r = self.run_node({"driver": "oracle", "sql": "select 1 from dual"}, python=venv_py)
        self.assertFalse(r["ok"])
        self.assertIn("sqlalchemy>=2 is not installed", r["error"])
        self.assertIn("-m pip install", r["error"])

    def test_missing_message_of_aihub_data(self):
        with self.assertRaises(aihub_data.Missing) as cm:
            aihub_data.need("surely_not_a_module_xyz", "surely-not", "the test")
        self.assertEqual(cm.exception.pips, ["surely-not"])
        self.assertIn("surely-not", cm.exception.command)
        self.assertTrue(aihub_data.is_query("-- a\n/* b */ WITH x AS (select 1) select * from x"))
        self.assertFalse(aihub_data.is_query("insert into t values (1)"))

    @unittest.skipUnless(HAS_SA, "sqlalchemy is not installed for this python")
    def test_sqlite_write_then_query_in_steps(self):
        db = os.path.join(self.tmp, "x.sqlite")
        rows = [{"country": "VN", "n": 2}, {"country": "FR", "n": 3}, {"country": "VN", "n": 5}]
        r = self.run_node({"driver": "sqlite", "database": db, "write_table": "orders", "if_exists": "replace",
                           "sql": "-- %% make\ncreate table extra (a int);\ninsert into extra values (1);\n-- %% totals\nselect country, sum(n) as total from orders group by country order by country"},
                          [{"text": "", "data": {"rows": rows}}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["rows"], [{"country": "FR", "total": 3}, {"country": "VN", "total": 7}])
        self.assertEqual(r["data"]["written"]["rows"], 3)
        self.assertEqual([s["name"] for s in r["data"]["steps"]], ["write orders", "make", "insert into extra", "totals"])
        r = self.run_node({"driver": "sqlite", "database": db, "sql": "select * from nope"})
        self.assertFalse(r["ok"])
        self.assertIn("no such table", r["error"])

    @unittest.skipUnless(HAS_SA, "sqlalchemy is not installed for this python")
    def test_urls_from_fields(self):
        u = aihub_data.url({"driver": "oracle", "host": "db", "user": "me", "password": "p w", "database": "XEPDB1"})
        self.assertEqual(u.drivername, "oracle+oracledb")
        self.assertEqual(u.query["service_name"], "XEPDB1")
        self.assertNotIn("p w", aihub_data.safe_url(u))
        u = aihub_data.url({"driver": "mssql-odbc", "host": "sql", "port": "1433", "user": "sa", "database": "sales"})
        self.assertEqual((u.drivername, u.query["driver"]), ("mssql+pyodbc", "ODBC Driver 18 for SQL Server"))
        self.assertEqual(aihub_data.url({"driver": "trino", "host": "t", "port": "8080"}).query["http_scheme"], "http")


class Magic(Base):
    def test_prompt_has_no_secrets_and_the_answer_is_checked(self):
        spec = self.cat["db.sql"]
        seen = {}
        answer = ('```json\n{"config": {"driver": "Oracle", "frame": "spark", "password": "hunter2", "max_rows": "50", "python": "x", "ghost": 1}, "note": "Reads orders."}\n```\n'
                  '```sql field=sql\nSELECT country, count(*) AS n FROM orders GROUP BY country\n```\n')

        def fake(system, prompt, model="", extra_env=None, engine="auto"):
            seen.setdefault("p", prompt)
            return answer, "m", "api"
        old = magic.ask_model
        magic.ask_model = fake
        try:
            r = magic.magic(spec, {"password": "TOPSECRET", "url": "postgresql://u:pw@h/db", "sql": "select 1"}, "orders per country from oracle",
                            context={"keys": [{"name": "day", "from": "from Start"}], "last": {"error": "boom"}})
            only = magic.magic(spec, {}, "x", field="sql")
        finally:
            magic.ask_model = old
        self.assertEqual(r["config"], {"driver": "oracle", "max_rows": 50, "sql": "SELECT country, count(*) AS n FROM orders GROUP BY country\n"})
        self.assertEqual({d["field"] for d in r["dropped"]}, {"frame", "password", "python", "ghost"})
        self.assertNotIn("TOPSECRET", seen["p"])
        self.assertNotIn("u:pw@h", seen["p"])
        self.assertIn("{{data.day}}", seen["p"])
        self.assertIn("boom", seen["p"])
        self.assertEqual(list(only["config"]), ["sql"])
        with self.assertRaises(ValueError):
            magic.magic(spec, {}, "   ")
        with self.assertRaises(ValueError):
            magic.magic(spec, {}, "x", field="password")

    def test_parse_plain_json_and_reject_prose(self):
        self.assertEqual(magic.parse('{"config": {"a": 1}, "note": "n"}'), ({"a": 1}, "n"))
        with self.assertRaises(ValueError):
            magic.parse("I cannot help with that.")


class TryRoute(Base):
    def test_run_from_the_editor_with_unsaved_settings_and_a_selection(self):
        import server
        server.BOX = self.box
        server.W._cat.update(t=0.0, v={})
        code = "import json\ndef main(args: dict) -> dict:\n    return {'rows': [{'a': 1}, {'a': 2}], 'got': args.get('day')}\n"
        r = server.work_try(None, {}, {"work": "", "node": {"id": "py", "type": "code.python", "config": {"code": code}}, "input": "", "data": {"day": "mon"}, "field": "code"})
        rid = r["run"]
        end = time.time() + 60
        while time.time() < end and server.TRY[rid]["status"] == "running":
            time.sleep(0.1)
        st = server.work_try_status(None, {"run": rid}, {})["run"]
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(st["result"]["data"], {"rows": [{"a": 1}, {"a": 2}], "got": "mon"})
        lg = server.work_log(None, {"work": "try", "run": rid, "node": "node"}, {})
        self.assertIn("python_fn.py", lg["text"])
        bad = server.work_try(None, {}, {"node": {"id": "py", "type": "code.python", "config": {"code": code}}, "field": "code", "selection": "raise SystemExit"})
        end = time.time() + 60
        while time.time() < end and server.TRY[bad["run"]]["status"] == "running":
            time.sleep(0.1)
        self.assertEqual(server.TRY[bad["run"]]["status"], "failed")
        with self.assertRaises(server.Err):
            server.work_try(None, {}, {"node": {"type": "flow.note", "config": {}}})
        self.assertIn("check", server.env_check(None, {}, {"type": "db.sql", "config": {"python": "data"}}))
        with self.assertRaises(server.Err):
            server.env_install(None, {}, {"env": "data", "packages": ["-e ."]})


class Sharing(Base):
    def test_requires_travel_and_a_machine_path_does_not(self):
        w = store.save_work(store.clean_work({"title": "etl", "nodes": [
            {"id": "s", "type": "start.manual", "config": {}},
            {"id": "q", "type": "db.sql", "config": {"driver": "mssql", "frame": "polars", "python": "data"}},
            {"id": "p", "type": "code.python", "config": {"code": "import pandas\ndef main(a: dict) -> dict:\n    return a\n", "python": "/opt/team/venv"}}],
            "edges": [{"from": "s", "to": "q"}, {"from": "q", "to": "p"}]}))
        out = workpkg.strip_for_share(w, self.cat)
        q = next(n for n in out["nodes"] if n["id"] == "q")
        p = next(n for n in out["nodes"] if n["id"] == "p")
        self.assertEqual(q["requires"], ["sqlalchemy>=2", "pymssql", "polars"])
        self.assertEqual(q["config"]["python"], "data")                              # a managed env NAME is made on the other machine
        self.assertEqual((p["config"]["python"], p["env_hint"], p["requires"]), ("", "venv", ["pandas"]))
        kept = store.clean_work(out)
        self.assertEqual(next(n for n in kept["nodes"] if n["id"] == "q")["requires"], ["sqlalchemy>=2", "pymssql", "polars"])
        bad = store.clean_work({"nodes": [{"id": "x", "type": "db.sql", "config": {}, "requires": ["ok-pkg", "-r evil", "http://x"]}]})
        self.assertEqual(bad["nodes"][0]["requires"], ["ok-pkg"])
        needs = workpkg.needs_setup(store.save_work(kept), self.cat)
        env = [x for x in needs if x["what"] == "env"]
        self.assertEqual(env[0]["node"], "q")
        self.assertEqual(env[0]["packages"], ["sqlalchemy>=2", "pymssql", "polars"])
        self.assertTrue(env[0]["required"])
        self.assertEqual([x for x in workpkg.needs_setup(kept, self.cat, env=False) if x["what"] == "env"], [])

    def test_cli_env_check_and_install_for_a_work(self):
        from aihubui import workscli
        w = store.save_work(store.clean_work({"title": "sql work", "nodes": [{"id": "q", "type": "db.sql", "config": {"driver": "sqlite", "python": "data"}}], "edges": []}))
        installed = []

        def fake(env, pips, spark=False):
            installed.append((env, list(pips)))
            job = {"id": "j1", "state": "done", "lines": ["ok"], "started": time.time(), "packages": pips, "python": "", "command": "x", "error": "", "env": env}
            nodeenv.JOBS["j1"] = dict(job, proc=None, cancel=False)
            return job
        old = nodeenv.install
        nodeenv.install = fake
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = workscli.main(["env", "check", w["id"], "--json"])
                self.assertEqual(rc, 1)                                              # a bare venv: sqlalchemy is missing
                rc = workscli.main(["env", "install", w["id"], "--env", "data", "--json"])
        finally:
            nodeenv.install = old
        self.assertEqual(rc, 0)
        self.assertEqual(installed, [("data", ["sqlalchemy>=2"])])
        self.assertEqual(store.get_work(w["id"])["nodes"][0]["config"]["python"], "data")


if __name__ == "__main__":
    unittest.main()
