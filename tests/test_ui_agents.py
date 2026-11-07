"""aihub-ui: agents in a chat (Claude Code sub-agents tagged by parent, AIHubness helper agents), messages sent into a running AIHubness
answer, the compacting status, and the lazy guide files that keep the frozen system prompt small."""
import json
import os
import tempfile
import unittest

from tests.test_ui_hint import Base, UI  # noqa: F401  (puts the UI folder on sys.path)
from tests.test_ui_harness import Api, text_reply, tool_reply
from aihubui import chat, harness, harnesstools as HT, store  # noqa: E402

# Claude Code with one sub-agent (Task): its streamed text, steps and usage carry parent_tool_use_id, then the main answer.
FAKE_SUB = r'''#!/usr/bin/env python3
import json, sys
def out(o): print(json.dumps(o), flush=True)
ln = sys.stdin.readline()
out({"type": "user", "isReplay": True, "message": {"role": "user", "content": json.loads(ln)["message"]["content"]}})
out({"type": "system", "subtype": "status", "status": "compacting"})
out({"type": "system", "subtype": "status", "status": None})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m1", "content": [{"type": "tool_use", "id": "T1", "name": "Task", "input": {"description": "find x", "prompt": "find x", "subagent_type": "Explore"}}], "usage": {"input_tokens": 100}}})
out({"type": "stream_event", "parent_tool_use_id": "T1", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "SUBTEXT"}}})
out({"type": "assistant", "parent_tool_use_id": "T1", "message": {"id": "s1", "content": [{"type": "tool_use", "id": "G1", "name": "Grep", "input": {"pattern": "x"}}], "usage": {"input_tokens": 9999}}})
out({"type": "user", "parent_tool_use_id": "T1", "message": {"content": [{"type": "tool_result", "tool_use_id": "G1", "content": "a.py:1"}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "T1", "content": "x is in a.py"}]}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "MAIN"}}})
out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "MAIN"}], "usage": {"input_tokens": 120}}})
out({"type": "result", "is_error": False, "usage": {"output_tokens": 3}, "duration_ms": 5})
'''


class ClaudeSubAgents(Base):
    def test_sub_agent_rows_are_tagged_and_kept_out_of_the_answer(self):
        self.fake(FAKE_SUB)
        s = store.create_session("t", "", "sys")
        store.append(s["id"], {"k": "user", "text": "go", "files": []})
        _, evs, _ = self.run_turn(s, "go")
        deltas = "".join(e["text"] for e in evs if e["k"] == "delta")
        self.assertEqual(deltas, "MAIN")                                         # the sub-agent's stream is not the answer
        self.assertEqual([e["used"] for e in evs if e["k"] == "ctx"], [100, 120])  # nor its context size
        self.assertEqual([e["status"] for e in evs if e["k"] == "status"], ["compacting", ""])
        rows = store.history(s["id"])
        sub = [r for r in rows if r.get("parent") == "T1"]
        self.assertEqual([r["k"] for r in sub], ["msg", "tool_result"])
        self.assertNotIn("Grep", store.transcript(rows))                         # a sub-agent's steps are not the conversation
        main_result = [r for r in rows if r.get("k") == "tool_result" and r.get("id") == "T1"][0]
        self.assertNotIn("parent", main_result)


class HelperAgents(Api):
    def test_a_helper_agent_researches_in_its_own_context(self):
        with open(os.path.join(self.dir, "a.py"), "w") as f:
            f.write("x = 1\n")
        self.seen["script"] = [tool_reply(("A1", "agent", {"name": "find x", "task": "where is x set?"})),
                               tool_reply(("G1", "grep", {"pattern": "x ="})),           # the helper
                               text_reply("x is set in a.py:1"),                          # its report
                               text_reply("x is set in a.py.")]                           # the main answer
        s = self.chat()
        evs = self.go(s, "where is x?")
        b = self.seen["bodies"]
        self.assertIn("agent", {t["name"] for t in b[0]["tools"]})
        self.assertEqual({t["name"] for t in b[1]["tools"]}, {"read", "ls", "grep", "digest"})   # look only
        self.assertIn("helper agent", b[1]["system"][0]["text"])
        self.assertEqual(b[1]["messages"][0]["content"][0]["text"], "where is x set?")     # its own fresh context
        main = json.dumps(b[3]["messages"])
        self.assertIn("x is set in a.py:1", main)                                # the report reaches the main context...
        self.assertNotIn("G1", main)                                             # ...its steps do not
        sub = [e for e in evs if e.get("parent") == "A1"]
        self.assertEqual([e["k"] for e in sub], ["msg", "tool_result", "msg"])
        res = [e for e in evs if e["k"] == "tool_result" and e["id"] == "A1"][0]
        self.assertFalse(res["error"])
        later = json.dumps(harness.history(store.history(s["id"])))
        self.assertNotIn("x is set in a.py:1\"", later.replace("a.py.", ""))    # next turns: no helper rows
        self.assertNotIn("grep", later)

    def test_helper_without_a_task_is_an_error_not_a_crash(self):
        self.seen["script"] = [tool_reply(("A1", "agent", {"name": "x"})), text_reply("ok")]
        evs = self.go(self.chat(), "go")
        res = [e for e in evs if e["k"] == "tool_result" and e["id"] == "A1"][0]
        self.assertTrue(res["error"])
        self.assertEqual([e["k"] for e in evs].count("result"), 1)


class HarnessHints(Api):
    def test_a_message_sent_while_it_works_reaches_the_next_step(self):
        s = self.chat()
        call = HT.Tools.call

        def spy(tools, name, args):
            self.assertTrue(harness.hint(s["id"], "use b.txt instead"))
            return call(tools, name, args)
        HT.Tools.call = spy
        self.addCleanup(setattr, HT.Tools, "call", call)
        self.seen["script"] = [tool_reply(("t1", "ls", {})), text_reply("ok")]
        evs = self.go(s, "list")
        hints = [e for e in evs if e["k"] == "user" and e.get("hint")]
        self.assertEqual([e["text"] for e in hints], ["use b.txt instead"])
        last = self.seen["bodies"][1]["messages"][-1]["content"]
        self.assertEqual(last[0]["type"], "tool_result")                         # tool results first, then the message
        self.assertTrue(last[1]["text"].endswith("use b.txt instead"))
        self.assertFalse(harness.hint(s["id"], "late"))                         # no running answer: the page queues it

    def test_a_message_during_the_final_answer_is_answered_in_the_same_turn(self):
        s = self.chat()
        rnd = harness._round
        n = {"i": 0}

        def wrapped(*a, **kw):
            for x in rnd(*a, **kw):
                if x[0] == "done" and n["i"] == 0:
                    harness.hint(s["id"], "and also y")
                n["i"] += 1 if x[0] == "done" else 0
                yield x
        harness._round = wrapped
        self.addCleanup(setattr, harness, "_round", rnd)
        self.seen["script"] = [text_reply("x done."), text_reply("y done.")]
        evs = self.go(s, "do x")
        self.assertEqual(len(self.seen["bodies"]), 2)
        self.assertIn("and also y", json.dumps(self.seen["bodies"][1]["messages"][-1]))
        self.assertEqual([e["k"] for e in evs].count("result"), 1)
        self.assertEqual(["x done.", "y done."], [b["text"] for e in evs if e["k"] == "msg" for b in e["blocks"]])


class LazyGuides(unittest.TestCase):
    def setUp(self):
        old = os.environ.get("AIHUB_HOME")
        os.environ["AIHUB_HOME"] = tempfile.mkdtemp()
        self.addCleanup(lambda: os.environ.__setitem__("AIHUB_HOME", old) if old else os.environ.pop("AIHUB_HOME", None))

    def test_long_page_rules_go_to_a_guide_file(self):
        extra = "# OpenUI Lang (canvas components)\n" + "rule\n" * 8000
        s = chat.build_system(extra)
        self.assertLess(len(s), 6000)                                            # the OpenUI rules are ~30 KB: only a pointer stays
        path = [ln.split(": ")[-1] for ln in s.splitlines() if "OpenUI" in ln][0]
        self.assertEqual(open(path, encoding="utf-8").read(), extra)
        self.assertEqual(chat.build_system(extra), s)                            # same inputs, same bytes (prompt cache)
        for g in ("diagrams.md", "documents.md"):
            self.assertIn(os.path.join(chat.guides_dir(), g), s)
            self.assertTrue(os.path.isfile(os.path.join(chat.guides_dir(), g)))

    def test_short_rules_stay_inline(self):
        self.assertIn("nobody watches", chat.build_system(chat.freechat.WORK_RULES))


if __name__ == "__main__":
    unittest.main()
