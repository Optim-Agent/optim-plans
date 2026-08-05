from __future__ import annotations

import hashlib
import json
import os
import shlex
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

    def permission_decision(self, out: dict) -> str:
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertIn("permissionDecisionReason", specific)
        return specific["permissionDecision"]

    def assert_allowed(self, out: dict) -> None:
        self.assertEqual(out, {})

    def owned_env(self, tmp: Path, *, scopes: str = "src") -> dict[str, str]:
        state = tmp / "state.json"
        state.write_text(json.dumps({"run_id": "r1", "worker_nonce": "n1"}), encoding="utf-8")
        return {
            "OPTIM_PLANS_RUN_ID": "r1",
            "OPTIM_PLANS_WORKER_NONCE": "n1",
            "OPTIM_PLANS_STATE_PATH": str(state),
            "OPTIM_PLANS_SCOPES": scopes,
        }

    def repo_with_events(self, tmp: Path, dirname: str, events: list[tuple[str, dict]]) -> Path:
        repo = tmp / dirname
        subprocess.run(["git", "init", str(repo)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        root = repo / ".git" / "optim-plans"
        run_id = "run1"
        worktree_id = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
        active_file = root / "worktrees" / worktree_id / "active.json"
        active_file.parent.mkdir(parents=True)
        active_file.write_text(json.dumps({"run_id": run_id, "artifact_dir": "docs/run1"}), encoding="utf-8")
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        lines = [
            json.dumps({"schema": 1, "seq": index, "type": event_type, "payload": payload})
            for index, (event_type, payload) in enumerate(events, start=1)
        ]
        (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return repo

    def test_unowned_session_noops(self) -> None:
        self.assertEqual(self.run_hook({"event": "SessionStart"})["action"], "noop")

    def test_session_start_injects_context_for_owned_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            env["OPTIM_PLANS_IDS"] = "TASK-001"
            env["OPTIM_PLANS_IGNORED_AUDIT_NOISE"] = '{"patterns":["runtime-output"]}'
            out = self.run_hook({"event": "SessionStart"}, env)
            self.assertEqual(out["action"], "inject")
            self.assertIn("TASK-001", out["context"])
            self.assertIn("runtime-output", out["context"])

    def test_pre_tool_use_denies_git_and_out_of_scope_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            denied_git = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "git -C repo reset --hard"}, env)
            self.assertEqual(self.permission_decision(denied_git), "deny")
            denied_write = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "other/file.py"}, env)
            self.assertEqual(self.permission_decision(denied_write), "deny")
            missing_path = self.run_hook({"event": "PreToolUse", "tool": "Write"}, env)
            self.assertEqual(self.permission_decision(missing_path), "deny")
            no_scopes = self.owned_env(Path(raw), scopes="")
            fail_closed = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "src/file.py"}, no_scopes)
            self.assertEqual(self.permission_decision(fail_closed), "deny")
            allowed = self.run_hook({"event": "PreToolUse", "tool": "Write", "path": "src/file.py"}, env)
            self.assert_allowed(allowed)

    def test_codex_pre_tool_use_emits_decision_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = {**self.owned_env(Path(raw)), "PLUGIN_DATA": raw}
            allowed = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "pwd"}, env)
            self.assert_allowed(allowed)
            blocked = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "git reset --hard"}, env)
            self.assertEqual(self.permission_decision(blocked), "deny")

    def test_codex_unowned_pre_tool_use_allows_with_valid_schema(self) -> None:
        out = self.run_hook(
            {"event": "PreToolUse", "tool": "Shell", "command": "pwd"},
            {"OPTIM_PLANS_PLUGIN_ROOT": str(ROOT)},
        )
        self.assert_allowed(out)

    def test_raw_codex_and_claude_pre_tool_use_envelopes_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            codex_raw = self.run_hook(
                {"hook_event_name": "PreToolUse", "tool_name": "Shell", "tool_input": {"command": "git reset --hard"}},
                {**env, "PLUGIN_DATA": raw},
            )
            self.assertEqual(self.permission_decision(codex_raw), "deny")
            claude_raw = self.run_hook(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git reset --hard"}},
                env,
            )
            self.assertEqual(self.permission_decision(claude_raw), "deny")

    def test_raw_write_and_edit_path_envelopes_are_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self.owned_env(Path(raw))
            write = self.run_hook(
                {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "other/file.py"}},
                env,
            )
            self.assertEqual(self.permission_decision(write), "deny")
            edit = self.run_hook(
                {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"path": "src/file.py"}},
                env,
            )
            self.assert_allowed(edit)

    def test_controller_polling_is_denied_during_active_wait(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.repo_with_events(
                Path(raw),
                "repo with spaces",
                [
                    (
                        "host_agent_registered",
                        {"item_id": "TASK-001", "attempt": 1, "assignment_nonce": "n1", "agent_handle": "agent-1"},
                    )
                ],
            )
            quoted_repo = shlex.quote(str(repo))
            relative = self.run_hook(
                {"event": "PreToolUse", "tool": "Shell", "command": f"python3 scripts/optim_plans.py status --repo {quoted_repo}"}
            )
            self.assertEqual(self.permission_decision(relative), "deny")
            absolute = self.run_hook(
                {
                    "event": "PreToolUse",
                    "tool": "Bash",
                    "command": f"python3 {shlex.quote(str(ROOT / 'scripts/optim_plans.py'))} advance-item --repo {quoted_repo} --item-id TASK-001",
                }
            )
            self.assertEqual(self.permission_decision(absolute), "deny")
            batch = self.run_hook(
                {
                    "event": "PreToolUse",
                    "tool": "Shell",
                    "command": f"python3 scripts/optim_plans.py advance-batch --repo {quoted_repo} --batch-id batch-1",
                }
            )
            self.assertEqual(self.permission_decision(batch), "deny")

    def test_controller_guard_allows_completion_no_wait_and_non_controller(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.repo_with_events(
                Path(raw),
                "repo",
                [
                    (
                        "host_agent_registered",
                        {"item_id": "TASK-001", "attempt": 1, "assignment_nonce": "n1", "agent_handle": "agent-1"},
                    )
                ],
            )
            repo_arg = shlex.quote(str(repo))
            complete = self.run_hook(
                {
                    "event": "PreToolUse",
                    "tool": "Shell",
                    "command": f"python3 scripts/optim_plans.py complete-item --repo {repo_arg} --item-id TASK-001 --assignment-nonce n1 --agent-handle agent-1 --evidence done",
                }
            )
            self.assert_allowed(complete)
            fail = self.run_hook(
                {
                    "event": "PreToolUse",
                    "tool": "Shell",
                    "command": f"python3 scripts/optim_plans.py fail-validator --repo {repo_arg} --item-id TASK-001 --validator-nonce v1 --agent-handle agent-1 --reason unknown --evidence done",
                }
            )
            self.assert_allowed(fail)
            non_controller = self.run_hook({"event": "PreToolUse", "tool": "Shell", "command": "python3 scripts/other.py status --repo /tmp/x"})
            self.assert_allowed(non_controller)

            done_repo = self.repo_with_events(
                Path(raw),
                "done repo",
                [
                    (
                        "host_agent_registered",
                        {"item_id": "TASK-001", "attempt": 1, "assignment_nonce": "n1", "agent_handle": "agent-1"},
                    ),
                    ("worker_completed", {"item_id": "TASK-001"}),
                ],
            )
            done = self.run_hook(
                {
                    "event": "PreToolUse",
                    "tool": "Shell",
                    "command": f"python3 scripts/optim_plans.py advance-item --repo {shlex.quote(str(done_repo))} --item-id TASK-001",
                }
            )
            self.assert_allowed(done)

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
