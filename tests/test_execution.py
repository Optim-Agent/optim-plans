from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

try:
    from helpers import git, make_executable, make_repo
except ModuleNotFoundError:
    from tests.helpers import git, make_executable, make_repo


ROOT = Path(__file__).resolve().parents[1]


class ExecutionTests(unittest.TestCase):
    def _host_worker(self) -> dict[str, object]:
        from scripts.optim_plans_core import host_executor_prompt_hash

        return {
            "mode": "host-multi-agent",
            "platform": "codex",
            "agent_type": "optim-plans-executor",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "prompt_protocol": "optim-plans-host-executor-v1",
            "prompt_hash": host_executor_prompt_hash(),
            "allowed_tools": ["Read", "Write", "Edit", "MultiEdit", "Bash"],
            "sandbox": "workspace-write",
            "result_schema": "optim-plans-worker-result-v1",
        }

    def _host_validator(self) -> dict[str, object]:
        from scripts.optim_plans_core import HOST_VALIDATOR_RESULT_SCHEMA, validator_prompt_hash

        return {
            "mode": "host-multi-agent",
            "platform": "codex",
            "agent_type": "optim-plans-validator",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "prompt_protocol": "optim-plans-host-validator-v1",
            "prompt_hash": validator_prompt_hash(),
            "allowed_tools": ["Read", "Bash"],
            "sandbox": "read-only",
            "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
        }

    def _foreground_validator(self) -> dict[str, object]:
        from scripts.optim_plans_core import (
            HOST_VALIDATOR_PROMPT_PROTOCOL,
            HOST_VALIDATOR_RESULT_SCHEMA,
            host_agent,
            validator_prompt_hash,
        )

        return {
            "mode": "foreground",
            "platform": host_agent(),
            "prompt_protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
            "prompt_hash": validator_prompt_hash(),
            "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
        }

    def _foreground_worker(self) -> dict[str, object]:
        from scripts.optim_plans_core import (
            HOST_EXECUTOR_PROMPT_PROTOCOL,
            HOST_EXECUTOR_RESULT_SCHEMA,
            host_agent,
            host_executor_prompt_hash,
        )

        return {
            "mode": "foreground",
            "platform": host_agent(),
            "prompt_protocol": HOST_EXECUTOR_PROMPT_PROTOCOL,
            "prompt_hash": host_executor_prompt_hash(),
            "result_schema": HOST_EXECUTOR_RESULT_SCHEMA,
        }

    def _validator_prompt(self) -> dict[str, object]:
        from scripts.optim_plans_core import HOST_VALIDATOR_PROMPT_PROTOCOL, VALIDATOR_PROMPT_CONTRACT, validator_prompt_hash

        return {
            "protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
            "hash": validator_prompt_hash(),
            "contract": VALIDATOR_PROMPT_CONTRACT,
        }

    def _worker(self, path: Path, body: str) -> Path:
        return make_executable(
            path,
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--optim-plans-smoke' in sys.argv:\n"
            "    print(json.dumps({'status': 'valid', 'evidence': 'adapter smoke ok'}))\n"
            "    raise SystemExit(0)\n"
            + body,
        )

    def _start_adapter_execution(
        self,
        repo: Path,
        *,
        worker: Path,
        verification_argv: list[str],
        allowed_paths: list[str] | None = None,
        worker_timeout_seconds: float = 5,
        verification_timeout_seconds: float = 5,
        worker_env: dict[str, str] | None = None,
        ignored_runtime_outputs: list[str] | None = None,
    ):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Adapter Execution", plan_hash="abc123")
        run_worktree = state.root / "run-worktrees" / state.run_id
        argv = [str(worker), "exec", "-C", str(run_worktree)]
        smoke_argv = [*argv, "--optim-plans-smoke"]
        manifest = {
            "plan_hash": "abc123",
            "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
            "integration_destination": "main",
            "run_worktree_path": str(run_worktree),
            "worker": {
                "adapter": "codex",
                "argv": argv,
                "env": worker_env or {},
                "smoke": {"argv": smoke_argv, "timeout_seconds": 5},
                "timeout_seconds": worker_timeout_seconds,
            },
            "verification_argv": verification_argv,
            "verification_timeout_seconds": verification_timeout_seconds,
            "items": [{"id": "TASK-001", "allowed_paths": allowed_paths or ["src/app.txt"]}],
        }
        if ignored_runtime_outputs is not None:
            manifest["ignored_runtime_outputs"] = ignored_runtime_outputs
        state.persist_execution_manifest(manifest)
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_current_adapter_execution(
        self,
        repo: Path,
        *,
        worker: Path,
        validator: Path,
        verification_argv: list[str],
        allowed_paths: list[str] | None = None,
        validator_retry_limit: int = 1,
        write_plan: bool = True,
        ignored_runtime_outputs: list[str] | None = None,
    ):
        from scripts.optim_plans_core import EXECUTION_PROTOCOL, EXECUTION_SCHEMA_VERSION, OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Validator Execution", plan_hash="abc123")
        if write_plan:
            self._write_full_plan(state)
        run_worktree = state.root / "run-worktrees" / state.run_id
        worker_argv = [str(worker), "exec", "-C", str(run_worktree)]
        validator_argv = [str(validator), "exec", "-C", str(run_worktree)]
        manifest = {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "protocol_version": EXECUTION_PROTOCOL,
            "plan_hash": "abc123",
            "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
            "integration_destination": "main",
            "run_worktree_path": str(run_worktree),
            "worker": {
                "adapter": "codex",
                "argv": worker_argv,
                "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"], "timeout_seconds": 5},
            },
            "validator_worker": {
                "adapter": "codex",
                "argv": validator_argv,
                "smoke": {"argv": [*validator_argv, "--optim-plans-smoke"], "timeout_seconds": 5},
                "timeout_seconds": 5,
            },
            "validator_prompt": self._validator_prompt(),
            "validator_retry_limit": validator_retry_limit,
            "verification_argv": verification_argv,
            "verification_timeout_seconds": 5,
            "items": [
                {
                    "id": "TASK-001",
                    "allowed_paths": allowed_paths or ["src/app.txt"],
                    "validator": {"check_ids": ["VC-TASK-001"]},
                }
            ],
        }
        if ignored_runtime_outputs is not None:
            manifest["ignored_runtime_outputs"] = ignored_runtime_outputs
        state.persist_execution_manifest(manifest)
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_host_execution(
        self,
        repo: Path,
        *,
        verification_argv: list[str],
        allowed_paths: list[str] | None = None,
    ):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Host Execution", plan_hash="abc123")
        run_worktree = state.root / "run-worktrees" / state.run_id
        state.persist_execution_manifest(
            {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": self._host_worker(),
                "verification_argv": verification_argv,
                "verification_timeout_seconds": 5,
                "items": [{"id": "TASK-001", "allowed_paths": allowed_paths or ["src/host.txt"]}],
            }
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_foreground_execution(
        self,
        repo: Path,
        *,
        verification_argv: list[str],
        allowed_paths: list[str] | None = None,
    ):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Foreground Execution", plan_hash="abc123")
        run_worktree = state.root / "run-worktrees" / state.run_id
        state.persist_execution_manifest(
            {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": self._foreground_worker(),
                "verification_argv": verification_argv,
                "verification_timeout_seconds": 5,
                "items": [{"id": "TASK-001", "allowed_paths": allowed_paths or ["src/foreground.txt"]}],
            }
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_host_batch_execution(
        self,
        repo: Path,
        items: list[dict[str, object]],
        *,
        verification_argv: list[str] | None = None,
        validator: bool = False,
        validator_retry_limit: int = 1,
        write_plan: bool = True,
    ):
        from scripts.optim_plans_core import EXECUTION_PROTOCOL, EXECUTION_SCHEMA_VERSION, OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Host Batch Execution", plan_hash="abc123")
        run_worktree = state.root / "run-worktrees" / state.run_id
        manifest: dict[str, object] = {
            "plan_hash": "abc123",
            "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
            "integration_destination": "main",
            "run_worktree_path": str(run_worktree),
            "worker": self._host_worker(),
            "verification_argv": verification_argv or [sys.executable, "-c", "pass"],
            "verification_timeout_seconds": 5,
            "items": items,
        }
        if validator:
            if write_plan:
                self._write_full_plan(state)
            manifest.update(
                {
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "protocol_version": EXECUTION_PROTOCOL,
                    "validator_worker": self._host_validator(),
                    "validator_prompt": self._validator_prompt(),
                    "validator_retry_limit": validator_retry_limit,
                }
            )
        state.persist_execution_manifest(manifest)
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_execution(self, repo: Path, items: list[dict[str, object]], *, integration_destination: str = "main"):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Execution", plan_hash="abc123")
        state.persist_execution_manifest(
            {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": integration_destination,
                "items": items,
            }
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        started = state.start_execution(question["nonce"])
        return state, Path(started["run_worktree"])

    def _prepare_manifest_path(self, root: Path, repo: Path, *, source_base: str | None = None) -> Path:
        manifest = {
            "plan_hash": "abc123",
            "source_base": source_base or git(repo, "rev-parse", "--verify", "HEAD"),
            "integration_destination": "main",
            "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_deep_research_prepare_requires_registered_ready_plan(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            state = OptimPlansState.initialize(repo, topic="Deep", plan_hash="abc123", plan_level_name="deep-research-plan")
            manifest_path = self._prepare_manifest_path(raw_path, repo)

            with self.assertRaisesRegex(ContractError, "PLAN_v1"):
                state.prepare_execution(manifest_path)

    def _finish_nonce(self, state, outcome: str) -> str:
        question = state.request_finish_approval()
        choices = {option["id"] for option in question["options"]}
        if outcome in choices:
            return question["nonce"]
        self.fail(f"missing finish approval question for {outcome}")

    def _answer_execution_summary(self, state, choice: str = "skip-summary") -> dict[str, object]:
        for event in reversed(state.replay().events):
            payload = event.get("payload", {})
            if event["type"] != "pending_question" or payload.get("stage") != "execution_summary":
                continue
            answered = any(
                current["type"] == "answer_recorded"
                and current.get("payload", {}).get("nonce") == payload["nonce"]
                for current in state.replay().events
            )
            if not answered:
                return state.record_answer(payload["nonce"], choice)
        self.fail("missing execution summary question")

    def _checkpoint_after_summary_choice(
        self,
        state,
        item_id: str = "TASK-001",
        *,
        evidence: str = "unit ok",
        choice: str = "skip-summary",
    ) -> dict[str, object]:
        checkpoint = state.checkpoint_item(item_id, evidence=evidence)
        if checkpoint.get("phase") == "awaiting_execution_summary":
            self._answer_execution_summary(state, choice)
            checkpoint = state.checkpoint_item(item_id, evidence=evidence)
        return checkpoint

    def _checkpoint_one_item(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        state.begin_item("TASK-001")
        target = run_worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")
        state.record_worker_completion("TASK-001", evidence="worker finished")
        return self._checkpoint_after_summary_choice(state)["commit"]

    def _checkpoint_message_for_item(
        self,
        item: dict[str, object],
        writes: dict[str, str] | None = None,
    ) -> tuple[str, str, list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            manifest_item = {"id": "TASK-001", "allowed_paths": ["src"], **item}
            state, run_worktree = self._start_execution(repo, [manifest_item])
            state.begin_item("TASK-001")
            for path, text in ({"src/done.txt": "done\n"} if writes is None else writes).items():
                target = run_worktree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            checkpoint = self._checkpoint_after_summary_choice(state)
            message = git(run_worktree, "log", "-1", "--format=%B", checkpoint["commit"])
            return state.run_id, message, checkpoint["changed_files"]

    def _verify_one_item(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        commit = self._checkpoint_one_item(state, run_worktree, path)
        state.final_audit()
        return commit

    def _enter_manual_finish_state(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        checkpoint = self._checkpoint_one_item(state, run_worktree, path)
        state.append_event("final_audit_passed", {"status": "passed", "final_commit": checkpoint, "changed_files": [path]})
        state.append_event("awaiting_integration", {"final_checkpoint": checkpoint})
        return checkpoint

    def _block_item_with_worker_failures(
        self,
        state,
        run_worktree: Path,
        *,
        path: str = "src/done.txt",
        evidence: str = "worker failed visibly",
    ) -> Path:
        target = run_worktree / path
        for _ in range(3):
            state.begin_item("TASK-001")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("failed attempt\n", encoding="utf-8")
            state.record_worker_failure("TASK-001", evidence=evidence)
        self.assertEqual(state.replay().status, "blocked")
        return target

    def _add_passing_full_proof_files(self, repo: Path) -> None:
        (repo / "scripts").mkdir()
        (repo / "hooks").mkdir()
        (repo / "tests").mkdir()
        (repo / "scripts" / "validate_structure.py").write_text("", encoding="utf-8")
        (repo / "scripts" / "placeholder.py").write_text("", encoding="utf-8")
        (repo / "hooks" / "placeholder.py").write_text("", encoding="utf-8")
        (repo / "tests" / "test_placeholder.py").write_text(
            "import unittest\n\n"
            "class PlaceholderTests(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(repo, "add", "scripts", "hooks", "tests")
        git(repo, "commit", "-m", "add proof harness")

    def _write_full_plan(
        self,
        state,
        *,
        name: str = "PLAN_v2.md",
        requirements: str = "- R-001: implement the assigned behavior.",
        non_goals: str = "- no strict gate",
    ) -> Path:
        state.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = state.artifact_dir / name
        path.write_text(
            "# Plan\n\n"
            "## Requirements\n"
            f"{requirements}\n\n"
            "## Acceptance Criteria\n"
            "- AC-001: controller checks pass.\n\n"
            "## Implementation Items\n"
            "- I-001: update scoped files.\n\n"
            "## Verifier Checklist\n"
            "- [ ] V-001: run focused tests.\n\n"
            "## Non-Goals\n"
            f"{non_goals}\n\n"
            "## Constraints\n",
            encoding="utf-8",
        )
        return path

    def _assert_available_plan_context(self, context: dict[str, object]) -> None:
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["source_version"], 2)
        self.assertTrue(str(context["source_path"]).endswith("PLAN_v2.md"))
        self.assertIsInstance(context["source_hash"], str)
        self.assertFalse(context["truncation"]["audit_breaking"])
        self.assertEqual(context["sections"]["Requirements"]["status"], "available")
        self.assertEqual(context["sections"]["Constraints"]["status"], "empty")

    def test_plan_context_selects_highest_plan_and_discloses_gaps_and_truncation(self) -> None:
        from scripts.optim_plans_core import plan_context

        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw)
            unavailable = plan_context(artifact)
            self.assertEqual(unavailable["status"], "unavailable")
            self.assertEqual(unavailable["sections"]["Requirements"]["status"], "missing")

            (artifact / "PLAN_v2.md").write_text("# Plan\n\n## Requirements\nold\n", encoding="utf-8")
            selected = artifact / "PLAN_v10.md"
            selected.write_text(
                "# Plan\n\n"
                "## Requirements\n"
                "new requirement text\n\n"
                "## Acceptance Criteria\n"
                "acceptance\n\n"
                "## Implementation Items\n"
                "implementation\n\n"
                "## Verifier Checklist\n"
                "verifier\n\n"
                "## Constraints\n",
                encoding="utf-8",
            )

            context = plan_context(artifact)
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["source_version"], 10)
            self.assertEqual(context["source_hash"], hashlib.sha256(selected.read_bytes()).hexdigest())
            self.assertIn("new requirement", context["sections"]["Requirements"]["content"])
            self.assertFalse(context["sections"]["Non-Goals"]["present"])
            self.assertTrue(context["sections"]["Constraints"]["present"])
            self.assertEqual(context["sections"]["Constraints"]["status"], "empty")

            truncated = plan_context(artifact, section_char_limit=8, total_char_limit=1000)
            self.assertTrue(truncated["truncated"])
            self.assertTrue(truncated["truncation"]["audit_breaking"])
            self.assertEqual(truncated["sections"]["Requirements"]["status"], "truncated")

        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw)
            (artifact / "PLAN_v1.md").write_text(
                "# Plan\n\n"
                "## Requirements\n"
                "R\n\n"
                "## Acceptance Criteria\n"
                "AC\n\n"
                "## Implementation Items\n"
                "I\n\n"
                "## Verifier Checklist\n"
                "V\n\n"
                "## Non-Goals\n"
                "noncritical section is long\n\n"
                "## Constraints\n",
                encoding="utf-8",
            )
            general = plan_context(artifact, section_char_limit=12, total_char_limit=1000)
            self.assertTrue(general["truncated"])
            self.assertFalse(general["truncation"]["audit_breaking"])
            self.assertEqual(general["sections"]["Non-Goals"]["status"], "truncated")

    def test_host_manifest_accepts_codex_block_without_smoke_and_run_item_rejects_it(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "pass"],
            )
            manifest = next(
                event["payload"]["manifest"]
                for event in state.replay().events
                if event["type"] == "execution_manifest_created"
            )
            self.assertEqual(manifest["worker"]["mode"], "host-multi-agent")
            self.assertNotIn("smoke", manifest["worker"])

            with self.assertRaisesRegex(ContractError, "host-multi-agent workers require assign-item"):
                state.run_item("TASK-001")

            for label, worker, message in (
                ("missing", {**self._host_worker(), "prompt_hash": None}, "prompt_hash"),
                ("cross_platform", {**self._host_worker(), "platform": "claude"}, "platform must be codex"),
                ("duplicate_tools", {**self._host_worker(), "allowed_tools": ["Read", "Read"]}, "duplicates"),
            ):
                with self.subTest(label=label):
                    case_root = Path(raw) / label
                    case_root.mkdir()
                    bad_repo = make_repo(case_root)
                    bad_state = OptimPlansState.initialize(bad_repo, topic=label, plan_hash="abc123")
                    with self.assertRaisesRegex(ContractError, message):
                        bad_state.persist_execution_manifest(
                            {
                                "plan_hash": "abc123",
                                "source_base": git(bad_repo, "rev-parse", "--verify", "HEAD"),
                                "integration_destination": "main",
                                "worker": worker,
                                "verification_argv": [sys.executable, "-c", "pass"],
                                "items": [{"id": "TASK-001", "allowed_paths": ["src/host.txt"]}],
                            }
                        )

    def test_host_assignment_authorization_registration_and_completion_are_bound(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState, ignored_audit_noise_policy

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_host_execution(
                repo,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/host.txt').read_text() == 'ok\\n'",
                ],
            )
            assignment = state.assign_item("TASK-001")
            artifact_scope = state.artifact_dir.relative_to(repo).as_posix()
            self.assertEqual(
                {"scope": "run_worktree", "ignored_files": "read_only", "writes": "allowed_paths_only"},
                assignment["launch_block"]["read_access"],
            )
            self.assertEqual(ignored_audit_noise_policy([".venv", artifact_scope]), assignment["launch_block"]["ignored_audit_noise"])
            reloaded = OptimPlansState.load_active(repo).assign_item("TASK-001")
            self.assertEqual(reloaded["assignment_nonce"], assignment["assignment_nonce"])
            self.assertEqual(
                1,
                sum(event["type"] == "item_started" for event in state.replay().events),
            )

            altered = json.loads(json.dumps(assignment["launch_block"]))
            altered["worker"]["model"] = "other-model"
            with self.assertRaisesRegex(ContractError, "launch block does not match"):
                state.authorize_spawn("TASK-001", assignment["assignment_nonce"], altered)

            authorized = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            authorized_again = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            self.assertEqual(authorized_again["launch_nonce"], authorized["launch_nonce"])

            registered = state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                agent_handle="agent-123",
                launch_block=assignment["launch_block"],
            )
            self.assertEqual(registered["agent_handle"], "agent-123")
            self.assertIn("wait_agent", registered["next_action"])
            self.assertIn("close_agent", registered["next_action"])
            self.assertIn("complete-item", registered["next_action"])
            self.assertIn("fail-item", registered["next_action"])
            self.assertEqual(state.assign_item("TASK-001")["next_action"], registered["next_action"])
            with self.assertRaisesRegex(ContractError, "stale or already used"):
                state.register_agent(
                    "TASK-001",
                    assignment_nonce=assignment["assignment_nonce"],
                    launch_nonce=authorized["launch_nonce"],
                    agent_handle="agent-123",
                    launch_block=assignment["launch_block"],
                )
            with self.assertRaisesRegex(ContractError, "registered host agent handle"):
                state.complete_host_item(
                    "TASK-001",
                    assignment_nonce=assignment["assignment_nonce"],
                    agent_handle="wrong-handle",
                    evidence="done",
                )

            (run_worktree / "src").mkdir()
            (run_worktree / "src/host.txt").write_text("ok\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="agent-123",
                evidence="wait_agent completed",
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_item("TASK-001")

            self.assertIn("commit", checkpoint)
            event_types = [event["type"] for event in state.replay().events]
            self.assertIn("host_spawn_authorized", event_types)
            self.assertIn("host_agent_registered", event_types)
            self.assertIn("checkpoint_created", event_types)
            self.assertIn("run_finished", event_types)

    def test_manifest_ignored_runtime_outputs_do_not_block_checkpoint_or_final_audit(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state = OptimPlansState.initialize(repo, topic="Runtime Outputs", plan_hash="abc123")
            run_worktree = state.root / "run-worktrees" / state.run_id
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "run_worktree_path": str(run_worktree),
                    "worker": self._host_worker(),
                    "ignored_runtime_outputs": ["runtime-output/"],
                    "verification_argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('src/host.txt').read_text() == 'ok\\n'",
                    ],
                    "verification_timeout_seconds": 5,
                    "items": [{"id": "TASK-001", "allowed_paths": ["src/host.txt"]}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            state.start_execution(question["nonce"])

            assignment = state.assign_item("TASK-001")
            artifact_scope = state.artifact_dir.relative_to(repo).as_posix()
            self.assertIn(".venv", assignment["launch_block"]["ignored_audit_noise"]["patterns"])
            self.assertIn(artifact_scope, assignment["launch_block"]["ignored_audit_noise"]["patterns"])
            self.assertIn("runtime-output", assignment["launch_block"]["ignored_audit_noise"]["patterns"])
            authorized = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                agent_handle="agent-123",
                launch_block=assignment["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/host.txt").write_text("ok\n", encoding="utf-8")
            runtime_output = run_worktree / "runtime-output" / "trace.json"
            runtime_output.parent.mkdir()
            runtime_output.write_text("{}\n", encoding="utf-8")
            artifact_output = run_worktree / artifact_scope / "controller-runtime.md"
            artifact_output.parent.mkdir(parents=True, exist_ok=True)
            artifact_output.write_text("# runtime note\n", encoding="utf-8")
            venv_python = run_worktree / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.symlink_to("/usr/bin/python3")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="agent-123",
                evidence="wait_agent completed",
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_item("TASK-001")

            self.assertEqual(checkpoint["phase"], "finalized")
            self.assertEqual(checkpoint["changed_files"], ["src/host.txt"])
            tree_paths = git(run_worktree, "ls-tree", "-r", "--name-only", "HEAD")
            self.assertNotIn(".venv/bin/python", tree_paths)
            self.assertNotIn("runtime-output/trace.json", tree_paths)
            self.assertNotIn(f"{artifact_scope}/controller-runtime.md", tree_paths)

    def test_host_validator_launch_and_result_are_bound_to_nonce_handle_and_delta(self) -> None:
        from scripts.optim_plans_core import EXECUTION_PROTOCOL, EXECUTION_SCHEMA_VERSION, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state = OptimPlansState.initialize(repo, topic="Host Validator", plan_hash="abc123")
            self._write_full_plan(state)
            run_worktree = state.root / "run-worktrees" / state.run_id
            state.persist_execution_manifest(
                {
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "protocol_version": EXECUTION_PROTOCOL,
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "run_worktree_path": str(run_worktree),
                    "worker": self._host_worker(),
                    "validator_worker": self._host_validator(),
                    "validator_prompt": self._validator_prompt(),
                    "validator_retry_limit": 0,
                    "verification_argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('src/host-validator.txt').read_text() == 'ok\\n'",
                    ],
                    "verification_timeout_seconds": 5,
                    "items": [
                        {
                            "id": "TASK-001",
                            "allowed_paths": ["src/host-validator.txt"],
                            "validator": {"check_ids": ["VC-TASK-001"]},
                        }
                    ],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            state.start_execution(question["nonce"])

            assignment = state.assign_item("TASK-001")
            self._assert_available_plan_context(assignment["launch_block"]["plan_context"])
            executor_auth = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=executor_auth["launch_nonce"],
                agent_handle="executor-agent",
                launch_block=assignment["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/host-validator.txt").write_text("ok\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="executor-agent",
                evidence="executor done",
            )
            self._write_full_plan(state, name="PLAN_v3.md", requirements="- mutated after executor completion.")
            validator_assignment = state.advance_item("TASK-001")
            self.assertEqual(validator_assignment["phase"], "validator_assigned")
            self.assertEqual(validator_assignment["validator_launch_block"]["plan_context"], assignment["launch_block"]["plan_context"])
            self._assert_available_plan_context(validator_assignment["validator_launch_block"]["plan_context"])
            validator_auth = state.authorize_validator_spawn(
                "TASK-001",
                validator_assignment["validator_nonce"],
                validator_assignment["validator_launch_block"],
            )
            registered_validator = state.register_validator_agent(
                "TASK-001",
                validator_nonce=validator_assignment["validator_nonce"],
                launch_nonce=validator_auth["launch_nonce"],
                agent_handle="validator-agent",
                launch_block=validator_assignment["validator_launch_block"],
            )
            replayed_validator = state.advance_item("TASK-001")
            self.assertEqual(replayed_validator["next_action"], registered_validator["next_action"])
            self.assertIn("close_agent", registered_validator["next_action"])
            self.assertIn("complete-validator", registered_validator["next_action"])
            self.assertIn("fail-validator", registered_validator["next_action"])
            for key in (
                "run_id",
                "item_id",
                "attempt",
                "nonce",
                "validator_config_hash",
                "validator_prompt_hash",
                "delta_fingerprint",
                "status",
                "evidence",
                "feedback_for_executor",
                "checked_items",
            ):
                self.assertIn(f'"{key}"', registered_validator["next_action"])
            self.assertIn(f'"nonce":"{validator_assignment["validator_nonce"]}"', registered_validator["next_action"])
            self.assertIn("status is pass or fail", registered_validator["next_action"])
            status = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "status", "--repo", str(repo)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["active_wait"]["role"], "validator")
            self.assertEqual(status_payload["active_wait"]["target_kind"], "item")
            self.assertEqual(status_payload["active_wait"]["agent_handle"], "validator-agent")
            self.assertEqual(status_payload["active_wait"]["close_command"], "host close_agent")
            self.assertEqual(status_payload["active_wait"]["close_agent_handle"], "validator-agent")
            self.assertEqual(status_payload["next_action"], registered_validator["next_action"])
            result = {
                "run_id": state.run_id,
                "item_id": "TASK-001",
                "attempt": validator_assignment["attempt"],
                "nonce": validator_assignment["validator_nonce"],
                "validator_config_hash": validator_assignment["validator_config_hash"],
                "validator_prompt_hash": validator_assignment["validator_prompt_hash"],
                "delta_fingerprint": validator_assignment["delta_fingerprint"],
                "status": "pass",
                "evidence": "host validator passed",
                "feedback_for_executor": "",
                "checked_items": ["VC-TASK-001"],
            }
            recorded = state.record_validator_result(
                "TASK-001",
                validator_nonce=validator_assignment["validator_nonce"],
                agent_handle="validator-agent",
                result=result,
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_item("TASK-001")

            self.assertEqual(recorded["agent_handle"], "validator-agent")
            self.assertEqual(recorded["launch_nonce"], validator_auth["launch_nonce"])
            self.assertIn("commit", checkpoint)

    def test_host_validator_item_resume_success_binds_prompt_hash(self) -> None:
        from scripts.optim_plans_core import ContractError

        items = [
            {"id": "TASK-001", "allowed_paths": ["src/1.txt"], "validator": {"check_ids": ["VC-1"]}},
            {"id": "TASK-002", "allowed_paths": ["src/2.txt"], "validator": {"check_ids": ["VC-2"]}},
        ]
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(
                repo,
                items,
                verification_argv=[sys.executable, "-c", "pass"],
                validator=True,
            )
            first = state.assign_item("TASK-001")
            first_auth = state.authorize_spawn("TASK-001", first["assignment_nonce"], first["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=first["assignment_nonce"],
                launch_nonce=first_auth["launch_nonce"],
                agent_handle="executor-one",
                launch_block=first["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/1.txt").write_text("one\n", encoding="utf-8")
            state.complete_host_item("TASK-001", assignment_nonce=first["assignment_nonce"], agent_handle="executor-one", evidence="executor done")
            validator_one = state.advance_item("TASK-001")
            validator_auth = state.authorize_validator_spawn(
                "TASK-001",
                validator_one["validator_nonce"],
                validator_one["validator_launch_block"],
            )
            state.register_validator_agent(
                "TASK-001",
                validator_nonce=validator_one["validator_nonce"],
                launch_nonce=validator_auth["launch_nonce"],
                agent_handle="validator-one",
                launch_block=validator_one["validator_launch_block"],
            )
            state.record_validator_result(
                "TASK-001",
                validator_nonce=validator_one["validator_nonce"],
                agent_handle="validator-one",
                result={
                    "run_id": state.run_id,
                    "item_id": "TASK-001",
                    "attempt": validator_one["attempt"],
                    "nonce": validator_one["validator_nonce"],
                    "validator_config_hash": validator_one["validator_config_hash"],
                    "validator_prompt_hash": validator_one["validator_prompt_hash"],
                    "delta_fingerprint": validator_one["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "validator one passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-1"],
                },
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                state.advance_item("TASK-001")

            second = state.assign_item("TASK-002")
            second_auth = state.authorize_spawn("TASK-002", second["assignment_nonce"], second["launch_block"])
            state.register_agent(
                "TASK-002",
                assignment_nonce=second["assignment_nonce"],
                launch_nonce=second_auth["launch_nonce"],
                agent_handle="executor-two",
                launch_block=second["launch_block"],
            )
            (run_worktree / "src/2.txt").write_text("two\n", encoding="utf-8")
            state.complete_host_item("TASK-002", assignment_nonce=second["assignment_nonce"], agent_handle="executor-two", evidence="executor two done")

            validator_two = state.advance_item("TASK-002")
            self.assertEqual(validator_two["validator_launch_block"]["prior_validator_agent_handle"], "validator-one")
            self.assertIn("authorize-validator-resume", validator_two["next_action"])
            altered = json.loads(json.dumps(validator_two["validator_launch_block"]))
            altered["validator_prompt_hash"] = "wrong"
            with self.assertRaisesRegex(ContractError, "validator launch block does not match"):
                state.authorize_validator_resume("TASK-002", validator_two["validator_nonce"], "validator-one", altered)
            resume_auth = state.authorize_validator_resume(
                "TASK-002",
                validator_two["validator_nonce"],
                "validator-one",
                validator_two["validator_launch_block"],
            )
            self.assertEqual(resume_auth["validator_prompt_hash"], validator_two["validator_prompt_hash"])
            self.assertEqual(resume_auth["prompt_hash"], validator_two["validator"]["prompt_hash"])
            with self.assertRaisesRegex(ContractError, "authorized prior handle"):
                state.register_validator_agent(
                    "TASK-002",
                    validator_nonce=validator_two["validator_nonce"],
                    resume_nonce=resume_auth["resume_nonce"],
                    agent_handle="other-validator",
                    launch_block=validator_two["validator_launch_block"],
                )
            registered = state.register_validator_agent(
                "TASK-002",
                validator_nonce=validator_two["validator_nonce"],
                resume_nonce=resume_auth["resume_nonce"],
                agent_handle="validator-one",
                launch_block=validator_two["validator_launch_block"],
            )
            self.assertTrue(registered["resumed"])
            recorded = state.record_validator_result(
                "TASK-002",
                validator_nonce=validator_two["validator_nonce"],
                agent_handle="validator-one",
                result={
                    "run_id": state.run_id,
                    "item_id": "TASK-002",
                    "attempt": validator_two["attempt"],
                    "nonce": validator_two["validator_nonce"],
                    "validator_config_hash": validator_two["validator_config_hash"],
                    "validator_prompt_hash": validator_two["validator_prompt_hash"],
                    "delta_fingerprint": validator_two["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "validator two passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-2"],
                },
            )
            self.assertEqual(recorded["resume_nonce"], resume_auth["resume_nonce"])
            event_types = [event["type"] for event in state.replay().events]
            self.assertEqual(event_types.count("validator_spawn_authorized"), 1)

    def test_batch_selection_uses_ready_prefix_and_projects_status(self) -> None:
        from scripts.optim_plans_core import ContractError

        items = [{"id": f"TASK-00{index}", "allowed_paths": [f"src/{index}.txt"]} for index in range(1, 5)]
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_batch_execution(repo, items)
            assignment = state.assign_batch()
            self.assertEqual(assignment["item_ids"], ["TASK-001", "TASK-002", "TASK-003", "TASK-004"])
            self.assertEqual(
                {item_id: state._item_statuses(state.replay().events, state._execution_manifest_record(state.replay().events)["manifest"])[item_id] for item_id in assignment["item_ids"]},
                {item_id: "in_progress" for item_id in assignment["item_ids"]},
            )

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_batch_execution(repo, items)
            with self.assertRaisesRegex(ContractError, "continuous ready prefix"):
                state.assign_batch(["TASK-002", "TASK-003", "TASK-004"])

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_batch_execution(repo, items + [{"id": "TASK-005", "allowed_paths": ["src/5.txt"]}])
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state.append_event("batch_checkpoint_created", {"batch_id": "B-old", "item_ids": ["TASK-001", "TASK-002", "TASK-003"], "commit": base, "changed_files": []})
            self.assertEqual(state.assign_batch()["item_ids"], ["TASK-004", "TASK-005"])

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_batch_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
                    {"id": "TASK-002", "depends_on": ["TASK-001"], "allowed_paths": ["src/2.txt"]},
                    {"id": "TASK-003", "allowed_paths": ["src/3.txt"]},
                ],
            )
            self.assertEqual(state.assign_batch()["item_ids"], ["TASK-001"])

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_batch_execution(repo, items)
            manifest = state._execution_manifest_record(state.replay().events)["manifest"]
            cases = [
                ("batch_started", {}, "in_progress"),
                ("batch_completed", {}, "completed"),
                ("batch_validator_assigned", {}, "validating"),
                ("batch_validator_result_recorded", {"status": "pass"}, "validated"),
                ("batch_validator_result_recorded", {"status": "fail"}, "failed"),
                ("batch_worker_failed", {}, "failed"),
                ("batch_retry_restored", {}, "pending"),
                ("batch_checkpoint_prepared", {}, "prepared"),
                ("batch_checkpoint_created", {}, "verified"),
            ]
            for event_type, extra, expected in cases:
                with self.subTest(event_type=event_type):
                    statuses = state._item_statuses(
                        [{"type": event_type, "payload": {"batch_id": "B-test", "item_ids": ["TASK-001", "TASK-002"], **extra}}],
                        manifest,
                    )
                    self.assertEqual(statuses["TASK-001"], expected)
                    self.assertEqual(statuses["TASK-002"], expected)

    def test_batch_host_workflow_blocks_item_commands_and_reuses_context(self) -> None:
        from scripts.optim_plans_core import ContractError, ignored_audit_noise_policy

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_host_batch_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/2.txt"]},
                    {"id": "TASK-003", "allowed_paths": ["src/3.txt"]},
                    {"id": "TASK-004", "allowed_paths": ["src/4.txt"]},
                ],
            )
            assignment = state.assign_batch(["TASK-001", "TASK-002", "TASK-003"])
            artifact_scope = state.artifact_dir.relative_to(repo).as_posix()
            self.assertEqual(
                {"scope": "run_worktree", "ignored_files": "read_only", "writes": "allowed_paths_only"},
                assignment["launch_block"]["read_access"],
            )
            self.assertEqual(ignored_audit_noise_policy([".venv", artifact_scope]), assignment["launch_block"]["ignored_audit_noise"])
            with self.assertRaisesRegex(ContractError, "active batch"):
                state.assign_item("TASK-001")
            authorized = state.authorize_batch_spawn(assignment["batch_id"], assignment["assignment_nonce"], assignment["launch_block"])
            registered_batch = state.register_batch_agent(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                agent_handle="batch-agent-1",
                launch_block=assignment["launch_block"],
            )
            self.assertIn("close_agent", registered_batch["next_action"])
            self.assertIn("complete-batch", registered_batch["next_action"])
            self.assertIn("fail-batch", registered_batch["next_action"])
            self.assertEqual(state.assign_batch(["TASK-001", "TASK-002", "TASK-003"])["next_action"], registered_batch["next_action"])
            self.assertEqual(state.advance_batch(assignment["batch_id"])["next_action"], registered_batch["next_action"])
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="batch-agent-1",
                evidence="batch one done",
            )
            checkpoint = state.advance_batch(assignment["batch_id"])
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_batch(assignment["batch_id"])
            self.assertIn("commit", checkpoint)

            tail = state.assign_batch()
            self.assertEqual(tail["item_ids"], ["TASK-004"])
            self.assertEqual(tail["launch_block"]["prior_executor_agent_handle"], "batch-agent-1")
            self.assertIn("batch one done", tail["launch_block"]["prior_context"])

    def test_host_item_resume_success_is_nonce_and_handle_bound(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/2.txt"]},
                ],
                verification_argv=[sys.executable, "-c", "pass"],
            )
            first = state.assign_item("TASK-001")
            first_auth = state.authorize_spawn("TASK-001", first["assignment_nonce"], first["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=first["assignment_nonce"],
                launch_nonce=first_auth["launch_nonce"],
                agent_handle="agent-one",
                launch_block=first["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/1.txt").write_text("one\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=first["assignment_nonce"],
                agent_handle="agent-one",
                evidence="first done",
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                state.advance_item("TASK-001")

            second = state.assign_item("TASK-002")
            self.assertEqual(second["launch_block"]["prior_executor_agent_handle"], "agent-one")
            self.assertIn("resume_action", second)
            self.assertIn("authorize-resume", second["next_action"])
            self.assertIn("authorize-spawn", second["resume_action"]["fresh_spawn_fallback"])
            self.assertIn("resume_agent", second["next_action"])
            self.assertIn("send_input", second["next_action"])

            resume_auth = state.authorize_resume(
                "TASK-002",
                second["assignment_nonce"],
                "agent-one",
                second["launch_block"],
            )
            self.assertEqual(resume_auth["prior_agent_handle"], "agent-one")
            self.assertEqual(resume_auth["worker_config_hash"], second["worker_config_hash"])
            self.assertEqual(resume_auth["prompt_hash"], second["worker"]["prompt_hash"])
            replayed_resume = state.assign_item("TASK-002")
            self.assertEqual(replayed_resume["phase"], "resume_authorized")
            self.assertIn("close_agent", replayed_resume["next_action"])

            altered = json.loads(json.dumps(second["launch_block"]))
            altered["worker"]["model"] = "other-model"
            with self.assertRaisesRegex(ContractError, "assignment nonce"):
                state.register_agent(
                    "TASK-002",
                    assignment_nonce="wrong",
                    resume_nonce=resume_auth["resume_nonce"],
                    agent_handle="agent-one",
                    launch_block=second["launch_block"],
                )
            with self.assertRaisesRegex(ContractError, "authorized prior handle"):
                state.register_agent(
                    "TASK-002",
                    assignment_nonce=second["assignment_nonce"],
                    resume_nonce=resume_auth["resume_nonce"],
                    agent_handle="other-agent",
                    launch_block=second["launch_block"],
                )
            with self.assertRaisesRegex(ContractError, "launch block does not match"):
                state.register_agent(
                    "TASK-002",
                    assignment_nonce=second["assignment_nonce"],
                    resume_nonce=resume_auth["resume_nonce"],
                    agent_handle="agent-one",
                    launch_block=altered,
                )

            registered = state.register_agent(
                "TASK-002",
                assignment_nonce=second["assignment_nonce"],
                resume_nonce=resume_auth["resume_nonce"],
                agent_handle="agent-one",
                launch_block=second["launch_block"],
            )
            self.assertTrue(registered["resumed"])
            self.assertEqual(registered["resume_nonce"], resume_auth["resume_nonce"])
            self.assertEqual(registered["prior_agent_handle"], "agent-one")
            with self.assertRaisesRegex(ContractError, "stale or already used"):
                state.register_agent(
                    "TASK-002",
                    assignment_nonce=second["assignment_nonce"],
                    resume_nonce=resume_auth["resume_nonce"],
                    agent_handle="agent-one",
                    launch_block=second["launch_block"],
                )
            with self.assertRaisesRegex(ContractError, "stale or already used"):
                state.fail_host_item(
                    "TASK-002",
                    assignment_nonce=second["assignment_nonce"],
                    resume_nonce=resume_auth["resume_nonce"],
                    evidence="late resume failure",
                )

            state.complete_host_item(
                "TASK-002",
                assignment_nonce=second["assignment_nonce"],
                agent_handle="agent-one",
                evidence="resumed done",
            )
            event_types = [event["type"] for event in state.replay().events]
            self.assertEqual(event_types.count("host_spawn_authorized"), 1)
            completed = [event["payload"] for event in state.replay().events if event["type"] == "worker_completed"][-1]
            self.assertEqual(completed["resume_nonce"], resume_auth["resume_nonce"])
            self.assertNotIn("launch_nonce", completed)

    def test_host_resume_failure_records_worker_failed_and_falls_back_to_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/2.txt"]},
                ],
                verification_argv=[sys.executable, "-c", "pass"],
            )
            first = state.assign_item("TASK-001")
            first_auth = state.authorize_spawn("TASK-001", first["assignment_nonce"], first["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=first["assignment_nonce"],
                launch_nonce=first_auth["launch_nonce"],
                agent_handle="bad-later",
                launch_block=first["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/1.txt").write_text("one\n", encoding="utf-8")
            state.complete_host_item("TASK-001", assignment_nonce=first["assignment_nonce"], agent_handle="bad-later", evidence="first done")
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                state.advance_item("TASK-001")

            second = state.assign_item("TASK-002")
            resume_auth = state.authorize_resume("TASK-002", second["assignment_nonce"], "bad-later", second["launch_block"])
            failed = state.fail_host_item(
                "TASK-002",
                assignment_nonce=second["assignment_nonce"],
                resume_nonce=resume_auth["resume_nonce"],
                resume_failure_kind="send_input",
                evidence="send_input failed before registration",
            )
            self.assertTrue(failed["auto_retry"])
            failure = [event["payload"] for event in state.replay().events if event["type"] == "worker_failed"][-1]
            self.assertEqual(failure["prior_agent_handle"], "bad-later")
            self.assertEqual(failure["resume_nonce"], resume_auth["resume_nonce"])
            self.assertEqual(failure["resume_failure_kind"], "send_input")
            self.assertIn("send_input failed", failure["evidence"])

            retry = state.assign_item("TASK-002")
            self.assertEqual(retry["attempt"], 2)
            self.assertNotIn("prior_executor_agent_handle", retry["launch_block"])
            self.assertNotIn("resume_action", retry)
            fresh = state.authorize_spawn("TASK-002", retry["assignment_nonce"], retry["launch_block"])
            self.assertIn("launch_nonce", fresh)

    def test_host_batch_resume_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/2.txt"]},
                    {"id": "TASK-003", "allowed_paths": ["src/3.txt"]},
                    {"id": "TASK-004", "allowed_paths": ["src/4.txt"]},
                ],
            )
            first = state.assign_batch(["TASK-001", "TASK-002", "TASK-003"])
            first_auth = state.authorize_batch_spawn(first["batch_id"], first["assignment_nonce"], first["launch_block"])
            state.register_batch_agent(
                first["batch_id"],
                assignment_nonce=first["assignment_nonce"],
                launch_nonce=first_auth["launch_nonce"],
                agent_handle="batch-agent",
                launch_block=first["launch_block"],
            )
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(first["batch_id"], assignment_nonce=first["assignment_nonce"], agent_handle="batch-agent", evidence="first batch done")
            checkpoint = state.advance_batch(first["batch_id"])
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                state.advance_batch(first["batch_id"])

            tail = state.assign_batch()
            self.assertIn("authorize-batch-resume", tail["next_action"])
            resume_auth = state.authorize_batch_resume(tail["batch_id"], tail["assignment_nonce"], "batch-agent", tail["launch_block"])
            registered = state.register_batch_agent(
                tail["batch_id"],
                assignment_nonce=tail["assignment_nonce"],
                resume_nonce=resume_auth["resume_nonce"],
                agent_handle="batch-agent",
                launch_block=tail["launch_block"],
            )
            self.assertTrue(registered["resumed"])
            (run_worktree / "src/4.txt").write_text("4\n", encoding="utf-8")
            state.complete_host_batch(tail["batch_id"], assignment_nonce=tail["assignment_nonce"], agent_handle="batch-agent", evidence="tail done")
            event_types = [event["type"] for event in state.replay().events]
            self.assertEqual(event_types.count("batch_host_spawn_authorized"), 1)
            completed = [event["payload"] for event in state.replay().events if event["type"] == "batch_completed"][-1]
            self.assertEqual(completed["resume_nonce"], resume_auth["resume_nonce"])

    def test_host_batch_validator_resume_success_binds_prompt_hash(self) -> None:
        from scripts.optim_plans_core import ContractError

        items = [
            {"id": "TASK-001", "allowed_paths": ["src/1.txt"], "validator": {"check_ids": ["VC-1"]}},
            {"id": "TASK-002", "allowed_paths": ["src/2.txt"], "validator": {"check_ids": ["VC-2"]}},
            {"id": "TASK-003", "allowed_paths": ["src/3.txt"], "validator": {"check_ids": ["VC-3"]}},
            {"id": "TASK-004", "allowed_paths": ["src/4.txt"], "validator": {"check_ids": ["VC-4"]}},
        ]
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items, validator=True)
            first = state.assign_batch(["TASK-001", "TASK-002", "TASK-003"])
            first_auth = state.authorize_batch_spawn(first["batch_id"], first["assignment_nonce"], first["launch_block"])
            state.register_batch_agent(
                first["batch_id"],
                assignment_nonce=first["assignment_nonce"],
                launch_nonce=first_auth["launch_nonce"],
                agent_handle="executor-batch",
                launch_block=first["launch_block"],
            )
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(first["batch_id"], assignment_nonce=first["assignment_nonce"], agent_handle="executor-batch", evidence="first batch done")
            validator_first = state.advance_batch(first["batch_id"])
            validator_auth = state.authorize_batch_validator_spawn(
                first["batch_id"],
                validator_first["validator_nonce"],
                validator_first["validator_launch_block"],
            )
            state.register_batch_validator_agent(
                first["batch_id"],
                validator_nonce=validator_first["validator_nonce"],
                launch_nonce=validator_auth["launch_nonce"],
                agent_handle="validator-batch",
                launch_block=validator_first["validator_launch_block"],
            )
            state.record_batch_validator_result(
                first["batch_id"],
                validator_nonce=validator_first["validator_nonce"],
                agent_handle="validator-batch",
                result={
                    "run_id": state.run_id,
                    "batch_id": first["batch_id"],
                    "item_ids": first["item_ids"],
                    "attempt": validator_first["attempt"],
                    "assignment_nonce": first["assignment_nonce"],
                    "nonce": validator_first["validator_nonce"],
                    "validator_config_hash": validator_first["validator_config_hash"],
                    "validator_prompt_hash": validator_first["validator_prompt_hash"],
                    "delta_fingerprint": validator_first["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "batch validator passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-1", "VC-2", "VC-3"],
                },
            )
            checkpoint = state.advance_batch(first["batch_id"])
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                state.advance_batch(first["batch_id"])

            tail = state.assign_batch()
            tail_auth = state.authorize_batch_spawn(tail["batch_id"], tail["assignment_nonce"], tail["launch_block"])
            state.register_batch_agent(
                tail["batch_id"],
                assignment_nonce=tail["assignment_nonce"],
                launch_nonce=tail_auth["launch_nonce"],
                agent_handle="executor-tail",
                launch_block=tail["launch_block"],
            )
            (run_worktree / "src/4.txt").write_text("4\n", encoding="utf-8")
            state.complete_host_batch(tail["batch_id"], assignment_nonce=tail["assignment_nonce"], agent_handle="executor-tail", evidence="tail done")

            validator_tail = state.advance_batch(tail["batch_id"])
            self.assertEqual(validator_tail["validator_launch_block"]["prior_validator_agent_handle"], "validator-batch")
            self.assertIn("authorize-batch-validator-resume", validator_tail["next_action"])
            altered = json.loads(json.dumps(validator_tail["validator_launch_block"]))
            altered["validator"]["model"] = "other-model"
            with self.assertRaisesRegex(ContractError, "validator launch block does not match"):
                state.authorize_batch_validator_resume(
                    tail["batch_id"],
                    validator_tail["validator_nonce"],
                    "validator-batch",
                    altered,
                )
            resume_auth = state.authorize_batch_validator_resume(
                tail["batch_id"],
                validator_tail["validator_nonce"],
                "validator-batch",
                validator_tail["validator_launch_block"],
            )
            self.assertEqual(resume_auth["validator_prompt_hash"], validator_tail["validator_prompt_hash"])
            registered = state.register_batch_validator_agent(
                tail["batch_id"],
                validator_nonce=validator_tail["validator_nonce"],
                resume_nonce=resume_auth["resume_nonce"],
                agent_handle="validator-batch",
                launch_block=validator_tail["validator_launch_block"],
            )
            self.assertTrue(registered["resumed"])
            recorded = state.record_batch_validator_result(
                tail["batch_id"],
                validator_nonce=validator_tail["validator_nonce"],
                agent_handle="validator-batch",
                result={
                    "run_id": state.run_id,
                    "batch_id": tail["batch_id"],
                    "item_ids": tail["item_ids"],
                    "attempt": validator_tail["attempt"],
                    "assignment_nonce": tail["assignment_nonce"],
                    "nonce": validator_tail["validator_nonce"],
                    "validator_config_hash": validator_tail["validator_config_hash"],
                    "validator_prompt_hash": validator_tail["validator_prompt_hash"],
                    "delta_fingerprint": validator_tail["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "tail validator passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-4"],
                },
            )
            self.assertEqual(recorded["resume_nonce"], resume_auth["resume_nonce"])
            event_types = [event["type"] for event in state.replay().events]
            self.assertEqual(event_types.count("batch_validator_spawn_authorized"), 1)

    def test_batch_validator_envelope_retry_and_checkpoint_atomicity(self) -> None:
        from scripts import optim_plans_core as core
        from scripts.optim_plans_core import ContractError

        items = [
            {"id": "TASK-001", "allowed_paths": ["src/1.txt"], "validator": {"check_ids": ["VC-1"]}},
            {"id": "TASK-002", "allowed_paths": ["src/2.txt"], "validator": {"check_ids": ["VC-2"]}},
            {"id": "TASK-003", "allowed_paths": ["src/3.txt"], "validator": {"check_ids": ["VC-3"]}},
        ]

        def prepare_batch(state, run_worktree: Path):
            assignment = state.assign_batch()
            self._assert_available_plan_context(assignment["launch_block"]["plan_context"])
            auth = state.authorize_batch_spawn(assignment["batch_id"], assignment["assignment_nonce"], assignment["launch_block"])
            state.register_batch_agent(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=auth["launch_nonce"],
                agent_handle="executor-agent",
                launch_block=assignment["launch_block"],
            )
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="executor-agent",
                evidence="executor done",
            )
            self._write_full_plan(state, name="PLAN_v3.md", requirements="- mutated after executor completion.")
            validator_assignment = state.advance_batch(assignment["batch_id"])
            self.assertEqual(validator_assignment["validator_launch_block"]["plan_context"], assignment["launch_block"]["plan_context"])
            self._assert_available_plan_context(validator_assignment["validator_launch_block"]["plan_context"])
            validator_auth = state.authorize_batch_validator_spawn(
                assignment["batch_id"],
                validator_assignment["validator_nonce"],
                validator_assignment["validator_launch_block"],
            )
            registered_validator = state.register_batch_validator_agent(
                assignment["batch_id"],
                validator_nonce=validator_assignment["validator_nonce"],
                launch_nonce=validator_auth["launch_nonce"],
                agent_handle="validator-agent",
                launch_block=validator_assignment["validator_launch_block"],
            )
            self.assertEqual(state.advance_batch(assignment["batch_id"])["next_action"], registered_validator["next_action"])
            self.assertIn("close_agent", registered_validator["next_action"])
            self.assertIn("complete-batch-validator", registered_validator["next_action"])
            self.assertIn("fail-batch-validator", registered_validator["next_action"])
            for key in (
                "run_id",
                "batch_id",
                "item_ids",
                "attempt",
                "assignment_nonce",
                "nonce",
                "validator_config_hash",
                "validator_prompt_hash",
                "delta_fingerprint",
                "status",
                "evidence",
                "feedback_for_executor",
                "checked_items",
            ):
                self.assertIn(f'"{key}"', registered_validator["next_action"])
            self.assertIn(f'"nonce":"{validator_assignment["validator_nonce"]}"', registered_validator["next_action"])
            self.assertIn("status is pass or fail", registered_validator["next_action"])
            return assignment, validator_assignment

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items, validator=True, validator_retry_limit=1)
            assignment, validator_assignment = prepare_batch(state, run_worktree)
            result = {
                "run_id": state.run_id,
                "batch_id": assignment["batch_id"],
                "item_ids": assignment["item_ids"],
                "attempt": validator_assignment["attempt"],
                "assignment_nonce": assignment["assignment_nonce"],
                "nonce": validator_assignment["validator_nonce"],
                "validator_config_hash": validator_assignment["validator_config_hash"],
                "validator_prompt_hash": validator_assignment["validator_prompt_hash"],
                "delta_fingerprint": validator_assignment["delta_fingerprint"],
                "status": "pass",
                "evidence": "batch validator passed",
                "feedback_for_executor": "",
                "checked_items": ["VC-1", "VC-2", "VC-3"],
            }
            recorded = state.record_batch_validator_result(
                assignment["batch_id"],
                validator_nonce=validator_assignment["validator_nonce"],
                agent_handle="validator-agent",
                result=result,
            )
            self.assertEqual(recorded["checked_items"], ["VC-1", "VC-2", "VC-3"])
            checkpoint = state.advance_batch(assignment["batch_id"])
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_batch(assignment["batch_id"])
            self.assertIn("commit", checkpoint)

        for label, mutate, error in (
            ("partial_checks", lambda result: {**result, "checked_items": ["VC-1"]}, "checked_items"),
            ("wrong_item_order", lambda result: {**result, "item_ids": list(reversed(result["item_ids"]))}, "item_ids"),
            ("wrong_batch", lambda result: {**result, "batch_id": "B-wrong"}, "batch_id"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                state, run_worktree = self._start_host_batch_execution(repo, items, validator=True, validator_retry_limit=0)
                assignment, validator_assignment = prepare_batch(state, run_worktree)
                result = {
                    "run_id": state.run_id,
                    "batch_id": assignment["batch_id"],
                    "item_ids": assignment["item_ids"],
                    "attempt": validator_assignment["attempt"],
                    "assignment_nonce": assignment["assignment_nonce"],
                    "nonce": validator_assignment["validator_nonce"],
                    "validator_config_hash": validator_assignment["validator_config_hash"],
                    "validator_prompt_hash": validator_assignment["validator_prompt_hash"],
                    "delta_fingerprint": validator_assignment["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "batch validator passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-1", "VC-2", "VC-3"],
                }
                rejected = state.record_batch_validator_result(
                    assignment["batch_id"],
                    validator_nonce=validator_assignment["validator_nonce"],
                    agent_handle="validator-agent",
                    result=mutate(result),
                )
                events = state.replay().events
                self.assertTrue(rejected["auto_validator_retry"])
                self.assertIn("batch_validator_protocol_rejected", [event["type"] for event in events])
                self.assertIn("batch_retry_restored", [event["type"] for event in events])
                self.assertIn(error, next(event["payload"]["evidence"] for event in events if event["type"] == "batch_validator_protocol_rejected"))
                self.assertNotIn("batch_checkpoint_created", [event["type"] for event in state.replay().events])

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items, validator=True, validator_retry_limit=1)
            assignment, validator_assignment = prepare_batch(state, run_worktree)
            failed = state.record_batch_validator_result(
                assignment["batch_id"],
                validator_nonce=validator_assignment["validator_nonce"],
                agent_handle="validator-agent",
                result={
                    "run_id": state.run_id,
                    "batch_id": assignment["batch_id"],
                    "item_ids": assignment["item_ids"],
                    "attempt": validator_assignment["attempt"],
                    "assignment_nonce": assignment["assignment_nonce"],
                    "nonce": validator_assignment["validator_nonce"],
                    "validator_config_hash": validator_assignment["validator_config_hash"],
                    "validator_prompt_hash": validator_assignment["validator_prompt_hash"],
                    "delta_fingerprint": validator_assignment["delta_fingerprint"],
                    "status": "fail",
                    "evidence": "needs changes",
                    "feedback_for_executor": "fix batch",
                    "checked_items": ["VC-1", "VC-2", "VC-3"],
                },
            )
            self.assertTrue(failed["auto_validator_retry"])
            retry = state.assign_batch()
            self.assertEqual(retry["batch_id"], assignment["batch_id"])
            self.assertEqual(retry["item_ids"], assignment["item_ids"])
            self.assertEqual(retry["attempt"], 2)
            self.assertEqual(retry["launch_block"]["validator_feedback"]["feedback_for_executor"], "fix batch")

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items, validator=True, validator_retry_limit=1)
            assignment, validator_assignment = prepare_batch(state, run_worktree)
            failed = state.fail_batch_validator(
                assignment["batch_id"],
                reason="process",
                validator_nonce=validator_assignment["validator_nonce"],
                agent_handle="validator-agent",
                evidence="validator crashed",
            )
            self.assertTrue(failed["auto_validator_retry"])
            retry = state.assign_batch()
            self.assertEqual(retry["batch_id"], assignment["batch_id"])
            self.assertEqual(retry["attempt"], 2)

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, [item | {"validator": {"check_ids": [f"VC-{index}"]}} for index, item in enumerate(items, start=1)])
            assignment = state.assign_batch()
            auth = state.authorize_batch_spawn(assignment["batch_id"], assignment["assignment_nonce"], assignment["launch_block"])
            state.register_batch_agent(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=auth["launch_nonce"],
                agent_handle="executor-agent",
                launch_block=assignment["launch_block"],
            )
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="executor-agent",
                evidence="executor done",
            )
            prepared = state.checkpoint_batch(assignment["batch_id"], evidence="verified")
            if prepared.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
            original_run = core.subprocess.run

            def fail_commit(argv, *args, **kwargs):
                if isinstance(argv, list) and "commit" in argv:
                    raise subprocess.CalledProcessError(1, argv)
                return original_run(argv, *args, **kwargs)

            with mock.patch.object(core.subprocess, "run", side_effect=fail_commit):
                with self.assertRaises((ContractError, subprocess.CalledProcessError)):
                    state.checkpoint_batch(assignment["batch_id"], evidence="verified")
            events = state.replay().events
            self.assertIn("batch_audit_failed", [event["type"] for event in events])
            self.assertNotIn("batch_checkpoint_created", [event["type"] for event in events])
            statuses = state._item_statuses(events, state._execution_manifest_record(events)["manifest"])
            self.assertEqual({statuses[item_id] for item_id in assignment["item_ids"]}, {"failed"})

    def test_host_advance_reports_resume_phases_and_failure_auto_retries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "pass"],
            )
            assignment = state.assign_item("TASK-001")
            self.assertEqual(state.advance_item("TASK-001")["phase"], "assigned")
            authorized = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            self.assertEqual(state.advance_item("TASK-001")["phase"], "spawn_authorized")
            registered = state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                agent_handle="agent-fail",
                launch_block=assignment["launch_block"],
            )
            replayed = state.advance_item("TASK-001")
            self.assertEqual(replayed["phase"], "agent_registered")
            self.assertEqual(replayed["next_action"], registered["next_action"])
            state.fail_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="agent-fail",
                evidence="wait_agent failed",
            )

            events = state.replay().events
            self.assertEqual(state.replay().status, "executing")
            self.assertIn("worker_failed", [event["type"] for event in events])
            self.assertIn("retry_restored", [event["type"] for event in events])
            self.assertNotIn("checkpoint_created", [event["type"] for event in events])
            failure = next(event["payload"] for event in events if event["type"] == "worker_failed")
            self.assertEqual(failure["agent_handle"], "agent-fail")
            retried = state.advance_item("TASK-001")
            self.assertEqual(retried["phase"], "assigned")
            self.assertEqual(retried["attempt"], 2)

    def test_host_failure_can_record_lost_handle_after_spawn_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, _run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "pass"],
            )
            assignment = state.assign_item("TASK-001")
            authorized = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])

            state.fail_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                evidence="spawned host agent handle was lost before registration",
            )

            failure = next(event["payload"] for event in state.replay().events if event["type"] == "worker_failed")
            self.assertTrue(failure["agent_handle_lost"])
            self.assertEqual(failure["launch_nonce"], authorized["launch_nonce"])
            self.assertEqual(state.replay().status, "executing")
            self.assertIn("retry_restored", [event["type"] for event in state.replay().events])

    def test_active_audit_failure_auto_retries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "pass"],
            )
            state.assign_item("TASK-001")
            target = run_worktree / "src/host.txt"
            target.parent.mkdir()
            target.write_text("bad\n", encoding="utf-8")

            restored = state.record_attempt_failure("audit_failed", "TASK-001", evidence="active audit failed")

            self.assertTrue(restored["auto_retry"])
            self.assertFalse(target.exists())
            self.assertEqual(state.replay().status, "executing")
            self.assertIn("retry_restored", [event["type"] for event in state.replay().events])

        items = [
            {"id": "TASK-001", "allowed_paths": ["src/1.txt"]},
            {"id": "TASK-002", "allowed_paths": ["src/2.txt"]},
            {"id": "TASK-003", "allowed_paths": ["src/3.txt"]},
        ]
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items)
            assignment = state.assign_batch()
            target = run_worktree / "src/1.txt"
            target.parent.mkdir()
            target.write_text("bad\n", encoding="utf-8")

            restored = state.record_batch_attempt_failure(
                "batch_audit_failed",
                assignment["batch_id"],
                evidence="active batch audit failed",
            )

            self.assertTrue(restored["auto_retry"])
            self.assertFalse(target.exists())
            self.assertEqual(state.replay().status, "executing")
            self.assertIn("batch_retry_restored", [event["type"] for event in state.replay().events])

    def test_host_retry_item_returns_assignment_after_manual_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "pass"],
            )
            state.assign_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/host.txt").write_text("bad\n", encoding="utf-8")
            state.record_attempt_failure("audit_failed", "TASK-001", evidence="manual audit failure", retryable=False)

            retried = state.retry_item("TASK-001", None)

            self.assertEqual(retried["phase"], "assigned")
            self.assertEqual(retried["attempt"], 2)

    def test_run_item_launches_manifest_adapter_and_verifier_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            self._add_passing_full_proof_files(repo)
            argv_log = raw_path / "argv.json"
            policy_log = raw_path / "policy.json"
            sentinel = raw_path / "shell-expanded"
            worker = self._worker(
                raw_path / "codex",
                "import json, os, sys\n"
                "from pathlib import Path\n"
                f"Path({str(argv_log)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "state = json.loads(Path(os.environ['OPTIM_PLANS_STATE_PATH']).read_text(encoding='utf-8'))\n"
                f"Path({str(policy_log)!r}).write_text(json.dumps({{'env': json.loads(os.environ['OPTIM_PLANS_IGNORED_AUDIT_NOISE']), 'state': state['ignored_audit_noise'], 'read_env': json.loads(os.environ['OPTIM_PLANS_READ_ACCESS']), 'read_state': state['read_access']}}), encoding='utf-8')\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "Path('runtime-output').mkdir(exist_ok=True)\n"
                "Path('runtime-output/trace.json').write_text('{}\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'verified',\n"
                "    'evidence': 'worker self-attested verified',\n"
                "}))\n",
            )
            state, run_worktree = self._start_adapter_execution(
                repo,
                worker=worker,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/app.txt').read_text() == 'ok\\n'",
                ],
                worker_env={"EXTRA_LITERAL": f"; touch {sentinel}"},
                ignored_runtime_outputs=["runtime-output/"],
            )
            checkpoint = state.run_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.run_item("TASK-001")

            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])
            self.assertEqual(
                git(run_worktree, "log", "-1", "--format=%s"),
                "Update src/app.txt",
            )
            self.assertFalse(sentinel.exists())
            self.assertFalse(any("schema" in arg for arg in json.loads(argv_log.read_text(encoding="utf-8"))))
            policy = json.loads(policy_log.read_text(encoding="utf-8"))
            self.assertIn("runtime-output", policy["env"]["patterns"])
            self.assertEqual(policy["env"], policy["state"])
            self.assertEqual("read_only", policy["read_env"]["ignored_files"])
            self.assertEqual(policy["read_env"], policy["read_state"])
            self.assertNotIn("runtime-output/trace.json", git(run_worktree, "ls-tree", "-r", "--name-only", "HEAD"))
            self.assertEqual(
                [event["type"] for event in state.replay().events[-7:]],
                [
                    "item_verified",
                    "checkpoint_prepared",
                    "pending_question",
                    "answer_recorded",
                    "checkpoint_created",
                    "final_audit_passed",
                    "run_finished",
                ],
            )
            self.assertFalse(any(event.get("payload", {}).get("stage") == "finish_run" for event in state.replay().events))

    def test_run_item_allows_foreground_executor_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_foreground_execution(
                repo,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/foreground.txt').read_text() == 'ok\\n'",
                ],
                allowed_paths=["src/foreground.txt"],
            )

            assignment = state.run_item("TASK-001")

            self.assertEqual(assignment["phase"], "foreground_assigned")
            self.assertEqual(assignment["worker"]["mode"], "foreground")
            self.assertIn("complete-item", assignment["next_action"])
            self.assertIn("advance-item", assignment["next_action"])
            (run_worktree / "src").mkdir()
            (run_worktree / "src/foreground.txt").write_text("ok\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="foreground",
                evidence="foreground session completed",
            )
            checkpoint = state.advance_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.advance_item("TASK-001")

            self.assertIn("commit", checkpoint)

    def test_current_manifest_runs_validator_before_verification_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            self._add_passing_full_proof_files(repo)
            worker_dir = raw_path / "worker"
            validator_dir = raw_path / "validator"
            worker_dir.mkdir()
            validator_dir.mkdir()
            validator_policy_log = raw_path / "validator-policy.json"
            worker = self._worker(
                worker_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': 'worker done',\n"
                "}))\n",
            )
            validator = self._worker(
                validator_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "state = json.loads(Path(os.environ['OPTIM_PLANS_VALIDATOR_STATE_PATH']).read_text(encoding='utf-8'))\n"
                f"Path({str(validator_policy_log)!r}).write_text(json.dumps({{'env': json.loads(os.environ['OPTIM_PLANS_IGNORED_AUDIT_NOISE']), 'state': state['ignored_audit_noise']}}), encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'run_id': os.environ['OPTIM_PLANS_RUN_ID'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_ITEM_ID'],\n"
                "    'attempt': int(os.environ['OPTIM_PLANS_ATTEMPT']),\n"
                "    'nonce': os.environ['OPTIM_PLANS_VALIDATOR_NONCE'],\n"
                "    'validator_config_hash': os.environ['OPTIM_PLANS_VALIDATOR_CONFIG_HASH'],\n"
                "    'validator_prompt_hash': os.environ['OPTIM_PLANS_VALIDATOR_PROMPT_HASH'],\n"
                "    'delta_fingerprint': os.environ['OPTIM_PLANS_DELTA_FINGERPRINT'],\n"
                "    'status': 'pass',\n"
                "    'evidence': 'validator passed',\n"
                "    'feedback_for_executor': '',\n"
                "    'checked_items': json.loads(os.environ['OPTIM_PLANS_CHECK_IDS']),\n"
                "}))\n",
            )
            state, run_worktree = self._start_current_adapter_execution(
                repo,
                worker=worker,
                validator=validator,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/app.txt').read_text() == 'ok\\n'",
                ],
                ignored_runtime_outputs=["runtime-output/"],
            )

            checkpoint = state.run_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.run_item("TASK-001")

            event_types = [event["type"] for event in state.replay().events]
            self.assertLess(event_types.index("worker_completed"), event_types.index("validator_result_recorded"))
            self.assertLess(event_types.index("validator_result_recorded"), event_types.index("item_verified"))
            validator_event = next(event["payload"] for event in state.replay().events if event["type"] == "validator_result_recorded")
            self.assertEqual(validator_event["status"], "pass")
            self.assertEqual(validator_event["checked_items"], ["VC-TASK-001"])
            validator_policy = json.loads(validator_policy_log.read_text(encoding="utf-8"))
            self.assertIn("runtime-output", validator_policy["env"]["patterns"])
            self.assertEqual(validator_policy["env"], validator_policy["state"])
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])

    def test_validator_fail_auto_retries_with_feedback_in_cli_executor_env_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            self._add_passing_full_proof_files(repo)
            worker_dir = raw_path / "worker"
            validator_dir = raw_path / "validator"
            worker_dir.mkdir()
            validator_dir.mkdir()
            attempts = raw_path / "attempts.txt"
            feedback_seen = raw_path / "feedback.txt"
            worker = self._worker(
                worker_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                f"attempts = Path({str(attempts)!r})\n"
                "count = int(attempts.read_text() or '0') + 1 if attempts.exists() else 1\n"
                "attempts.write_text(str(count), encoding='utf-8')\n"
                "state = json.loads(Path(os.environ['OPTIM_PLANS_STATE_PATH']).read_text(encoding='utf-8'))\n"
                "feedback = os.environ.get('OPTIM_PLANS_VALIDATOR_FEEDBACK', '') or state.get('validator_feedback', {}).get('feedback_for_executor', '')\n"
                f"Path({str(feedback_seen)!r}).write_text(feedback, encoding='utf-8')\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': f'worker attempt {count}',\n"
                "}))\n",
            )
            validator_count = raw_path / "validator-count.txt"
            validator = self._worker(
                validator_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                f"count_path = Path({str(validator_count)!r})\n"
                "count = int(count_path.read_text() or '0') + 1 if count_path.exists() else 1\n"
                "count_path.write_text(str(count), encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'run_id': os.environ['OPTIM_PLANS_RUN_ID'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_ITEM_ID'],\n"
                "    'attempt': int(os.environ['OPTIM_PLANS_ATTEMPT']),\n"
                "    'nonce': os.environ['OPTIM_PLANS_VALIDATOR_NONCE'],\n"
                "    'validator_config_hash': os.environ['OPTIM_PLANS_VALIDATOR_CONFIG_HASH'],\n"
                "    'validator_prompt_hash': os.environ['OPTIM_PLANS_VALIDATOR_PROMPT_HASH'],\n"
                "    'delta_fingerprint': os.environ['OPTIM_PLANS_DELTA_FINGERPRINT'],\n"
                "    'status': 'fail' if count == 1 else 'pass',\n"
                "    'evidence': 'needs another pass' if count == 1 else 'validator passed',\n"
                "    'feedback_for_executor': 'fix validator finding' if count == 1 else '',\n"
                "    'checked_items': json.loads(os.environ['OPTIM_PLANS_CHECK_IDS']),\n"
                "}))\n",
            )
            state, _run_worktree = self._start_current_adapter_execution(
                repo,
                worker=worker,
                validator=validator,
                verification_argv=[sys.executable, "-c", "from pathlib import Path; assert Path('src/app.txt').exists()"],
                validator_retry_limit=1,
            )

            result = state.run_item("TASK-001")
            if result.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                result = state.run_item("TASK-001")

            self.assertIn("commit", result)
            self.assertEqual(attempts.read_text(encoding="utf-8"), "2")
            self.assertEqual(feedback_seen.read_text(encoding="utf-8"), "fix validator finding")
            events = state.replay().events
            self.assertEqual(
                [event["payload"]["status"] for event in events if event["type"] == "validator_result_recorded"],
                ["fail", "pass"],
            )
            retry = next(event["payload"] for event in events if event["type"] == "retry_restored")
            self.assertTrue(retry["auto_validator_retry"])

    def test_context_integrity_recovery_status_summary_and_retry_boundaries(self) -> None:
        from scripts.optim_plans_core import ContractError, EXECUTION_PROTOCOL, EXECUTION_SCHEMA_VERSION, OptimPlansState

        def foreground_assignment(repo: Path, *, plan: str | None):
            state = OptimPlansState.initialize(repo, topic="Foreground Context", plan_hash="abc123")
            if plan == "critical":
                self._write_full_plan(state, requirements="R" * 7000)
            elif plan == "general":
                self._write_full_plan(state, non_goals="N" * 7000)
            run_worktree = state.root / "run-worktrees" / state.run_id
            state.persist_execution_manifest(
                {
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "protocol_version": EXECUTION_PROTOCOL,
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "run_worktree_path": str(run_worktree),
                    "worker": self._host_worker(),
                    "validator_worker": self._foreground_validator(),
                    "validator_prompt": self._validator_prompt(),
                    "validator_retry_limit": 1,
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"], "validator": {"check_ids": ["VC-TASK-001"]}}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            state.start_execution(question["nonce"])
            assignment = state.assign_item("TASK-001")
            auth = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=auth["launch_nonce"],
                agent_handle="executor-agent",
                launch_block=assignment["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/app.txt").write_text("ok\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="executor-agent",
                evidence="done",
            )
            return state, state.advance_item("TASK-001")

        for label, plan, reason in (
            ("unavailable", None, "plan_context_unavailable"),
            ("critical", "critical", "plan_context_audit_breaking_truncation"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                state, assignment = foreground_assignment(repo, plan=plan)
                result = {
                    "run_id": state.run_id,
                    "item_id": "TASK-001",
                    "attempt": assignment["attempt"],
                    "nonce": assignment["validator_nonce"],
                    "validator_config_hash": assignment["validator_config_hash"],
                    "validator_prompt_hash": assignment["validator_prompt_hash"],
                    "delta_fingerprint": assignment["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "validator passed",
                    "feedback_for_executor": "",
                    "checked_items": ["VC-TASK-001"],
                }
                recovery = state.record_validator_result("TASK-001", validator_nonce=assignment["validator_nonce"], result=result)
                replayed = state.replay()
                details = replayed.status_details["context_integrity_recovery"]
                summary = state._execution_summary_results(
                    replayed.events,
                    state._execution_manifest_record(replayed.events)["manifest"],
                )["TASK-001"]
                runtime = json.loads(state.runtime_file.read_text(encoding="utf-8"))
                event_types = [event["type"] for event in replayed.events]

                self.assertEqual(recovery["status"], "recovery_required")
                self.assertEqual(recovery["reason"], reason)
                self.assertEqual(replayed.status, "context_integrity_recovery")
                self.assertEqual(runtime["status"], "context_integrity_recovery")
                self.assertEqual(runtime["status_details"]["context_integrity_recovery"], details)
                self.assertEqual(details["status"], "recovery_required")
                self.assertEqual(details["reason"], reason)
                self.assertEqual(details["source_path"], recovery["plan_context"]["source_path"])
                self.assertEqual(details["source_hash"], recovery["plan_context"]["source_hash"])
                self.assertEqual(details["truncation"], recovery["plan_context"]["truncation"])
                self.assertEqual(summary["status"], "context_integrity_recovery")
                self.assertEqual(summary["context_integrity"], details)
                self.assertIn("source_path=", summary["limitations"])
                self.assertIn("source_hash=", summary["limitations"])
                self.assertNotIn("awaiting_retry_decision", event_types)
                self.assertNotIn("retry_restored", event_types)
                self.assertNotIn("checkpoint_created", event_types)
                with self.assertRaisesRegex(ContractError, "lifecycle is context_integrity_recovery"):
                    state.request_retry("TASK-001")

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, assignment = foreground_assignment(repo, plan="general")
            result = {
                "run_id": state.run_id,
                "item_id": "TASK-001",
                "attempt": assignment["attempt"],
                "nonce": assignment["validator_nonce"],
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "status": "pass",
                "evidence": "validator passed",
                "feedback_for_executor": "",
                "checked_items": ["VC-TASK-001"],
            }
            recorded = state.record_validator_result("TASK-001", validator_nonce=assignment["validator_nonce"], result=result)
            self.assertEqual(recorded["status"], "pass")
            self.assertTrue(assignment["validator_launch_block"]["plan_context"]["truncated"])
            self.assertFalse(assignment["validator_launch_block"]["plan_context"]["truncation"]["audit_breaking"])
            self.assertNotIn("context_integrity_recovery", [event["type"] for event in state.replay().events])

    def test_batch_context_integrity_recovery_does_not_retry_or_checkpoint(self) -> None:
        from scripts.optim_plans_core import ContractError

        items = [
            {"id": "TASK-001", "allowed_paths": ["src/1.txt"], "validator": {"check_ids": ["VC-1"]}},
            {"id": "TASK-002", "allowed_paths": ["src/2.txt"], "validator": {"check_ids": ["VC-2"]}},
            {"id": "TASK-003", "allowed_paths": ["src/3.txt"], "validator": {"check_ids": ["VC-3"]}},
        ]
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_host_batch_execution(repo, items, validator=True, write_plan=False)
            assignment = state.assign_batch()
            auth = state.authorize_batch_spawn(assignment["batch_id"], assignment["assignment_nonce"], assignment["launch_block"])
            state.register_batch_agent(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=auth["launch_nonce"],
                agent_handle="executor-agent",
                launch_block=assignment["launch_block"],
            )
            for index in range(1, 4):
                target = run_worktree / f"src/{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"{index}\n", encoding="utf-8")
            state.complete_host_batch(
                assignment["batch_id"],
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="executor-agent",
                evidence="done",
            )
            validator_assignment = state.advance_batch(assignment["batch_id"])
            recovery = state.fail_batch_validator(
                assignment["batch_id"],
                reason="process",
                validator_nonce=validator_assignment["validator_nonce"],
                evidence="validator crashed",
            )
            replayed = state.replay()
            event_types = [event["type"] for event in replayed.events]
            summary = state._execution_summary_results(
                replayed.events,
                state._execution_manifest_record(replayed.events)["manifest"],
            )

            self.assertEqual(recovery["status"], "recovery_required")
            self.assertEqual(recovery["reason"], "plan_context_unavailable")
            self.assertEqual(replayed.status, "context_integrity_recovery")
            self.assertEqual(replayed.status_details["context_integrity_recovery"]["batch_id"], assignment["batch_id"])
            for item_id in assignment["item_ids"]:
                self.assertEqual(summary[item_id]["status"], "context_integrity_recovery")
            self.assertNotIn("awaiting_retry_decision", event_types)
            self.assertNotIn("batch_retry_restored", event_types)
            self.assertNotIn("batch_checkpoint_created", event_types)
            with self.assertRaisesRegex(ContractError, "lifecycle is context_integrity_recovery"):
                state.request_batch_retry(assignment["batch_id"])

    def test_validator_protocol_rejection_auto_retries_until_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker_dir = raw_path / "worker"
            validator_dir = raw_path / "validator"
            worker_dir.mkdir()
            validator_dir.mkdir()
            worker = self._worker(
                worker_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'], 'item_id': os.environ['OPTIM_PLANS_IDS'], 'status': 'completed', 'evidence': 'done'}))\n",
            )
            validator = self._worker(
                validator_dir / "codex",
                "import json, os\n"
                "print(json.dumps({\n"
                "    'run_id': os.environ['OPTIM_PLANS_RUN_ID'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_ITEM_ID'],\n"
                "    'attempt': int(os.environ['OPTIM_PLANS_ATTEMPT']),\n"
                "    'nonce': os.environ['OPTIM_PLANS_VALIDATOR_NONCE'],\n"
                "    'validator_config_hash': os.environ['OPTIM_PLANS_VALIDATOR_CONFIG_HASH'],\n"
                "    'validator_prompt_hash': os.environ['OPTIM_PLANS_VALIDATOR_PROMPT_HASH'],\n"
                "    'delta_fingerprint': os.environ['OPTIM_PLANS_DELTA_FINGERPRINT'],\n"
                "    'status': 'pass',\n"
                "    'evidence': 'wrong check list',\n"
                "    'feedback_for_executor': '',\n"
                "    'checked_items': ['wrong-check'],\n"
                "}))\n",
            )
            state, _run_worktree = self._start_current_adapter_execution(
                repo,
                worker=worker,
                validator=validator,
                verification_argv=[sys.executable, "-c", "pass"],
            )

            result = state.run_item("TASK-001")

            event_types = [event["type"] for event in state.replay().events]
            self.assertEqual(result["reason"], "three consecutive equivalent retryable failures")
            self.assertIn("validator_protocol_rejected", event_types)
            self.assertIn("retry_restored", event_types)
            self.assertIn("execution_blocked", event_types)
            self.assertNotIn("awaiting_retry_decision", event_types)
            self.assertNotIn("checkpoint_created", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in state.replay().events))

    def test_verifier_delta_drift_after_validator_pass_blocks_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker_dir = raw_path / "worker"
            validator_dir = raw_path / "validator"
            worker_dir.mkdir()
            validator_dir.mkdir()
            worker = self._worker(
                worker_dir / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'], 'item_id': os.environ['OPTIM_PLANS_IDS'], 'status': 'completed', 'evidence': 'done'}))\n",
            )
            validator = self._worker(
                validator_dir / "codex",
                "import json, os\n"
                "print(json.dumps({\n"
                "    'run_id': os.environ['OPTIM_PLANS_RUN_ID'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_ITEM_ID'],\n"
                "    'attempt': int(os.environ['OPTIM_PLANS_ATTEMPT']),\n"
                "    'nonce': os.environ['OPTIM_PLANS_VALIDATOR_NONCE'],\n"
                "    'validator_config_hash': os.environ['OPTIM_PLANS_VALIDATOR_CONFIG_HASH'],\n"
                "    'validator_prompt_hash': os.environ['OPTIM_PLANS_VALIDATOR_PROMPT_HASH'],\n"
                "    'delta_fingerprint': os.environ['OPTIM_PLANS_DELTA_FINGERPRINT'],\n"
                "    'status': 'pass',\n"
                "    'evidence': 'validator passed',\n"
                "    'feedback_for_executor': '',\n"
                "    'checked_items': json.loads(os.environ['OPTIM_PLANS_CHECK_IDS']),\n"
                "}))\n",
            )
            state, _run_worktree = self._start_current_adapter_execution(
                repo,
                worker=worker,
                validator=validator,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('src/app.txt').write_text('mutated\\n', encoding='utf-8')",
                ],
            )

            with self.assertRaisesRegex(Exception, "after verification"):
                state.run_item("TASK-001")

            event_types = [event["type"] for event in state.replay().events]
            self.assertIn("validator_result_recorded", event_types)
            self.assertIn("audit_failed", event_types)
            self.assertNotIn("checkpoint_created", event_types)

    def test_execution_summary_question_blocks_before_checkpoint(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = self._worker(
                raw_path / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': 'worker done',\n"
                "}))\n",
            )
            state, run_worktree = self._start_adapter_execution(
                repo,
                worker=worker,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/app.txt').read_text() == 'ok\\n'",
                ],
            )

            result = state.run_item("TASK-001")
            self.assertEqual(result["phase"], "awaiting_execution_summary")
            question = result["question"]
            self.assertEqual(question["stage"], "execution_summary")
            self.assertEqual(
                [option["id"] for option in question["options"]],
                ["generate-summary", "skip-summary", "always-skip-summary"],
            )
            self.assertEqual(question["recommended_option_id"], "generate-summary")
            self.assertNotIn("checkpoint_created", [event["type"] for event in state.replay().events])
            self.assertEqual(
                git(run_worktree, "rev-parse", "--verify", "HEAD"),
                git(repo, "rev-parse", "--verify", "HEAD"),
            )

            (run_worktree / "src/app.txt").write_text("mutated\n", encoding="utf-8")
            self._answer_execution_summary(state)
            with self.assertRaisesRegex(ContractError, "changed since checkpoint preparation"):
                state.run_item("TASK-001")
            self.assertNotIn("checkpoint_created", [event["type"] for event in state.replay().events])

    def test_execution_summary_resume_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            self._add_passing_full_proof_files(repo)
            worker_count = raw_path / "worker-count.txt"
            worker = self._worker(
                raw_path / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                f"count = Path({str(worker_count)!r})\n"
                "count.write_text(str(int(count.read_text() or '0') + 1) if count.exists() else '1')\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': 'worker done',\n"
                "}))\n",
            )
            state, _run_worktree = self._start_adapter_execution(
                repo,
                worker=worker,
                verification_argv=[sys.executable, "-c", "from pathlib import Path; assert Path('src/app.txt').exists()"],
            )

            self.assertEqual(state.run_item("TASK-001")["phase"], "awaiting_execution_summary")
            self._answer_execution_summary(state)
            checkpoint = state.run_item("TASK-001")
            self.assertIn("commit", checkpoint)
            self.assertEqual(worker_count.read_text(encoding="utf-8"), "1")
            self.assertEqual(
                1,
                sum(event["type"] == "item_started" for event in state.replay().events),
            )

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_host_execution(
                repo,
                verification_argv=[sys.executable, "-c", "from pathlib import Path; assert Path('src/host.txt').exists()"],
            )
            assignment = state.assign_item("TASK-001")
            authorized = state.authorize_spawn("TASK-001", assignment["assignment_nonce"], assignment["launch_block"])
            state.register_agent(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                launch_nonce=authorized["launch_nonce"],
                agent_handle="agent-123",
                launch_block=assignment["launch_block"],
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/host.txt").write_text("ok\n", encoding="utf-8")
            state.complete_host_item(
                "TASK-001",
                assignment_nonce=assignment["assignment_nonce"],
                agent_handle="agent-123",
                evidence="wait_agent completed",
            )

            self.assertEqual(state.advance_item("TASK-001")["phase"], "awaiting_execution_summary")
            self._answer_execution_summary(state)
            checkpoint = state.advance_item("TASK-001")
            self.assertIn("commit", checkpoint)
            self.assertEqual(
                1,
                sum(event["type"] == "item_started" for event in state.replay().events),
            )
            self.assertEqual(
                1,
                sum(event["type"] == "checkpoint_created" for event in state.replay().events),
            )

    def test_execution_summary_skip_and_always_skip_config(self) -> None:
        from scripts.optim_plans_core import EXECUTION_SUMMARY_CONFIG_KEY

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/skip.txt"]}]
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/skip.txt").write_text("skip\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            self.assertEqual(state.checkpoint_item("TASK-001", evidence="unit ok")["phase"], "awaiting_execution_summary")
            self._answer_execution_summary(state, "skip-summary")
            self.assertIn("commit", state.checkpoint_item("TASK-001", evidence="unit ok"))
            self.assertFalse((state.artifact_dir / "EXECUTION_SUMMARY.md").exists())

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/one.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/two.txt"]},
                ],
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/one.txt").write_text("one\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            pending = state.checkpoint_item("TASK-001", evidence="unit ok")
            nonce = pending["question"]["nonce"]
            with mock.patch("scripts.optim_plans_core.write_json_atomic", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    state.record_answer(nonce, "always-skip-summary")
            self.assertFalse(any(event["type"] == "answer_recorded" and event["payload"]["nonce"] == nonce for event in state.replay().events))

            state.record_answer(nonce, "always-skip-summary")
            config = json.loads((repo / ".git" / "optim-plans" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config[EXECUTION_SUMMARY_CONFIG_KEY]["mode"], "always-skip")
            self.assertIn("commit", state.checkpoint_item("TASK-001", evidence="unit ok"))
            state.begin_item("TASK-002")
            (run_worktree / "src/two.txt").write_text("two\n", encoding="utf-8")
            state.record_worker_completion("TASK-002", evidence="worker finished")
            self.assertIn("commit", state.checkpoint_item("TASK-002", evidence="unit ok"))
            self.assertEqual(
                1,
                sum(
                    event["type"] == "pending_question"
                    and event.get("payload", {}).get("stage") == "execution_summary"
                    for event in state.replay().events
                ),
            )
            self.assertFalse((state.artifact_dir / "EXECUTION_SUMMARY.md").exists())

    def test_execution_summary_two_item_and_terminal_updates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/one.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["src/two.txt"]},
                ],
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/one.txt").write_text("one\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker one")
            first = state.checkpoint_item("TASK-001", evidence="unit one")
            self.assertEqual(first["phase"], "awaiting_execution_summary")
            self.assertFalse((state.artifact_dir / "EXECUTION_SUMMARY.md").exists())
            self._answer_execution_summary(state, "generate-summary")
            first = state.checkpoint_item("TASK-001", evidence="unit one")
            summary_path = state.artifact_dir / "EXECUTION_SUMMARY.md"
            text = summary_path.read_text(encoding="utf-8")
            self.assertIn("# EXECUTION_SUMMARY", text)
            self.assertIn("TASK-001", text)
            self.assertIn(first["commit"], text)

            state.begin_item("TASK-002")
            (run_worktree / "src/two.txt").write_text("two\n", encoding="utf-8")
            state.record_worker_completion("TASK-002", evidence="worker two")
            second = state.checkpoint_item("TASK-002", evidence="unit two")
            self.assertIn("commit", second)
            state.final_audit()

            text = summary_path.read_text(encoding="utf-8")
            self.assertIn("TASK-002", text)
            self.assertIn(second["commit"], text)
            self.assertIn("run_finished/integrated", text)
            self.assertEqual(
                1,
                sum(
                    event["type"] == "pending_question"
                    and event.get("payload", {}).get("stage") == "execution_summary"
                    for event in state.replay().events
                ),
            )

    def test_codex_host_rejects_claude_worker_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            sentinel = raw_path / "launched"
            worker = self._worker(
                raw_path / "claude",
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('launched', encoding='utf-8')\n"
                "print('{}')\n",
            )
            from scripts.optim_plans_core import ContractError, OptimPlansState

            state = OptimPlansState.initialize(repo, topic="Cross Platform", plan_hash="abc123")
            with self.assertRaisesRegex(ContractError, "cross-platform delegated worker"):
                state.persist_execution_manifest(
                    {
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "worker": {
                            "adapter": "claude",
                            "argv": [str(worker), "-p", "--json-schema", "{}"],
                        },
                        "verification_argv": [sys.executable, "-c", "pass"],
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
                    }
                )
            self.assertFalse(sentinel.exists())
            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])

    def test_worker_self_attestation_does_not_skip_manifest_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = self._worker(
                raw_path / "codex",
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'verified',\n"
                "    'evidence': 'worker claims verified',\n"
                "}))\n",
            )
            state, run_worktree = self._start_adapter_execution(
                repo,
                worker=worker,
                verification_argv=[str(raw_path / "missing-verifier")],
            )

            with self.assertRaises(Exception):
                state.run_item("TASK-001")

            events = state.replay().events
            event_types = [event["type"] for event in events]
            self.assertIn("worker_completed", event_types)
            self.assertIn("verification_failed", event_types)
            self.assertIn("retry_restored", event_types)
            self.assertIn("execution_blocked", event_types)
            self.assertNotIn("item_verified", event_types)
            self.assertNotIn("checkpoint_created", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in events))
            self.assertTrue((run_worktree / "src/app.txt").exists())

    def test_verifier_failures_auto_retry_until_blocked_without_checkpoint(self) -> None:
        cases = [
            ("nonzero", [sys.executable, "-c", "import sys; sys.stderr.write('x' * 10000); sys.exit(7)"], 5),
            ("missing", ["/definitely/missing/optim-plans-verifier"], 5),
            ("timeout", [sys.executable, "-c", "import time; time.sleep(10)"], 0.2),
        ]
        for name, verification_argv, timeout_seconds in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                raw_path = Path(raw)
                repo = make_repo(raw_path)
                worker = self._worker(
                    raw_path / "codex",
                    "import json, os\n"
                    "from pathlib import Path\n"
                    "Path('src').mkdir(exist_ok=True)\n"
                    "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                    "print(json.dumps({\n"
                    "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                    "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                    "    'status': 'completed',\n"
                    "    'evidence': 'worker done',\n"
                    "}))\n",
                )
                state, run_worktree = self._start_adapter_execution(
                    repo,
                    worker=worker,
                    verification_argv=verification_argv,
                    verification_timeout_seconds=timeout_seconds,
                )

                with self.assertRaises(Exception):
                    state.run_item("TASK-001")

                event_types = [event["type"] for event in state.replay().events]
                self.assertIn("verification_failed", event_types)
                self.assertIn("retry_restored", event_types)
                self.assertIn("execution_blocked", event_types)
                self.assertNotIn("checkpoint_created", event_types)
                self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in state.replay().events))
                self.assertTrue((run_worktree / "src/app.txt").exists())
                failure = next(event for event in state.replay().events if event["type"] == "verification_failed")
                self.assertLessEqual(len(failure["payload"]["evidence"]), 4096)

    def test_worker_timeout_field_does_not_limit_executor_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = self._worker(
                raw_path / "codex",
                "import json, os, time\n"
                "from pathlib import Path\n"
                "time.sleep(0.3)\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'verified',\n"
                "    'evidence': 'worker finished despite legacy timeout field',\n"
                "}))\n",
            )
            state, run_worktree = self._start_adapter_execution(
                repo,
                worker=worker,
                verification_argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/app.txt').read_text() == 'ok\\n'",
                ],
                worker_timeout_seconds=0.01,
            )

            checkpoint = state.run_item("TASK-001")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                self._answer_execution_summary(state)
                checkpoint = state.run_item("TASK-001")

            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])
            event_types = [event["type"] for event in state.replay().events]
            self.assertNotIn("worker_failed", event_types)
            self.assertIn("checkpoint_created", event_types)

    def test_adapter_validation_rejects_non_adapter_argv_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            sentinel = raw_path / "launched"
            fake = self._worker(
                raw_path / "not-codex",
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('launched', encoding='utf-8')\n",
            )
            from scripts.optim_plans_core import ContractError, OptimPlansState

            state = OptimPlansState.initialize(repo, topic="Adapter Validation", plan_hash="abc123")
            run_worktree = state.root / "run-worktrees" / state.run_id
            worker_argv = [str(fake), "exec", "-C", str(run_worktree)]
            with self.assertRaisesRegex(ContractError, "worker adapter argv executable does not match adapter"):
                state.persist_execution_manifest(
                    {
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "run_worktree_path": str(run_worktree),
                        "worker": {
                            "adapter": "codex",
                            "argv": worker_argv,
                            "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"]},
                        },
                        "verification_argv": [sys.executable, "-c", "pass"],
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
                    }
                )

            self.assertFalse(sentinel.exists())
            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])
            self.assertNotIn("item_started", [event["type"] for event in state.replay().events])

    def test_codex_home_cannot_escape_controller_state(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = self._worker(raw_path / "codex", "print('{}')\n")
            state = OptimPlansState.initialize(repo, topic="Codex Home", plan_hash="abc123")
            run_worktree = state.root / "run-worktrees" / state.run_id
            worker_argv = [str(worker), "exec", "-C", str(run_worktree)]
            with self.assertRaisesRegex(ContractError, "CODEX_HOME path must live"):
                state.persist_execution_manifest(
                    {
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "run_worktree_path": str(run_worktree),
                        "worker": {
                            "adapter": "codex",
                            "argv": worker_argv,
                            "env": {"CODEX_HOME": str(raw_path / "outside-codex-home")},
                            "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"]},
                        },
                        "verification_argv": [sys.executable, "-c", "pass"],
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
                    }
                )

    def test_adapter_generated_files_cannot_escape_controller_state(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            sentinel = raw_path / "launched"
            worker = self._worker(
                raw_path / "codex",
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('launched', encoding='utf-8')\n",
            )
            state = OptimPlansState.initialize(repo, topic="Adapter Files", plan_hash="abc123")
            run_worktree = state.root / "run-worktrees" / state.run_id
            outside = raw_path / "outside-config.json"
            worker_argv = [str(worker), "exec", "-C", str(run_worktree)]
            with self.assertRaises(Exception):
                state.persist_execution_manifest(
                    {
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "run_worktree_path": str(run_worktree),
                        "worker": {
                            "adapter": "codex",
                            "argv": worker_argv,
                            "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"]},
                            "config_files": [{"path": str(outside), "content": {}}],
                        },
                        "verification_argv": [sys.executable, "-c", "pass"],
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
                    }
                )
            self.assertFalse(sentinel.exists())
            self.assertFalse(outside.exists())
            self.assertNotIn("item_started", [event["type"] for event in state.replay().events])

    def test_authoritative_audit_catches_git_bypass_forms(self) -> None:
        git_exe = shutil.which("git") or "git"
        cases = {
            "absolute": lambda raw_path: f"subprocess.run([{git_exe!r}, 'config', 'optim-plans.bypass', '1'], check=True)\n",
            "wrapper": lambda raw_path: (
                make_executable(
                    raw_path / "git-wrapper",
                    "#!/usr/bin/env python3\n"
                    "import shutil, subprocess\n"
                    "subprocess.run([shutil.which('git') or 'git', 'config', 'optim-plans.bypass', '1'], check=True)\n",
                ),
                f"subprocess.run([{str(raw_path / 'git-wrapper')!r}], check=True)\n",
            )[1],
        }
        for name, bypass in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                raw_path = Path(raw)
                repo = make_repo(raw_path)
                worker = self._worker(
                    raw_path / "codex",
                    "import json, os, subprocess\n"
                    "from pathlib import Path\n"
                    f"{bypass(raw_path)}"
                    "Path('src').mkdir(exist_ok=True)\n"
                    "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
                    "print(json.dumps({\n"
                    "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                    "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                    "    'status': 'completed',\n"
                    "    'evidence': 'worker done',\n"
                    "}))\n",
                )
                state, _run_worktree = self._start_adapter_execution(
                    repo,
                    worker=worker,
                    verification_argv=[sys.executable, "-c", "pass"],
                )

                with self.assertRaises(Exception):
                    state.run_item("TASK-001")

                event_types = [event["type"] for event in state.replay().events]
                self.assertIn("audit_failed", event_types)
                self.assertIn("awaiting_retry_decision", event_types)
                self.assertNotIn("checkpoint_created", event_types)
                self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in state.replay().events))
                self.assertEqual(git(repo, "config", "optim-plans.bypass"), "1")

    def test_final_cumulative_audit_failure_records_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/ok.txt"]}]
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/ok.txt").write_text("ok\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            self._checkpoint_after_summary_choice(state)

            (run_worktree / "README.md").write_text("manual drift\n", encoding="utf-8")
            git(run_worktree, "add", "README.md")
            git(run_worktree, "commit", "-m", "manual out of scope")
            with self.assertRaises(Exception):
                state.final_audit()

            events = state.replay().events
            event_types = [event["type"] for event in events]
            self.assertIn("audit_failed", event_types)
            self.assertIn("awaiting_retry_decision", event_types)
            self.assertNotIn("final_audit_passed", event_types)
            self.assertNotIn("awaiting_integration", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in events))
            failure = next(event for event in events if event["type"] == "audit_failed")
            self.assertLessEqual(len(failure["payload"]["evidence"]), 4096)

    def test_all_verified_auto_integrates_checked_out_destination_and_releases_active(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            before = git(repo, "rev-parse", "--verify", "main")
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._verify_one_item(state, run_worktree)

            self.assertEqual(state.replay().status, "completed")
            event_types = [event["type"] for event in state.replay().events]
            self.assertIn("final_audit_passed", event_types)
            self.assertIn("run_finished", event_types)
            self.assertNotIn("awaiting_integration", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") == "finish_run" for event in state.replay().events))

            finished = next(event["payload"] for event in state.replay().events if event["type"] == "run_finished")
            self.assertEqual(finished["outcome"], "integrated")
            self.assertEqual(finished["final_checkpoint"], checkpoint)
            self.assertEqual(finished["destination_ref"], "main")
            self.assertEqual(finished["before_destination_oid"], before)
            self.assertEqual(finished["after_destination_oid"], checkpoint)
            self.assertEqual(git(repo, "rev-parse", "--verify", "main"), checkpoint)
            self.assertIn("integration verification exited 0", finished["integration_verification"]["evidence"])
            self.assertEqual(state.replay().status, "completed")
            self.assertFalse(state.active_file.exists())
            self.assertTrue(run_worktree.is_dir())
            self.assertEqual(git(repo, "rev-parse", "--verify", finished["run_branch"]), checkpoint)
            second = OptimPlansState.initialize(repo, topic="Second", plan_hash="def456")
            self.assertTrue(second.active_file.exists())

    def test_final_audit_awaits_integration_when_destination_is_not_checked_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            git(repo, "branch", "release", "main")
            release_before = git(repo, "rev-parse", "--verify", "release")
            state, run_worktree = self._start_execution(
                repo,
                [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}],
                integration_destination="release",
            )
            checkpoint = self._checkpoint_one_item(state, run_worktree)

            audit = state.final_audit()

            self.assertEqual(audit["status"], "passed")
            self.assertEqual(git(repo, "rev-parse", "--verify", "release"), release_before)
            self.assertEqual(state.replay().status, "awaiting_integration")
            self.assertTrue(state.active_file.exists())
            awaiting = next(event["payload"] for event in state.replay().events if event["type"] == "awaiting_integration")
            self.assertEqual(awaiting["final_checkpoint"], checkpoint)
            self.assertEqual(awaiting["destination_ref"], "release")
            self.assertEqual(awaiting["destination_oid"], release_before)
            self.assertIn("checked-out destination", awaiting["evidence"])
            self.assertLessEqual(len(awaiting["evidence"]), 4096)

    def test_auto_integration_proof_failure_records_destination_oids_and_status_creates_finish_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            before = git(repo, "rev-parse", "--verify", "main")
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._checkpoint_one_item(state, run_worktree)

            audit = state.final_audit()

            self.assertEqual(audit["status"], "passed")
            self.assertEqual(git(repo, "rev-parse", "--verify", "main"), checkpoint)
            self.assertEqual(state.replay().status, "awaiting_integration")
            failure = next(
                event["payload"] for event in state.replay().events if event["type"] == "integration_verification_failed"
            )
            self.assertEqual(failure["stage"], "integration_verification")
            self.assertEqual(failure["before_destination_oid"], before)
            self.assertEqual(failure["after_destination_oid"], checkpoint)
            self.assertLessEqual(len(failure["evidence"]), 4096)

            status = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "status", "--repo", str(repo)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            payload = json.loads(status.stdout)
            self.assertEqual(payload["status"], "awaiting_integration")
            self.assertIn("finish_approval_nonce", payload)
            self.assertIn("pr-opened", payload["finish_choices"])

    def test_integrated_finish_requires_manifest_destination_containing_checkpoint(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._enter_manual_finish_state(state, run_worktree)
            started = next(event["payload"] for event in state.replay().events if event["type"] == "execution_started")
            nonce = self._finish_nonce(state, "integrated")
            state.record_answer(nonce, "integrated")

            with self.assertRaises(ContractError):
                state.finish_run("integrated", approval_nonce=nonce, target_ref=started["run_branch"])
            with self.assertRaises(ContractError):
                state.finish_run("integrated", approval_nonce=nonce, target_ref="refs/optim-plans/proof/demo")
            with self.assertRaises(ContractError):
                state.finish_run("integrated", approval_nonce=nonce, target_ref="main")

            git(repo, "merge", "--ff-only", checkpoint)
            finished = state.finish_run("integrated", approval_nonce=nonce, target_ref="main")

            self.assertEqual(finished["outcome"], "integrated")
            self.assertEqual(finished["destination_ref"], "main")
            self.assertEqual(finished["object_id"], git(repo, "rev-parse", "--verify", "main"))
            self.assertIn("integration verification exited 0", finished["integration_verification"]["evidence"])

    def test_integrated_finish_requires_full_repo_verification_before_terminal_event(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._enter_manual_finish_state(state, run_worktree)
            git(repo, "merge", "--ff-only", checkpoint)
            (repo / "tests" / "test_placeholder.py").write_text(
                "import unittest\n\n"
                "class PlaceholderTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.fail('integration broke full proof')\n",
                encoding="utf-8",
            )
            git(repo, "add", "tests/test_placeholder.py")
            git(repo, "commit", "-m", "break proof")
            nonce = self._finish_nonce(state, "integrated")
            state.record_answer(nonce, "integrated")

            with self.assertRaisesRegex(ContractError, "integration verification"):
                state.finish_run("integrated", approval_nonce=nonce, target_ref="main")

            events = state.replay().events
            self.assertIn("integration_verification_failed", [event["type"] for event in events])
            self.assertFalse(any(event["type"] == "run_finished" for event in events))
            self.assertEqual(state.replay().status, "awaiting_integration")
            failure = next(event for event in events if event["type"] == "integration_verification_failed")
            self.assertEqual(failure["payload"]["stage"], "integration_verification")
            self.assertLessEqual(len(failure["payload"]["evidence"]), 4096)

    def test_integrated_finish_verifies_checked_out_destination(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._enter_manual_finish_state(state, run_worktree)
            git(repo, "merge", "--ff-only", checkpoint)
            git(repo, "checkout", "-b", "other", "HEAD~1")
            nonce = self._finish_nonce(state, "integrated")
            state.record_answer(nonce, "integrated")

            with self.assertRaisesRegex(ContractError, "checked-out"):
                state.finish_run("integrated", approval_nonce=nonce, target_ref="main")

            events = state.replay().events
            self.assertIn("integration_verification_failed", [event["type"] for event in events])
            self.assertFalse(any(event["type"] == "run_finished" for event in events))

    def test_integrated_finish_accepts_hashed_legacy_manifest_file(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            self._add_passing_full_proof_files(repo)
            checkpoint = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Legacy Manifest", plan_hash="abc123")
            manifest = {
                "plan_hash": "abc123",
                "base_commit": checkpoint,
                "integration_destination": "main",
                "items": [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}],
            }
            manifest_path = repo / "legacy-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            state.append_event(
                "execution_manifest_written",
                {
                    "manifest_path": "legacy-manifest.json",
                    "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                },
            )
            state.append_event(
                "execution_started",
                {
                    "base_commit": checkpoint,
                    "run_branch": "optim-plans/run/legacy",
                    "run_worktree": str(Path(raw) / "legacy-run"),
                },
            )
            state.append_event("final_audit_passed", {"final_commit": checkpoint})
            state.append_event("awaiting_integration", {"final_checkpoint": checkpoint})
            nonce = state.request_finish_approval()["nonce"]
            state.record_answer(nonce, "integrated")

            finished = state.finish_run("integrated", approval_nonce=nonce, target_ref="main")

            self.assertEqual(finished["outcome"], "integrated")
            self.assertFalse(state.active_file.exists())

    def test_pr_finish_requires_remote_ref_containing_checkpoint_and_records_url(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            checkpoint = self._enter_manual_finish_state(state, run_worktree)
            remote = raw_path / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git(repo, "remote", "add", "origin", str(remote))
            git(repo, "push", "origin", f"{git(repo, 'rev-parse', '--verify', 'HEAD')}:refs/heads/base-only")
            nonce = self._finish_nonce(state, "pr-opened")
            state.record_answer(nonce, "pr-opened")

            with self.assertRaises(ContractError):
                state.finish_run(
                    "pr-opened",
                    approval_nonce=nonce,
                    pr_url="https://example.test/pr/1",
                    remote="origin",
                    remote_ref="refs/heads/base-only",
                )

            git(repo, "push", "origin", f"{checkpoint}:refs/heads/pr")
            finished = state.finish_run(
                "pr-opened",
                approval_nonce=nonce,
                pr_url="https://example.test/pr/1",
                remote="origin",
                remote_ref="refs/heads/pr",
            )

            self.assertEqual(finished["outcome"], "pr-opened")
            self.assertEqual(finished["pr_url"], "https://example.test/pr/1")
            self.assertEqual(finished["remote_ref"], "refs/heads/pr")
            self.assertEqual(finished["object_id"], checkpoint)

    def test_discard_requires_confirmation_and_removes_only_owned_worktree_and_branch(self) -> None:
        from scripts.optim_plans_core import ContractError, git_maybe

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            self._enter_manual_finish_state(state, run_worktree)
            started = next(event["payload"] for event in state.replay().events if event["type"] == "execution_started")
            nonce = self._finish_nonce(state, "discarded")
            state.record_answer(nonce, "discarded")

            with self.assertRaises(ContractError):
                state.finish_run("discarded", approval_nonce=nonce)
            self.assertTrue(run_worktree.exists())
            self.assertIsNotNone(git_maybe(repo, "rev-parse", "--verify", started["run_branch"]))

            finished = state.finish_run("discarded", approval_nonce=nonce, confirm_discard=True)

            self.assertEqual(finished["outcome"], "discarded")
            self.assertFalse(run_worktree.exists())
            self.assertIsNone(git_maybe(repo, "rev-parse", "--verify", started["run_branch"]))

    def test_discard_removes_confirmed_dirty_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            self._block_item_with_worker_failures(state, run_worktree, evidence="worker failed")
            nonce = state.request_finish_approval()["nonce"]
            state.record_answer(nonce, "discarded")

            finished = state.finish_run("discarded", approval_nonce=nonce, confirm_discard=True)

            self.assertEqual(finished["outcome"], "discarded")
            self.assertFalse(run_worktree.exists())

    def test_failed_finish_without_failure_requires_evidence(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            self._enter_manual_finish_state(state, run_worktree)
            nonce = self._finish_nonce(state, "failed")
            state.record_answer(nonce, "failed")

            with self.assertRaises(ContractError):
                state.finish_run("failed", approval_nonce=nonce)

            finished = state.finish_run(
                "failed", approval_nonce=nonce, evidence="integration could not be completed"
            )
            self.assertEqual(finished["evidence"], "integration could not be completed")

    def test_concurrent_finish_has_one_terminal_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            self._enter_manual_finish_state(state, run_worktree)
            nonce = self._finish_nonce(state, "kept")
            state.record_answer(nonce, "kept")
            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "from scripts.optim_plans_core import OptimPlansState\n"
                "OptimPlansState.load_active(Path(sys.argv[1])).finish_run('kept', approval_nonce=sys.argv[2])\n"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), nonce],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(2)
            ]

            self.assertEqual(sorted(worker.wait(timeout=10) for worker in workers), [0, 1])
            events = state.replay().events
            self.assertEqual(len([event for event in events if event["type"] == "run_finished"]), 1)
            self.assertFalse(state.active_file.exists())

    def test_finish_rejects_swapped_active_pointer_before_terminal_event(self) -> None:
        from scripts.optim_plans_core import ContractError, write_json_atomic

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
            )
            self._enter_manual_finish_state(state, run_worktree)
            nonce = self._finish_nonce(state, "kept")
            state.record_answer(nonce, "kept")
            write_json_atomic(state.active_file, {"run_id": "other", "artifact_dir": "docs/other"})

            with self.assertRaises(ContractError):
                state.finish_run("kept", approval_nonce=nonce)

            self.assertFalse(any(event["type"] == "run_finished" for event in state.replay().events))

    def test_failed_and_aborted_finish_outcomes_preserve_failure_evidence(self) -> None:
        for outcome, expected_status in (("failed", "failed"), ("aborted", "aborted")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                state, run_worktree = self._start_execution(
                    repo, [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}]
                )
                self._block_item_with_worker_failures(state, run_worktree)
                nonce = state.request_finish_approval()["nonce"]
                state.record_answer(nonce, outcome)

                finished = state.finish_run(outcome, approval_nonce=nonce)

                self.assertEqual(state.replay().status, expected_status)
                self.assertEqual(finished["outcome"], outcome)
                self.assertEqual(finished["failure_event_type"], "execution_blocked")
                self.assertEqual(finished["failure_item_id"], "TASK-001")
                self.assertIn("worker failed visibly", finished["failure_evidence"])
                self.assertTrue(run_worktree.exists())
                self.assertFalse(state.active_file.exists())

    def test_execution_manifest_dag_validation_and_stable_topological_order(self) -> None:
        from scripts.optim_plans_core import ContractError, canonical_execution_manifest

        manifest = {
            "plan_hash": "abc",
            "integration_destination": "main",
            "items": [
                {"id": "TASK-002", "depends_on": ["TASK-001"]},
                {"id": "TASK-001"},
                {"id": "TASK-003"},
            ],
        }
        ordered = canonical_execution_manifest(manifest)["items"]
        self.assertEqual([item["id"] for item in ordered], ["TASK-001", "TASK-003", "TASK-002"])

        independent = canonical_execution_manifest(
            {"plan_hash": "abc", "integration_destination": "main", "items": [{"id": "A"}, {"id": "B"}]}
        )
        self.assertEqual([item["id"] for item in independent["items"]], ["A", "B"])

        invalid = [
            {"items": []},
            {"items": [{"id": ""}]},
            {"items": [{"id": "A"}, {"id": "A"}]},
            {"items": [{"id": "A", "depends_on": ["B"]}]},
            {"items": [{"id": "A", "depends_on": ["A"]}]},
            {"items": [{"id": "A", "depends_on": ["B"]}, {"id": "B", "depends_on": ["A"]}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ContractError):
                    canonical_execution_manifest(payload)

    def test_execution_manifest_is_hash_bound_write_once_and_rehashed(self) -> None:
        from scripts.optim_plans_core import (
            ContractError,
            EXECUTION_PROTOCOL,
            EXECUTION_SCHEMA_VERSION,
            HOST_VALIDATOR_PROMPT_PROTOCOL,
            HOST_VALIDATOR_RESULT_SCHEMA,
            OptimPlansState,
            execution_manifest_hash,
            host_agent,
            json_text,
            validator_prompt_hash,
        )

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Manifest", plan_hash="abc123")
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "items": [{"id": "TASK-001"}],
            }
            record = state.persist_execution_manifest(manifest)
            self.assertEqual(record["manifest_hash"], execution_manifest_hash(record["manifest"]))
            self.assertNotIn("validator_worker", record["manifest"])
            self.assertIn(".venv", record["manifest"]["ignored_runtime_outputs"])
            self.assertIn(state.artifact_dir.relative_to(repo).as_posix(), record["manifest"]["ignored_runtime_outputs"])

            changed = dict(record["manifest"])
            changed["integration_destination"] = "release"
            self.assertNotEqual(record["manifest_hash"], execution_manifest_hash(changed))
            with self.assertRaises(ContractError):
                state.persist_execution_manifest(manifest)

            tampered: list[str] = []
            for line in state.events_file.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event["type"] == "execution_manifest_created":
                    event["payload"]["manifest"]["integration_destination"] = "release"
                tampered.append(json_text(event))
            state.events_file.write_text("\n".join(tampered) + "\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                state.request_execution_approval()

            current_root = Path(raw) / "current"
            current_root.mkdir()
            current_repo = make_repo(current_root)
            current_state = OptimPlansState.initialize(current_repo, topic="Current Manifest", plan_hash="abc123")
            current = {
                "schema_version": EXECUTION_SCHEMA_VERSION,
                "protocol_version": EXECUTION_PROTOCOL,
                "plan_hash": "abc123",
                "source_base": git(current_repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": self._host_worker(),
                "validator_worker": self._host_validator(),
                "validator_prompt": self._validator_prompt(),
                "validator_retry_limit": 1,
                "verification_argv": [sys.executable, "-c", "pass"],
                "items": [{"id": "TASK-001", "validator": {"check_ids": ["VC-TASK-001"]}}],
            }
            current_record = current_state.persist_execution_manifest(current)
            self.assertEqual(current_record["manifest"]["schema_version"], EXECUTION_SCHEMA_VERSION)
            self.assertEqual(current_record["manifest"]["validator_worker"]["agent_type"], "optim-plans-validator")
            self.assertEqual(current_record["manifest"]["items"][0]["validator"]["check_ids"], ["VC-TASK-001"])

            foreground_root = Path(raw) / "foreground-current"
            foreground_root.mkdir()
            foreground_repo = make_repo(foreground_root)
            foreground_state = OptimPlansState.initialize(foreground_repo, topic="Foreground Manifest", plan_hash="abc123")
            foreground = dict(current)
            foreground["source_base"] = git(foreground_repo, "rev-parse", "--verify", "HEAD")
            foreground["validator_worker"] = {
                "mode": "foreground",
                "platform": host_agent(),
                "prompt_protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
                "prompt_hash": validator_prompt_hash(),
                "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
            }
            foreground_record = foreground_state.persist_execution_manifest(foreground)
            self.assertEqual(foreground_record["manifest"]["validator_worker"]["mode"], "foreground")

            bad_host_root = Path(raw) / "bad-host-current"
            bad_host_root.mkdir()
            bad_host_repo = make_repo(bad_host_root)
            bad_host_state = OptimPlansState.initialize(bad_host_repo, topic="Bad Host Validator", plan_hash="abc123")
            bad_host = dict(current)
            bad_host["source_base"] = git(bad_host_repo, "rev-parse", "--verify", "HEAD")
            bad_host["validator_worker"] = {**self._host_validator(), "allowed_tools": ["Read", "Write"]}
            with self.assertRaisesRegex(ContractError, "read-only"):
                bad_host_state.persist_execution_manifest(bad_host)

            bad_root = Path(raw) / "bad-current"
            bad_root.mkdir()
            bad_repo = make_repo(bad_root)
            bad_state = OptimPlansState.initialize(bad_repo, topic="Bad Current Manifest", plan_hash="abc123")
            bad = dict(current)
            bad["source_base"] = git(bad_repo, "rev-parse", "--verify", "HEAD")
            del bad["validator_prompt"]
            with self.assertRaisesRegex(ContractError, "validator_prompt"):
                bad_state.persist_execution_manifest(bad)

    def test_prepare_execution_smokes_worker_before_manifest_is_write_once(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    print(json.dumps({'status': 'invalid'}))\n"
                "    raise SystemExit(0)\n"
                "print('{}')\n",
            )
            state = OptimPlansState.initialize(repo, topic="Smoke", plan_hash="abc123")
            worker_argv = [str(worker), "exec", "-C", str(raw_path / "run-worktree")]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": {
                    "adapter": "codex",
                    "argv": worker_argv,
                    "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"]},
                },
                "verification_argv": [sys.executable, "-c", "pass"],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "worker adapter smoke result status must be valid"):
                state.prepare_execution(manifest_path)

            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])

    def test_prepare_execution_dirty_source_asks_auto_commit_before_manifest(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Dirty Source", plan_hash="abc123")
            manifest_path = self._prepare_manifest_path(raw_path, repo, source_base=base)
            (repo / "src").mkdir()
            (repo / "src/app.txt").write_text("dirty\n", encoding="utf-8")

            question = state.prepare_execution(manifest_path)

            self.assertEqual(question["stage"], "source_auto_commit")
            self.assertEqual([option["id"] for option in question["options"]], ["approve", "stop", "other"])
            self.assertEqual(question["source_head"], base)
            self.assertIn("source_snapshot_fingerprint", question)
            self.assertNotIn("auto", [option["id"] for option in question["options"]])
            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])

    def test_prepare_execution_approved_source_snapshot_commit_rewrites_manifest_base(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Dirty Source", plan_hash="abc123")
            manifest_path = self._prepare_manifest_path(raw_path, repo, source_base=base)
            (repo / "src").mkdir()
            (repo / "src/app.txt").write_text("dirty\n", encoding="utf-8")

            question = state.prepare_execution(manifest_path)
            state.record_answer(question["nonce"], "approve")
            approval = state.prepare_execution(manifest_path)
            head = git(repo, "rev-parse", "--verify", "HEAD")
            events = state.replay().events
            manifest = next(event["payload"]["manifest"] for event in events if event["type"] == "execution_manifest_created")

            self.assertEqual(approval["stage"], "execution_launch")
            self.assertEqual(git(repo, "rev-list", "--count", f"{base}..HEAD"), "1")
            self.assertEqual([event["type"] for event in events].count("source_snapshot_committed"), 1)
            self.assertEqual(manifest["source_base"], head)
            self.assertEqual(manifest["base_commit"], head)
            self.assertEqual(approval["manifest"]["source_base"], head)
            self.assertEqual(git(repo, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_prepare_execution_declined_source_auto_commit_blocks_manifest(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Decline Source", plan_hash="abc123")
            manifest_path = self._prepare_manifest_path(raw_path, repo, source_base=base)
            (repo / "src").mkdir()
            (repo / "src/app.txt").write_text("dirty\n", encoding="utf-8")

            question = state.prepare_execution(manifest_path)
            state.record_answer(question["nonce"], "stop")
            with self.assertRaisesRegex(ContractError, "source auto-commit"):
                state.prepare_execution(manifest_path)

            self.assertEqual(git(repo, "rev-parse", "--verify", "HEAD"), base)
            self.assertNotIn(
                "source_snapshot_committed",
                [event["type"] for event in state.replay().events],
            )
            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])

    def test_prepare_execution_mutation_after_source_auto_commit_approval_reasks(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Changed Source", plan_hash="abc123")
            manifest_path = self._prepare_manifest_path(raw_path, repo, source_base=base)
            (repo / "src").mkdir()
            target = repo / "src/app.txt"
            target.write_text("first\n", encoding="utf-8")

            first = state.prepare_execution(manifest_path)
            state.record_answer(first["nonce"], "approve")
            target.write_text("second\n", encoding="utf-8")
            second = state.prepare_execution(manifest_path)

            self.assertEqual(second["stage"], "source_auto_commit")
            self.assertNotEqual(second["nonce"], first["nonce"])
            self.assertNotEqual(second["source_snapshot_fingerprint"], first["source_snapshot_fingerprint"])
            self.assertEqual(git(repo, "rev-parse", "--verify", "HEAD"), base)
            self.assertNotIn("execution_manifest_created", [event["type"] for event in state.replay().events])

    def test_concurrent_prepare_after_source_snapshot_does_not_persist_old_base(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state = OptimPlansState.initialize(repo, topic="Concurrent Source", plan_hash="abc123")
            manifest_path = self._prepare_manifest_path(raw_path, repo, source_base=base)
            (repo / "src").mkdir()
            (repo / "src/app.txt").write_text("dirty\n", encoding="utf-8")
            question = state.prepare_execution(manifest_path)
            state.record_answer(question["nonce"], "approve")
            script = (
                "import json, sys\n"
                "from pathlib import Path\n"
                "from scripts.optim_plans_core import OptimPlansState\n"
                "result = OptimPlansState.load_active(Path(sys.argv[1])).prepare_execution(Path(sys.argv[2]))\n"
                "print(json.dumps(result))\n"
            )

            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), str(manifest_path)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            results = [worker.communicate(timeout=10) for worker in workers]
            returncodes = [worker.returncode for worker in workers]
            events = state.replay().events
            manifests = [event["payload"]["manifest"] for event in events if event["type"] == "execution_manifest_created"]
            head = git(repo, "rev-parse", "--verify", "HEAD")

            self.assertEqual(returncodes.count(0), 1, results)
            self.assertEqual(len(manifests), 1)
            self.assertEqual([event["type"] for event in events].count("source_snapshot_committed"), 1)
            self.assertEqual(manifests[0]["source_base"], head)
            self.assertNotEqual(manifests[0]["source_base"], base)

    def test_launch_files_are_refreshed_before_smoke_cache_skip(self) -> None:
        from scripts.optim_plans_core import OptimPlansState, worker_launch_files

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            count_path = raw_path / "smoke-count.txt"
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    count = Path(os.environ['SMOKE_COUNT'])\n"
                "    value = int(count.read_text(encoding='utf-8') or '0') if count.exists() else 0\n"
                "    count.write_text(str(value + 1), encoding='utf-8')\n"
                "    print(json.dumps({'status': 'valid', 'evidence': 'adapter smoke'}))\n"
                "    raise SystemExit(0)\n"
                "print('{}')\n",
            )
            paths = worker_launch_files(repo)
            worker_argv = [str(worker), "exec", "-C", str(raw_path / "run-worktree")]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": {
                    "adapter": "codex",
                    "argv": worker_argv,
                    "env": {"CODEX_HOME": str(paths["codex_home"])},
                    "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"], "env": {"SMOKE_COUNT": str(count_path)}},
                },
                "verification_argv": [sys.executable, "-c", "pass"],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
            }
            state = OptimPlansState.initialize(repo, topic="Launch Refresh", plan_hash="abc123")
            state.persist_execution_manifest(manifest)
            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")

            codex_home = paths["codex_home"]
            (codex_home / "config.toml").write_text("tampered = true\n", encoding="utf-8")
            second_repo = raw_path / "repo-2"
            git(repo, "worktree", "add", "--detach", str(second_repo), "HEAD")
            second = OptimPlansState.initialize(second_repo, topic="Launch Refresh 2", plan_hash="abc123")
            second.persist_execution_manifest(manifest)

            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")
            self.assertTrue(codex_home.is_dir())
            self.assertFalse((codex_home / "config.toml").exists())

    def test_hash_only_config_files_are_verified_before_smoke_cache_skip(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState, worker_launch_files

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = self._worker(raw_path / "codex", "print('{}')\n")
            paths = worker_launch_files(repo)
            codex_home = paths["codex_home"]
            codex_home.mkdir(parents=True)
            config_file = codex_home / "config.toml"
            config_file.write_text('model = "gpt-test"\n', encoding="utf-8")
            digest = hashlib.sha256(config_file.read_bytes()).hexdigest()
            worker_argv = [str(worker), "exec", "-C", str(raw_path / "run-worktree")]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": {
                    "adapter": "codex",
                    "argv": worker_argv,
                    "env": {"CODEX_HOME": str(codex_home)},
                    "config_files": [{"path": str(config_file), "sha256": digest}],
                    "smoke": {"argv": [*worker_argv, "--optim-plans-smoke"]},
                },
                "verification_argv": [sys.executable, "-c", "pass"],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
            }
            state = OptimPlansState.initialize(repo, topic="Hashed Config", plan_hash="abc123")
            state.persist_execution_manifest(manifest)
            self.assertEqual(config_file.read_text(encoding="utf-8"), 'model = "gpt-test"\n')

            config_file.write_text("tampered = true\n", encoding="utf-8")
            second_repo = raw_path / "repo-2"
            git(repo, "worktree", "add", "--detach", str(second_repo), "HEAD")
            second = OptimPlansState.initialize(second_repo, topic="Hashed Config 2", plan_hash="abc123")
            with self.assertRaisesRegex(ContractError, "sha256 mismatch"):
                second.persist_execution_manifest(manifest)

    def test_worker_launch_file_config_rejects_controller_state_collision(self) -> None:
        from scripts.optim_plans_core import save_optim_plans_config_value, worker_launch_files

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            config_path = repo / ".git" / "optim-plans" / "config.json"
            save_optim_plans_config_value(repo, "worker_launch_files", {"codex_home": str(config_path)})
            with self.assertRaisesRegex(Exception, "worker_launch_files.codex_home"):
                worker_launch_files(repo)

    def test_successful_smoke_is_cached_for_matching_later_manifest(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            count_path = raw_path / "smoke-count.txt"
            fail_path = raw_path / "fail-smoke"
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    count = Path(os.environ['SMOKE_COUNT'])\n"
                "    value = int(count.read_text(encoding='utf-8') or '0') if count.exists() else 0\n"
                "    count.write_text(str(value + 1), encoding='utf-8')\n"
                "    status = 'invalid' if Path(os.environ['SMOKE_FAIL']).exists() else 'valid'\n"
                "    print(json.dumps({'status': status, 'evidence': 'adapter smoke'}))\n"
                "    raise SystemExit(0)\n"
                "print('{}')\n",
            )
            worker_argv = [str(worker), "exec", "-C", str(raw_path / "run-worktree")]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": {
                    "adapter": "codex",
                    "argv": worker_argv,
                    "smoke": {
                        "argv": [*worker_argv, "--optim-plans-smoke"],
                        "env": {"SMOKE_COUNT": str(count_path), "SMOKE_FAIL": str(fail_path)},
                    },
                },
                "verification_argv": [sys.executable, "-c", "pass"],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/app.txt"]}],
            }
            state = OptimPlansState.initialize(repo, topic="Smoke Cache", plan_hash="abc123")
            state.persist_execution_manifest(manifest)
            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")
            config_path = repo / ".git" / "optim-plans" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["smoke_tested_workers"][0]["smoke"]["argv"], [*worker_argv, "--optim-plans-smoke"])

            second_repo = raw_path / "repo-2"
            git(repo, "worktree", "add", "--detach", str(second_repo), "HEAD")
            fail_path.write_text("fail\n", encoding="utf-8")
            second = OptimPlansState.initialize(second_repo, topic="Smoke Cache 2", plan_hash="abc123")
            second.persist_execution_manifest(manifest)

            self.assertEqual(count_path.read_text(encoding="utf-8"), "1")

    def test_execution_approval_consumption_is_events_backed_and_single_use(self) -> None:
        import subprocess
        import sys

        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Approval", plan_hash="abc123")
            record = state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "items": [{"id": "TASK-001"}],
                }
            )
            question = state.request_execution_approval()
            self.assertEqual([option["id"] for option in question["options"]], ["approve", "stop", "other"])
            self.assertEqual(question["manifest_hash"], record["manifest_hash"])
            self.assertEqual(question["manifest"], record["manifest"])
            state.record_answer(question["nonce"], "approve")

            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "from scripts.optim_plans_core import OptimPlansState\n"
                "OptimPlansState.load_active(Path(sys.argv[1])).start_execution(sys.argv[2])\n"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), question["nonce"]],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(2)
            ]
            self.assertEqual(sorted(worker.wait(timeout=10) for worker in workers), [0, 1])
            starts = [event for event in state.replay().events if event["type"] == "execution_started"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["payload"]["approval_nonce"], question["nonce"])
            self.assertEqual(starts[0]["payload"]["source_base"], record["manifest"]["source_base"])
            self.assertTrue(starts[0]["payload"]["source_clean"])

    def test_start_execution_requires_source_base_commit(self) -> None:
        import subprocess

        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state = OptimPlansState.initialize(repo, topic="No Commit", plan_hash="abc123")
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": "missing",
                    "integration_destination": "main",
                    "items": [{"id": "TASK-001"}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            with self.assertRaises(ContractError):
                state.start_execution(question["nonce"])

    def test_start_execution_ignores_controller_artifacts_when_checking_source_cleanliness(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Artifact", plan_hash="abc123")
            (state.artifact_dir / "PLAN_v5.md").write_text("# plan\n", encoding="utf-8")
            (state.artifact_dir / 'quoted "plan".md').write_text("# quoted\n", encoding="utf-8")
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "items": [{"id": "TASK-001"}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            self.assertTrue(state.start_execution(question["nonce"])["source_clean"])

    def test_run_worktree_checkpoint_uses_git_identity_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            git(repo, "config", "user.name", "Ada User")
            git(repo, "config", "user.email", "ada@example.invalid")
            (repo / "src").mkdir()
            (repo / "src/app.py").write_text("v1\n", encoding="utf-8")
            git(repo, "add", "src/app.py")
            git(repo, "commit", "-m", "add app")
            source_head = git(repo, "rev-parse", "--verify", "HEAD")

            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/app.py"]}]
            )
            self.assertNotEqual(run_worktree.resolve(), repo.resolve())
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), source_head)

            state.begin_item("TASK-001")
            (run_worktree / "src/app.py").write_text("v2\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            checkpoint = self._checkpoint_after_summary_choice(state)

            self.assertEqual(git(repo, "rev-parse", "--verify", "HEAD"), source_head)
            self.assertEqual((repo / "src/app.py").read_text(encoding="utf-8"), "v1\n")
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])
            metadata = git(
                run_worktree,
                "show",
                "-s",
                "--format=%an <%ae>%n%cn <%ce>%n%aI%n%cI",
                checkpoint["commit"],
            ).splitlines()
            self.assertEqual(metadata[0], "Ada User <ada@example.invalid>")
            self.assertEqual(metadata[1], "Ada User <ada@example.invalid>")
            self.assertFalse(metadata[2].startswith("2000-01-01T00:00:00"))
            self.assertFalse(metadata[3].startswith("2000-01-01T00:00:00"))
            self.assertEqual(git(repo, "status", "--porcelain=v1"), "")
            self.assertEqual(
                [event["type"] for event in state.replay().events[-5:]],
                [
                    "item_verified",
                    "checkpoint_prepared",
                    "pending_question",
                    "answer_recorded",
                    "checkpoint_created",
                ],
            )

    def test_checkpoint_commit_subject_uses_manifest_precedence_and_normalization(self) -> None:
        cases = [
            (
                "commit_message",
                {
                    "commit_message": "\n  Ship\t compact   subject  \nignored",
                    "summary": "Use summary",
                    "description": "Use description",
                },
                "Ship compact subject",
            ),
            (
                "invalid_commit_message_uses_summary",
                {
                    "commit_message": "bad\0subject",
                    "summary": "Use summary",
                    "description": "Use description",
                },
                "Use summary",
            ),
            (
                "invalid_summary_uses_description",
                {
                    "commit_message": " \n\t ",
                    "summary": "bad\x01summary",
                    "description": "Use description",
                },
                "Use description",
            ),
            ("non_string_uses_summary", {"commit_message": 123, "summary": "Use summary"}, "Use summary"),
            ("control_character_uses_summary", {"commit_message": "bad\x7fsubject", "summary": "Use summary"}, "Use summary"),
        ]
        for label, item, expected in cases:
            with self.subTest(label=label):
                run_id, message, _changed_files = self._checkpoint_message_for_item(item)
                self.assertEqual(message.splitlines()[0], expected)
                self.assertIn(f"optim-plans run: {run_id}", message)
                self.assertIn("item: TASK-001", message)
                self.assertIn("attempt: 1", message)
                self.assertNotIn("optim-plans checkpoint", message.splitlines()[0])

    def test_checkpoint_commit_subject_falls_back_for_invalid_text_and_path_counts(self) -> None:
        cases = [
            ("one_path", {}, {"src/one.txt": "one\n"}, "Update src/one.txt", ["src/one.txt"]),
            (
                "multiple_paths",
                {},
                {"src/one.txt": "one\n", "src/two.txt": "two\n"},
                "Update 2 files",
                ["src/one.txt", "src/two.txt"],
            ),
            ("empty_checkpoint", {}, {}, "Record empty checkpoint", []),
            (
                "newline_path",
                {},
                {"src/new\nline.txt": "done\n"},
                "Update src/new line.txt",
                ["src/new\nline.txt"],
            ),
            (
                "invalid_manifest_text",
                {"commit_message": "\0", "summary": "\x02", "description": 123},
                {"src/fallback.txt": "done\n"},
                "Update src/fallback.txt",
                ["src/fallback.txt"],
            ),
        ]
        for label, item, writes, expected_subject, expected_changed in cases:
            with self.subTest(label=label):
                _run_id, message, changed_files = self._checkpoint_message_for_item(item, writes)
                self.assertEqual(message.splitlines()[0], expected_subject)
                self.assertEqual(changed_files, expected_changed)
                self.assertNotRegex(message.splitlines()[0], r"[\r\n\x00-\x08\x0b-\x1f\x7f]")

    def test_dependent_item_waits_for_checkpoint_in_same_worktree(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            git(repo, "add", "src")
            state, run_worktree = self._start_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/dep.txt"]},
                    {"id": "TASK-002", "depends_on": ["TASK-001"], "allowed_paths": ["src/use.txt"]},
                ],
            )

            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            (run_worktree / "src/dep.txt").write_text("from task 1\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")

            self._checkpoint_after_summary_choice(state)
            assignment = state.begin_item("TASK-002")
            self.assertEqual(Path(assignment["run_worktree"]), run_worktree)
            self.assertEqual((run_worktree / "src/dep.txt").read_text(encoding="utf-8"), "from task 1\n")

    def test_independent_items_follow_stable_serial_manifest_order(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["first.txt"]},
                    {"id": "TASK-002", "allowed_paths": ["second.txt"]},
                ],
            )
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            state.begin_item("TASK-001")
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            (run_worktree / "first.txt").write_text("first\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            self._checkpoint_after_summary_choice(state)
            self.assertEqual(state.begin_item("TASK-002")["item_id"], "TASK-002")

    def test_failed_attempt_blocks_dependents_until_auto_retry_restore(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            git(repo, "add", "src")
            state, run_worktree = self._start_execution(
                repo,
                [
                    {"id": "TASK-001", "allowed_paths": ["src/task.txt"]},
                    {"id": "TASK-002", "depends_on": ["TASK-001"], "allowed_paths": ["src/use.txt"]},
                ],
            )

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            dirty = run_worktree / "src/task.txt"
            dirty.write_text("bad attempt\n", encoding="utf-8")
            state.record_worker_failure("TASK-001", evidence="worker failed")
            self.assertEqual(state.replay().status, "executing")
            self.assertFalse(dirty.exists())
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            with self.assertRaises(ContractError):
                state.restore_retry("TASK-001", None)

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            dirty.write_text("good attempt\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            self._checkpoint_after_summary_choice(state)
            self.assertEqual(state.begin_item("TASK-002")["item_id"], "TASK-002")

    def test_repeated_failure_at_same_checkpoint_gets_fresh_retry_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/task.txt"]}]
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            target = run_worktree / "src/task.txt"
            target.write_text("first failure\n", encoding="utf-8")
            state.record_attempt_failure("audit_failed", "TASK-001", evidence="first failure", retryable=False)
            first_finish = state.request_finish_approval()
            state.record_answer(first_finish["nonce"], "failed")
            first = state.request_retry("TASK-001")
            state.record_answer(first["nonce"], "approve")
            state.restore_retry("TASK-001", first["nonce"])

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            target.write_text("second failure\n", encoding="utf-8")
            state.record_attempt_failure("audit_failed", "TASK-001", evidence="second failure", retryable=False)
            second = state.request_retry("TASK-001")
            second_finish = state.request_finish_approval()

            self.assertNotEqual(second["nonce"], first["nonce"])
            self.assertNotEqual(second_finish["nonce"], first_finish["nonce"])

    def test_item_start_requires_clean_latest_checkpoint(self) -> None:
        from scripts.optim_plans_core import ContractError

        for mutation in ("dirty", "commit"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                state, run_worktree = self._start_execution(
                    repo, [{"id": "TASK-001", "allowed_paths": ["src/task.txt"]}]
                )
                (run_worktree / "src").mkdir()
                (run_worktree / "src/task.txt").write_text("unexpected\n", encoding="utf-8")
                if mutation == "commit":
                    git(run_worktree, "add", "src/task.txt")
                    git(run_worktree, "commit", "-m", "unauthorized")
                with self.assertRaises(ContractError):
                    state.begin_item("TASK-001")

    def test_retry_refuses_destructive_cleanup_after_worktree_path_swap(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state, run_worktree = self._start_execution(
                repo, [{"id": "TASK-001", "allowed_paths": ["src/task.txt"]}]
            )
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/task.txt").write_text("failed\n", encoding="utf-8")
            state.record_attempt_failure("audit_failed", "TASK-001", evidence="manual audit failure", retryable=False)
            retry = state.request_retry("TASK-001")
            state.record_answer(retry["nonce"], "approve")

            displaced = run_worktree.with_name(f"{run_worktree.name}-displaced")
            run_worktree.rename(displaced)
            run_worktree.mkdir()
            git(run_worktree, "init", "-b", "main")
            sentinel = run_worktree / "keep.txt"
            sentinel.write_text("foreign data\n", encoding="utf-8")

            with self.assertRaises(ContractError):
                state.restore_retry("TASK-001", retry["nonce"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign data\n")


if __name__ == "__main__":
    unittest.main()
