"""`aihub --json CMD` / `CMD --json` / AIHUB_JSON=1: one JSON document per command, never a secret, human output unchanged."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from aihub.cli import api, hooks, main, ui


class Json(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"AIHUB_HOME": self.home, "AIHUB_NO_UPDATE": "1", "AIHUB_RUN_REPORT": "off"})
        p.start()
        self.addCleanup(p.stop)
        saved = (ui.JSON, ui.TTY, ui.COLOR, ui.FANCY, ui.INTERACTIVE)
        self.addCleanup(lambda: setattr(ui, "JSON", saved[0]) or setattr(ui, "TTY", saved[1]) or setattr(ui, "COLOR", saved[2])
                        or setattr(ui, "FANCY", saved[3]) or setattr(ui, "INTERACTIVE", saved[4]))
        with open(os.path.join(self.home, "credentials.json"), "w") as f:
            json.dump({"token": "tok-SECRET-123456", "username": "sam"}, f)
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"hub": "http://hub.example", "client_id": "c1", "ci_api_key": "k-SECRET"}, f)
        os.makedirs(os.path.join(self.home, "packages", "demo"))
        with open(os.path.join(self.home, "state.json"), "w") as f:
            json.dump({"packages": {"demo": {"path": os.path.join(self.home, "packages", "demo"), "version": "1.2.0", "components": ["skill:demo"]}}}, f)

    def run_cli(self, *argv, stdin=""):
        """-> (exit code, stdout text). Child-process output of the command goes to stderr in JSON mode, so stdout is captured at fd level."""
        out = tempfile.TemporaryFile(mode="w+")
        keep = os.dup(1)
        sys.stdout.flush()
        os.dup2(out.fileno(), 1)
        try:
            with mock.patch.object(main.updater, "auto", lambda *a: None), mock.patch.object(hooks, "verify", lambda *a, **k: None), \
                    mock.patch.object(main, "record_cli_install", lambda: None), mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    mock.patch.object(sys, "stdout", os.fdopen(os.dup(1), "w")):
                code = main.main(list(argv))
                sys.stdout.flush()
        finally:
            os.dup2(keep, 1)
            os.close(keep)
        out.seek(0)
        return code, out.read()

    def doc(self, *argv, **kw):
        code, text = self.run_cli(*argv, **kw)
        self.assertNotIn("SECRET", text)
        d = json.loads(text)                                  # exactly one document, nothing else on stdout
        self.assertEqual(code, d["exit"])
        self.assertEqual(set(d), {"schema", "ok", "command", "exit", "error", "hint", "data", "output", "messages", "changes", "context"})
        return d

    def test_list_three_ways(self):
        for argv in (("list", "--json"), ("--json", "list")):
            d = self.doc(*argv)
            self.assertEqual((d["ok"], d["command"]), (True, "list"))
            self.assertEqual([(p["name"], p["version"], p["enabled"]) for p in d["data"]], [("demo", "1.2.0", True)])
            self.assertEqual(d["context"]["username"], "sam")
            self.assertTrue(d["context"]["signed_in"])
        with mock.patch.dict(os.environ, {"AIHUB_JSON": "1"}):
            self.assertEqual(self.doc("list")["data"][0]["name"], "demo")

    def test_human_and_plain_output_unchanged(self):
        self.assertEqual(self.run_cli("list", "--plain"), (0, "demo 1.2.0\n"))
        code, text = self.run_cli("list")
        self.assertIn("demo", text)
        self.assertRaises(ValueError, json.loads, text)

    def test_config_and_context_never_show_secrets(self):
        d = self.doc("--json", "config")
        self.assertEqual((d["data"]["ci_api_key"], d["data"]["hub"]), ("set", "http://hub.example"))
        d = self.doc("config", "set", "my_token", "t-SECRET", "--json")
        self.assertEqual(d["data"]["my_token"], "set")
        self.assertNotIn("t-SECRET", json.dumps(d["messages"]))
        self.assertNotIn("token", json.dumps(d["context"]).replace("signed_in", ""))

    def test_token_is_scrubbed_even_when_a_command_prints_it(self):
        def leaky(a):
            print("debug tok-SECRET-123456")
        with mock.patch.object(main, "cmd_logout", leaky):
            parser = main.build_parser
            with mock.patch.object(main, "build_parser", lambda: _with(parser(), "logout", leaky)):
                code, text = self.run_cli("--json", "logout")
        self.assertNotIn("tok-SECRET", text)
        self.assertIn("***", json.loads(text)["output"])

    def test_errors_are_documents(self):
        d = self.doc("--json", "bogus-command")
        self.assertEqual((d["ok"], d["exit"]), (False, 2))
        self.assertIn("invalid choice", d["error"])
        d = self.doc("list", "--json", "--nope")
        self.assertIn("unrecognized", d["error"])
        with mock.patch.object(main.registry, "get", side_effect=api.ApiError("cannot reach hub http://hub.example (refused)")):
            d = self.doc("--json", "info", "x")
        self.assertEqual((d["ok"], d["exit"]), (False, 1))
        self.assertIn("aihub doctor", d["hint"])

    def test_login_password_on_stdin_and_shared_credentials(self):
        seen = {}
        def call(method, path, body=None, **kw):
            seen.update(body)
            return {"token": "new-SECRET-token-999", "username": "sam"}
        with mock.patch.object(main.api, "call", call), mock.patch("aihub.cli.usagesync.kick", lambda: None):
            d = self.doc("login", "sam", "--password-stdin", "--json", stdin="pw-SECRET\n")
        self.assertEqual(seen, {"username": "sam", "password": "pw-SECRET"})
        self.assertEqual(d["data"], {"signed_in": True, "username": "sam", "hub": "http://hub.example"})
        with open(os.path.join(self.home, "credentials.json")) as f:
            self.assertEqual(json.load(f)["token"], "new-SECRET-token-999")       # the same file every CLI command (and so the UI) uses
        d = self.doc("--json", "login")                                           # JSON mode never prompts
        self.assertFalse(d["ok"])
        d = self.doc("logout", "--json")
        self.assertEqual((d["data"]["signed_out"], d["context"]["signed_in"]), ("sam", False))

    def test_destructive_commands_are_not_auto_confirmed(self):
        d = self.doc("--json", "self-uninstall")
        self.assertTrue(os.path.isdir(self.home))
        self.assertIn("nothing changed", json.dumps(d["messages"]))

    def test_changes_report_what_a_command_did(self):
        def uninstall(name, quiet=False):
            with open(os.path.join(self.home, "state.json"), "w") as f:
                json.dump({"packages": {}}, f)
        with mock.patch.object(main.installer, "uninstall", uninstall):
            d = self.doc("uninstall", "demo", "--json")
        self.assertEqual(d["changes"], {"removed": [{"name": "demo", "version": "1.2.0"}]})

    def test_vault_list_shows_hosts_not_secrets(self):
        with open(os.path.join(self.home, "vault.json"), "w") as f:
            json.dump({"git.example.com": {"scheme": "https", "username": "ann"}}, f)
        from aihub.cli import vault
        with mock.patch.object(vault, "_secret", lambda h: {"username": "ann", "password": "pw-SECRET"}):
            d = self.doc("vault", "list", "--json")
        self.assertEqual(d["data"]["remotes"], [{"host": "git.example.com", "scheme": "https", "username": "ann", "login": "stored"}])


def _with(parser, name, fn):
    for act in parser._subparsers._group_actions:
        if name in getattr(act, "choices", {}):
            act.choices[name].set_defaults(fn=fn)
    return parser


if __name__ == "__main__":
    unittest.main()
