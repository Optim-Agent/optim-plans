from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StructureTests(unittest.TestCase):
    def test_plugin_manifests_and_marketplaces_are_parseable(self) -> None:
        required = [
            ".codex-plugin/plugin.json",
            ".claude-plugin/plugin.json",
            ".agents/plugins/marketplace.json",
            ".claude-plugin/marketplace.json",
            "LICENSE",
            "README.md",
            "CONTRIBUTING.md",
            "THIRD-PARTY-NOTICES.md",
        ]
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", relative],
                cwd=ROOT,
                check=False,
            )
            self.assertNotEqual(ignored.returncode, 0, f"{relative} must not be ignored")

        codex_manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(codex_manifest["name"], "optim-plans")
        self.assertEqual(codex_manifest["skills"], "./skills/")
        self.assertNotIn("hooks", codex_manifest)
        for field in ("displayName", "shortDescription", "longDescription", "developerName"):
            self.assertTrue(codex_manifest["interface"][field])

        claude_manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(claude_manifest["name"], "optim-plans")
        self.assertTrue((ROOT / "hooks/hooks.json").is_file())

        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        entries = [entry for entry in marketplace["plugins"] if entry["name"] == "optim-plans"]
        self.assertEqual(len(entries), 1)
        source_path = (ROOT / entries[0]["source"]["path"]).resolve()
        self.assertEqual(source_path, ROOT)

    def test_referenced_plugin_paths_exist(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        for key in ("skills",):
            self.assertTrue((ROOT / manifest[key]).exists(), key)

    def test_public_github_urls_point_to_live_repository(self) -> None:
        expected = "https://github.com/Optim-Agent/optim-plans"
        for relative in ("README.md", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(expected, text, relative)

    def test_documented_scope_matches_execution_lifecycle(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        implemented = readme.split("Implemented:", 1)[1].split("Not implemented yet:", 1)[0]
        not_implemented = readme.split("Not implemented yet:", 1)[1]
        for expected in (
            "immutable manifest-bound execution approval",
            "one controller-owned run worktree and run branch",
            "serial item execution with checkpoint commits",
            "adapter-only argv launch with `shell=False`",
            "controller verification and Git audits",
            "explicit retry approval",
            "`awaiting_integration`",
            "evidence-backed `finish-run` outcomes",
        ):
            self.assertIn(expected, implemented)
            self.assertNotIn(expected, not_implemented)
        self.assertNotIn("isolated execution worktree creation", not_implemented)
        self.assertNotIn("merge/cherry-pick/keep integration gate", not_implemented)

    def test_release_e2e_lifecycle_transcript_exists(self) -> None:
        e2e = (ROOT / "tests/test_e2e.py").read_text(encoding="utf-8")
        self.assertIn("test_cli_lifecycle_rejects_invalid_states_before_mutation_and_finishes_kept", e2e)
        for expected in (
            "prepare-execution",
            "start-execution",
            "run-item",
            "awaiting_integration",
            "finish-run",
            "init",
        ):
            self.assertIn(expected, e2e)


if __name__ == "__main__":
    unittest.main()
