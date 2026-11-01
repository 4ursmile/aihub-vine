"""aihub-ui ETL: list credential sets (buckets), aihub_vault grants, multi-step Python, the Trino node (fake coordinator), the Spark
launcher, sharing a work (no logins, mentioned files bundled, working folder dropped), the scheduler lease and the headless runner."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import secretbox, store, workpkg, workscli, works as W  # noqa: E402

BUCKETS = {"buckets": [{"name": "raw", "bucket": "raw-data", "endpoint": "http://minio:9000", "public": "AK-RAW", "secret": "SK-RAW-SECRET"},
                       {"name": "lake", "bucket": "lake-prod", "access_key_id": "AK-LAKE", "aws_secret_access_key": "SK-LAKE-SECRET", "region": "eu-west-1"}]}


def node(cfg, path):
    p = subprocess.run([sys.executable, os.path.join(UI, "nodes", path)], input=json.dumps(cfg), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONPATH=W.LIB_DIR))
    return json.loads(p.stdout.strip().splitlines()[-1]), p.stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "ui"), exist_ok=True)
        self.box = secretbox.Box(os.path.join(self.tmp, "ui"))
        W.A.run = lambda args, **k: {"data": []}
        W._cat.update(t=0.0, v={})
        self.engine = W.Engine(self.box)
        self.addCleanup(self.engine.shutdown)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old

    def lake(self):
        return W.save_credential({"kind": "s3", "name": "Lake", "fields": BUCKETS}, self.box)

    def save(self, nodes, edges, title="t", enabled=False):
        w = store.clean_work({"title": title, "enabled": enabled, "nodes": nodes, "edges": edges})
        W.strip_secrets(w, self.box)
        return store.save_work(w)

    def run_work(self, w, data=None):
        n, ev = W.manual_event(w, None, "", data or {})
        rid = self.engine.fire(w, n, ev, "test")
        end = time.time() + 60
        while time.time() < end:
            r = W.get_run(w["id"], rid)
            if r and r["status"] in ("done", "failed", "stopped"):
                return r
            time.sleep(0.1)
        self.fail("run did not end")


class ListCredentials(Base):
    def test_buckets_take_aliases_and_keep_secrets_only_in_the_box(self):
        c = self.lake()
        rows = c["fields"]["buckets"]
        self.assertEqual([(r["name"], r["bucket"], r["access_key"], r["secret_key"]) for r in rows],
                         [("raw", "raw-data", "AK-RAW", W.MARK), ("lake", "lake-prod", "AK-LAKE", W.MARK)])
        self.assertEqual(rows[0]["endpoint_url"], "http://minio:9000")
        self.assertNotIn("SECRET", open(W._creds_file()).read())
        full = W.resolve_credential(c["id"], self.box)
        self.assertEqual([b["secret_key"] for b in full["fields"]["buckets"]], ["SK-RAW-SECRET", "SK-LAKE-SECRET"])

    def test_resave_with_mark_keeps_a_secret_and_a_dropped_bucket_loses_its_own(self):
        c = self.lake()
        raw, lake = c["fields"]["buckets"]
        W.save_credential({"id": c["id"], "kind": "s3", "name": "Lake", "fields": {"buckets": [dict(raw, bucket="raw-v2")]}}, self.box)
        full = W.resolve_credential(c["id"], self.box)["fields"]["buckets"]
        self.assertEqual([(b["bucket"], b["secret_key"]) for b in full], [("raw-v2", "SK-RAW-SECRET")])
        self.assertFalse([k for k in self.box.get_all() if lake["id"] in k])
        self.assertTrue(W.delete_credential(c["id"], self.box))
        self.assertFalse([k for k in self.box.get_all() if k.startswith("cred:")])

    def test_a_free_kind_holds_any_list_and_a_dict_of_entries_works_too(self):
        c = W.save_credential({"kind": "generic", "name": "Mixed", "fields": {"region": "x", "dbs": {"main": {"host": "h", "password": "PW1"}}}}, self.box)
        self.assertEqual(c["fields"]["dbs"][0]["name"], "main")
        self.assertEqual(c["fields"]["dbs"][0]["password"], W.MARK)
        self.assertEqual(W.resolve_credential(c["id"], self.box)["fields"]["dbs"][0]["password"], "PW1")

    def test_template_and_headless_add_from_stdin(self):
        self.assertEqual(set(W.kind_template(W.kinds()["s3"])["buckets"][0]), {"name", "bucket", "endpoint_url", "region", "access_key", "secret_key", "path_style"})
        sys.stdin, old = io.StringIO(json.dumps(BUCKETS["buckets"])), sys.stdin
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(workscli.main(["cred", "add", "--kind", "s3", "--name", "From stdin", "--file", "-"]), 0)
        finally:
            sys.stdin = old
        c = next(c for c in W.credentials().values() if c["name"] == "From stdin")
        self.assertEqual(len(c["fields"]["buckets"]), 2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            workscli.main(["--json", "cred", "show", c["id"]])
        self.assertNotIn("SECRET", buf.getvalue())


class Vault(Base):
    def test_a_node_sees_only_the_sets_granted_to_it_and_the_grant_file_is_gone_after(self):
        c = self.lake()
        other = W.save_credential({"kind": "generic", "name": "Not granted", "fields": {"token": "NOPE-TOKEN"}}, self.box)
        code = ("# %% read\nb = vault.bucket('raw')\nseen = [b.bucket, b.endpoint_url, b.path_style, b.secret_key == 'SK-RAW-SECRET']\n"
                "# %% names\nresult['seen'] = seen\nresult['sets'] = [s['name'] for s in vault.info()['sets']]\nresult['mode'] = vault.info()['mode']\nprint(b.secret_key)\n")
        w = self.save([{"id": "s", "type": "start.manual"}, {"id": "p", "type": "code.python", "vault": [c["id"]], "config": {"code": code}}], [{"from": "s", "to": "p"}])
        r = self.run_work(w)
        self.assertEqual(r["status"], "done", r)
        out = json.loads(r["nodes"]["p"]["output"])
        self.assertEqual(out["seen"], ["raw-data", "http://minio:9000", True, True])
        self.assertEqual((out["sets"], out["mode"]), (["Lake"], "node"))
        self.assertNotIn(other["name"], out["sets"])
        self.assertEqual(os.listdir(store.ui_dir("tmp")), [])
        log = W.read_log(w["id"], r["id"], "p")["text"]
        self.assertIn("step 2/2 names: ok", log)
        self.assertNotIn("SK-RAW-SECRET", log)

    def test_local_mode_reads_every_set_and_masks_secrets_in_repr(self):
        self.lake()
        os.environ.pop("AIHUB_VAULT_FILE", None)
        sys.path.insert(0, W.LIB_DIR)
        import aihub_vault as v
        v.reload()
        b = v.bucket("lake-prod")
        self.assertEqual((b.name, b.region, b.path_style), ("lake", "eu-west-1", False))
        self.assertNotIn("SK-LAKE", repr(b))
        conf = v.s3a_conf()
        self.assertEqual(conf["spark.hadoop.fs.s3a.bucket.raw-data.endpoint"], "http://minio:9000")
        self.assertEqual(conf["spark.hadoop.fs.s3a.bucket.raw-data.path.style.access"], "true")
        self.assertEqual(conf["spark.hadoop.fs.s3a.bucket.raw-data.connection.ssl.enabled"], "false")
        self.assertEqual(b.storage_options()["client_kwargs"], {"region_name": "eu-west-1"})
        with self.assertRaises(KeyError):
            v.credential("nope")

    def test_a_deleted_or_shared_without_logins_grant_fails_the_node_clearly(self):
        w = self.save([{"id": "s", "type": "start.manual"}, {"id": "p", "type": "code.python", "vault": ["gone"], "config": {"code": "result['x']=1\n# %% b\n"}}], [{"from": "s", "to": "p"}])
        r = self.run_work(w)
        self.assertIn("aihub_vault", r["nodes"]["p"]["error"])


class Steps(unittest.TestCase):
    def test_steps_share_one_namespace_and_a_failing_step_is_named(self):
        res, outp = node({"config": {"code": "# %% one\nx = data['n'] + 1\n# %% two\nresult['x'] = x * 2\n", "args": "{\"n\": 4}"}}, "python_fn.py")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["x"], 10)
        self.assertEqual([s["name"] for s in res["data"]["steps"]], ["one", "two"])
        self.assertIn("step 1/2 one: ok", outp)
        res, _ = node({"config": {"code": "# %% load\nimport json\n# %% boom\nraise ValueError('bad row 7')\n# %% never\nresult['x']=1\n"}}, "python_fn.py")
        self.assertFalse(res["ok"])
        self.assertIn("step 2/3 boom failed", res["error"])
        self.assertIn("bad row 7", res["error"])
        self.assertIn("line 4", res["error"])

    def test_typed_main_after_the_steps_still_works_and_old_code_is_unchanged(self):
        res, _ = node({"config": {"code": "# %% prep\nK = 3\n# %% fn\ndef main(a: dict) -> dict:\n    return {'y': a['n'] * K}\n", "args": "{\"n\": 2}"}}, "python_fn.py")
        self.assertEqual(res["data"], {"y": 6})
        res, _ = node({"config": {"code": "def main(a: dict) -> dict:\n    return {'z': 1}\n"}}, "python_fn.py")
        self.assertEqual(res["data"], {"z": 1})

    def test_split_ignores_a_comment_only_head(self):
        sys.path.insert(0, os.path.join(UI, "nodes"))
        import _steps
        self.assertEqual([c[0] for c in _steps.split("# my etl\n\n# %% a\nx=1\n#%% b - load\ny=2")], ["a", "b - load"])


class FakeTrino(BaseHTTPRequestHandler):
    Q = {}

    def log_message(self, *a):
        pass

    def _j(self, d, h=None):
        b = json.dumps(d).encode()
        self.send_response(200)
        for k, v in (h or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        sql = self.rfile.read(int(self.headers["Content-Length"])).decode()
        FakeTrino.Q.setdefault("auth", []).append(self.headers.get("Authorization"))
        FakeTrino.Q.setdefault("sql", []).append(sql)
        if sql.lower().startswith("use "):
            return self._j({"stats": {"state": "FINISHED"}}, {"X-Trino-Set-Schema": "tiny"})
        if "boom" in sql:
            return self._j({"error": {"errorName": "SYNTAX_ERROR", "message": "mismatched input", "errorLocation": {"lineNumber": 1, "columnNumber": 8}}})
        self._j({"nextUri": "http://%s/v1/statement/x/1?schema=%s" % (self.headers["Host"], self.headers.get("X-Trino-Schema", ""))})

    def do_GET(self):
        schema = self.path.split("schema=")[-1]
        if "/1?" in self.path:
            return self._j({"columns": [{"name": "n"}, {"name": "schema"}], "data": [[1, schema], [2, schema]], "nextUri": "http://%s%s" % (self.headers["Host"], self.path.replace("/1?", "/2?"))})
        self._j({"columns": [{"name": "n"}, {"name": "schema"}], "data": [[3, schema]], "stats": {"state": "FINISHED"}})


class Trino(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTrino)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = str(cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def cfg(self, **k):
        return {"config": dict({"host": "127.0.0.1", "port": self.port, "http_scheme": "http", "user": "me"}, **k), "workdir": tempfile.mkdtemp()}

    def test_statements_run_in_one_session_and_the_last_result_is_kept(self):
        res, outp = node(self.cfg(sql="USE tpch.tiny;\n-- %% count it\nSELECT ';' AS a, 1 -- not; a split\n", max_rows=2), "trino_query.py")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["row_count"], 3)
        self.assertTrue(res["data"]["truncated"])
        self.assertEqual(res["data"]["rows"][0], {"n": 1, "schema": "tiny"})            # the USE of step 1 reached step 2
        self.assertEqual([s["name"] for s in res["data"]["steps"]], ["USE tpch.tiny", "count it"])
        self.assertIn("step 2/2 count it: ok", outp)
        self.assertIn("SELECT ';' AS a, 1", FakeTrino.Q["sql"][-1])

    def test_path_output_streams_every_row_to_a_file(self):
        c = self.cfg(sql="select 1", output="path", file_format="jsonl", file_name="out")
        res, _ = node(c, "trino_query.py")
        self.assertEqual(res["output"], os.path.join(c["workdir"], "out.jsonl"))
        self.assertEqual(len(open(res["output"]).read().splitlines()), 3)

    def test_errors_name_the_step_and_a_password_needs_https(self):
        res, _ = node(self.cfg(sql="select 1;\nselect boom"), "trino_query.py")
        self.assertIn("step 2/2", res["error"])
        self.assertIn("SYNTAX_ERROR (line 1, column 8)", res["error"])
        res, _ = node(self.cfg(sql="select 1", password="pw"), "trino_query.py")
        self.assertIn("https", res["error"])


class Spark(unittest.TestCase):
    def test_without_pyspark_the_launcher_says_how_to_install_it(self):
        try:
            import pyspark  # noqa: F401
            self.skipTest("pyspark is installed here")
        except ImportError:
            pass
        res, _ = node({"config": {"code": "# %% a\nresult['n'] = 1\n"}, "workdir": tempfile.mkdtemp()}, "spark_job.py")
        self.assertFalse(res["ok"])
        self.assertIn("pip install pyspark", res["error"])

    def test_spark_settings_must_be_key_value_lines(self):
        res, _ = node({"config": {"code": "x=1", "conf": "spark.executor.memory=2g\nnot a setting"}, "workdir": tempfile.mkdtemp()}, "spark_job.py")
        self.assertIn("key=value", res["error"])


class Share(Base):
    def test_export_strips_logins_bundles_mentions_and_drops_machine_folders(self):
        c = self.lake()
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, "data"))
        open(os.path.join(proj, "brief.md"), "w").write("the brief")
        open(os.path.join(proj, "data", "a.csv"), "w").write("a,b\n1,2\n")
        open(os.path.join(proj, ".env"), "w").write("TOKEN=x")
        workpkg.HOME_ROOT = self.tmp
        self.addCleanup(setattr, workpkg, "HOME_ROOT", None)
        w = self.save([{"id": "s", "type": "start.manual"},
                       {"id": "c", "type": "ai.claude", "config": {"cwd": proj, "prompt": "Read @file:brief.md and @folder:data, not @file:.env"}},
                       {"id": "p", "type": "etl.spark", "credential": c["id"], "vault": [c["id"]], "config": {"code": "x=1"}}],
                      [{"from": "s", "to": "c"}, {"from": "c", "to": "p"}])
        dest = os.path.join(self.tmp, "export")
        os.makedirs(dest)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(workscli.main(["export", w["id"], "-o", dest]), 0)
        wf = os.path.join(dest, "t.work.json")
        shared = json.load(open(wf))
        cn, pn = shared["nodes"][1], shared["nodes"][2]
        self.assertEqual((cn["config"]["cwd"], cn["folder_hint"]), ("", "proj"))
        self.assertIn("@file:{{work.files}}/brief.md", cn["config"]["prompt"])
        self.assertIn("@file:{{work.files}}/data", cn["config"]["prompt"])
        self.assertIn("@file:.env", cn["config"]["prompt"])                         # a secret file is never bundled
        self.assertEqual(sorted(os.listdir(workpkg.files_dir_of(wf))), ["brief.md", "data"])
        self.assertNotIn("credential", pn)
        self.assertNotIn("vault", pn)
        self.assertEqual((pn["credential_kind"], pn["vault_kinds"]), ("s3", [{"kind": "s3", "name": "Lake"}]))
        self.assertNotIn("SK-", open(wf).read())

        W.delete_credential(c["id"], self.box)                                       # another machine: no sets yet
        r = workpkg.import_file(wf)
        w2 = r["work"]
        self.assertEqual(r["files"], 2)
        self.assertTrue(os.path.isfile(os.path.join(W.work_files_dir(w2["id"]), "brief.md")))
        self.assertEqual(sorted(x["what"] for x in r["needs"] if x["what"] not in ("env", "system")), ["credential", "folder", "vault"])
        self.assertTrue(any(x["what"] == "env" and x["node"] == "p" for x in r["needs"]) or W.nodeenv.check(W.catalogue()["etl.spark"], {})["ok"])
        sys.stdin, old = io.StringIO(json.dumps({"s3": BUCKETS})), sys.stdin
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(workscli.main(["setup", w2["id"], "--creds", "-", "--folder", "c=" + proj]), 0)
        finally:
            sys.stdin = old
        w2 = store.get_work(w2["id"])
        self.assertEqual(workpkg.needs_setup(w2, env=False), [])                    # logins and folders done (pyspark / Java: `aihub work env`, tests/test_ui_codenodes)
        pn = w2["nodes"][2]
        self.assertEqual(pn["vault"], [pn["credential"]])
        self.assertEqual(w2["nodes"][1]["config"]["cwd"], proj)

    def test_an_installed_workflow_package_is_found_without_a_type_in_its_record(self):
        pkg = os.path.join(self.tmp, "pkg")
        os.makedirs(os.path.join(pkg, "works"))
        json.dump({"title": "From pkg", "nodes": [{"id": "s", "type": "start.manual"}], "edges": []}, open(os.path.join(pkg, "works", "x.work.json"), "w"))
        old = workpkg.A.run
        workpkg.A.run = lambda args, **k: {"data": [{"name": "wf-x", "path": pkg}, {"name": "a-skill", "path": pkg, "type": "skill"}]}
        self.addCleanup(setattr, workpkg.A, "run", old)
        self.assertEqual([(x["package"], x["title"]) for x in workpkg.workflows()], [("wf-x", "From pkg")])

    def test_creds_without_a_name_take_the_name_the_work_was_shared_with(self):
        w = self.save([{"id": "s", "type": "start.manual"}, {"id": "q", "type": "db.trino", "credential_kind": "trino", "credential_name": "Warehouse", "config": {"sql": "select 1"}}],
                      [{"from": "s", "to": "q"}])
        sys.stdin, old = io.StringIO(json.dumps({"trino": {"host": "h", "username": "bob", "password": "PW-BOB"}})), sys.stdin
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(workscli.main(["setup", w["id"], "--creds", "-"]), 0)
        finally:
            sys.stdin = old
        c = W.credential(store.get_work(w["id"])["nodes"][1]["credential"])
        self.assertEqual((c["name"], c["fields"]["user"], c["fields"]["password"]), ("Warehouse", "bob", W.MARK))

    def test_work_files_are_rendered_into_prompts_and_read_as_mentions(self):
        w = self.save([{"id": "s", "type": "start.manual"}], [])
        d = W.work_files_dir(w["id"], make=True)
        open(os.path.join(d, "spec.txt"), "w").write("SPEC-BODY")
        txt = W.render("see @file:{{work.files}}/spec.txt", {"work": {"files": d}})
        self.assertIn("SPEC-BODY", W.mention_text(txt, self.tmp))


class Lease(Base):
    def test_one_scheduler_per_machine_and_a_dead_holder_is_replaced(self):
        self.assertTrue(self.engine.lease.take())
        h = self.engine.lease.holder()
        h.update(pid=999999, beat=time.time())                                       # another (dead) process on this host
        store._write(self.engine.lease.path, h)
        self.assertTrue(W.Lease("serve").take())
        h.update(pid=1, host="elsewhere", beat=time.time())                          # alive somewhere else: not taken until stale
        store._write(self.engine.lease.path, h)
        self.assertFalse(W.Lease("serve").take())
        h["beat"] = time.time() - W.LEASE_STALE - 1
        store._write(self.engine.lease.path, h)
        self.assertTrue(W.Lease("serve").take())

    def test_a_cli_run_does_not_mark_runs_of_a_live_process_interrupted(self):
        d = W.runs_dir("abcd1234")
        os.makedirs(d, exist_ok=True)
        live = {"id": "r1", "status": "running", "pid": os.getppid(), "host": W._host()}
        dead = {"id": "r2", "status": "running", "pid": 999999, "host": W._host()}
        for r in (live, dead):
            open(os.path.join(d, r["id"] + ".json"), "w").write(json.dumps(r))
        W.Engine(self.box, schedule=False).shutdown()
        self.assertEqual(json.load(open(os.path.join(d, "r1.json")))["status"], "running")
        self.assertEqual(json.load(open(os.path.join(d, "r2.json")))["status"], "interrupted")


class Headless(Base):
    def test_run_from_the_terminal_returns_the_result_and_the_exit_code(self):
        w = self.save([{"id": "s", "type": "start.manual", "config": {"inputs": "n|N|number||*"}},
                       {"id": "p", "type": "code.python", "label": "Double", "config": {"code": "# %% x\nresult['d'] = data['n'] * 2\n", "args": "{\"n\": {{data.n}}}"}}],
                      [{"from": "s", "to": "p"}], title="Doubler")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(workscli.main(["--json", "run", "doubler", "--input", "n=21"]), 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["status"], "done")
        self.assertEqual(json.loads(doc["result"]["text"])["d"], 42)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(workscli.main(["run", w["id"]]), 1)                     # the required input is missing
        with redirect_stdout(io.StringIO()):
            self.assertEqual(workscli.main(["set", w["id"], "p", "timeout=30", "--folder", self.tmp]), 0)
        n = store.get_work(w["id"])["nodes"][1]
        self.assertEqual((n["config"]["timeout"], n["config"]["cwd"]), (30, self.tmp))


if __name__ == "__main__":
    unittest.main()
