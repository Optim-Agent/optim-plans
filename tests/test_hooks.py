from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/optim_plans_hook.py"


class HookTests(unittest.TestCase):
    def run_hook(self, event: dict, env: dict[str, str] | None = None) -> dict:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            check=True,
        )
        return json.loads(result.stdout)

    def owned_env(self, tmp: Path, *, scopes: str = "src") -> dict[str, str]:
        state = tmp / "state.json"
        state.write_text(json.dumps({"run_id": "r1", "worker_nonce": "n1"}), encoding="utf-8")
        return {
            "OPTIM_PLANS_RUN_ID": "r1",
            "OPTIM_PLANS_WORKER_NONCE": "n1",
            "OPTIM_PLANS_STATE_PATH": str(state),
            "OPTIM_PLANS_SCOPES": scopes,
        }

    def test_unowned_session_noops(self) -> None:
        self.assertEqual(self.run_hook({"event": "SessionStart"})["action"], "noop")

    def test_session_start_injects_context_for_owned_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            env["OPTIM_PLANS_IDS"] = "TASK-001"
            out = self.run_hook({"event": "SessionStart"}, env)
            self.assertEqual(out["action"], "inject")
            self.assertIn("TASK-001", out["context"])

    def test_pre_tool_use_denies_git_and_out_of_scope_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            denied_git = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "git -C repo reset --hard"}, env)
            self.assertEqual(denied_git["action"], "deny")
            denied_write = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "other/file.py"}, env)
            self.assertEqual(denied_write["action"], "deny")
            missing_path = self.run_hook({"event": "PreToolUse", "tool": "Write"}, env)
            self.assertEqual(missing_path["action"], "deny")
            no_scopes = self.owned_env(Path(raw), scopes="")
            fail_closed = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "src/file.py"}, no_scopes)
            self.assertEqual(fail_closed["action"], "deny")
            allowed = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "src/file.py"}, env)
            self.assertEqual(allowed["action"], "allow")

    def test_codex_pre_tool_use_emits_decision_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = {**self.owned_env(Path(raw)), "PLUGIN_DATA": raw}
            allowed = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "pwd"}, env)
            self.assertEqual(allowed, {"decision": "allow"})
            blocked = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "git reset --hard"}, env)
            self.assertEqual(blocked["decision"], "block")
            self.assertIn("reason", blocked)

    def test_codex_unowned_pre_tool_use_allows_with_valid_schema(self) -> None:
        out = self.run_hook(
            {"event": "PreToolUse", "tool": "Shell", "command": "pwd"},
            {"OPTIM_PLANS_PLUGIN_ROOT": str(ROOT)},
        )
        self.assertEqual(out, {"decision": "allow"})

    def test_codex_session_start_uses_additional_context_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = {**self.owned_env(Path(raw)), "PLUGIN_DATA": raw, "OPTIM_PLANS_IDS": "TASK-001"}
            out = self.run_hook({"event": "SessionStart"}, env)
            specific = out["hookSpecificOutput"]
            self.assertEqual(specific["hookEventName"], "SessionStart")
            self.assertIn("TASK-001", specific["additionalContext"])

    def test_owned_session_with_invalid_state_fails_closed(self) -> None:
        out = self.run_hook(
            {"event": "SessionStart"},
            {"OPTIM_PLANS_RUN_ID": "r1", "OPTIM_PLANS_WORKER_NONCE": "n1", "OPTIM_PLANS_STATE_PATH": "/missing"},
        )
        self.assertEqual(out["action"], "deny")

    def test_partial_owned_environment_fails_closed(self) -> None:
        out = self.run_hook({"event": "SessionStart"}, {"OPTIM_PLANS_RUN_ID": "r1"})
        self.assertEqual(out["action"], "deny")

    def test_hook_configs_do_not_register_stop(self) -> None:
        for relative in ("hooks/codex-hooks.json", "hooks/hooks.json"):
            data = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertNotIn("Stop", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
