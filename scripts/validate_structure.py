#!/usr/bin/env python3
"""Validate optim-plans repository structure without external dependencies."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(path: str) -> Path:
    target = ROOT / path
    if not target.exists():
        raise AssertionError(f"missing {path}")
    return target


def load_json(path: str) -> dict:
    with require(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain an object")
    return payload


def main() -> int:
    manifest = load_json(".codex-plugin/plugin.json")
    if manifest.get("name") != "optim-plans":
        raise AssertionError("Codex manifest name mismatch")
    if "hooks" in manifest:
        raise AssertionError("Codex manifest must not use unsupported hooks field")

    expected_version = "0.2.1"
    claude_manifest = load_json(".claude-plugin/plugin.json")
    claude_marketplace = load_json(".claude-plugin/marketplace.json")
    versions = {
        ".codex-plugin/plugin.json": manifest.get("version"),
        ".claude-plugin/plugin.json": claude_manifest.get("version"),
    }
    entries = [entry for entry in claude_marketplace.get("plugins", []) if entry.get("name") == "optim-plans"]
    if len(entries) != 1:
        raise AssertionError("Claude marketplace optim-plans entry mismatch")
    versions[".claude-plugin/marketplace.json"] = entries[0].get("version")
    for path, version in versions.items():
        if version != expected_version:
            raise AssertionError(f"{path} version must be {expected_version}")
    changelog = require("CHANGELOG.md").read_text(encoding="utf-8")
    if "## 0.2.1 - 2026-07-31" not in changelog:
        raise AssertionError("CHANGELOG.md missing 0.2.1 entry")
    if "## 0.2.0 - 2026-07-28" not in changelog:
        raise AssertionError("CHANGELOG.md missing 0.2.0 entry")
    if "## 0.1.2 - 2026-07-27" not in changelog:
        raise AssertionError("CHANGELOG.md missing 0.1.2 entry")

    for path in (
        ".claude-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        "skills/optim-plans/SKILL.md",
        "skills/analyze-and-plan/SKILL.md",
        "skills/analyze-and-plan/agents/openai.yaml",
        "skills/review-and-plan/SKILL.md",
        "skills/review-and-plan/agents/openai.yaml",
        "skills/search-and-plan/SKILL.md",
        "skills/mini-plan/SKILL.md",
        "skills/small-plan/SKILL.md",
        "skills/plan/SKILL.md",
        "skills/big-plan/SKILL.md",
        "skills/huge-plan/SKILL.md",
        "skills/resume-previous-plan/SKILL.md",
        "hooks/codex-hooks.json",
        "hooks/hooks.json",
        "scripts/optim_plans.py",
        "scripts/optim_plans_core.py",
        "scripts/agent_adapters.py",
        "LICENSE",
        "README.md",
        "CONTRIBUTING.md",
        "THIRD-PARTY-NOTICES.md",
    ):
        require(path)
    for path in ("skills/optim-plans/SKILL.md", "skills/analyze-and-plan/SKILL.md"):
        skill = require(path).read_text(encoding="utf-8")
        if "[TODO:" in skill:
            raise AssertionError(f"{path} contains TODO placeholder")
    print("optim-plans structure OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"validate_structure: {exc}", file=sys.stderr)
        raise SystemExit(1)
