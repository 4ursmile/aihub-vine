"""aihub-ui builtin package: crypto box, .env writing, manifest, path safety, and the HTTP guards."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from aihub.core import manifest as M

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import aihub as A, secretbox, store  # noqa: E402


class Pkg(unittest.TestCase):
    def test_manifest_is_a_valid_unique_service(self):
        root = os.path.dirname(UI)
        with open(os.path.join(root, "aihub.toml"), encoding="utf-8") as f:
            m = M.normalize(M.parse(f.read()))
        self.assertEqual(m["package"]["type"], "service")
        self.assertTrue(M.runnable(m)["unique"])
        self.assertTrue(os.path.isfile(os.path.join(root, M.runnable(m)["run"])))


class Box(unittest.TestCase):
    def test_roundtrip_tamper_and_private_files(self):
        with tempfile.TemporaryDirectory() as d:
            b = secretbox.Box(d)
            b.put("K", "s3cr3t")
            self.assertEqual(b.get_all(), {"K": "s3cr3t"})
            raw = open(b.path, "rb").read()
            self.assertNotIn(b"s3cr3t", raw)
            self.assertEqual(os.stat(b.path).st_mode & 0o077, 0)
            self.assertEqual(os.stat(b.key_path).st_mode & 0o077, 0)
            open(b.path, "wb").write(raw[:-1] + bytes([raw[-1] ^ 1]))
            self.assertEqual(b.get_all(), {})
            self.assertIn("integrity", b.error)


class Env(unittest.TestCase):
    def test_write_env_keeps_other_lines_and_refuses_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            open(p, "w").write("# c\nA=1\nB=2\n")
            A.write_env(p, {"B": "9", "C": "x"})
            self.assertEqual(open(p).read(), "# c\nA=1\nB=9\nC=x\n")
            with self.assertRaises(A.CliError):
                A.write_env(p, {"B": "1\nEVIL=1"})

    def test_masks_secret_looking_env(self):
        self.assertEqual(A.mask_env({"ANTHROPIC_AUTH_TOKEN": "abc", "X_URL": "u"}), {"ANTHROPIC_AUTH_TOKEN": "set", "X_URL": "u"})

    def test_env_spec_reads_service_env(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "aihub.toml"), "w").write('[package]\nname="x"\n[service]\nrun="a"\n[[service.env]]\nname="K"\nsecret=true\nrequired=true\n')
            self.assertEqual([(e["name"], e["secret"]) for e in A.env_spec({"path": d})], [("K", True)])


class Paths(unittest.TestCase):
    def test_safe_join_blocks_escape(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(store.safe_join(d, "../x"))
            self.assertIsNone(store.safe_join(d, "a/../../x"))
            self.assertTrue(store.safe_join(d, "a/b.txt").startswith(os.path.realpath(d)))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        from http.server import ThreadingHTTPServer
        cls.tmp = tempfile.mkdtemp()
        os.environ["AIHUB_HOME"] = cls.tmp
        server.BOX = secretbox.Box(cls.tmp)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        server.ADDR["port"] = cls.srv.server_address[1]
        cls.port, cls.server = server.ADDR["port"], server
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, method="GET", headers=None, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as x:
                return x.status, x.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_guards(self):
        ck = {"Cookie": "aihub_ui=" + self.server.TOKEN}
        self.assertEqual(self.req("/api/sessions")[0], 401)                                          # no key
        self.assertEqual(self.req("/api/sessions", headers=dict(ck, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.req("/api/sessions", headers=dict(ck, Host="evil.example"))[0], 403)     # DNS rebinding
        self.assertEqual(self.req("/api/sessions", "POST", ck, b"{}")[0], 403)                         # no custom header
        self.assertEqual(self.req("/api/sessions", headers=ck)[0], 200)
        self.assertEqual(self.req("/?t=wrong")[0], 403)

    def test_chat_files_need_the_signature(self):
        s = store.create_session("t")
        os.makedirs(store.work_dir(s["id"]), exist_ok=True)
        open(os.path.join(store.work_dir(s["id"]), "a.html"), "w").write("<p>x</p>")
        self.assertEqual(self.req("/w/%s/%s/a.html" % (s["id"], self.server.sig(s["id"])))[0], 200)
        self.assertEqual(self.req("/w/%s/bad/a.html" % s["id"])[0], 403)
        code, _ = self.req("/w/%s/%s/a.html" % (s["id"], self.server.sig(s["id"])))
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()


class Prefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = self.tmp
        self.addCleanup(lambda: os.environ.pop("AIHUB_HOME", None) if self.old is None else os.environ.__setitem__("AIHUB_HOME", self.old))

    def test_defaults_clamp_and_env(self):
        self.assertEqual(store.prefs_env(), {"AIHUB_JUDGE": "1", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"})     # policy off for UI chats, auto-compact at 80% of a 1M window
        store.save_prefs({"policy": "on", "compact_pct": 5})
        self.assertEqual(store.prefs_env(), {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"})                        # clamped, guard back on
        store.save_prefs({"autocompact": False, "policy": "bogus"})
        self.assertEqual(store.prefs_env(), {"DISABLE_AUTO_COMPACT": "1", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"})
        store.save_prefs({"window": 200000})
        self.assertEqual(store.prefs()["window"], 200000)
        self.assertEqual(store.prefs_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")
        with self.assertRaises(ValueError):
            store.save_prefs({"window": "lots"})

    def test_models_carry_a_window_and_slash_commands_are_discovered(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude", "skills", "demo"))
        os.makedirs(os.path.join(home, ".claude", "commands", "team"))
        open(os.path.join(home, ".claude", "skills", "demo", "SKILL.md"), "w").write("---\nname: demo\ndescription: Does a demo\n---\n")
        open(os.path.join(home, ".claude", "commands", "team", "ship.md"), "w").write("---\ndescription: Ship it\n---\nbody")
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old))
        json.dump({"model": "sonnet[1m]", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "x-opus"}}, open(os.path.join(home, ".claude", "settings.json"), "w"))
        names = {c["name"]: c for c in A.slash_commands()}
        self.assertEqual(names["demo"]["description"], "Does a demo")
        self.assertEqual(names["team:ship"]["source"], "command")
        ms = {m["id"]: m for m in A.models()["models"]}
        self.assertNotIn("sonnet[1m]", ms)                                          # only the three tiers (+ picker options when switched on)
        self.assertEqual(ms["opus"]["window"], 200_000)


class WorkingFolder(unittest.TestCase):
    """A chat can work in a folder the user chose: it is created, never deleted, listed as a tree, editable, comparable, and searchable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(lambda: os.environ.pop("AIHUB_HOME", None) if self.old is None else os.environ.__setitem__("AIHUB_HOME", self.old))
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.proj, "src"))
        with open(os.path.join(self.proj, "src", "a.py"), "w") as f:
            f.write("x = 1\n")

    def test_folder_is_created_used_and_never_deleted(self):
        from aihubui import chat
        new = store.check_cwd(os.path.join(self.tmp, "fresh", "deep"))
        self.assertTrue(os.path.isdir(new))
        for bad in ("", "/"):
            with self.assertRaises(ValueError):
                store.check_cwd(bad)
        name = store.link_folder(self.proj)
        self.assertEqual(name, "proj")
        self.assertEqual(store.link_folder(self.proj), "proj")                            # same path, same sidebar folder
        other = os.path.join(self.tmp, "x", "proj")
        os.makedirs(other)
        self.assertEqual(store.link_folder(other), "proj (2)")                            # same name, other path
        s = store.create_session("t", name, chat.build_system(""), cwd=self.proj)
        self.assertEqual(store.session_dir(s["id"]), self.proj)
        self.assertEqual([f["path"] for f in store.tree(s["id"])], ["src/a.py"])
        self.assertFalse(os.path.isdir(store.work_dir(s["id"])))                          # no private folder made
        store.delete_session(s["id"])
        self.assertTrue(os.path.isfile(os.path.join(self.proj, "src", "a.py")))           # the user's folder survives
        self.assertEqual(store.folder_dir("proj"), self.proj)

    def test_edit_and_diff_stay_inside_the_folder(self):
        s = store.create_session("t", "", "", cwd=self.proj)
        sid = s["id"]
        store.snapshot(sid, store.tree(sid))
        store.write_text(sid, "src/a.py", "x = 2\ny = 3\n")
        self.assertEqual(store.snap_text(sid, "src/a.py"), "x = 1\n")
        self.assertEqual(store.read_text(sid, "src/a.py")["text"], "x = 2\ny = 3\n")
        self.assertEqual(store.snap_text(sid, "src/new.py"), "")                          # not there at the snapshot = a new file
        for bad in ("../outside", "/etc/passwd", ".git/config", "src/missing.py"):
            self.assertIsNone(store.read_text(sid, bad))
            with self.assertRaises(ValueError):
                store.write_text(sid, bad, "x")
        with open(os.path.join(self.proj, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2")
        self.assertEqual(store.read_text(sid, "bin.dat"), {"binary": True})
        self.assertIsNone(store.git_text(sid, "src/a.py"))                                # not a git repo

    def test_per_chat_soul_and_search(self):
        from aihubui import chat
        self.assertIn("pirate", chat.build_system("", soul="I am a pirate"))
        self.assertNotIn("# Soul", chat.build_system("", soul="   "))
        s = store.create_session("Payments work", "", "")
        store.append(s["id"], {"k": "user", "text": "please refactor the Billing module"})
        store.append(s["id"], {"k": "msg", "blocks": [{"type": "text", "text": "Done with the invoice code"}]})
        self.assertEqual([r["id"] for r in store.search("payments")], [s["id"]])           # title
        hit = store.search("BILLING")
        self.assertEqual(len(hit), 1)
        self.assertIn("Billing", hit[0]["snippet"])
        self.assertTrue(store.search("invoice"))
        self.assertEqual(store.search("x"), [])                                           # too short
        self.assertEqual(store.search("nothing-like-this"), [])


MCP_ECHO = r'''import json, sys
for ln in sys.stdin:
    m = json.loads(ln)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "echo", "description": "echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
    else:
        r = {"content": [{"type": "text", "text": "echo: " + m["params"]["arguments"]["text"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


class FreeChat(unittest.TestCase):
    """Free chat = the UI's own harness: a streaming call to the Messages API, same events and history as a Claude Code turn."""

    def setUp(self):
        import http.server
        self.tmp = tempfile.mkdtemp()
        self.old = {k: os.environ.get(k) for k in ("AIHUB_HOME", "HOME", "CLAUDE_HOME")}
        os.environ.update(AIHUB_HOME=os.path.join(self.tmp, "h"), HOME=self.tmp)
        os.environ.pop("CLAUDE_HOME", None)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in self.old.items()])
        seen = self.seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": i} for i in seen.get("models", [])]}).encode())

            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if seen.get("only") and seen["body"]["model"] != seen["only"]:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": "The requested model '%s' is not available for this API key" % seen["body"]["model"]}}).encode())
                    return
                seen.setdefault("bodies", []).append(seen["body"])
                seen["key"] = self.headers.get("x-api-key")
                if seen.get("fail"):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": "bad key sk-ant-secret"}}).encode())
                    return
                self.send_response(200)
                self.end_headers()
                if seen.get("tools"):
                    if len(seen["bodies"]) == 1:
                        evs = (("message_start", {"message": {"usage": {"input_tokens": 5}}}),
                               ("content_block_start", {"content_block": {"type": "tool_use", "id": "tu1", "name": "echo__echo"}}),
                               ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{\"text\":"}}),
                               ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": " \"hi\"}"}}),
                               ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}))
                    else:
                        evs = (("content_block_start", {"content_block": {"type": "text"}}),
                               ("content_block_delta", {"delta": {"type": "text_delta", "text": "done: echo: hi"}}),
                               ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}))
                    for k, d in evs:
                        self.wfile.write(("event: %s\ndata: %s\n\n" % (k, json.dumps(d))).encode())
                    return
                for k, d in (("message_start", {"message": {"usage": {"input_tokens": 12, "cache_read_input_tokens": 3}}}),
                             ("content_block_start", {"content_block": {"type": "thinking"}}),
                             ("content_block_delta", {"delta": {"type": "thinking_delta", "thinking": "hmm"}}),
                             ("content_block_start", {"content_block": {"type": "text"}}),
                             ("content_block_delta", {"delta": {"type": "text_delta", "text": "Hel"}}),
                             ("content_block_delta", {"delta": {"type": "text_delta", "text": "lo"}}),
                             ("message_delta", {"usage": {"output_tokens": 5}})):
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (k, json.dumps(d))).encode())
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env = {"ANTHROPIC_API_KEY": "sk-ant-secret", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % self.srv.server_address[1]}

    def test_events_history_and_request(self):
        from aihubui import freechat
        s = store.create_session("t", "", "")
        store.append(s["id"], {"k": "user", "text": "earlier"})
        store.append(s["id"], {"k": "msg", "blocks": [{"type": "tool", "name": "Bash"}, {"type": "text", "text": "old answer"}]})
        store.append(s["id"], {"k": "user", "text": "now?"})
        evs = list(freechat.turn(s, "now?", "haiku", "medium", extra_env=self.env))
        self.assertEqual([e["k"] for e in evs], ["ctx", "think", "delta", "delta", "msg", "result", "done"])
        self.assertEqual(evs[-2]["usage"]["output"], 5)
        b = self.seen["body"]
        self.assertEqual([(m["role"], m["content"][0]["text"]) for m in b["messages"]], [("user", "earlier"), ("assistant", "old answer"), ("user", "now?")])   # tools left out, sent once
        self.assertEqual(b["thinking"]["budget_tokens"], 5000)
        self.assertEqual(self.seen["key"], "sk-ant-secret")
        self.assertEqual(store.history(s["id"])[-2]["blocks"], [{"type": "thinking", "text": "hmm"}, {"type": "text", "text": "Hello"}])
        self.assertEqual(store.get_session(s["id"])["engine"], "free")

    def test_report_in_an_html_block_becomes_a_file_for_the_canvas(self):
        from aihubui import freechat
        s = store.create_session("t", "", "")
        doc = "<!doctype html><title>Q3 Report</title><meta name=\"aihub-kind\" content=\"doc\"><article class=\"doc\"><h1>Q3 Report</h1>" + "<p>text</p>" * 30 + "</article>"
        made = freechat._save_reports(s["id"], "Here you go\n```html\n" + doc + "\n```\nDone.")
        self.assertEqual([m["path"] for m in made], ["q3-report.html"])
        self.assertIn("<article", open(os.path.join(store.session_dir(s["id"]), "q3-report.html")).read())
        self.assertEqual(freechat._save_reports(s["id"], "```html\n<b>hi</b>\n```"), [])                     # a snippet is not a report
        again = freechat._save_reports(s["id"], "```html\n" + doc + "\n```")
        self.assertEqual(again[0]["path"], "q3-report-2.html")                                              # never overwrites

    def test_mcp_tool_loop(self):
        """The model asks for a tool of an MCP server the chat switched on; free chat calls it, sends the result back and shows the final answer."""
        from aihubui import freechat
        server = os.path.join(self.tmp, "mcp_echo.py")
        with open(server, "w") as f:
            f.write(MCP_ECHO)
        store.save_ui_mcp({"mcpServers": {"echo": {"command": sys.executable, "args": [server]}}})
        s = store.create_session("t", "", "")
        store.update_session(s["id"], mcp=["echo", "gone"])
        s = store.get_session(s["id"])
        self.seen["tools"] = True
        evs = list(freechat.turn(s, "say hi through the tool", extra_env=self.env))
        kinds = [e["k"] for e in evs]
        self.assertIn("tool_result", kinds)
        self.assertEqual(kinds[-1], "done")
        tr = next(e for e in evs if e["k"] == "tool_result")
        self.assertEqual((tr["text"], tr["error"]), ("echo: hi", False))
        msgs = [e for e in evs if e["k"] == "msg"]
        self.assertEqual(msgs[0]["blocks"][-1]["name"], "echo__echo")                       # the tool call is shown like a Claude Code one
        self.assertEqual(msgs[-1]["blocks"][-1], {"type": "text", "text": "done: echo: hi"})
        self.assertTrue(any(e["k"] == "note" and "gone" in e["text"] for e in evs))        # an unknown server is a note, not a failure
        second = self.seen["bodies"][1]["messages"]
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")
        self.assertEqual(second[-2]["content"][-1]["type"], "tool_use")
        self.assertEqual(self.seen["bodies"][0]["tools"][0]["name"], "echo__echo")

    def test_unavailable_default_model_is_replaced_by_one_the_key_has(self):
        from aihubui import freechat
        self.seen.update(only="gw/sonnet-4-5", models=["gw/haiku-4", "gw/sonnet-4-5", "gw/sonnet-3-7"])
        s = store.create_session("t", "", "")
        evs = list(freechat.turn(s, "hi", model="sonnet", extra_env=self.env))
        self.assertEqual(self.seen["body"]["model"], "gw/sonnet-4-5")
        self.assertTrue(any(e["k"] == "note" and "gw/sonnet-4-5" in e["text"] for e in evs))
        self.assertEqual(next(e for e in evs if e["k"] == "msg")["blocks"][-1]["text"], "Hello")
        self.assertFalse(any(e["k"] == "error" for e in evs))
        self.assertEqual(store.prefs()["free_model"], "gw/sonnet-4-5")                    # remembered
        self.seen["bodies"] = []
        list(freechat.turn(s, "again", model="sonnet", extra_env=self.env))
        self.assertEqual(len(self.seen["bodies"]), 1)                                      # straight to the right model now
        store.save_prefs({"free_model": "gw/sonnet-3-7"})
        self.seen.update(only="")
        list(freechat.turn(s, "x", model="haiku", extra_env=self.env))
        self.assertEqual(self.seen["body"]["model"], "gw/sonnet-3-7")                      # the setting answers for any alias
        list(freechat.turn(s, "x", model="gw/haiku-4", extra_env=self.env))
        self.assertEqual(self.seen["body"]["model"], "gw/haiku-4")                         # an explicit id wins

    def test_no_models_endpoint_error_says_what_to_do(self):
        from aihubui import freechat
        self.seen.update(only="nothing-matches", models=[])
        s = store.create_session("t", "", "")
        evs = list(freechat.turn(s, "hi", extra_env=self.env))
        self.assertIn("Free chat model", next(e for e in evs if e["k"] == "error")["text"])

    def test_errors_never_carry_the_key(self):
        from aihubui import freechat
        s = store.create_session("t", "", "")
        self.seen["fail"] = True
        evs = list(freechat.turn(s, "hi", extra_env=self.env))
        err = next(e for e in evs if e["k"] == "error")
        self.assertIn("API 401", err["text"])
        self.assertNotIn("sk-ant-secret", err["text"])
        self.assertEqual(evs[-1], {"k": "done"})

    def test_no_key_says_how_to_add_one(self):
        from aihubui import freechat
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}):
            with self.assertRaises(ValueError) as c:
                freechat.credentials({})
        self.assertIn("API key", str(c.exception))

    def test_model_aliases(self):
        from aihubui import freechat
        self.assertEqual(freechat.resolve_model("sonnet[1m]", {"ANTHROPIC_DEFAULT_SONNET_MODEL": "my-sonnet"}), "my-sonnet")
        self.assertEqual(freechat.resolve_model("claude-x", {}), "claude-x")
        self.assertEqual(freechat.resolve_model("", {"ANTHROPIC_MODEL": "opus"}), freechat.DEFAULT_MODELS["opus"])


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_OUT"], "w").write(json.dumps({k: os.environ.get(k) for k in ("AIHUB_JUDGE", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "DISABLE_AUTO_COMPACT")}) + "\n" + " ".join(sys.argv[1:]))
sys.stdin.read()
def out(o): print(json.dumps(o), flush=True)
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}})
out({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto", "pre_tokens": 1203, "post_tokens": 300}})
out({"type": "result", "is_error": False, "usage": {"input_tokens": 3, "output_tokens": 5}, "total_cost_usd": 0.01, "duration_ms": 10})
'''


FAKE_WRITER = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
os.makedirs("out", exist_ok=True)
open("out/report.txt", "w").write("made by a shell command")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 1}}}), flush=True)
print(json.dumps({"type": "result", "is_error": False, "usage": {}, "duration_ms": 5}), flush=True)
'''


class FilesListed(unittest.TestCase):
    def test_turn_lists_files_it_created_even_without_a_write_tool(self):
        from aihubui import chat
        tmp = tempfile.mkdtemp()
        binp = os.path.join(tmp, "bin")
        os.makedirs(binp)
        open(os.path.join(binp, "claude"), "w").write(FAKE_WRITER)
        os.chmod(os.path.join(binp, "claude"), 0o755)
        env = {"AIHUB_HOME": tmp, "PATH": binp + os.pathsep + os.environ["PATH"]}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        s = store.create_session("t", "", "sys")
        evs = list(chat.turn(s, "make a report"))
        files = [e for e in evs if e["k"] == "files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["items"][0]["path"], "out/report.txt")
        self.assertTrue(files[0]["items"][0]["new"])
        self.assertIn("files", [e["k"] for e in store.history(s["id"])])                  # kept: shown again after a reload


class ChatTurn(unittest.TestCase):
    def test_turn_reports_context_and_compaction_and_applies_prefs(self):
        from aihubui import chat
        tmp = tempfile.mkdtemp()
        binp = os.path.join(tmp, "bin")
        os.makedirs(binp)
        open(os.path.join(binp, "claude"), "w").write(FAKE_CLAUDE)
        os.chmod(os.path.join(binp, "claude"), 0o755)
        env = {"AIHUB_HOME": tmp, "PATH": binp + os.pathsep + os.environ["PATH"], "FAKE_OUT": os.path.join(tmp, "seen.txt")}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        s = store.create_session("t", "", "frozen system text")
        evs = list(chat.turn(s, "hello", model="haiku", effort="high", mode="plan"))
        kinds = [e["k"] for e in evs]
        self.assertEqual(kinds, ["ctx", "msg", "compact", "ctx", "result", "done"])
        self.assertEqual(evs[0]["used"], 1203)                                       # input + cache read + cache write of the last request
        self.assertEqual((evs[2]["pre"], evs[2]["post"]), (1203, 300))
        self.assertEqual(store.get_session(s["id"])["ctx"], 300)                       # after the compaction
        seen = open(env["FAKE_OUT"]).read().split("\n")
        self.assertEqual(json.loads(seen[0]), {"AIHUB_JUDGE": "1", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80", "DISABLE_AUTO_COMPACT": None})
        for flag in ("--model haiku", "--effort high", "--permission-mode plan", "--append-system-prompt-file"):
            self.assertIn(flag, seen[1])


class UsageAndSpaces(unittest.TestCase):
    """Usage tab data, price alerts, hub upload of free chat turns, and the Space share route's path rules."""

    def setUp(self):
        from aihubui import hubusage, usagereport
        self.hu, self.ur = hubusage, usagereport
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home})
        p.start()
        self.addCleanup(p.stop)

    def test_rules_by_period_with_several_thresholds(self):
        import datetime
        now = datetime.datetime(2026, 10, 29, 12)                       # a Thursday; week starts Mon 26th, quarter Oct 1
        days = [{"name": "2026-10-29", "cost": 10}, {"name": "2026-10-27", "cost": 20}, {"name": "2026-10-05", "cost": 30},
                {"name": "2026-09-20", "cost": 100}]
        R = lambda **k: dict({"id": "x", "period": "month", "n": 7, "limit": 100, "at": [50, 80, 100]}, **k)
        st, al = self.ur.evaluate(days, [R(period="today", limit=10), R(period="week", limit=100), R(period="month", limit=100),
                                         R(period="quarter", limit=1000), R(period="last", n=3, limit=1000)], now)
        self.assertEqual([round(x["spent"]) for x in st], [10, 30, 60, 60, 30])
        self.assertEqual([x["level"] for x in st], ["over", "ok", "warn", "ok", "ok"])
        self.assertEqual(st[2]["crossed"], 50)
        self.assertEqual(sorted(a["level"] for a in al), ["over", "warn"])
        self.assertEqual(self.ur.window("quarter", 0, now)[1], 92)

    def test_prefs_validate_alert_rules(self):
        with mock.patch.object(store, "ui_dir", return_value=self.home):
            d = store.save_prefs({"alerts": [{"period": "last", "n": 9999, "limit": "50", "at": "100, 50,80,50"}]})
            r = d["alerts"][0]
            self.assertEqual((r["n"], r["limit"], r["at"]), (365, 50.0, [50, 80, 100]))
            for bad in ([{"period": "year", "limit": 1}], [{"period": "month", "limit": 0}], [{"period": "month", "limit": "lots"}]):
                with self.assertRaises(ValueError):
                    store.save_prefs({"alerts": bad})

    def test_free_chat_turn_is_logged_and_claude_turn_is_not(self):
        with mock.patch.object(self.hu, "kick") as k:
            self.hu.note("s1", "claude", "sonnet", {"input": 5, "output": 5})
            self.assertFalse(os.path.exists(self.hu.log_path()))
            self.hu.note("s2", "free", "claude-sonnet-4", {"input": 10, "output": 5})
            self.assertEqual(k.call_count, 2)
        line = json.loads(open(self.hu.log_path()).read())
        self.assertEqual((line["model"], line["input"], line["output"]), ("claude-sonnet-4", 10, 5))

    def test_cli_reads_the_ui_log_as_tool_aihub_ui(self):
        from aihub.cli import usage
        self.hu.note("s2", "free", "claude-sonnet-4", {"input": 1000, "output": 500})
        recs = usage.collect(("aihub-ui",))
        self.assertEqual([(r["tool"], r["model"], r["input"], r["output"]) for r in recs], [("aihub-ui", "claude-sonnet-4", 1000, 500)])
        self.assertIn("aihub-ui", usage.TOOLS)

    def test_kicks_are_debounced(self):
        with mock.patch.object(self.hu.threading, "Timer") as t:
            self.hu._timer = None
            self.hu.kick()
            self.hu.kick()
            self.assertEqual(t.call_count, 1)
        self.hu._timer = None

    def test_use_event_goes_through_aihub_hook_use(self):
        with mock.patch.object(self.hu.subprocess, "run") as run:
            self.hu.use("chat", session="s1", detail={"engine": "free"}).join(5)
        args, kw = run.call_args
        self.assertEqual(args[0][-4:], ["hook", "--use", "--source", "aihub-ui"])
        self.assertEqual(json.loads(kw["input"]), {"package": "aihub-ui", "component": "chat", "session": "s1", "detail": {"engine": "free"}})

    def test_cli_hook_use_records_one_use_event(self):
        from aihub.cli import hooks
        with mock.patch.object(hooks.telemetry, "record") as rec:
            hooks.use_event(json.dumps({"package": "git-data-dashboard", "component": "work:ab12", "session": "r1", "detail": {"status": "done"}}))
            hooks.use_event(json.dumps({"package": "bad name!", "component": "chat"}))
            hooks.use_event("not json")
        self.assertEqual(rec.call_count, 1)
        ev = rec.call_args[0][0]
        self.assertEqual((ev["kind"], ev["package"], ev["component"], ev["source"]), ("use", "git-data-dashboard", "work:ab12", "aihub-ui"))

    def test_bundle_inlines_what_the_page_loads(self):
        from aihubui import spacebundle
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "data"))
        w = lambda p, t: open(os.path.join(root, p), "w").write(t)
        w("data/sales.json", '{"a": 1}')
        w("app.js", "fetch('data/sales.json').then(r => r.json())")
        w("style.css", "body{background:url(logo.png)}")
        w("logo.png", "PNG")
        w("page.html", '<html><head><link rel="stylesheet" href="style.css"></head><body><img src="logo.png">'
                       '<script src="app.js"></script><script src="https://cdn.jsdelivr.net/npm/x@1/x.js"></script>'
                       '<script src="../outside.js"></script></body></html>')
        text, used, skipped = spacebundle.bundle(os.path.join(root, "page.html"), root)
        self.assertEqual(sorted(used), ["app.js", "data/sales.json", "logo.png", "style.css"])
        self.assertNotIn('src="app.js"', text)
        self.assertIn("fetch('data/sales.json')", text)                     # the call stays; the shim answers it
        self.assertIn('"data/sales.json":["application/json"', text)
        self.assertIn("url(data:image/png;base64,", text)
        self.assertIn('src="https://cdn.jsdelivr.net/npm/x@1/x.js"', text)
        self.assertIn('src="../outside.js"', text)                           # outside the chat folder: never read
        self.assertEqual(skipped, [])

    def test_share_bundles_a_page_and_saves_a_block(self):
        import server
        root = tempfile.mkdtemp()
        open(os.path.join(root, "rows.csv"), "w").write("a,b\n1,2\n")
        open(os.path.join(root, "report.html"), "w").write("<html><head></head><body><script>d3.csv('rows.csv')</script></body></html>")
        sent = []

        def run(args, **k):
            sent.append((args, open(args[2]).read() if os.path.isfile(args[2]) else ""))
            return {"ok": True, "data": {"space": {"ref": "me@r"}, "url": "u", "created": True}}
        with mock.patch.object(server, "_chat_of", return_value={"id": "c1"}), mock.patch.object(server.store, "session_dir", return_value=root), \
                mock.patch.object(server.A, "run", side_effect=run):
            r = server.space_publish(None, {}, {"id": "c1", "path": "report.html", "name": "r"})
            self.assertEqual(r["bundled"], ["rows.csv"])
            self.assertIn('"rows.csv":["text/csv"', sent[0][1])
            self.assertNotEqual(sent[0][0][2], os.path.join(root, "report.html"))      # a bundled copy, the chat file is untouched
            self.assertFalse(os.path.exists(sent[0][0][2]))                            # temp copy removed after the upload
            r = server.space_publish(None, {}, {"id": "c1", "content": "<svg></svg>", "filename": "My chart.html"})
            self.assertEqual(r["path"], "shared/My-chart.html")
            self.assertTrue(os.path.isfile(os.path.join(root, "shared", "My-chart.html")))
            with self.assertRaises(server.Err):
                server.space_publish(None, {}, {"id": "c1", "content": "x", "filename": "a.exe"})

    def test_mentions_bring_another_chats_history_and_tools(self):
        import server
        rows = [{"k": "user", "text": "q%d" % i} if i % 2 == 0 else {"k": "msg", "blocks": [{"type": "text", "text": "a%d" % i}]} for i in range(10)]
        get = lambda cid: {"id": cid, "title": "Sales"} if cid in ("aaaa1111", "bbbb2222") else None
        with mock.patch.object(server.store, "get_session", side_effect=get), mock.patch.object(server.store, "history", return_value=rows):
            more, tools = server.mentions({"id": "bbbb2222"}, "see @chat:aaaa1111:3 and @chat:bbbb2222 @chat:cccc3333 with @tool:git-mcp")
        self.assertIn('chat "Sales" (@chat:aaaa1111, last 3 messages)', more)
        self.assertIn("User: q8", more)
        self.assertNotIn("q6", more)                                       # limit: only the last 3 messages
        self.assertEqual(more.count("[History of the chat"), 1)            # itself and unknown chats skipped
        self.assertEqual(tools, ["git-mcp"])
        self.assertEqual(server.mentions({"id": "x"}, "plain text"), ("", []))

    def test_work_chats_never_ask(self):
        from aihubui import freechat
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertIn("Never ask anything", freechat.system_text({"id": "w", "work": True}))
            self.assertNotIn("Unattended run", freechat.system_text({"id": "c"}))
        self.assertIn("```form block", freechat.FREE_HELP)                    # interactive chats are guided to ask with a form
