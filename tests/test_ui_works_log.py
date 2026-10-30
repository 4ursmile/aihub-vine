"""aihub-ui Works: per-node logs, Stop, the running list, and the Publish node (no hang, same staging as Share to Space)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tests.test_ui_works import Base, UI, W, store, wait

NODE_JSON = {"id": "slow", "title": "Slow", "kind": "action", "run": ["{python}", "nodes/slow.py"], "timeout": 60,
             "fields": [{"name": "mode", "label": "Mode", "type": "text", "default": "ok"},
                        {"name": "key", "label": "Key", "type": "password", "secret": True}]}
PROGRAM = '''
import json, sys, time, subprocess
d = json.load(sys.stdin)
cfg = d["config"]
print("hello from the node", flush=True)
sys.stderr.write("careful: key is %s\\n" % cfg.get("key")); sys.stderr.flush()
m = cfg.get("mode")
if m == "sleep":
    time.sleep(60)
if m == "grandchild":
    subprocess.Popen(["sleep", "20"])          # keeps the inherited pipes open after this program exits
if m == "dirs":
    open(d["run"]["node_dir"] + "/made.txt", "w").write("x")
    print(json.dumps({"ok": True, "output": json.dumps({"run": d["run"], "workdir": d["workdir"], "env": __import__("os").environ.get("AIHUB_RUN_DIR")})}))
    sys.exit(0)
print(json.dumps({"ok": True, "output": "done " + m}))
'''


def make_pkg():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "nodes"))
    json.dump(NODE_JSON, open(os.path.join(d, "nodes", "slow.node.json"), "w"))
    open(os.path.join(d, "nodes", "slow.py"), "w").write(PROGRAM)
    return d


class Logs(Base):
    def setUp(self):
        super().setUp()
        self.pkg = make_pkg()
        W.A.run = lambda args, **k: {"data": [{"name": "slowpkg", "path": self.pkg, "enabled": True}]}
        W._cat.update(t=0.0, v={})
        self.t = "pkg:slowpkg/slow"

    def work(self, mode, n=2):
        nodes = [{"id": "s", "type": "start.manual", "config": {}}, {"id": "a", "type": self.t, "config": {"mode": mode, "key": "topsecret-123"}}]
        edges = [{"from": "s", "to": "a"}]
        if n > 1:
            nodes.append({"id": "b", "type": "flow.template", "config": {"template": "after {{input}}"}})
            edges.append({"from": "a", "to": "b"})
        return self.save(nodes, edges, enabled=False)

    def fire(self, w):
        return self.engine.fire(w, next(x for x in w["nodes"] if x["type"] == "start.manual"), {"text": "go"}, "manual")

    def finished(self, wid, rid, secs=15):
        return wait(lambda: (r := W.get_run(wid, rid)) and r["status"] not in ("queued", "running") and r, secs)

    def test_log_has_command_streams_exit_and_no_secret(self):
        w = self.work("ok")
        rid = self.fire(w)
        r = self.finished(w["id"], rid)
        self.assertEqual(r["status"], "done")
        rec = r["nodes"]["a"]
        self.assertEqual(rec["exit"], 0)
        self.assertIn("slow.py", rec["cmd"])
        self.assertTrue(rec["started"] and rec["ended"] >= rec["started"])
        lg = W.read_log(w["id"], rid, "a")["text"]
        self.assertIn("hello from the node", lg)
        self.assertIn("[err] careful", lg)
        self.assertIn("exit 0", lg)
        self.assertNotIn("topsecret-123", lg)
        self.assertIn("***", lg)

    def test_run_folder_reaches_programs_templates_and_is_listed(self):
        nodes = [{"id": "s", "type": "start.manual", "config": {}}, {"id": "a", "type": self.t, "config": {"mode": "dirs"}},
                 {"id": "t", "type": "flow.template", "config": {"template": "dir={{run.dir}} node={{node.dir}}"}}]
        w = self.save(nodes, [{"from": "s", "to": "a"}, {"from": "a", "to": "t"}], enabled=False)
        rid = self.fire(w)
        r = self.finished(w["id"], rid)
        self.assertEqual(r["status"], "done", r)
        rdir = W.run_dir(w["id"], rid, make=False)
        info = json.loads(r["nodes"]["a"]["output"])
        self.assertEqual(info["run"]["dir"], rdir)
        self.assertEqual(info["workdir"], rdir)
        self.assertEqual(info["env"], rdir)
        self.assertEqual(info["run"]["node_dir"], os.path.join(rdir, "a"))
        self.assertIn("dir=" + rdir, r["nodes"]["t"]["output"])
        self.assertEqual(r["dir"], rdir)
        files = W.run_files(w["id"], rid)
        self.assertEqual([(f["path"], f["node"]) for f in files], [("a/made.txt", "a")])
        self.assertEqual(r["event"]["text"], "go")                                 # kept so the run can be started again

    def test_delete_work_stops_the_run_and_removes_records_logs_and_folders(self):
        w = self.work("sleep", n=1)
        rid = self.fire(w)
        self.assertTrue(wait(lambda: (r := W.get_run(w["id"], rid)) and r["nodes"].get("a", {}).get("status") == "running", 10))
        rdir = W.runs_dir(w["id"])
        self.assertTrue(os.path.isdir(os.path.join(rdir, rid)))
        self.assertEqual(W.drop_work(w["id"]), 1)                                  # the running run was stopped first
        self.assertIsNone(store.get_work(w["id"]))
        self.assertFalse(os.path.exists(rdir))                                      # run records, node logs and run folders
        self.assertTrue(wait(lambda: rid not in W.CTL, 10))                          # the run ended
        self.assertEqual(W.drop_work(w["id"]), 0)                                   # deleting twice is harmless
        self.assertEqual(W.drop_work("../etc"), 0)                                  # bad ids touch nothing

    def test_log_offsets_and_cap(self):
        lg = W.NodeLog(os.path.join(tempfile.mkdtemp(), "x.log"))
        lg.write("one")
        first = open(lg.path, "rb").read()
        lg.write("two")
        self.assertTrue(open(lg.path, "rb").read().startswith(first))
        old = W.LOG_MAX
        W.LOG_MAX = 2000
        try:
            for i in range(200):
                lg.write("line %d " % i + "x" * 40)
        finally:
            W.LOG_MAX = old
        self.assertLess(lg.size, 2500)
        txt = open(lg.path, encoding="utf-8").read()
        self.assertIn("line 199", txt)
        self.assertIn("[earlier output cut]", txt)

    def test_stop_kills_the_node_marks_stopped_and_skips_the_rest(self):
        w = self.work("sleep")
        rid = self.fire(w)
        self.assertTrue(wait(lambda: (W.get_run(w["id"], rid) or {}).get("nodes", {}).get("a", {}).get("status") == "running"))
        act = W.active()
        self.assertEqual([x["id"] for x in act], [rid])
        self.assertEqual(act[0]["total"], 3)
        t0 = time.time()
        self.assertTrue(W.stop_run(rid))
        r = self.finished(w["id"], rid, 10)
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(r["status"], "stopped")
        self.assertEqual(r["nodes"]["a"]["status"], "stopped")
        self.assertEqual(r["nodes"]["b"]["status"], "skipped")
        self.assertFalse(r.get("error"))
        self.assertTrue(wait(lambda: not W.active()))
        self.assertFalse(W.stop_run(rid))

    def test_stop_a_queued_run(self):
        w = self.work("sleep", 1)
        first = self.fire(w)
        self.assertTrue(wait(lambda: (W.get_run(w["id"], first) or {}).get("status") == "running"))
        second = self.fire(w)
        self.assertEqual(W.get_run(w["id"], second)["status"], "queued")
        W.stop_run(second)
        W.stop_run(first)
        self.assertEqual(self.finished(w["id"], second)["status"], "stopped")

    def test_a_background_child_holding_the_pipes_does_not_hold_the_node(self):
        w = self.work("grandchild", 1)
        t0 = time.time()
        rid = self.fire(w)
        r = self.finished(w["id"], rid)
        self.assertEqual(r["status"], "done")
        self.assertLess(time.time() - t0, 10)

    def test_run_single_logs_and_stops(self):
        spec = W.catalogue()[self.t]
        rec = W.new_chat_run(spec, "", "c1", {"mode": "ok"}, "")
        out = W.run_single(spec, {"mode": "ok", "key": "topsecret-123"}, "", tempfile.gettempdir(), rid=rec["id"], rec=rec)
        self.assertEqual(out["exit"], 0)
        self.assertEqual(rec["nodes"]["node"]["status"], "ok")
        self.assertNotIn("topsecret-123", W.read_log(W.CHAT_RUNS, rec["id"], "node")["text"])
        rec2 = W.new_chat_run(spec, "", "c2", {"mode": "sleep"}, "")
        res = {}

        def go():
            try:
                W.run_single(spec, {"mode": "sleep"}, "", tempfile.gettempdir(), rid=rec2["id"], rec=rec2)
            except W.Stopped:
                res["stopped"] = True
        import threading
        th = threading.Thread(target=go)
        th.start()
        self.assertTrue(wait(lambda: any(x["id"] == rec2["id"] for x in W.active())))
        W.stop_run(rec2["id"])
        th.join(10)
        self.assertTrue(res.get("stopped"))


class Schema(Base):
    def test_legacy_keys_still_give_a_schema_and_new_keys_win(self):
        ins, outs = W.norm_io({"kind": "action", "out": "path", "out_note": "the folder", "outputs": ["loud"], "needs": "path", "needs_note": "a path"})
        self.assertEqual([o["name"] for o in outs], ["text", "loud"])
        self.assertEqual((outs[0]["type"], outs[0]["desc"]), ("path", "the folder"))
        self.assertEqual((ins[0]["name"], ins[0]["type"], ins[0]["required"]), ("path", "path", True))
        ins, outs = W.norm_io({"kind": "action", "inputs": [{"name": "url", "type": "url", "required": True, "desc": "where"}],
                               "outputs": [{"name": "text", "type": "json", "example": "{}"}, {"name": "id", "type": "text", "desc": "new id"}, {"name": "bad name!"}]})
        self.assertEqual([i["name"] for i in ins], ["url"])
        self.assertEqual([o["name"] for o in outs], ["text", "id"])
        leg = W.legacy_of(ins, outs)
        self.assertEqual((leg["out"], leg["outputs"], leg["needs"]), ("json", ["id"], ""))
        self.assertEqual(W.norm_io({"kind": "trigger"})[0], [])               # a trigger takes nothing
        self.assertEqual(W.norm_io(None)[0][0]["name"], "input")             # garbage never raises

    def test_every_builtin_and_bundled_node_declares_inputs_and_outputs(self):
        os_pkgs = [os.path.join(UI, "..", "..", "aihub-nodes-teams"), os.path.join(UI, "..", "..", "aihub-nodes-outlook")]
        W.A.run = lambda args, **k: {"data": [{"name": "p%d" % i, "path": p, "enabled": True} for i, p in enumerate(os_pkgs)]}
        W._cat.update(t=0.0, v={})
        cat = W.catalogue(force=True)
        for t, sp in cat.items():
            self.assertIn("io", sp, t)
            self.assertTrue(sp["io"]["outputs"] and sp["io"]["outputs"][0]["name"] == "text", t)
            if sp["kind"] == "trigger":
                self.assertEqual(sp["io"]["inputs"], [], t)
        self.assertEqual(cat["git.pull"]["io"]["outputs"][1]["name"], "path")
        self.assertTrue(cat["space.publish"]["io"]["inputs"][0]["required"])
        self.assertEqual(cat["space.publish"]["needs"], "path")                # derived legacy key, still read by repair
        mail = [s for s in cat.values() if s["type"].endswith("outlook-receive")][0]
        self.assertIn("subject", [o["name"] for o in mail["io"]["outputs"]])

    def test_reference_to_an_undeclared_output_gets_a_warning(self):
        cat = W.catalogue()
        w = {"nodes": [{"id": "a", "type": "start.manual", "config": {"inputs": "repo|Repo|text||*"}},
                       {"id": "g", "type": "git.pull", "label": "Git", "config": {"url": "{{data.repo}}"}},
                       {"id": "c", "type": "ai.claude", "label": "Ask", "config": {"prompt": "see {{data.path}} and {{data.nope}}"}}],
             "edges": [{"from": "a", "to": "g"}, {"from": "g", "to": "c"}]}
        warns = W.ref_warnings(w, cat)
        self.assertEqual(len(warns), 1)
        self.assertIn("nope", warns[0])
        self.assertIn("path", warns[0])                                        # lists what IS declared
        w["edges"] = [{"from": "a", "to": "g"}, {"from": "a", "to": "c"}]                                # path is only declared by git.pull, which is no longer before it
        self.assertEqual(len(W.ref_warnings(w, cat)), 2)

    def test_generate_example_is_valid_without_warnings_and_the_catalogue_lists_io(self):
        warns = []
        w = W.checked_work(json.loads(json.dumps(W.GEN_EXAMPLE)), warnings=warns)
        self.assertEqual(warns, [])
        self.assertEqual(len(w["nodes"]), 4)
        cat = W._gen_catalogue()
        self.assertIn("in: path:path*", cat)
        self.assertIn("text:path", cat)
        self.assertIn("{{data.name}}", W.GEN_RULES)


class Credentials(Base):
    def setUp(self):
        super().setUp()
        self.cat = W.catalogue(force=True)

    def test_brief_names_the_sets_but_never_values_and_skips_their_fields(self):
        spec = self.cat["send.teams"]
        W.save_credential({"kind": "teams", "name": "Ops", "fields": {"webhook_url": "https://secret.example/hook-XYZ"}}, self.box)
        sets = [c for c in W.credentials().values() if c["kind"] == "teams"]
        b = W.node_brief(spec, sets)
        self.assertIn("Ops", b)
        self.assertIn("webhook_url", b)
        self.assertNotIn("hook-XYZ", b)
        self.assertIn("Do NOT ask", b)
        self.assertEqual(W.cred_fields(spec), ["webhook_url"])
        cv = W.cred_values({"credential": sets[0]["id"]}, spec, self.box)
        self.assertEqual(cv["webhook_url"], "https://secret.example/hook-XYZ")      # the card's run merges this on the server

    def test_generated_nodes_get_the_only_set_or_ask_by_kind(self):
        w = {"title": "x", "nodes": [{"id": "a", "type": "start.manual", "config": {}}, {"id": "t", "type": "send.teams", "config": {"webhook_url": "https://leak", "text": "hi"}}],
             "edges": [{"from": "a", "to": "t"}]}
        out = W.checked_work(json.loads(json.dumps(w)))
        t = next(n for n in out["nodes"] if n["id"] == "t")
        self.assertEqual(t.get("credential_kind"), "teams")
        self.assertFalse(t["config"].get("webhook_url"))                           # a secret value from the model is dropped
        c = W.save_credential({"kind": "teams", "name": "Only", "fields": {"webhook_url": "https://x/y"}}, self.box)
        t = next(n for n in W.checked_work(json.loads(json.dumps(w)))["nodes"] if n["id"] == "t")
        self.assertEqual(t.get("credential"), c["id"])
        self.assertNotIn("credential_kind", t)
        self.assertIn("credential kind: teams", W._gen_catalogue())

    def test_ref_issues_name_the_node(self):
        w = {"nodes": [{"id": "a", "type": "start.manual", "config": {}}, {"id": "c", "type": "ai.claude", "label": "Ask", "config": {"prompt": "{{data.zzz}}"}}], "edges": [{"from": "a", "to": "c"}]}
        self.assertEqual([i[0] for i in W.ref_issues(w, self.cat)], ["c"])


FAKE = """#!/bin/sh
# fake aihub CLI: would hang on `cat` if stdin were left open
n=$(cat | wc -c)
echo "$@" > "$FAKE_OUT.argv"
eval "p=\\${$(( $# - 1 ))}"
for a in "$@"; do case "$a" in /*) p="$a";; esac; done
ls -A "$p" > "$FAKE_OUT.ls" 2>&1
echo "stdin=$n env_noupdate=$AIHUB_NO_UPDATE" > "$FAKE_OUT.env"
sleep 12 &
echo '{"ok": true, "data": {"space": {"ref": "me@t1", "rev": 1, "kind": "html", "visibility": "private"}, "url": "http://hub/s/me@t1", "created": true}}'
"""


class PublishNode(unittest.TestCase):
    def run_node(self, cfg, **env):
        tmp = tempfile.mkdtemp()
        fake = os.path.join(tmp, "aihub")
        open(fake, "w").write(FAKE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        out = os.path.join(tmp, "o")
        e = dict(os.environ, AIHUB_CLI=fake, FAKE_OUT=out, HOME=tmp, **env)
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(UI, "nodes", "space_publish.py")], input=json.dumps({"config": cfg, "workdir": tmp}), env=e,
                           capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout.strip().splitlines()[-1]), time.time() - t0, out

    def test_publishes_a_staged_folder_without_hanging_on_stdin_or_pipes(self):
        site = tempfile.mkdtemp()
        os.makedirs(os.path.join(site, ".git"))
        os.makedirs(os.path.join(site, "node_modules"))
        open(os.path.join(site, "index.html"), "w").write("<h1>hi</h1><script src='app.js'></script>")
        open(os.path.join(site, "app.js"), "w").write("var a=1")
        r, secs, out = self.run_node({"path": site, "name": "t1"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["space"], "me@t1")
        self.assertLess(secs, 8)                                              # the fake's background `sleep 12` holds nothing open
        ls = open(out + ".ls").read().split()
        self.assertIn("index.html", ls)
        self.assertNotIn(".git", ls)
        self.assertNotIn("node_modules", ls)
        env = open(out + ".env").read()
        self.assertRegex(env, r"stdin=\s*0\b")
        self.assertIn("env_noupdate=1", env)
        self.assertIn("--private", open(out + ".argv").read())

    def test_a_folder_without_a_page_is_a_clear_error(self):
        site = tempfile.mkdtemp()
        open(os.path.join(site, "a.txt"), "w").write("x")
        r, _, _ = self.run_node({"path": site})
        self.assertFalse(r["ok"])
        self.assertIn("index.html", r["error"])


if __name__ == "__main__":
    unittest.main()
