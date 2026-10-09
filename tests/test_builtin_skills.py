import os
import re
import unittest

from aihub.core import manifest as M
from aihub.server import builtin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
GUIDE = os.path.join(builtin.DIR, "aihub-guide")
PACKAGE = os.path.join(builtin.DIR, "aihub-package")
GUIDE_SKILL = os.path.join(GUIDE, "skills", "aihub-guide", "SKILL.md")
PACKAGE_SKILL = os.path.join(PACKAGE, "skills", "aihub-package", "SKILL.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _front_matter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


class BuiltinSkillTest(unittest.TestCase):
    def test_bundled_versions(self):
        self.assertEqual(M.parse(_read(os.path.join(GUIDE, "aihub.toml")))["package"]["version"], "2.3.0")
        self.assertEqual(M.parse(_read(os.path.join(PACKAGE, "aihub.toml")))["package"]["version"], "2.2.0")

    def test_guide_mentions_and_ships_offline_reference(self):
        text = _read(GUIDE_SKILL)
        self.assertIn("offline.md", text)
        offline = os.path.join(GUIDE, "skills", "aihub-guide", "offline.md")
        self.assertTrue(os.path.isfile(offline))
        self.assertGreater(os.path.getsize(offline), 0)
        self.assertIn("skills/aihub-guide/offline.md", builtin._files(GUIDE))

    def test_skill_front_matter(self):
        for path, name in ((GUIDE_SKILL, "aihub-guide"), (PACKAGE_SKILL, "aihub-package")):
            fm = _front_matter(_read(path))
            self.assertIsNotNone(fm, path)
            self.assertEqual(fm.get("name"), name, path)
            self.assertTrue(fm.get("description"), path)

    def test_guide_docs_slugs_exist(self):
        table = _read(GUIDE_SKILL).split("| slug | read it for |", 1)[1].split("\n\n", 1)[0]
        slugs = re.findall(r"^\| ([a-z][a-z-]*) \|", table, re.M)
        self.assertTrue(slugs)
        for slug in slugs:
            self.assertTrue(os.path.isfile(os.path.join(DOCS, slug + ".md")), slug)


if __name__ == "__main__":
    unittest.main()
