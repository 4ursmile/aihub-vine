"""aihub-ui: one question = one card (duplicate Claude events), and free chat handing a message over to Claude Code."""
import http.server
import json
import os
import sys
import threading
import unittest

from tests.test_ui_hint import Base, UI  # noqa: F401  (puts the UI folder on sys.path)
from aihubui import chat, freechat, store  # noqa: E402

# Claude Code can send one message several times: a block per event, then the whole content again (same message id, same tool_use id).
FAKE_ASK = r'''#!/usr/bin/env python3
import json, sys
def out(o): print(json.dumps(o), flush=True)
first = sys.stdin.readline()
out({"type": "user", "isReplay": True, "message": {"role": "user", "content": json.loads(first)["message"]["content"]}})
q = {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": [{"question": "Which?", "options": [{"label": "a"}, {"label": "b"}]}]}}
t = {"type": "text", "text": "I need to know:"}
out({"type": "assistant", "message": {"id": "m1", "content": [t], "usage": {"input_tokens": 1}}})
out({"type": "assistant", "message": {"id": "m1", "content": [q], "usage": {"input_tokens": 1}}})
out({"type": "assistant", "message": {"id": "m1", "content": [t, q], "usage": {"input_tokens": 1}}})
out({"type": "assistant", "message": {"id": "m2", "content": [q], "usage": {"input_tokens": 1}}})
out({"type": "result", "is_error": False, "usage": {"output_tokens": 3}, "duration_ms": 5})
for ln in sys.stdin:
    pass
'''

# Records its argv and the first prompt, answers once.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
def out(o): print(json.dumps(o), flush=True)
first = sys.stdin.readline()
open(os.environ["FAKE_OUT"], "w").write(" ".join(sys.argv[1:]) + "\n" + json.loads(first)["message"]["content"])
out({"type": "user", "isReplay": True, "message": {"role": "user", "content": json.loads(first)["message"]["content"]}})
out({"type": "assistant", "message": {"id": "c1", "content": [{"type": "text", "text": "edited by claude"}], "usage": {"input_tokens": 2}}})
out({"type": "result", "is_error": False, "usage": {"output_tokens": 3}, "duration_ms": 5})
for ln in sys.stdin:
    pass
'''


class OneQuestion(Base):
    def test_duplicate_events_become_one_question(self):
        self.fake(FAKE_ASK)
        s = store.create_session("t", "", "sys")
        chat.start(s, "plan it", "", "", "auto")
        evs = list(chat.follow(s["id"]))
        blocks = [b for e in evs if e["k"] == "msg" for b in e["blocks"]]
        self.assertEqual([b["id"] for b in blocks if b["type"] == "tool"], ["q1"])         # the same tool_use id is never a second row
        self.assertEqual(len([b for b in blocks if b["type"] == "text"]), 1)
        saved = [b for e in store.history(s["id"]) if e["k"] == "msg" for b in e["blocks"] if b["type"] == "tool"]
        self.assertEqual(len(saved), 1)

    def test_fresh_keeps_what_has_no_id(self):
        seen = set()
        a = chat._fresh([{"type": "text", "text": "x"}], None, seen)
        b = chat._fresh([{"type": "text", "text": "x"}], None, seen)
        self.assertEqual((len(a), len(b)), (1, 1))                                         # no message id: nothing to compare, nothing dropped


class Handoff(Base):
    def setUp(self):
        self.seen = seen = {"bodies": []}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["bodies"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                if seen.get("plain"):
                    evs = (("content_block_start", {"content_block": {"type": "text"}}),
                           ("content_block_delta", {"delta": {"type": "text_delta", "text": "free answer"}}),
                           ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}))
                else:
                    evs = (("message_start", {"message": {"usage": {"input_tokens": 7}}}),
                           ("content_block_start", {"content_block": {"type": "tool_use", "id": "h1", "name": "aihub_handoff"}}),
                           ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{\"reason\": \"it edits files\"}"}}),
                           ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}))
                for k, d in evs:
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (k, json.dumps(d))).encode())
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.env = {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % self.srv.server_address[1]}

    def go(self, sess, text="rename the files in the folder"):
        store.append(sess["id"], {"k": "user", "text": "earlier question"})
        store.append(sess["id"], {"k": "msg", "blocks": [{"type": "text", "text": "earlier answer"}]})
        store.append(sess["id"], {"k": "user", "text": text, "files": []})
        chat.start(sess, text, "", "", "auto", extra_env=self.env, engine="free", files=[])
        return list(chat.follow(sess["id"]))

    def test_free_chat_hands_the_same_message_to_claude_code(self):
        env = self.fake(FAKE_CLAUDE)
        s = store.update_session(store.create_session("t", "", "sys")["id"], engine="free")
        evs = self.go(s)
        self.assertIn("aihub_handoff", [t["name"] for t in self.seen["bodies"][0]["tools"]])
        self.assertIn("aihub_handoff", json.dumps(self.seen["bodies"][0]["system"]))
        kinds = [e["k"] for e in evs]
        self.assertEqual(kinds.count("done"), 1)                                           # one run, one end
        note = next(e for e in evs if e["k"] == "note")
        self.assertEqual(note["text"], "Switched to Claude Code: it edits files")
        self.assertIn({"k": "engine", "engine": "claude"}, evs)
        self.assertIn("edited by claude", json.dumps(evs))
        self.assertEqual(kinds.count("result"), 1)                                         # the free part sends no result row of its own
        argv, _, prompt = open(env["FAKE_OUT"]).read().partition("\n")
        self.assertIn("rename the files in the folder", prompt)                            # the same message
        self.assertIn("earlier answer", prompt)                                            # the conversation travelled as text
        self.assertEqual(prompt.count("rename the files in the folder"), 1)               # the message is not in the seed as well
        self.assertIn("--session-id", argv)                                                # a NEW Claude session
        now = store.get_session(s["id"])
        self.assertEqual((now["engine"], now["started"]), ("claude", True))
        self.assertNotIn("aihub_handoff", json.dumps(store.history(s["id"])))              # no tool row for the switch
        self.assertEqual(len([e for e in store.history(s["id"]) if e["k"] == "note"]), 1)
        self.assertEqual(now["usage"]["turns"], 1 + 1)                                     # the free part and Claude's own turn, each once

    def test_off_pref_and_works_chats_get_no_handoff_tool(self):
        self.fake(FAKE_CLAUDE)
        self.seen["plain"] = True
        store.save_prefs({"auto_engine": False})
        s = store.update_session(store.create_session("t", "", "sys")["id"], engine="free")
        evs = self.go(s)
        self.assertNotIn("tools", self.seen["bodies"][-1])
        self.assertNotIn("aihub_handoff", json.dumps(self.seen["bodies"][-1]["system"]))
        self.assertNotIn("note", [e["k"] for e in evs])
        store.save_prefs({"auto_engine": True})
        w = store.update_session(store.create_session("t", "", "sys")["id"], engine="free", work=True)
        self.go(w)
        self.assertNotIn("tools", self.seen["bodies"][-1])
        w2 = store.update_session(store.create_session("t", "", "sys")["id"], engine="free")
        self.go(w2)
        self.assertIn("tools", self.seen["bodies"][-1])


if __name__ == "__main__":
    unittest.main()
