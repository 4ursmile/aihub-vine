"""aihub-ui AIHubness (harness.py / harnesstools.py): the tool loop against a scripted fake Messages API, the token savers, the
free chat -> AIHubness -> Claude Code handoff chain, and the hub usage line carrying the model the API really ran."""
import http.server
import json
import os
import tempfile
import threading
import unittest

from tests.test_ui_hint import Base, UI  # noqa: F401  (puts the UI folder on sys.path)
from aihubui import chat, freechat, harness, harnesstools as HT, hubusage, store  # noqa: E402

SERVED = "claude-sonnet-5-5-20261001"


def sse(evs):
    return "".join("event: %s\ndata: %s\n\n" % (k, json.dumps(d)) for k, d in evs).encode()


def text_reply(t, model=SERVED):
    return [("message_start", {"message": {"model": model, "usage": {"input_tokens": 11, "cache_read_input_tokens": 5}}}),
            ("content_block_start", {"content_block": {"type": "text"}}),
            ("content_block_delta", {"delta": {"type": "text_delta", "text": t}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}})]


def tool_reply(*calls):
    evs = [("message_start", {"message": {"model": SERVED, "usage": {"input_tokens": 20}}})]
    for cid, name, args in calls:
        evs += [("content_block_start", {"content_block": {"type": "tool_use", "id": cid, "name": name}}),
                ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}})]
    return evs + [("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}})]


class Api(Base):
    """A fake /v1/messages that plays SCRIPT (a list of SSE event lists, or (status, json) errors) in order and records the bodies."""

    def setUp(self):
        self.fake("#!/bin/sh\n")
        kick, hubusage.kick = hubusage.kick, lambda: None                    # no `aihub usage-sync` from tests
        self.addCleanup(setattr, hubusage, "kick", kick)
        self.seen = seen = {"bodies": [], "script": []}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["bodies"].append(body)
                step = seen["script"].pop(0) if seen["script"] else text_reply("done")
                if isinstance(step, tuple):
                    self.send_response(step[0])
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(step[1]).encode())
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(sse(step))
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env = {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % self.srv.server_address[1]}
        self.dir = tempfile.mkdtemp()

    def chat(self, engine="harness"):
        s = store.create_session("t", "", "sys", cwd=self.dir)
        return store.update_session(s["id"], engine=engine)

    def go(self, sess, text, engine="harness", mode="auto"):
        store.append(sess["id"], {"k": "user", "text": text, "files": []})
        chat.start(sess, text, "", "", mode, extra_env=self.env, engine=engine, files=[])
        return list(chat.follow(sess["id"]))

    def usage_lines(self):
        try:
            with open(hubusage.log_path(), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []


class Loop(Api):
    def test_reads_edits_and_answers_in_the_working_folder(self):
        with open(os.path.join(self.dir, "a.py"), "w") as f:
            f.write("x = 1\nprint(x)\n")
        self.seen["script"] = [tool_reply(("t1", "read", {"path": "a.py"})),
                               tool_reply(("t2", "edit", {"path": "a.py", "old": "x = 1", "new": "x = 2"})),
                               text_reply("Changed x to 2.")]
        evs = self.go(self.chat(), "set x to 2")
        self.assertEqual(open(os.path.join(self.dir, "a.py")).read(), "x = 2\nprint(x)\n")
        kinds = [e["k"] for e in evs]
        self.assertEqual((kinds.count("result"), kinds.count("done"), kinds.count("error")), (1, 1, 0))
        res = [e for e in evs if e["k"] == "tool_result"]
        self.assertIn("1\tx = 1", res[0]["text"])
        self.assertIn("edited a.py", res[1]["text"])
        self.assertIn("x = 2", res[1]["text"])                                   # the changed lines come back: no re-read
        self.assertIn("a.py", [f["path"] for e in evs if e["k"] == "files" for f in e["items"]])
        b = self.seen["bodies"]
        self.assertEqual({t["name"] for t in b[0]["tools"]} >= {"read", "edit", "write", "run", "grep", "ls", "digest", "memory"}, True)
        self.assertIn(self.dir, b[0]["system"][0]["text"])
        self.assertLess(len(json.dumps(b[0]["system"])) + len(json.dumps(b[0]["tools"])), 16000)   # a light prompt (~4k tokens at most)
        last = b[-1]["messages"][-1]["content"][-1]
        self.assertEqual(last.get("cache_control"), {"type": "ephemeral"})     # moving cache breakpoint
        self.assertEqual(sum(1 for m in b[-1]["messages"] for c in m["content"] if isinstance(c, dict) and "cache_control" in c), 1)

    def test_usage_line_has_the_served_model_not_the_alias(self):
        s = self.chat()
        store.update_session(s["id"], model="sonnet")                           # the picker value
        self.seen["script"] = [text_reply("hi")]
        self.go(store.get_session(s["id"]), "hello")
        line = self.usage_lines()[-1]
        self.assertEqual(line["model"], SERVED)
        self.assertEqual((line["input"], line["output"], line["cache_read"]), (11, 4, 5))

    def test_free_chat_usage_has_the_served_model(self):
        s = self.chat("free")
        store.save_prefs({"auto_engine": False})
        self.seen["script"] = [text_reply("hi", model="claude-haiku-5-5-x")]
        self.go(s, "hello", engine="free")
        self.assertEqual(self.usage_lines()[-1]["model"], "claude-haiku-5-5-x")

    def test_free_chat_counts_a_gateway_unknown_model_as_the_asked_model(self):
        s = self.chat("free")
        store.save_prefs({"auto_engine": False, "free_model": "claude-sonnet-5-5-y"})
        self.seen["script"] = [text_reply("hi", model="unknown")]               # some gateways name no model
        self.go(s, "hello", engine="free")
        self.assertEqual(self.usage_lines()[-1]["model"], "claude-sonnet-5-5-y")

    def test_free_chat_counts_tokens_of_a_failed_answer(self):
        s = self.chat("free")
        store.save_prefs({"auto_engine": False})
        bad = text_reply("") + [("error", {"error": {"message": "overloaded"}})]
        self.seen["script"] = [bad]
        evs = self.go(s, "hello", engine="free")
        self.assertTrue(any(e.get("k") == "error" for e in evs))
        self.assertEqual(self.usage_lines()[-1]["input"], 11)                   # spent even though the answer failed

    def test_note_never_logs_unknown_when_a_model_can_be_resolved(self):
        store.save_prefs({"free_model": "claude-opus-5-5-z"})
        for m in ("", "unknown", "opus"):
            hubusage.note("s1", "free", m, {"input": 1, "output": 1})
            self.assertNotEqual(self.usage_lines()[-1]["model"], "unknown", m)
        self.assertEqual(self.usage_lines()[-3]["model"], "claude-opus-5-5-z")
        hubusage.note("s1", "harness", "claude-x-1", {"input": 1})
        self.assertEqual(self.usage_lines()[-1]["model"], "claude-x-1")

    def test_failed_command_comes_back_as_an_error_and_the_model_retries(self):
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "exit 3"})), tool_reply(("r2", "run", {"cmd": "echo fixed"})), text_reply("ok")]
        evs = self.go(self.chat(), "run it", mode="bypass")
        res = [e for e in evs if e["k"] == "tool_result"]
        self.assertTrue(res[0]["error"])
        self.assertTrue(res[0]["text"].startswith("exit 3"))
        self.assertFalse(res[1]["error"])
        self.assertIn("fixed", res[1]["text"])
        tr = self.seen["bodies"][1]["messages"][-1]["content"][0]
        self.assertEqual((tr["type"], tr["is_error"]), ("tool_result", True))

    def test_busy_api_is_retried(self):
        harness.WAITS = (0.05, 0.05, 0.05)
        self.seen["script"] = [(529, {"error": {"type": "overloaded_error", "message": "Overloaded"}}), text_reply("after retry")]
        evs = self.go(self.chat(), "hi")
        self.assertIn("after retry", json.dumps(evs))
        self.assertNotIn("error", [e["k"] for e in evs])
        self.assertTrue(any(e["k"] == "note" and "busy" in e["text"] for e in evs))

    def test_read_only_mode_offers_no_changing_tools(self):
        self.seen["script"] = [tool_reply(("w1", "write", {"path": "x.txt", "content": "no"})), text_reply("ok")]
        evs = self.go(self.chat(), "write x", mode="plan")
        names = {t["name"] for t in self.seen["bodies"][0]["tools"]}
        self.assertFalse(names & {"write", "edit", "run"})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.txt")))
        self.assertTrue(next(e for e in evs if e["k"] == "tool_result")["error"])

    def test_later_turns_get_steps_not_old_output(self):
        with open(os.path.join(self.dir, "big.txt"), "w") as f:
            f.write("SECRETLINE\n" * 50)
        s = self.chat()
        self.seen["script"] = [tool_reply(("t1", "read", {"path": "big.txt"})), text_reply("Read it.")]
        self.go(s, "read big")
        self.seen["script"] = [text_reply("second")]
        self.go(store.get_session(s["id"]), "next")
        msgs = json.dumps(self.seen["bodies"][-1]["messages"])
        self.assertIn("[steps: read big.txt]", msgs)
        self.assertNotIn("SECRETLINE", msgs)


class Policy(unittest.TestCase):
    def tools(self, mode="auto", allowed=()):
        return HT.Tools(tempfile.mkdtemp(), "s1", mode, {"cancel": threading.Event()}, allowed=allowed)

    def v(self, cmd, **kw):
        return self.tools(**kw).check("run", {"cmd": cmd})

    def test_read_only_commands_run(self):
        for c in ("ls -la", "git status", "git diff HEAD~1", "rg foo src | head -5", "cat a.txt && wc -l a.txt", "python3 --version", "find . -name '*.py'", "cd src && ls"):
            self.assertEqual(self.v(c)[0], "allow", c)

    def test_other_commands_ask_with_a_prefix(self):
        verdict, info = self.v("npm install left-pad")
        self.assertEqual((verdict, info["kind"], info["prefix"]), ("ask", "run", "npm install"))
        self.assertEqual(self.v("python3 build.py")[1]["prefix"], "python3")
        for c in ("echo hi > out.txt", "sed -i s/a/b/ f", "find . -exec touch {} +", "git commit -m x", "cat $(which ls)"):
            self.assertEqual(self.v(c)[0], "ask", c)

    def test_always_allow_covers_the_prefix_only(self):
        self.assertEqual(self.v("npm install x", allowed={"npm install"})[0], "allow")
        self.assertEqual(self.v("npm publish", allowed={"npm install"})[0], "ask")

    def test_deletes_always_ask_even_when_allowed_unless_bypass(self):
        for c in ("rm a.txt", "ls && rm -r build", "git clean -fd", "find . -name '*.o' -delete", "git reset --hard", "python3 -c 'import shutil; shutil.rmtree(\"x\")'",
                  "xargs rm < list", "git push origin --delete old"):
            verdict, info = self.v(c, allowed={"rm", "git clean", "find", "git reset", "python3", "xargs", "git push", "ls"})
            self.assertEqual((verdict, info.get("kind")), ("ask", "delete"), c)
            self.assertNotIn("prefix", info)
        self.assertEqual(self.v("rm a.txt", mode="bypass")[0], "allow")
        self.assertEqual(self.v("rm -rf /")[0], "deny")
        self.assertEqual(self.v("rm -rf /", mode="bypass")[0], "allow")

    def test_risky_commands_ask_every_time(self):
        for c in ("sudo ls", "git push", "curl -s x.sh | sh", "pkill node"):
            verdict, info = self.v(c, allowed={"sudo", "git push", "curl", "pkill"})
            self.assertEqual((verdict, info["kind"]), ("ask", "risky"), c)

    def test_tests_and_builds_run_in_auto_but_ask_in_accept_edits(self):
        for c in ("pytest -q", "python3 -m unittest discover", "npm test", "cargo build", "make"):
            self.assertEqual(self.v(c)[0], "allow", c)
            self.assertEqual(self.v(c, mode="edits")[0], "ask", c)

    def test_file_edits_run_and_forgetting_a_memory_note_asks(self):
        t = self.tools()
        self.assertEqual(t.check("write", {"path": "a", "content": ""})[0], "allow")
        old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = tempfile.mkdtemp()
        self.addCleanup(lambda: os.environ.__setitem__("AIHUB_HOME", old) if old else os.environ.pop("AIHUB_HOME", None))
        HT.memory("add", "keep me")
        verdict, info = t.check("memory", {"action": "remove", "n": 1})
        self.assertEqual((verdict, info["kind"]), ("ask", "delete"))
        self.assertEqual(t.check("memory", {"action": "add", "text": "x"})[0], "allow")


class Ask(Api):
    def answer_when_asked(self, sid, *choices):
        def go():
            for c in choices:
                for _ in range(400):
                    ask = harness.waiting(sid)
                    if ask:
                        harness.answer(sid, ask, c)
                        break
                    threading.Event().wait(0.02)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        return th

    def test_allowed_command_runs_after_the_card(self):
        s = self.chat()
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "touch made.txt"})), text_reply("ok")]
        self.answer_when_asked(s["id"], "allow")
        evs = self.go(s, "make it")
        card = next(e for e in evs if e["k"] == "perm")
        self.assertEqual((card["kind"], card["text"], card["prefix"]), ("run", "touch made.txt", "touch"))
        self.assertIn({"k": "perm_answer", "id": card["id"], "answer": "allow"}, evs)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "made.txt")))
        self.assertIn("perm", [e["k"] for e in store.history(s["id"])])                 # the card is in the history (a reload shows it)

    def test_denied_delete_does_not_run(self):
        with open(os.path.join(self.dir, "keep.txt"), "w") as f:
            f.write("x")
        s = self.chat()
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "rm keep.txt"})), text_reply("left it")]
        self.answer_when_asked(s["id"], "deny")
        evs = self.go(s, "delete it")
        card = next(e for e in evs if e["k"] == "perm")
        self.assertEqual((card["kind"], card["prefix"]), ("delete", ""))                # no "Always allow" for a delete
        self.assertTrue(os.path.exists(os.path.join(self.dir, "keep.txt")))
        res = next(e for e in evs if e["k"] == "tool_result")
        self.assertTrue(res["error"])
        self.assertIn("denied", res["text"])

    def test_always_allow_is_remembered_for_the_chat(self):
        s = self.chat()
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "touch a.txt"})), tool_reply(("r2", "run", {"cmd": "touch b.txt"})), text_reply("ok")]
        self.answer_when_asked(s["id"], "always")
        evs = self.go(s, "make two")
        self.assertEqual([e["k"] for e in evs].count("perm"), 1)                        # the second touch did not ask
        self.assertTrue(os.path.exists(os.path.join(self.dir, "b.txt")))
        self.assertEqual(store.get_session(s["id"])["perm_allow"], ["touch"])
        self.seen["script"] = [tool_reply(("r3", "run", {"cmd": "touch c.txt"})), text_reply("ok")]
        evs = self.go(store.get_session(s["id"]), "one more")
        self.assertNotIn("perm", [e["k"] for e in evs])

    def test_stop_while_asking_ends_the_turn(self):
        s = self.chat()
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "touch x.txt"}))]
        store.append(s["id"], {"k": "user", "text": "go", "files": []})
        chat.start(s, "go", "", "", "auto", extra_env=self.env, engine="harness", files=[])
        for _ in range(400):
            if harness.waiting(s["id"]):
                break
            threading.Event().wait(0.02)
        self.assertTrue(chat.stop(s["id"]))
        evs = list(chat.follow(s["id"]))
        self.assertEqual([e["k"] for e in evs].count("done"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.txt")))
        self.assertEqual(harness.waiting(s["id"]), "")

    def test_unattended_work_refuses_instead_of_asking(self):
        s = store.update_session(self.chat()["id"], work=True)
        self.seen["script"] = [tool_reply(("r1", "run", {"cmd": "touch x.txt"})), text_reply("could not")]
        evs = self.go(s, "go")
        self.assertNotIn("perm", [e["k"] for e in evs])
        self.assertIn("nobody can approve", next(e for e in evs if e["k"] == "tool_result")["text"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.txt")))


class Handoffs(Api):
    def test_free_chat_hands_to_aihubness_which_answers(self):
        s = self.chat("free")
        self.seen["script"] = [tool_reply(("h1", "aihub_handoff", {"reason": "needs files"})), text_reply("done by aihubness")]
        evs = self.go(s, "rename the files", engine="free")
        self.assertIn({"k": "engine", "engine": "harness"}, evs)
        self.assertEqual(next(e for e in evs if e["k"] == "note")["text"], "Switched to AIHubness: needs files")
        self.assertIn("done by aihubness", json.dumps(evs))
        self.assertEqual([e["k"] for e in evs].count("done"), 1)
        self.assertIn("read", {t["name"] for t in self.seen["bodies"][1]["tools"]})
        self.assertNotIn("aihub_handoff", {t["name"] for t in self.seen["bodies"][1]["tools"]})    # AIHubness -> Claude Code is off by default
        self.assertEqual(store.get_session(s["id"])["engine"], "harness")
        self.assertEqual(json.dumps(self.seen["bodies"][1]["messages"]).count("rename the files"), 1)

    def test_claude_code_chat_without_claude_installed_is_answered_by_aihubness(self):
        from aihubui import aihub as A
        os.environ["PATH"] = tempfile.mkdtemp()                               # restored by fake()'s cleanup
        cands, A._claude_candidates = A._claude_candidates, lambda: iter(())
        self.addCleanup(setattr, A, "_claude_candidates", cands)
        A._claude.update(t=0.0, path=None)
        self.addCleanup(A._claude.update, t=0.0, path=None)
        self.assertIsNone(A.claude_bin())
        s = self.chat("claude")
        self.seen["script"] = [text_reply("answered without claude")]
        evs = self.go(s, "hello", engine="claude")
        self.assertIn({"k": "engine", "engine": "harness"}, evs)
        self.assertIn("not installed", next(e for e in evs if e["k"] == "note")["text"])
        self.assertIn("answered without claude", json.dumps(evs))
        self.assertNotIn("error", [e["k"] for e in evs])

    def test_claude_found_outside_path(self):
        from aihubui import aihub as A
        d = tempfile.mkdtemp()
        exe = os.path.join(d, "claude")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        os.environ["PATH"] = tempfile.mkdtemp()
        cands, A._claude_candidates = A._claude_candidates, lambda: iter([os.path.join(d, "nope"), exe])
        self.addCleanup(setattr, A, "_claude_candidates", cands)
        self.addCleanup(A._claude.update, t=0.0, path=None)
        self.assertEqual(A.claude_bin(fresh=True), exe)
        self.assertEqual(A.claude_cmd(), exe)

    def test_handoff_not_offered_is_not_obeyed(self):
        s = self.chat()
        self.seen["script"] = [tool_reply(("h1", "aihub_handoff", {"reason": "lazy"})), text_reply("did it myself")]
        evs = self.go(s, "do it")
        self.assertNotIn("engine", [e["k"] for e in evs])
        self.assertTrue(next(e for e in evs if e["k"] == "tool_result")["error"])
        self.assertEqual(store.get_session(s["id"])["engine"], "harness")

    def test_aihubness_hands_to_claude_code_only_when_on(self):
        from tests.test_ui_handoff import FAKE_CLAUDE
        env = self.fake(FAKE_CLAUDE)
        store.save_prefs({"auto_engine_claude": True})
        s = self.chat()
        self.seen["script"] = [tool_reply(("l1", "ls", {})), tool_reply(("h1", "aihub_handoff", {"reason": "needs the web"}))]
        evs = self.go(s, "look it up online")
        self.assertIn({"k": "engine", "engine": "claude"}, evs)
        self.assertIn("edited by claude", json.dumps(evs))
        prompt = open(env["FAKE_OUT"]).read().partition("\n")[2]
        self.assertIn("AIHubness worked on this message first", prompt)
        self.assertEqual(store.get_session(s["id"])["engine"], "claude")


class Tools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = HT.Tools(self.dir, "s1", "auto", {"cancel": threading.Event()})
        self.t.out_dir = tempfile.mkdtemp()

    def w(self, name, text):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def test_big_file_is_cut_with_an_outline_and_paged(self):
        self.w("m.py", "".join("def f%d():\n    return %d\n" % (i, i) for i in range(500)))
        out, bad = self.t.call("read", {"path": "m.py"})
        self.assertFalse(bad)
        self.assertIn("1000 lines", out)
        self.assertIn("Outline:", out)
        self.assertIn("L999: def f499():", out)
        self.assertNotIn("return 499", out)
        page, _ = self.t.call("read", {"path": "m.py", "offset": 999, "limit": 5})
        self.assertIn("return 499", page)

    def test_unchanged_reread_is_not_sent_twice(self):
        self.w("a.txt", "hello\n")
        self.t.call("read", {"path": "a.txt"})
        again, _ = self.t.call("read", {"path": "a.txt"})
        self.assertIn("unchanged", again)
        self.t.call("write", {"path": "a.txt", "content": "new\n"})
        fresh, _ = self.t.call("read", {"path": "a.txt"})
        self.assertIn("new", fresh)

    def test_paths_stay_in_the_working_folder(self):
        out, bad = self.t.call("read", {"path": "../../etc/passwd"})
        self.assertTrue(bad)
        self.assertIn("outside the working folder", out)

    def test_edit_needs_a_unique_match(self):
        self.w("b.txt", "a\na\n")
        out, bad = self.t.call("edit", {"path": "b.txt", "old": "a", "new": "b"})
        self.assertTrue(bad)
        self.assertIn("2 times", out)
        out, bad = self.t.call("edit", {"path": "b.txt", "old": "a", "new": "b", "all": True})
        self.assertFalse(bad)
        self.assertEqual(open(os.path.join(self.dir, "b.txt")).read(), "b\nb\n")

    def test_long_output_keeps_head_and_tail_and_saves_the_rest(self):
        out, bad = self.t.call("run", {"cmd": "python3 -c \"[print('line', i) for i in range(20000)]\""})
        self.assertFalse(bad)
        self.assertIn("line 0", out)
        self.assertIn("line 19999", out)
        self.assertNotIn("line 9000\n", out)
        saved = out.split("full text: ")[1].split(".")[0] + ".txt"
        self.assertTrue(os.path.isfile(saved))
        hit, _ = self.t.call("grep", {"pattern": "line 9000$", "path": saved})
        self.assertIn("line 9000", hit)

    def test_repeated_lines_fold(self):
        self.assertIn("same line 99 more times", HT.fold("x\n" * 100))

    def test_catastrophic_commands_are_refused_unless_bypass(self):
        out, bad = self.t.call("run", {"cmd": "rm -rf /"})
        self.assertTrue(bad)
        self.assertIn("refused", out)

    def test_glob_and_grep(self):
        self.w("src/a.py", "import os\n")
        self.w("src/sub/b.py", "import sys\n")
        self.w("node_modules/x.py", "import os\n")
        out, _ = self.t.call("ls", {"glob": "**/*.py"})
        self.assertEqual(sorted(out.splitlines()), ["src/a.py", "src/sub/b.py"])
        out, _ = self.t.call("grep", {"pattern": "import os"})
        self.assertEqual(out.strip(), "src/a.py:1:import os")

    def test_memory_add_list_remove(self):
        old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = tempfile.mkdtemp()
        self.addCleanup(lambda: os.environ.__setitem__("AIHUB_HOME", old) if old else os.environ.pop("AIHUB_HOME", None))
        self.assertEqual(self.t.call("memory", {"action": "add", "text": "user likes tabs"})[1], False)
        self.assertIn("1. user likes tabs", self.t.call("memory", {"action": "list"})[0])
        self.t.call("memory", {"action": "remove", "n": 1})
        self.assertEqual(HT.memory_items(), [])

    def test_compact_stubs_old_results_and_keeps_the_last_rounds(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
        for i in range(8):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": "read", "input": {"path": "x"}}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": "y" * 30000}]})
        saved = harness.compact(msgs, 1, self.t)
        self.assertGreater(saved, 100000)
        self.assertIn("old result cut", msgs[2]["content"][0]["content"])
        self.assertEqual(msgs[-1]["content"][0]["content"], "y" * 30000)


if __name__ == "__main__":
    unittest.main()
