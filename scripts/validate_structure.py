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
    for path in (
        ".claude-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        "skills/optim-plans/SKILL.md",
        "skills/mini-plan/SKILL.md",
        "skills/small-plan/SKILL.md",
        "skills/plan/SKILL.md",
        "skills/big-plan/SKILL.md",
        "skills/huge-plan/SKILL.md",
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
    skill = require("skills/optim-plans/SKILL.md").read_text(encoding="utf-8")
    if "[TODO:" in skill:
        raise AssertionError("skill contains TODO placeholder")
    print("optim-plans structure OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"validate_structure: {exc}", file=sys.stderr)
        raise SystemExit(1)
