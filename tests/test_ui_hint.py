"""aihub-ui: messages sent into a running Claude Code answer (chat.hint) and /btw side questions (chat.btw)."""
import json
import os
import sys
import tempfile
import time
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aihub", "builtin_packages", "aihub-ui", "ui")
sys.path.insert(0, UI)
from aihubui import chat, store  # noqa: E402

# Like real `claude -p --input-format stream-json --replay-user-messages`: echoes each stdin message it reads, reads a hint at its
# "next step" (after the tool call), answers once, then waits for stdin to close. FAKE_MODE=late: no hint read, answers at once.
FAKE = r'''#!/usr/bin/env python3
import json, os, queue, sys, threading
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(ln) for ln in sys.stdin] + [lines.put(None)], daemon=True).start()
def out(o): print(json.dumps(o), flush=True)
def echo(ln): out({"type": "user", "isReplay": True, "message": {"role": "user", "content": json.loads(ln)["message"]["content"]}})
open(os.environ["FAKE_OUT"], "w").write(" ".join(sys.argv[1:]))
first = lines.get()
echo(first)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 1"}}], "usage": {"input_tokens": 1}}})
heard = ""
if os.environ.get("FAKE_MODE") != "late":
    try:
        ln = lines.get(timeout=5)
    except queue.Empty:
        ln = None
    if ln:
        echo(ln)
        heard = json.loads(ln)["message"]["content"]
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "done " + heard}], "usage": {"input_tokens": 2}}})
out({"type": "result", "is_error": False, "usage": {"output_tokens": 3}, "duration_ms": 5})
for ln in iter(lines.get, None):                                         # a message after the answer = the next answer, until stdin closes
    echo(ln)
    out({"type": "result", "is_error": False, "usage": {}, "duration_ms": 1})
'''

FAKE_BTW = r'''#!/usr/bin/env python3
import json, os, sys
q = sys.stdin.read()
open(os.environ["FAKE_OUT"], "w").write(" ".join(sys.argv[1:]) + "\n" + q)
print(json.dumps({"type": "result", "is_error": False, "result": "side answer", "usage": {"input_tokens": 4, "output_tokens": 2}, "total_cost_usd": 0.001, "modelUsage": {"m1": {}}}))
'''


class Base(unittest.TestCase):
    def fake(self, script, **env):
        tmp = tempfile.mkdtemp()
        binp = os.path.join(tmp, "bin")
        os.makedirs(binp)
        with open(os.path.join(binp, "claude"), "w") as f:
            f.write(script)
        os.chmod(os.path.join(binp, "claude"), 0o755)
        env = dict({"AIHUB_HOME": tmp, "PATH": binp + os.pathsep + os.environ["PATH"], "FAKE_OUT": os.path.join(tmp, "seen.txt")}, **env)
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        chat.auth.ready = lambda: {"ok": True}
        return env

    def run_turn(self, sess, text, hint=None):
        run = chat.start(sess, text, "", "", "auto")
        sent = None
        if hint:
            for _ in range(100):                                         # wait for claude to be running
                if chat.feeds.get(sess["id"]):
                    break
                time.sleep(0.05)
            sent = chat.hint(sess["id"], hint)
        evs = list(chat.follow(sess["id"]))
        return run, evs, sent


class Hint(Base):
    def test_a_hint_joins_the_running_answer_and_claude_ends(self):
        env = self.fake(FAKE)
        s = store.create_session("t", "", "sys")
        run, evs, sent = self.run_turn(s, "first task", hint="also say BANANA")
        self.assertTrue(sent)
        self.assertIn("--input-format stream-json", open(env["FAKE_OUT"]).read())
        users = [e for e in evs if e["k"] == "user"]
        self.assertEqual(users, [{"k": "user", "text": "also say BANANA", "files": [], "hint": True}])      # the first prompt's echo is not repeated
        self.assertIn("done also say BANANA", json.dumps(evs))
        self.assertEqual([e["k"] for e in evs].count("result"), 1)                               # one answer, not a second turn
        self.assertNotIn("hint_lost", [e["k"] for e in evs])
        self.assertEqual(evs[-1], {"k": "done"})
        hist = [e for e in store.history(s["id"]) if e["k"] == "user"]
        self.assertEqual(hist[-1]["text"], "also say BANANA")
        self.assertFalse(chat.hint(s["id"], "too late"))                                         # finished: the page sends it as a new message

    def test_a_hint_after_the_last_step_gets_its_own_answer(self):
        self.fake(FAKE, FAKE_MODE="late")
        s = store.create_session("t", "", "sys")
        orig = chat.Feed.answered
        wrote = []

        def answered(self):                                               # the hint lands just before the first answer's result is handled
            if not wrote:
                wrote.append(self.write("one more thing", hint=True))
            orig(self)
        chat.Feed.answered = answered
        self.addCleanup(setattr, chat.Feed, "answered", orig)
        chat.start(s, "first task", "", "", "auto")
        evs = list(chat.follow(s["id"]))
        self.assertEqual(wrote, [True])
        self.assertEqual([e["k"] for e in evs].count("result"), 2)
        self.assertIn({"k": "user", "text": "one more thing", "files": [], "hint": True}, evs)
        self.assertNotIn("hint_lost", [e["k"] for e in evs])

    def test_no_running_answer_refuses(self):
        self.assertFalse(chat.hint("deadbeef", "x"))


class Btw(Base):
    def test_btw_forks_the_session_and_keeps_history_clean(self):
        env = self.fake(FAKE_BTW)
        s = store.create_session("t", "", "sys")
        s = store.update_session(s["id"], started=True)
        before = store.history(s["id"])
        r = chat.btw(s, "what file did we change?")
        self.assertEqual(r["answer"], "side answer")
        self.assertTrue(r["ok"])
        argv, _, q = open(env["FAKE_OUT"]).read().partition("\n")
        for flag in ("--fork-session", "--no-session-persistence", "--max-turns 1", "--resume " + s["claude"]):
            self.assertIn(flag, argv)
        self.assertIn("what file did we change?", q)
        self.assertEqual(store.history(s["id"]), before)                                        # nothing added to the chat


if __name__ == "__main__":
    unittest.main()
