import json
import os
import tempfile
import unittest
from unittest import mock

from aihub.cli import hooks, paths, policy

BLOCK = ["psql -c 'DROP TABLE users'", "mysql -e 'truncate table orders'", "psql -c 'DELETE FROM users;'", "psql -c \"update users set a=1\"",
         "aws s3 rm s3://b/x --recursive", "aws s3 sync . s3://b --delete", "rm -rf /", "rm -rf ~", "dropdb prod", "redis-cli flushall",
         "echo x > /etc/passwd", "aihub policy off",
         "spark-sql -e 'DELETE FROM db.events'", "spark-sql -e 'INSERT OVERWRITE TABLE t SELECT * FROM s'", "spark-sql -e 'DROP TABLE t'",
         "spark-sql -e 'ALTER TABLE t DROP PARTITION (dt=1)'", "hdfs dfs -rm -r /data/events", "aws s3 mv s3://a/x s3://b/x", "s3cmd del s3://b/x",
         "python -c \"df.write.mode('overwrite').parquet('s3://b/events')\"", "rclone purge remote:bucket",
         "python -c \"boto3.resource('s3').Bucket('b').objects.all().delete()\""]
ALLOW = ["ls -la", "psql -c 'select 1'", "psql -c 'DELETE FROM users WHERE id=1'", "psql -c 'update t set a=1 where id=2'", "rm -rf build",
         "aws s3 ls", "git status", "echo hello > out.txt",
         "spark-sql -e 'DELETE FROM db.events WHERE dt = 1'", "spark-sql -e \"INSERT OVERWRITE TABLE t PARTITION (dt='1') SELECT 1\"",
         "python -c \"df.write.mode('overwrite').parquet('s3://b/events/dt=2026-01-01')\"",
         "python -c \"df.write.mode('overwrite').option('replaceWhere','dt=1').save('x')\"", "python -c \"df.write.mode('append').parquet('x')\"",
         "spark-sql -e 'select * from t'"]


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"AIHUB_HOME": self.d})
        p.start()
        self.addCleanup(p.stop)

    def test_blocks_and_allows(self):
        for c in BLOCK:
            self.assertIsNotNone(policy.decide("Bash", {"command": c}), c)
        for c in ALLOW:
            self.assertIsNone(policy.decide("Bash", {"command": c}), c)

    def test_spark_code_in_written_files_asks(self):
        bad = "df.write.mode('overwrite').parquet('s3://b/events')"
        self.assertEqual(policy.decide("Write", {"file_path": "/tmp/job.py", "content": bad})["action"], "ask")
        self.assertEqual(policy.decide("Edit", {"file_path": "/tmp/job.py", "new_string": "spark.sql('DROP VIEW v')"})["action"], "ask")
        self.assertIsNone(policy.decide("Write", {"file_path": "/tmp/job.py", "content": "df.write.mode('overwrite').parquet('s3://b/e/dt=1')"}))
        self.assertIsNone(policy.decide("Write", {"file_path": "/tmp/job.py", "content": "df.write.partitionBy('dt').mode('append').save('x')"}))

    def test_menu_toggles_guard_and_rules(self):
        answers = iter(["toggle", "back"])
        with mock.patch("aihub.cli.ui.select", side_effect=lambda *a, **k: next(answers)):
            policy.menu()
        self.assertFalse(policy.user()["enabled"])
        policy.cli(mock.Mock(action="on", target=None))
        answers = iter(["rules", "back"])
        keep = lambda t, o, multi=False, preset=(): [x for x, _ in o if x != "sql-drop"] if multi else next(answers)
        with mock.patch("aihub.cli.ui.select", side_effect=keep):
            policy.menu()
        self.assertEqual(policy.user()["disabled"], ["sql-drop"])

    def test_s3_overwrite_asks_and_file_tools(self):
        self.assertEqual(policy.decide("Bash", {"command": "aws s3 cp a s3://b/a"})["action"], "ask")
        self.assertEqual(policy.decide("Write", {"file_path": os.path.expanduser("~/.ssh/authorized_keys")})["action"], "deny")
        self.assertIsNone(policy.decide("Write", {"file_path": "/tmp/x.txt"}))
        self.assertEqual(policy.decide("mcp__db__delete_rows", {})["action"], "ask")

    def test_switches_and_user_rules(self):
        ns = lambda **k: mock.Mock(action=k.pop("action"), target=k.pop("target", None), pattern=None, reason=None, mode="deny", tool="Bash",
                                   field="command", force=False, **k)
        policy.cli(ns(action="off"))
        self.assertIsNone(policy.decide("Bash", {"command": "dropdb x"}))
        policy.cli(ns(action="on"))
        policy.cli(ns(action="disable", target="s3-delete"))
        self.assertIsNone(policy.decide("Bash", {"command": "aws s3 rm s3://b/x"}))
        policy.cli(ns(action="enable", target="s3-delete"))
        self.assertIsNotNone(policy.decide("Bash", {"command": "aws s3 rm s3://b/x"}))
        a = ns(action="add", target="no-kube")
        a.pattern = "kubectl .* prod"
        policy.cli(a)
        self.assertEqual(policy.decide("Bash", {"command": "kubectl delete pod x -n prod"})["id"], "no-kube")
        policy.cli(ns(action="remove", target="no-kube"))
        self.assertIsNone(policy.decide("Bash", {"command": "kubectl delete pod x -n prod"}))

    def test_hub_package_overrides_and_retires(self):
        d = paths.ensure("packages", "aihub-policy")
        os.makedirs(os.path.join(d, "rules"))
        json.dump({"revision": "9", "retire": ["rm-root"]}, open(os.path.join(d, "policy.json"), "w"))
        json.dump({"rules": [{"id": "hub-x", "pattern": "forbidden-cmd", "reason": "hub"}]}, open(os.path.join(d, "rules", "x.json"), "w"))
        self.assertEqual(policy.decide("Bash", {"command": "forbidden-cmd"})["id"], "hub-x")
        self.assertIsNone(policy.decide("Bash", {"command": "rm -rf /"}))
        self.assertEqual(policy.revision(), "9")

    def test_hook_prints_deny_and_logs(self):
        with mock.patch("sys.stdout") as out:
            hooks.handle(json.dumps({"tool_name": "Bash", "tool_input": {"command": "dropdb prod"}}))
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(policy.tail()[-1]["id"], "db-cli-drop")

    def test_auto_updates_when_hub_newer(self):
        with mock.patch.object(paths, "hub_configured", return_value=True), mock.patch.object(policy, "update", return_value="1.1.0") as up:
            policy.auto("search")
            policy.auto("search")                       # throttled
        self.assertEqual(up.call_count, 1)


class JudgeTest(PolicyTest):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"AIHUB_POLICY_JUDGE": "on"})
        p.start()
        self.addCleanup(p.stop)
        w = mock.patch("shutil.which", return_value="/usr/bin/claude")
        w.start()
        self.addCleanup(w.stop)

    def test_verdicts_and_cache(self):
        call = {"command": "python3 -c \"import shutil; shutil.rmtree('/var/lib/postgresql')\""}
        with mock.patch.object(policy, "_ask_model", return_value={"verdict": "deny", "reason": "wipes pg"}) as m:
            d = policy.judge("Bash", call)
            policy.judge("Bash", call)                              # cached: one model call
        self.assertEqual((d["action"], d["id"]), ("deny", "judge"))
        self.assertEqual(m.call_count, 1)
        with mock.patch.object(policy, "_ask_model", return_value={"verdict": "allow", "reason": "ok"}):
            self.assertIsNone(policy.judge("Bash", {"command": "make deploy-staging"}))

    def test_failure_becomes_ask(self):
        import subprocess
        for boom in (RuntimeError("x"), subprocess.TimeoutExpired("claude", 15)):
            with mock.patch.object(policy, "_ask_model", side_effect=boom):
                d = policy.judge("Bash", {"command": "node cleanup.js"})
            self.assertEqual((d["action"], d["id"]), ("ask", "judge-error"))

    def test_safe_and_in_project_calls_skip_the_model(self):
        with mock.patch.object(policy, "_ask_model", side_effect=AssertionError("no model call expected")):
            self.assertIsNone(policy.judge("Bash", {"command": "git status"}))
            self.assertIsNone(policy.judge("Bash", {"command": "ls -la src"}))
            self.assertIsNone(policy.judge("Write", {"file_path": os.getcwd() + "/a.txt"}, os.getcwd()))
            self.assertIsNone(policy.judge("Read", {"file_path": "/etc/hosts"}))

    def test_off_switch_and_missing_claude(self):
        policy.cli(mock.Mock(action="judge", target="off"))
        with mock.patch.dict(os.environ, {"AIHUB_POLICY_JUDGE": ""}), mock.patch.object(policy, "_ask_model", side_effect=AssertionError):
            self.assertIsNone(policy.judge("Bash", {"command": "node x.js"}))
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(policy.judge("Bash", {"command": "node x.js"}))

    def test_hook_order_and_recursion_guard(self):
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "node wipe.js"}})
        with mock.patch.object(policy, "_ask_model", return_value={"verdict": "ask", "reason": "unclear"}), mock.patch("sys.stdout") as out:
            hooks.handle(ev)
        self.assertEqual(json.loads("".join(c.args[0] for c in out.write.call_args_list))["hookSpecificOutput"]["permissionDecision"], "ask")
        rule_hit = json.dumps({"tool_name": "Bash", "tool_input": {"command": "dropdb x"}})
        with mock.patch.dict(os.environ, {"AIHUB_JUDGE": "1"}), mock.patch("sys.stdout") as out:
            hooks.handle(rule_hit)
        out.write.assert_not_called()                              # nested judge session: guard stays silent
        with mock.patch.object(policy, "_ask_model", side_effect=AssertionError), mock.patch("sys.stdout") as out:
            hooks.handle(rule_hit)
        self.assertIn("deny", "".join(c.args[0] for c in out.write.call_args_list))   # a rule hit never reaches the model


if __name__ == "__main__":
    unittest.main()
