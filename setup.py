"""Wheel build: copy the aihub-policy rules into aihub/core/policy_rules (the CLI package cannot include builtin_packages)."""
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = os.path.dirname(os.path.abspath(__file__))


class Build(build_py):
    def run(self):
        super().run()
        dst = os.path.join(self.build_lib, "aihub", "core", "policy_rules")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(os.path.join(HERE, "aihub", "builtin_packages", "aihub-policy", "rules"), dst,
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(HERE, "aihub", "builtin_packages", "aihub-prices", "prices.json"),
                    os.path.join(self.build_lib, "aihub", "core", "prices.json"))


setup(cmdclass={"build_py": Build})
