from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


class PluginSmokeTests(unittest.TestCase):
    def _tool(self, name: str) -> str:
        path = shutil.which(name)
        if path is None:
            self.skipTest(f"{name} executable is unavailable")
        return path

    def _help(self, argv: list[str]) -> str:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"{' '.join(argv[:-1])} help is unsupported")
        return result.stdout + result.stderr

    def _run(self, argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def _strings(self, value: Any) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            out: set[str] = set()
            for key, item in value.items():
                out.add(str(key))
                out.update(self._strings(item))
            return out
        if isinstance(value, list):
            out: set[str] = set()
            for item in value:
                out.update(self._strings(item))
            return out
        return set()

    def test_validate_structure_script(self) -> None:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate_structure.py")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_ci_workflow_exists(self) -> None:
        workflow = ROOT / ".github/workflows/ci.yml"
        self.assertTrue(workflow.is_file())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover", text)
        self.assertIn("scripts/validate_structure.py", text)

    def test_claude_plugin_validate_strict_when_supported(self) -> None:
        claude = self._tool("claude")
        self._help([claude, "plugin", "validate", "--help"])
        self._run([claude, "plugin", "validate", "--strict", "."])

    def test_codex_clean_home_plugin_install_inventory_when_supported(self) -> None:
        codex = self._tool("codex")
        plugin_help = self._help([codex, "plugin", "--help"])
        for command in ("marketplace", "add", "list"):
            if command not in plugin_help:
                self.skipTest(f"codex plugin {command} is unsupported")
        self._help([codex, "plugin", "marketplace", "add", "--help"])
        self._help([codex, "plugin", "add", "--help"])
        list_help = self._help([codex, "plugin", "list", "--help"])
        if "--json" not in list_help:
            self.skipTest("codex plugin list --json is unsupported")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            codex_home = Path(raw) / "codex-home"
            home.mkdir()
            codex_home.mkdir()
            env = os.environ.copy()
            env.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
            self._run([codex, "plugin", "marketplace", "add", "."], env=env)
            self._run([codex, "plugin", "add", "optim-plans@optim-plans-dev"], env=env)
            listed = self._run([codex, "plugin", "list", "--json"], env=env)

        payload = json.loads(listed.stdout)
        strings = self._strings(payload)
        for expected in ("optim-plans-dev", "optim-plans", "0.1.2"):
            self.assertIn(expected, strings)
        if "skills" not in strings and "components" not in strings:
            self.skipTest("codex plugin list --json does not expose component inventory")
        for expected in ("skills", "big-plan"):
            self.assertIn(expected, strings)


if __name__ == "__main__":
    unittest.main()
