from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginSmokeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
