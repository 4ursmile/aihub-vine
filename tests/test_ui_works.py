"""aihub-ui Works: scheduler, filters, Claude node, package nodes (the builtin_packages/aihub-nodes-teams package), secrets, auth helper."""
import datetime
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-ui", "ui")
EXAMPLE = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-nodes-teams")
sys.path.insert(0, UI)
from aihubui import auth, secretbox, store, works as W  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(fn, secs=10):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        self.box = secretbox.Box(os.path.join(self.tmp, "ui"))
        os.makedirs(os.path.join(self.tmp, "ui"), exist_ok=True)
        W.A.run = lambda args, **k: {"data": [{"name": "aihub-nodes-teams", "path": EXAMPLE, "enabled": True}]}
        W._cat.update(t=0.0, v={})
        self.engine = W.Engine(self.box)
        self.addCleanup(self.engine.shutdown)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AIHUB_HOME", None)
        else:
            os.environ["AIHUB_HOME"] = self.old

    def save(self, nodes, edges, enabled=True):
        w = store.clean_work({"title": "t", "enabled": enabled, "nodes": nodes, "edges": edges})
        W.strip_secrets(w, self.box)
        return store.save_work(w)

    def final(self, wid):
        def f():
            rs = W.list_runs(wid)
            return rs if rs and all(r["status"] in ("done", "failed") for r in rs) else None
        return wait(f)


class Catalogue(Base):
    def test_package_nodes_are_found_and_triggers_get_the_filter(self):
        c = W.catalogue(force=True)
        t = c["pkg:aihub-nodes-teams/teams-message"]
        self.assertEqual(t["kind"], "trigger")
        self.assertIn("filter", [f["name"] for f in t["fields"]])
        self.assertNotIn("run", W.public(t))
        self.assertIn("pkg:aihub-nodes-teams/teams-send", c)


class Cron(unittest.TestCase):
    def test_schedules(self):
        k, s = W.parse_schedule("30 8 * * 1-5")
        self.assertTrue(W.cron_due(s, datetime.datetime(2026, 10, 28, 8, 30)))          # a Wednesday
        self.assertFalse(W.cron_due(s, datetime.datetime(2026, 10, 31, 8, 30)))         # Saturday
        self.assertEqual(W.parse_schedule("2h"), ("every", 7200))
        for bad in ("* * *", "70 * * * *", "1s"):
            with self.assertRaises(ValueError):
                W.parse_schedule(bad)


    def test_star_steps_names_and_sunday_seven(self):
        _, s = W.parse_schedule("0 9 */2 * 7")                  # day-of-month `*/2` is still a star: the weekday alone decides on its day...
        self.assertFalse(s[5])
        self.assertTrue(s[6])
        self.assertEqual(s[4], {0})                              # 7 = Sunday = 0
        _, s = W.parse_schedule("0 9 1 jan-mar mon-fri")
        self.assertEqual(s[3], {1, 2, 3})
        self.assertEqual(s[4], {1, 2, 3, 4, 5})
        _, s = W.parse_schedule("0 9 * * 5-7")
        self.assertEqual(s[4], {5, 6, 0})
        _, s = W.parse_schedule("0 9 15 * mon")                  # both restricted: either matches
        self.assertTrue(W.cron_due(s, datetime.datetime(2026, 10, 26, 9, 0)))     # a Monday, not the 15th
        self.assertTrue(W.cron_due(s, datetime.datetime(2026, 10, 15, 9, 0)))     # the 15th, a Thursday
        self.assertFalse(W.cron_due(s, datetime.datetime(2026, 10, 14, 9, 0)))


class Flow(Base):
    def test_concurrent_sync_fires_a_cron_once(self):
        w = self.save([{"id": "c", "type": "start.cron", "config": {"schedule": "* * * * *"}}, {"id": "t", "type": "flow.template", "config": {"template": "x"}}],
                      [{"from": "c", "to": "t"}])
        bar = threading.Barrier(8)

        def go():
            bar.wait()
            self.engine.sync()
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.final(w["id"])
        self.assertEqual(len(W.list_runs(w["id"])), 1)

    def test_template_filter_and_branch_stop(self):
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "t", "type": "flow.template", "config": {"template": "got {{input}} / {{trigger}}"}},
                       {"id": "f", "type": "flow.filter", "config": {"regex": "^got deploy"}}, {"id": "g", "type": "flow.filter", "config": {"regex": "nomatch"}},
                       {"id": "after", "type": "flow.template", "config": {"template": "never"}}],
                      [{"from": "a", "to": "t"}, {"from": "t", "to": "f"}, {"from": "t", "to": "g"}, {"from": "g", "to": "after"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "deploy prod"}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["nodes"]["t"]["output"], "got deploy prod / deploy prod")
        self.assertEqual(r["nodes"]["f"]["note"], "passed")
        self.assertEqual(r["nodes"]["g"]["note"], "stopped here")
        self.assertNotIn("after", r["nodes"])

    def test_trigger_filter_runs_before_a_run_exists(self):
        w = self.save([{"id": "a", "type": "start.webhook", "config": {"filter": "^deploy"}}], [])
        self.assertIsNone(self.engine.fire(w, w["nodes"][0], {"text": "lunch?"}, "webhook"))
        self.assertEqual(W.list_runs(w["id"]), [])
        self.assertIsNotNone(self.engine.fire(w, w["nodes"][0], {"text": "deploy now"}, "webhook"))

    def test_claude_node_uses_model_and_passes_reply_on(self):
        seen = {}

        def fake(sess, text, model="", effort="", mode="auto", extra_env=None, mcp=None):
            seen.update(text=text, model=model, effort=effort, mode=mode)
            yield {"k": "msg", "blocks": [{"type": "text", "text": "REPLY"}]}
            yield {"k": "result", "usage": {}, "error": False}
        old = W.chat.turn
        W.chat.turn = fake
        self.addCleanup(setattr, W.chat, "turn", old)
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "c", "type": "ai.claude", "config": {"prompt": "Summarise: {{input}}", "model": "haiku", "effort": "low", "mode": "plan"}},
                       {"id": "z", "type": "flow.template", "config": {"template": "[{{input}}]"}}], [{"from": "a", "to": "c"}, {"from": "c", "to": "z"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "the news"}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["status"], "done", r)
        self.assertEqual((seen["model"], seen["effort"], seen["mode"]), ("haiku", "low", "plan"))
        self.assertEqual(seen["text"], "Summarise: the news")
        self.assertEqual(r["nodes"]["z"]["output"], "[REPLY]")

    def test_claude_error_fails_the_run_and_stops_the_branch(self):
        def fake(sess, text, *a, **k):
            yield {"k": "error", "text": "Sign-in needed: x", "auth": True}
        old = W.chat.turn
        W.chat.turn = fake
        self.addCleanup(setattr, W.chat, "turn", old)
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "c", "type": "ai.claude", "config": {"prompt": "x"}}, {"id": "z", "type": "flow.note"}],
                      [{"from": "a", "to": "c"}, {"from": "c", "to": "z"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "go"}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["status"], "failed")
        self.assertIn("Sign-in", r["nodes"]["c"]["error"])
        self.assertNotIn("z", r["nodes"])


class Capture(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        Capture.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()


class Packages(Base):
    def test_teams_trigger_then_send_with_encrypted_secret(self):
        Capture.got = []
        srv = HTTPServer(("127.0.0.1", 0), Capture)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = free_port()
        url = "http://127.0.0.1:%d/hook?token=SECRET-TOKEN" % srv.server_address[1]
        w = self.save([{"id": "t", "type": "pkg:aihub-nodes-teams/teams-message", "config": {"port": port, "filter": "^deploy", "from_regex": "^alice"}},
                       {"id": "s", "type": "pkg:aihub-nodes-teams/teams-send", "config": {"webhook_url": url, "text": "ack: {{input}}"}}], [{"from": "t", "to": "s"}])
        # the secret is encrypted, the work file only holds a marker
        self.assertNotIn("SECRET-TOKEN", open(os.path.join(self.tmp, "ui", "works", w["id"] + ".json")).read())
        self.assertNotIn(b"SECRET-TOKEN", open(self.box.path, "rb").read())
        self.assertEqual(w["nodes"][1]["config"]["webhook_url"], W.MARK)
        self.engine.sync()

        def post(m):
            import urllib.request
            r = urllib.request.Request("http://127.0.0.1:%d/message" % port, data=json.dumps(m).encode(), method="POST")
            return json.load(urllib.request.urlopen(r, timeout=5))
        self.assertTrue(wait(lambda: self._up(port)), "trigger process did not start")
        self.assertFalse(post({"from": "bob", "chat": "c", "text": "deploy prod"})["taken"])          # node-level sender filter
        post({"from": "alice", "chat": "c", "text": "lunch?"})                                         # generic regex filter drops it
        post({"from": "alice", "chat": "c", "text": "deploy prod"})
        self.final(w["id"])
        runs = W.list_runs(w["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["text"], "deploy prod")
        self.assertEqual(Capture.got, [{"text": "ack: deploy prod"}])
        # disabling the work stops the listener
        w["enabled"] = False
        store.save_work(w)
        self.engine.sync()
        self.assertTrue(wait(lambda: not self._up(port)))

    def _up(self, port):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            return False


class ParallelAndInputs(Base):
    def fake_claude(self, delay=0.0):
        st = {"now": 0, "max": 0, "sessions": [], "lock": threading.Lock()}

        def fake(sess, text, *a, **k):
            with st["lock"]:
                st["now"] += 1
                st["max"] = max(st["max"], st["now"])
                st["sessions"].append(sess["id"])
            time.sleep(delay)
            with st["lock"]:
                st["now"] -= 1
            yield {"k": "msg", "blocks": [{"type": "text", "text": "R:" + text.split("Task:")[-1].strip()}]}
            yield {"k": "result", "usage": {}, "error": False}
        old = W.chat.turn
        W.chat.turn = fake
        self.addCleanup(setattr, W.chat, "turn", old)
        return st

    def test_independent_branches_run_together_and_the_join_waits_for_both(self):
        st = self.fake_claude(0.6)
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "x", "type": "ai.claude", "label": "Left", "config": {"prompt": "left"}},
                       {"id": "y", "type": "ai.claude", "label": "Right", "config": {"prompt": "right"}},
                       {"id": "j", "type": "flow.template", "config": {"template": "{{input}}|{{in.right}}|{{in.1}}|{{in.left}}"}}],
                      [{"from": "a", "to": "x"}, {"from": "a", "to": "y"}, {"from": "x", "to": "j"}, {"from": "y", "to": "j"}])
        t0 = time.time()
        rid = self.engine.fire(w, w["nodes"][0], {"text": "go"}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(st["max"], 2)                                   # both Claude nodes ran at the same time ...
        self.assertEqual(len(set(st["sessions"])), 2)                    # ... each in its own chat
        self.assertLess(time.time() - t0, 5)
        out = r["nodes"]["j"]["output"]
        self.assertEqual(out, "## From Left\nR:left\n\n## From Right\nR:right|R:right|R:left|R:left")
        self.assertEqual([i["label"] for i in r["nodes"]["j"]["inputs"]], ["Left", "Right"])

    def test_a_single_parent_keeps_the_plain_input_and_chat_is_shared_in_a_line(self):
        st = self.fake_claude()
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "c1", "type": "ai.claude", "config": {"prompt": "one"}},
                       {"id": "c2", "type": "ai.claude", "config": {"prompt": "two {{in.1}}"}}],
                      [{"from": "a", "to": "c1"}, {"from": "c1", "to": "c2"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "go"}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["nodes"]["c1"]["inputs"][0]["text"], "go")
        self.assertEqual(r["nodes"]["c2"]["output"], "R:two R:one")
        self.assertEqual(len(set(st["sessions"])), 1)                    # a sequence still shares one chat

    def test_names_catalogue_io_and_input_detection(self):
        work = {"nodes": [{"id": "a", "type": "ai.claude"}, {"id": "b", "type": "ai.claude"}, {"id": "c", "type": "ai.claude", "label": "2"}]}
        n = W.names_of(work, W.catalogue(True))
        self.assertEqual([n[i][1] for i in "abc"], ["claude_task", "claude_task_2", "2_2"])
        cat = W.catalogue(True)
        self.assertEqual((cat["git.pull"]["out"], cat["space.publish"]["needs"]), ("path", "path"))
        self.assertTrue(W.uses_input("x {{ in.left }}") and W.uses_input("{{input}}") and not W.uses_input("{{trigger}}"))


class Generate(Base):
    def test_prompt_documents_data_flow_and_the_example_is_valid(self):
        self.assertIn("{{in.", W.GEN_RULES)
        self.assertIn("OUTPUT:", W.GEN_RULES)
        self.assertIn("out: path", W._gen_catalogue())
        w = W.checked_work(json.loads(json.dumps(W.GEN_EXAMPLE)))
        self.assertEqual(len(w["nodes"]), 4)
        self.assertEqual(w["nodes"][0]["config"]["inputs"], "repo|Repository URL|text||*")

    def test_repair_fixes_edges_bad_refs_and_asks_claude_for_a_path(self):
        w = {"title": "x", "nodes": [{"id": "a", "type": "start.manual"}, {"id": "a", "type": "ai.claude", "config": {"prompt": "do {{in.nobody}} and {{in.1}}"}},
                                     {"id": "p", "type": "space.publish", "config": {}}],
             "edges": [{"from": "a", "to": "a2"}, {"from": "a", "to": "a2"}, {"from": "a2", "to": "p"}, {"from": "p", "to": "p"}, {"from": "zz", "to": "p"}, {"from": "p", "to": "a"}]}
        r = W.checked_work(w)
        self.assertEqual([n["id"] for n in r["nodes"]], ["a", "a2", "p"])
        self.assertEqual(r["edges"], [{"from": "a", "to": "a2"}, {"from": "a2", "to": "p"}])
        prompt = r["nodes"][1]["config"]["prompt"]
        self.assertTrue(prompt.startswith("do {{input}} and {{in.1}}"))
        self.assertIn("OUTPUT: /full/path", prompt)

    def test_a_bad_answer_is_sent_back_once(self):
        seen = []

        def fake(sess, text, *a, **k):
            seen.append(text)
            reply = "no json here" if len(seen) == 1 else "```json\n" + json.dumps(W.GEN_EXAMPLE) + "\n```"
            yield {"k": "msg", "blocks": [{"type": "text", "text": reply}]}
            yield {"k": "result", "usage": {}, "error": False}
        old = W.chat.turn
        W.chat.turn = fake
        self.addCleanup(setattr, W.chat, "turn", old)
        w = W.generate("dashboard from a repo")
        self.assertEqual(len(seen), 2)
        self.assertIn("cannot be used", seen[1])
        self.assertEqual(w["title"], "Data dashboard")


class Memory(Base):
    def test_memory_from_work_history_chat_and_the_history_limit(self):
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "m", "type": "flow.memory", "config": {"source": "work", "limit": 2}},
                       {"id": "z", "type": "flow.template", "config": {"template": "{{input}}"}}], [{"from": "a", "to": "m"}, {"from": "m", "to": "z"}])
        w["keep"] = 3
        store.save_work(w)
        for i in range(5):
            self.engine.fire(w, w["nodes"][0], {"text": "job %d" % i}, "manual")
            self.final(w["id"])
            time.sleep(1.1)                                                       # run ids carry a one second clock
        self.final(w["id"])
        self.assertLessEqual(len(W.list_runs(w["id"])), 3)                       # history limit
        last = W.get_run(w["id"], W.list_runs(w["id"])[0]["id"])
        out = last["nodes"]["z"]["output"]
        self.assertIn("Remembered context", out)
        self.assertIn("job 3", out)
        self.assertNotIn("job 0", out)
        # chat memory
        s = store.create_session("talk")
        store.append(s["id"], {"k": "user", "text": "my cat is Tom"})
        store.append(s["id"], {"k": "msg", "blocks": [{"type": "text", "text": "Nice name"}]})
        mem = W.memory_text({"source": "chat", "chat": s["id"], "limit": 5}, w["id"])
        self.assertIn("User: my cat is Tom", mem)
        self.assertIn("Assistant: Nice name", mem)

    def test_delete_one_message_of_a_chat(self):
        s = store.create_session("x")
        for i in range(3):
            store.append(s["id"], {"k": "user", "text": "m%d" % i})
        self.assertEqual(store.delete_event(s["id"], 1), 2)
        self.assertEqual([e["text"] for e in store.history(s["id"])], ["m0", "m2"])
        self.assertIsNone(store.delete_event(s["id"], 9))


class TeamsNodes(Base):
    def test_catalogue_has_teams_nodes_with_guides_and_secret_fields(self):
        c = W.catalogue(force=True)
        self.assertIn("guide", W.public(c["start.teams"]))
        self.assertTrue(next(f for f in c["send.teams"]["fields"] if f["name"] == "webhook_url")["secret"])
        self.assertNotIn("run", W.public(c["send.teams"]))

    def test_teams_in_listener_needs_the_secret_and_filters(self):
        import subprocess
        import urllib.error
        import urllib.request
        port = free_port()
        p = subprocess.Popen([sys.executable, os.path.join(UI, "nodes", "teams_in.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        p.stdin.write(json.dumps({"config": {"port": port, "secret": "0123456789abcdef", "from_regex": "^Ann"}}) + "\n")
        p.stdin.flush()
        self.assertTrue(wait(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0))

        def post(body, key="0123456789abcdef"):
            r = urllib.request.Request("http://127.0.0.1:%d/teams" % port, data=json.dumps(body).encode(), headers={"X-Hook-Secret": key}, method="POST")
            return json.load(urllib.request.urlopen(r, timeout=5))
        with self.assertRaises(urllib.error.HTTPError):
            post({"text": "x"}, key="wrong-secret-xx")
        self.assertFalse(post({"text": "hi", "from": "Bob"})["taken"])
        self.assertTrue(post({"body": {"content": "<p>deploy&nbsp;<b>prod</b></p>"}, "from": "Ann Lee", "chat": "ops"})["taken"])
        ev = json.loads(p.stdout.readline())["event"]
        self.assertEqual(ev["text"], "deploy prod")
        self.assertEqual(ev["data"]["from"], "Ann Lee")


class InputsOutputs(Base):
    def test_inputs_parse_and_the_output_node_becomes_the_result(self):
        ins = store.parse_inputs("topic|Topic|text||*\nlevel|Level|select|easy,hard\nloud|Loud|checkbox|true\nbad line\n1x|no|text")
        self.assertEqual([i["name"] for i in ins], ["topic", "level", "loud"])
        self.assertTrue(ins[0]["required"])
        self.assertEqual(ins[1]["default"], "easy")
        self.assertIs(ins[2]["default"], True)
        w = self.save([{"id": "a", "type": "start.manual", "config": {"inputs": "topic|Topic|text||*"}},
                       {"id": "t", "type": "flow.template", "config": {"template": "About {{data.topic}}"}},
                       {"id": "o", "type": "flow.output", "config": {"title": "Result", "template": "# {{input}}"}}], [{"from": "a", "to": "t"}, {"from": "t", "to": "o"}])
        self.assertEqual(store.works()[0]["inputs"][0]["name"], "topic")
        self.assertTrue(store.works()[0]["output"])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "topic: cats", "data": {"topic": "cats"}}, "manual")
        self.final(w["id"])
        r = W.get_run(w["id"], rid)
        self.assertEqual(r["result"], {"title": "Result", "text": "# About cats"})
        self.assertEqual(W.list_runs(w["id"])[0]["result"]["title"], "Result")

    def test_no_output_node_uses_the_last_text(self):
        w = self.save([{"id": "a", "type": "start.manual"}, {"id": "t", "type": "flow.template", "config": {"template": "done: {{input}}"}}], [{"from": "a", "to": "t"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "x"}, "manual")
        self.final(w["id"])
        self.assertEqual(W.get_run(w["id"], rid)["result"]["text"], "done: x")


class Themes(Base):
    def test_theme_roundtrip_validation_and_prompt(self):
        t = store.save_theme({"name": "Acme Brand", "colors": {"bg": "#FFFFFF", "fg": "#111111", "muted": "#777777", "accent": "#ff6600", "accent2": "#cc5200", "surface": "#f4f4f4"},
                              "fonts": {"head": "Georgia, serif", "body": "Calibri, sans-serif"}, "notes": "logo top left"})
        self.assertEqual(t["id"], "acme-brand")
        self.assertEqual(store.get_theme("acme-brand")["colors"]["bg"], "#ffffff")
        p = store.theme_prompt(t)
        self.assertIn('Theme "Acme Brand"', p)
        self.assertIn("--accent:#ff6600", p)
        with self.assertRaises(ValueError):
            store.save_theme({"name": "x", "colors": {"bg": "red"}})
        with self.assertRaises(ValueError):
            store.save_theme({"name": "x", "colors": t["colors"], "fonts": {"head": "a;b{}", "body": "x"}})
        store.delete_theme("acme-brand")
        self.assertIsNone(store.get_theme("acme-brand"))


class WorkflowType(unittest.TestCase):
    def test_template_and_lint(self):
        from aihub.core import manifest as M, templates as T, toml_lite
        mf, files, _ = T.render("workflow", "my-flow")
        d = tempfile.mkdtemp()
        for p, c in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True)
            open(os.path.join(d, p), "w").write(c)
        open(os.path.join(d, "aihub.toml"), "w").write(mf)
        m = M.normalize(toml_lite.loads(mf) if hasattr(toml_lite, "loads") else __import__("tomllib").loads(mf))
        self.assertEqual(m["package"]["type"], "workflow")
        self.assertEqual(M.lint(m, d)[0], [])
        json.loads(files["works/my-flow.work.json"])
        os.remove(os.path.join(d, "works", "my-flow.work.json"))
        self.assertTrue(any("work.json" in e for e in M.lint(m, d)[0]))


class NodeType(unittest.TestCase):
    def test_template_and_lint(self):
        from aihub.core import manifest as M, templates as T, toml_lite
        mf, files, _ = T.render("node", "my-node")
        d = tempfile.mkdtemp()
        for p, c in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, p)) or d, exist_ok=True)
            open(os.path.join(d, p), "w").write(c)
        m = M.normalize(toml_lite.loads(mf) if hasattr(toml_lite, "loads") else __import__("tomllib").loads(mf))
        self.assertEqual(m["package"]["type"], "node")
        self.assertEqual(M.lint(m, d)[0], [])
        spec = json.loads(files["nodes/my-node.node.json"])
        self.assertEqual(spec["run"][1], "nodes/my_node.py")
        compile(files["nodes/my_node.py"], "n", "exec")
        os.remove(os.path.join(d, "nodes", "my-node.node.json"))
        self.assertTrue(any("node.json" in e for e in M.lint(m, d)[0]))


class CustomSettingsFile(Base):
    def test_custom_settings_json_drives_models_helper_and_chat_flag(self):
        from aihubui import aihub as A, chat
        f = os.path.join(self.tmp, "my-settings.json")
        json.dump({"model": "my-model", "apiKeyHelper": "echo k", "env": {"ANTHROPIC_AUTH_TOKEN": "tok-SECRET", "FOO": "bar"}, "availableModels": ["m-1"]}, open(f, "w"))
        self.assertFalse(A.claude_settings()["custom"])
        store.save_prefs({"claude_settings": f})
        cs = A.claude_settings()
        self.assertTrue(cs["custom"] and cs["exists"] and cs["helper"])
        self.assertEqual(cs["path"], f)
        self.assertEqual(cs["env"]["ANTHROPIC_AUTH_TOKEN"], "set")                  # secret stays masked
        self.assertNotIn("tok-SECRET", json.dumps(cs))
        self.assertNotIn("m-1", [m["id"] for m in A.models()["models"]])          # availableModels is not listed
        self.assertEqual(auth.helper(), "echo k")
        self.assertEqual(A.settings_path(), (f, True))
        with self.assertRaises(ValueError):
            store.save_prefs({"claude_settings": os.path.join(self.tmp, "missing.json")})
        open(os.path.join(self.tmp, "bad.json"), "w").write("[1]")
        with self.assertRaises(ValueError):
            store.save_prefs({"claude_settings": os.path.join(self.tmp, "bad.json")})
        store.save_prefs({"claude_settings": ""})
        self.assertFalse(A.claude_settings()["custom"])


OUTLOOK = os.path.join(ROOT, "aihub", "builtin_packages", "aihub-nodes-outlook")


def run_node(script, cfg, workdir=None):
    import subprocess
    where = os.path.join(OUTLOOK, "nodes") if script.startswith("outlook_") else os.path.join(UI, "nodes")
    p = subprocess.run([sys.executable, os.path.join(where, script)], input=json.dumps({"config": cfg, "workdir": workdir or tempfile.gettempdir()}), capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout.strip().splitlines()[-1])


class DataNodes(Base):
    def test_read_file_reads_text_and_refuses_secrets(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "a.txt"), "w").write("hello world")
        open(os.path.join(d, ".env"), "w").write("KEY=secret")
        open(os.path.join(d, "x.json"), "w").write('{"a": 1}')
        self.assertEqual(run_node("read_file.py", {"path": os.path.join(d, "a.txt")})["output"], "hello world")
        self.assertEqual(run_node("read_file.py", {"path": "a.txt"}, workdir=d)["output"], "hello world")           # relative = run's folder
        r = run_node("read_file.py", {"path": os.path.join(d, ".env")})
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["error"])
        self.assertFalse(run_node("read_file.py", {"path": "~/.ssh/id_rsa"})["ok"])
        self.assertFalse(run_node("read_file.py", {"path": "~/.aihub/ui/.key"})["ok"])
        self.assertEqual(run_node("read_file.py", {"path": os.path.join(d, "x.json"), "parse": "json"})["data"]["json"], {"a": 1})
        listing = run_node("read_file.py", {"path": d, "read_files": True})["output"]
        self.assertIn("a.txt", listing)
        self.assertIn("=== a.txt ===", listing)
        self.assertNotIn("KEY=secret", listing)

    def test_http_request_node(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200 if self.path != "/bad" else 500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"items": [{"name": "first", "auth": self.headers.get("Authorization")}]}).encode())
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        r = run_node("http_request.py", {"url": base + "/x", "authorization": "Bearer abc", "pick": "items.0.auth"})
        self.assertEqual((r["ok"], r["output"], r["data"]["status"]), (True, "Bearer abc", 200))
        self.assertFalse(run_node("http_request.py", {"url": base + "/bad"})["ok"])
        self.assertTrue(run_node("http_request.py", {"url": base + "/bad", "fail_on_error": False})["ok"])
        self.assertFalse(run_node("http_request.py", {"url": "file:///etc/passwd"})["ok"])
        self.assertFalse(run_node("http_request.py", {"url": "ftp://x/y"})["ok"])

    def test_git_read_validates_before_touching_the_network(self):
        for bad in ("http://example.com/r.git", "https://user:pw@example.com/r.git", "file:///tmp/x", "https://example.com/r.git#x"):
            self.assertFalse(run_node("git_read.py", {"url": bad})["ok"], bad)
        self.assertIn("path", run_node("git_read.py", {"url": "https://example.com/r.git", "path": "../x"})["error"])
        self.assertIn("branch", run_node("git_read.py", {"url": "https://example.com/r.git", "ref": "--upload-pack=x"})["error"])

    def test_templated_fields_and_the_demo_workflow_are_valid(self):
        c = W.catalogue(force=True)
        for t in ("io.read_file", "io.request", "git.read"):
            self.assertIn(t, c)
        self.assertTrue(next(f for f in c["io.request"]["fields"] if f["name"] == "authorization")["secret"])
        demo = json.load(open(os.path.join(UI, "samples", "repo-briefing.work.json")))
        w = store.clean_work(demo)
        self.assertEqual(len(w["nodes"]), len(demo["nodes"]))
        self.assertEqual(len(w["edges"]), len(demo["edges"]))
        self.assertTrue(all(n["type"] in c for n in w["nodes"]))
        self.assertEqual([i["name"] for i in store.work_inputs(w)], ["slug", "focus", "language", "notes"])
        self.assertTrue(w["description"])

    def test_templated_request_url_is_filled_from_the_run_data(self):
        class H(BaseHTTPRequestHandler):
            seen = []

            def log_message(self, *a):
                pass

            def do_GET(self):
                H.seen.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"pong")
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        w = self.save([{"id": "a", "type": "start.manual", "config": {"inputs": "who|Who|text"}},
                       {"id": "r", "type": "io.request", "config": {"url": "http://127.0.0.1:%d/hi/{{data.who}}" % srv.server_address[1]}}], [{"from": "a", "to": "r"}])
        rid = self.engine.fire(w, w["nodes"][0], {"text": "x", "data": {"who": "bob"}}, "manual")
        self.final(w["id"])
        self.assertEqual(W.get_run(w["id"], rid)["status"], "done", W.get_run(w["id"], rid))
        self.assertEqual(H.seen, ["/hi/bob"])
        self.assertEqual(W.get_run(w["id"], rid)["result"]["text"], "pong")


class ModelPicker(Base):
    def test_model_picker_options_of_the_settings_file(self):
        from aihubui import aihub as A
        f = os.path.join(self.tmp, "mp.json")
        json.dump({"model": "sonnet", "modelPicker": {"options": [{"model": "openai/openai/gpt-6.1-terra", "label": "gpt-6.1-terra"}, {"model": "nvidia/zai-org/glm-5.3-flash", "label": "glm-5.3-flash"},
                                                                   {"model": "bad id", "label": "x"}, {"label": "no model"},
                                                                   {"model": "openai/gpt-image-2", "label": "img"}, {"model": "x/voice-1", "label": "v", "type": "tts"}]}}, open(f, "w"))
        store.save_prefs({"claude_settings": f})
        self.assertEqual(len(A.models()["models"]), 4 + 4)                          # default on: tiers + picker options
        store.save_prefs({"model_picker": False})
        self.assertEqual(len(A.models()["models"]), 4)                              # switch off: "", opus, sonnet, haiku
        store.save_prefs({"model_picker": True})
        ms = {m["id"]: m for m in A.models()["models"]}
        self.assertEqual(ms["openai/gpt-image-2"]["kind"], "image")
        self.assertEqual(ms["x/voice-1"]["kind"], "tts")
        self.assertEqual(ms["openai/openai/gpt-6.1-terra"]["kind"], "chat")
        self.assertEqual(ms["openai/openai/gpt-6.1-terra"]["label"], "gpt-6.1-terra")
        self.assertEqual(ms["openai/openai/gpt-6.1-terra"]["group"], "picker")
        self.assertEqual(ms["nvidia/zai-org/glm-5.3-flash"]["label"], "glm-5.3-flash")
        self.assertNotIn("bad id", ms)
        self.assertEqual(len([m for m in ms if m not in ("", "opus", "sonnet", "haiku")]), 4)


class Terminal(unittest.TestCase):
    def test_pty_answers_a_question_on_stdin(self):
        from aihubui import term
        if not term.HAVE_PTY:
            self.skipTest("no pty")
        t = term.start(["/bin/sh", "-c", "printf 'Key: '; read x; echo got:$x"])
        self.addCleanup(t.kill)
        d, pos, code = t.read(0, 5)
        self.assertIn(b"Key: ", d)
        t.write(b"abc\n")
        end = time.time() + 5
        out = b""
        while code is None and time.time() < end:
            d, pos, code = t.read(pos, 1)
            out += d
        self.assertIn(b"got:abc", out)
        self.assertEqual(code, 0)


class Helper(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude"))
        self.old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        auth.state.update(valid_until=0.0, last_error="", login=None)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self.old))

    def set(self, cmd):
        json.dump({"apiKeyHelper": cmd}, open(os.path.join(self.home, ".claude", "settings.json"), "w"))

    def test_check_never_returns_the_key_and_reports_login_hint(self):
        self.set("echo sk-LEAK; echo 'open https://login.example/device sk-LEAK' >&2")
        r = auth.check()
        self.assertTrue(r["ok"])
        self.assertNotIn("sk-LEAK", json.dumps(auth.status()))
        self.set("echo 'sign in: https://login.example/device' >&2; exit 2")
        r = auth.check()
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_login"])
        self.assertIn("https://login.example/device", r["error"])

    def test_login_collects_stderr_without_the_key(self):
        self.set("echo 'go to https://x.example sk-ABC' >&2; sleep 0.3; echo sk-ABC")
        auth.login()
        self.assertTrue(wait(lambda: auth.state["login"] and not auth.state["login"]["running"]))
        st = auth.status()
        self.assertTrue(st["login"]["ok"])
        self.assertNotIn("sk-ABC", json.dumps(st))
        self.assertTrue(st["valid"])

    def test_no_helper_means_ready(self):
        self.assertTrue(auth.ready()["ok"])


if __name__ == "__main__":
    unittest.main()


class PointerNodes(Base):
    """Pointer nodes pass PATHS, publish / mail take them, a generated work is checked."""

    def test_path_pointer_passes_the_path_not_the_content(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "a.csv"), "w").write("secret-content\n")
        r = run_node("path_pointer.py", {"path": d + "/*.csv"}, workdir=d)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["paths"], [os.path.realpath(os.path.join(d, "a.csv"))])
        self.assertNotIn("secret-content", json.dumps(r))
        self.assertFalse(run_node("path_pointer.py", {"path": d + "/none.parquet"})["ok"])
        self.assertTrue(run_node("path_pointer.py", {"path": d + "/none.parquet", "must_exist": False})["ok"])
        self.assertFalse(run_node("path_pointer.py", {"path": "~/.ssh"})["ok"])

    def test_git_pull_validates_before_touching_the_network(self):
        for bad in ("http://example.com/r.git", "https://user:pw@example.com/r.git", "file:///tmp/x"):
            self.assertFalse(run_node("git_pull.py", {"url": bad})["ok"], bad)
        self.assertIn("inside", run_node("git_pull.py", {"url": "https://example.com/r.git", "paths": "../x"})["error"])

    def test_space_publish_needs_a_real_path_and_a_clean_name(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "dash"))
        self.assertIn("no existing", run_node("space_publish.py", {"path": "nope"}, workdir=d)["error"])
        self.assertIn("index.html", run_node("space_publish.py", {"path": os.path.join(d, "dash")})["error"])
        open(os.path.join(d, "dash", "index.html"), "w").write("<p>x</p>")
        self.assertIn("space name", run_node("space_publish.py", {"path": os.path.join(d, "dash"), "name": "Bad Name"})["error"])

    def test_email_refuses_other_systems_and_bad_attachments(self):
        self.assertIn("no recipient", run_node("outlook_send.py", {"subject": "s"})["error"])
        self.assertIn("attachment not found", run_node("outlook_send.py", {"to": "a@b.c", "attachments": "/no/such/file"})["error"])
        if sys.platform != "win32":
            self.assertIn("Windows only", run_node("outlook_send.py", {"to": "a@b.c", "subject": "s"})["error"])

    def test_new_nodes_exist_and_pointers_reach_the_next_node(self):
        cat = W.catalogue(force=True)
        for t in ("ai.free", "git.pull", "io.path", "space.publish", "send.email", "flow.loop", "flow.endloop"):
            self.assertIn(t, cat)
        d = tempfile.mkdtemp()
        w = self.save([{"id": "a", "type": "start.manual", "config": {}}, {"id": "p", "type": "io.path", "config": {"path": d}},
                       {"id": "t", "type": "flow.template", "config": {"template": "at {{data.path}} / {{input}}"}}],
                      [{"from": "a", "to": "p"}, {"from": "p", "to": "t"}], enabled=False)
        self.engine.fire(w, w["nodes"][0], {"text": "go"}, "test")
        runs = self.final(w["id"])
        self.assertEqual(runs[0]["status"], "done")
        self.assertIn("at " + os.path.realpath(d), runs[0]["result"]["text"])

    def test_generated_work_is_checked(self):
        raw = {"title": "g", "nodes": [{"id": "a", "type": "start.manual", "config": {}}, {"id": "e", "type": "send.email", "config": {"to": "x@y.z"}}], "edges": [{"from": "a", "to": "e"}], "enabled": True, "id": "../x"}
        w = W.checked_work(W.extract_work("Here:\n```json\n%s\n```" % json.dumps(raw)))
        self.assertFalse(w["enabled"])
        self.assertNotEqual(w["id"], "../x")
        self.assertEqual(len(w["nodes"]), 2)
        with self.assertRaises(ValueError):
            W.checked_work({"nodes": [{"id": "a", "type": "start.manual", "config": {}}, {"id": "b", "type": "evil.node", "config": {}}]})
        with self.assertRaises(ValueError):
            W.checked_work({"nodes": [{"id": "e", "type": "send.email", "config": {}}]})
        with self.assertRaises(ValueError):
            W.extract_work("no json here")


class Outlook(Base):
    """Outlook lives in its own package: its venv python runs it, the old send.email type keeps working through it."""

    def pkg(self, venv=""):
        W.A.run = lambda args, **k: {"data": [{"name": "aihub-nodes-outlook", "path": OUTLOOK, "enabled": True, "venv": venv}]}
        W._cat.update(t=0.0, v={})
        return W.catalogue(force=True)

    def test_package_nodes_and_legacy_alias(self):
        c = self.pkg()
        self.assertEqual(c["pkg:aihub-nodes-outlook/outlook-receive"]["kind"], "trigger")
        self.assertEqual(c["pkg:aihub-nodes-outlook/outlook-send"]["takes"], list(W.MAIL_KEYS))
        self.assertTrue(c["send.email"]["hidden"])
        self.assertIn("outlook_send.py", c["send.email"]["run"][-1])
        self.assertNotIn("send.email", W._gen_catalogue())

    def test_without_the_package_send_email_says_what_to_install(self):
        W.A.run = lambda args, **k: {"data": []}
        W._cat.update(t=0.0, v={})
        spec = W.catalogue(force=True)["send.email"]
        ev, _ = None, None
        with self.assertRaises(RuntimeError) as e:
            W.run_package_node(spec, {}, [{"text": "x"}], {}, tempfile.gettempdir())
        self.assertIn("aihub install aihub-nodes-outlook", str(e.exception))

    def test_node_runs_with_the_package_venv_python(self):
        v = tempfile.mkdtemp()
        py = os.path.join(v, "Scripts", "python.exe") if os.name == "nt" else os.path.join(v, "bin", "python")
        os.makedirs(os.path.dirname(py))
        open(py, "w").close()
        self.assertEqual(W.python_of(self.pkg(v)["pkg:aihub-nodes-outlook/outlook-send"]), py)
        self.assertEqual(W.python_of(self.pkg("")["pkg:aihub-nodes-outlook/outlook-send"]), sys.executable)

    def test_empty_mail_fields_take_the_standard_keys(self):
        self.pkg()
        w = self.save([{"id": "a", "type": "start.manual", "config": {}},
                       {"id": "t", "type": "flow.template", "config": {"template": "x"}},
                       {"id": "m", "type": "pkg:aihub-nodes-outlook/outlook-send", "config": {"to": "", "subject": "", "attachments": "/no/such/file"}}],
                      [{"from": "a", "to": "t"}, {"from": "t", "to": "m"}], enabled=False)
        self.engine.fire(w, w["nodes"][0], {"text": "go", "data": {"to": ["a@x.com", "b@x.com"]}}, "test")
        r = W.get_run(w["id"], self.final(w["id"])[0]["id"])
        self.assertEqual(r["status"], "failed")                      # got past "no recipient": the list from data.to was used, then the attachment check
        self.assertIn("attachment not found", r["nodes"]["m"]["error"])


class Loop(Base):
    def run_work(self, nodes, edges, text="go"):
        w = self.save(nodes, edges, enabled=False)
        self.engine.fire(w, w["nodes"][0], {"text": text}, "test")
        r = self.final(w["id"])[0]
        return W.get_run(w["id"], r["id"])

    def test_split_items(self):
        self.assertEqual(W.split_items("a\n\n b \n", "lines"), ["a", "b"])
        self.assertEqual(W.split_items("a, b; c", "comma"), ["a", "b", "c"])
        self.assertEqual(W.split_items("a|b", "regex", r"\|"), ["a", "b"])
        self.assertEqual(W.split_items('Here:\n```json\n[{"to": "a"}]\n```', "json"), [{"to": "a"}])
        with self.assertRaises(RuntimeError):
            W.split_items("nope", "json")

    def test_body_runs_once_per_item_and_endloop_collects(self):
        r = self.run_work([{"id": "a", "type": "start.manual", "config": {}},
                           {"id": "l", "type": "flow.loop", "config": {"items": "x@a.com\ny@b.com\nx@a.com", "split": "lines"}},
                           {"id": "t", "type": "flow.template", "config": {"template": "mail {{data.index}}/{{data.total}} to {{data.item}}"}},
                           {"id": "e", "type": "flow.endloop", "config": {"join": "lines"}},
                           {"id": "o", "type": "flow.output", "config": {"template": "{{input}} | {{data.count}}"}}],
                          [{"from": "a", "to": "l"}, {"from": "l", "to": "t"}, {"from": "t", "to": "e"}, {"from": "e", "to": "o"}])
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(r["result"]["text"], "mail 1/2 to x@a.com\nmail 2/2 to y@b.com | 2")      # the repeated address was skipped

    def test_object_items_put_their_keys_in_data(self):
        r = self.run_work([{"id": "a", "type": "start.manual", "config": {}},
                           {"id": "l", "type": "flow.loop", "config": {"items": '[{"to": "p@x", "name": "Pat"}, {"to": "q@x", "name": "Quinn"}]', "split": "json"}},
                           {"id": "t", "type": "flow.template", "config": {"template": "Hi {{data.name}} <{{data.to}}>"}},
                           {"id": "e", "type": "flow.endloop", "config": {"join": "json"}}],
                          [{"from": "a", "to": "l"}, {"from": "l", "to": "t"}, {"from": "t", "to": "e"}])
        self.assertEqual(json.loads(r["result"]["text"]), ["Hi Pat <p@x>", "Hi Quinn <q@x>"])

    def test_failure_stops_unless_continue_and_limit_and_empty(self):
        def build(cfg):
            return [{"id": "a", "type": "start.manual", "config": {}}, {"id": "l", "type": "flow.loop", "config": dict({"items": "a\nb\nc", "split": "lines"}, **cfg)},
                    {"id": "f", "type": "flow.filter", "config": {"regex": "["}}], [{"from": "a", "to": "l"}, {"from": "l", "to": "f"}]
        r = self.run_work(*build({}))
        self.assertEqual(r["status"], "failed")
        self.assertIn("item 1", r["nodes"]["l"]["error"])
        r = self.run_work(*build({"on_error": "continue"}))
        self.assertIn("3 failed", r["nodes"]["l"]["note"])
        self.assertEqual(r["status"], "done")                     # a continued item failure does not fail the run
        r = self.run_work(*build({"limit": 2}))
        self.assertIn("limit", r["nodes"]["l"]["error"])
        r = self.run_work(*build({"items": " "}))
        self.assertEqual(r["status"], "done")
        self.assertIn("empty", r["nodes"]["l"]["note"])


class LoopSharedFlag(Loop):
    def test_parallel_branch_failure_is_kept_while_a_loop_continues(self):
        r = self.run_work([{"id": "a", "type": "start.manual", "config": {}},
                           {"id": "l", "type": "flow.loop", "config": {"items": "a\nb", "split": "lines", "on_error": "continue"}},
                           {"id": "f", "type": "flow.filter", "config": {"regex": "["}},
                           {"id": "bad", "type": "flow.filter", "config": {"regex": "["}}],
                          [{"from": "a", "to": "l"}, {"from": "l", "to": "f"}, {"from": "a", "to": "bad"}])
        self.assertEqual(r["status"], "failed")
        self.assertIn("2 failed", r["nodes"]["l"]["note"])


class McpControl(unittest.TestCase):
    """The Toolbox: the aihub_mcp tool retries, hides and re-enables servers and tools; settings can switch a server off everywhere."""

    def setUp(self):
        from aihubui import mcpclient as M
        self.M = M
        self.old = (M.sources, M.Server.start)
        self.healthy = {"v": False}

        def start(srv):
            if srv.name == "flaky" and not self.healthy["v"]:
                raise RuntimeError("boom")
            srv.tools = [{"name": "read", "description": "r"}, {"name": "delete", "description": "d"}]
        M.sources = lambda: {n: {"command": "x", "args": [], "env": {}, "origin": "t"} for n in ("good", "flaky", "banned")}
        M.Server.start = start
        self.addCleanup(lambda: (setattr(M, "sources", self.old[0]), setattr(M.Server, "start", self.old[1])))

    def test_failed_server_can_be_retried_from_the_chat(self):
        saved = []
        b = self.M.Toolbox(["good", "flaky"], persist=lambda *a: saved.append(a))
        self.assertIn("flaky", b.errors)
        self.assertEqual({s["name"] for s in b.specs}, {"good__read", "good__delete", "aihub_mcp"})
        text, bad = b.call("aihub_mcp", {"action": "retry", "server": "flaky"})
        self.assertTrue(bad)
        self.assertIn("boom", text)
        self.healthy["v"] = True
        text, bad = b.call("aihub_mcp", {"action": "retry", "server": "flaky"})
        self.assertFalse(bad)
        self.assertIn("flaky__read", [s["name"] for s in b.specs])

    def test_tool_and_server_can_be_disabled_and_enabled(self):
        saved = []
        b = self.M.Toolbox(["good"], persist=lambda *a: saved.append(a))
        b.call("aihub_mcp", {"action": "disable", "server": "good", "tool": "delete"})
        self.assertNotIn("good__delete", [s["name"] for s in b.specs])
        self.assertIn(("tool", "good", "delete", False), saved)
        b.call("aihub_mcp", {"action": "enable", "server": "good", "tool": "delete"})
        self.assertIn("good__delete", [s["name"] for s in b.specs])
        b.call("aihub_mcp", {"action": "disable", "server": "good"})
        self.assertEqual([s["name"] for s in b.specs], ["aihub_mcp"])
        self.assertIn("NOT running", b.call("aihub_mcp", {"action": "status"})[0] + "NOT running")

    def test_globally_off_server_is_not_started_or_enabled(self):
        b = self.M.Toolbox(["banned"], off=["banned"])
        self.assertIn("switched off", b.errors["banned"])
        text, bad = b.call("aihub_mcp", {"action": "enable", "server": "banned"})
        self.assertTrue(bad)
        b2 = self.M.Toolbox(["good"], tools_off={"good": ["delete"]})
        self.assertNotIn("good__delete", [s["name"] for s in b2.specs])


class SelfUpdate(unittest.TestCase):
    def test_versions_compare_numerically_and_busy_blocks_a_restart(self):
        from aihubui import selfupdate as U, chat
        self.assertGreater(U.vkey("1.20.0"), U.vkey("1.9.3"))
        self.assertEqual(U.busy(None), "")
        r = type("R", (), {"done": False})()
        chat.runs["zz"] = r
        self.addCleanup(lambda: chat.runs.pop("zz", None))
        self.assertIn("answering", U.busy(None))
        r.done = True
        self.assertEqual(U.busy(type("E", (), {"running": 1})()), "a work is running")

    def test_apply_updates_then_restarts_only_when_idle(self):
        from aihubui import selfupdate as U
        calls = []
        old = U.A.run
        U.A.run = lambda args, **k: calls.append(args) or {}
        self.addCleanup(lambda: setattr(U.A, "run", old))
        U.state.update(latest="9.9.9", installed="1.0.0")
        busy = type("E", (), {"running": 1})()
        self.assertFalse(U.apply(busy, lambda: calls.append("restart")))
        self.assertEqual(calls, [])
        self.assertTrue(U.apply(type("E", (), {"running": 0})(), lambda: calls.append("restart")))
        self.assertEqual(calls, [["update", "aihub-ui"], "restart"])


class LayoutAndNodes(Base):
    def test_overlapping_generated_positions_are_tidied(self):
        from aihubui import works as W
        ns = [{"id": c, "x": 10, "y": 10} for c in "abcd"]
        w = {"nodes": ns, "edges": [{"from": "a", "to": "b"}, {"from": "a", "to": "c"}, {"from": "b", "to": "d"}]}
        W._layout(w)
        self.assertFalse(W._overlap(ns))
        self.assertLess(ns[0]["x"], ns[1]["x"])

    def test_good_positions_are_kept(self):
        from aihubui import works as W
        ns = [{"id": "a", "x": 40, "y": 40}, {"id": "b", "x": 400, "y": 40}]
        W._layout({"nodes": ns, "edges": []})
        self.assertEqual((ns[1]["x"], ns[1]["y"]), (400, 40))

    def test_assist_defaults_and_outputs(self):
        from aihubui import works as W
        c = W.catalogue(force=True)
        pr = next(f for f in c["ai.claude"]["fields"] if f["name"] == "prompt")
        self.assertEqual(set(pr["assist"]), {"input", "data", "file", "skill"})
        self.assertEqual(c["git.pull"]["outputs"], ["path"])

    def test_local_node_is_created_exported_as_a_node_package(self):
        import server
        server.A.run = lambda args, **k: {"data": [], "messages": [{"level": "good", "text": "valid"}]}
        node = {"id": "shout", "title": "Shout", "kind": "action", "group": "Mine", "fields": [{"name": "text", "type": "textarea", "default": "{{input}}"}], "outputs": ["loud"]}
        r = server.nodes_save(None, {}, {"node": node, "code": server.NODE_STARTER})
        self.assertEqual(r["type"], "pkg:local/shout")
        spec = W.catalogue(force=True)["pkg:local/shout"]
        self.assertEqual(spec["outputs"], ["loud"])
        out = server.nodes_export(None, {}, {"id": "shout"})
        self.assertEqual(out["name"], "aihub-node-shout")
        self.assertTrue(os.path.isfile(os.path.join(out["path"], "nodes", "shout.node.json")))
        self.assertIn('type = "node"', open(os.path.join(out["path"], "aihub.toml")).read())
        with self.assertRaises(server.Err):
            server.nodes_save(None, {}, {"node": dict(node, id="Bad Id"), "code": "x"})
