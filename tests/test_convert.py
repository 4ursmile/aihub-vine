"""`aihub dev init --convert / --convert-mono`: read an existing project, fill aihub.toml, rename install scripts, ask only when custom."""
import argparse
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from aihub.cli import main, ui
from aihub.core import convert, manifest


def tree(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)


SKILL = "---\nname: demo\ndescription: Use when demoing.\n---\n# x\n"


class Analyze(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_standard_skill_with_scripts_needs_no_questions(self):
        tree(self.d, {"skills/demo/SKILL.md": SKILL, "scripts/setup_mac.sh": "git status\n", "scripts/Setup.ps1": "Write-Host 1\n",
                      "requirements.txt": "requests>=2\n", "README.md": "# T\n\nDoes demo things.\n"})
        p = convert.analyze(self.d)
        self.assertEqual((p["type"], p["questions"]), ("skill", []))
        self.assertEqual(p["description"], "Does demo things.")
        self.assertEqual(p["scripts"], {"install_macos": "scripts/install-macos.sh", "install_windows": "scripts/install-windows.ps1"})
        self.assertEqual(sorted(p["renames"]), [("scripts/Setup.ps1", "scripts/install-windows.ps1"), ("scripts/setup_mac.sh", "scripts/install-macos.sh")])
        self.assertEqual([c["name"] for c in p["requires"]["commands"]], ["git"])
        self.assertEqual(p["python"]["requires"], ["requests>=2"])
        m = manifest.parse(convert.build(p))                       # the generated text is a valid manifest
        self.assertEqual(m["skills"][0]["path"], "skills/demo")
        self.assertEqual(m["requires"]["os"], [])                  # a script for some OSes never restricts the install

    def test_mcp_json_makes_an_mcp_package(self):
        tree(self.d, {".mcp.json": '{"mcpServers": {"My Srv": {"command": "node", "args": ["s.js"], "env": {"K": "v"}}}}', "s.js": "1"})
        p = convert.analyze(self.d)
        self.assertEqual(p["type"], "mcp")
        m = manifest.parse(convert.build(p))
        self.assertEqual((m["mcp_servers"][0]["name"], m["mcp_servers"][0]["command"], m["mcp_servers"][0]["args"]), ("my-srv", "node", ["s.js"]))

    def test_unmatched_files_become_one_question_not_a_guess(self):
        tree(self.d, {"notes.txt": "x", "src/main.c": "int main(){}"})
        p = convert.analyze(self.d)
        self.assertIsNone(p["type"])
        self.assertEqual([q["id"] for q in p["questions"]], ["type"])

    def test_already_named_scripts_are_not_renamed(self):
        tree(self.d, {"scripts/install-macos.sh": "x", "scripts/install-windows.ps1": "x", "bin/t.py": "x"})
        p = convert.analyze(self.d)
        self.assertEqual(p["renames"], [])
        self.assertEqual(p["type"], "tool")

    def test_nested_packages_are_excluded_from_the_parent_scan(self):
        tree(self.d, {"skills/a/SKILL.md": SKILL, "child/skills/b/SKILL.md": SKILL})
        self.assertEqual([s["path"] for s in convert.analyze(self.d, exclude=["child"])["skills"]], ["skills/a"])


class DevInit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        e = mock.patch.dict(os.environ, {"AIHUB_HOME": tempfile.mkdtemp()})
        e.start()
        self.addCleanup(e.stop)

    def args(self, **k):
        base = dict(path=self.d, name=None, type=None, description=None, sub=False, mono=False, packages=None, remote=None, branch=None,
                    force=False, convert=False, convert_mono=False, yes=True)
        return argparse.Namespace(**dict(base, **k))

    def git(self):
        subprocess.run(["git", "init", "-q", self.d], check=True)
        subprocess.run(["git", "-C", self.d, "remote", "add", "origin", "https://example.com/x/r.git"], check=True)

    def test_convert_one_folder_renames_and_writes_a_valid_manifest(self):
        self.git()
        tree(self.d, {"skills/demo/SKILL.md": SKILL, "scripts/setup.sh": "git x\n"})
        with mock.patch.object(ui, "INTERACTIVE", False):
            main.dev_init(self.args(convert=True))
        self.assertTrue(os.path.exists(os.path.join(self.d, "scripts", "install.sh")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "scripts", "setup.sh")))
        m = manifest.parse(open(os.path.join(self.d, "aihub.toml")).read())
        self.assertEqual((m["package"]["type"], m["scripts"]["install_linux"], m["git"]["url"]), ("skill", "scripts/install.sh", "https://example.com/x/r.git"))

    def test_custom_structure_without_a_terminal_names_the_question(self):
        tree(self.d, {"notes.txt": "x"})
        with mock.patch.object(ui, "INTERACTIVE", False):
            with self.assertRaises(ValueError) as cm:
                main.dev_init(self.args(convert=True))
        self.assertIn("What kind of package", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "aihub.toml")))

    def test_custom_structure_asks_once_in_a_terminal(self):
        tree(self.d, {"notes.txt": "x"})
        with mock.patch.object(ui, "INTERACTIVE", True), mock.patch.object(ui, "select", return_value="agent"), \
                mock.patch.object(ui, "confirm", return_value=True), mock.patch("builtins.input", return_value=""):
            main.dev_init(self.args(convert=True, yes=False, name="custom-one"))
        self.assertEqual(manifest.parse(open(os.path.join(self.d, "aihub.toml")).read())["package"]["type"], "agent")

    def test_an_existing_manifest_is_kept(self):
        tree(self.d, {"aihub.toml": "KEEP", "skills/demo/SKILL.md": SKILL})
        with mock.patch.object(ui, "INTERACTIVE", False):
            main.dev_init(self.args(convert=True))
        self.assertEqual(open(os.path.join(self.d, "aihub.toml")).read(), "KEEP")

    def test_mono_checklist_converts_only_the_ticked_folders(self):
        self.git()
        tree(self.d, {"one/skills/one/SKILL.md": SKILL, "two/skills/two/SKILL.md": SKILL, "docs/readme.txt": "x", "kept/aihub.toml": "x"})
        seen = {}
        def pick(title, opts, multi=False, preset=()):
            seen["opts"] = [v for v, _ in opts]
            return ["two"]
        with mock.patch.object(ui, "INTERACTIVE", True), mock.patch.object(ui, "select", side_effect=pick):
            main.dev_init(self.args(convert_mono=True, yes=False))
        self.assertEqual(sorted(seen["opts"]), ["one", "two"])                    # docs/ is not offered; kept/ is already a package
        self.assertTrue(os.path.exists(os.path.join(self.d, "two", "aihub.toml")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "one", "aihub.toml")))

    def test_interactive_init_offers_the_three_modes(self):
        tree(self.d, {"a/skills/a/SKILL.md": SKILL})
        seen = {}
        def pick(title, opts, multi=False, preset=()):
            seen.setdefault("modes", [v for v, _ in opts])               # the first question is the mode, the second the checklist
            return "mono" if not multi else ["a"]
        self.git()
        with mock.patch.object(ui, "INTERACTIVE", True), mock.patch.object(ui, "select", side_effect=pick):
            main.dev_init(self.args(yes=False))
        self.assertEqual(seen["modes"], ["new", "one", "mono"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "a", "aihub.toml")))


if __name__ == "__main__":
    unittest.main()
