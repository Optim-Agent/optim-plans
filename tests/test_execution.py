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
from pathlib import Path

from helpers import git, make_executable, make_repo


class ExecutionTests(unittest.TestCase):
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
    ):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Adapter Execution", plan_hash="abc123")
        run_worktree = state.root / "run-worktrees" / state.run_id
        argv = [str(worker), "exec", "-C", str(run_worktree)]
        smoke_argv = [*argv, "--optim-plans-smoke"]
        state.persist_execution_manifest(
            {
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
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        state.start_execution(question["nonce"])
        return state, run_worktree

    def _start_execution(self, repo: Path, items: list[dict[str, object]]):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Execution", plan_hash="abc123")
        state.persist_execution_manifest(
            {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "items": items,
            }
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        started = state.start_execution(question["nonce"])
        return state, Path(started["run_worktree"])

    def _finish_nonce(self, state, outcome: str) -> str:
        question = state.request_finish_approval()
        choices = {option["id"] for option in question["options"]}
        if outcome in choices:
            return question["nonce"]
        self.fail(f"missing finish approval question for {outcome}")

    def _checkpoint_one_item(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        state.begin_item("TASK-001")
        target = run_worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")
        state.record_worker_completion("TASK-001", evidence="worker finished")
        return state.checkpoint_item("TASK-001", evidence="unit ok")["commit"]

    def _verify_one_item(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        commit = self._checkpoint_one_item(state, run_worktree, path)
        state.final_audit()
        return commit

    def _enter_manual_finish_state(self, state, run_worktree: Path, path: str = "src/done.txt") -> str:
        checkpoint = self._checkpoint_one_item(state, run_worktree, path)
        state.append_event("final_audit_passed", {"status": "passed", "final_commit": checkpoint, "changed_files": [path]})
        state.append_event("awaiting_integration", {"final_checkpoint": checkpoint})
        return checkpoint

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

    def test_run_item_launches_manifest_adapter_and_verifier_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            argv_log = raw_path / "argv.json"
            sentinel = raw_path / "shell-expanded"
            worker = self._worker(
                raw_path / "codex",
                "import json, os, sys\n"
                "from pathlib import Path\n"
                f"Path({str(argv_log)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/app.txt').write_text('ok\\n', encoding='utf-8')\n"
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
            )
            checkpoint = state.run_item("TASK-001")

            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])
            self.assertEqual(
                git(run_worktree, "log", "-1", "--format=%s"),
                f"optim-plans checkpoint {state.run_id} TASK-001 attempt 1",
            )
            self.assertFalse(sentinel.exists())
            self.assertFalse(any("schema" in arg for arg in json.loads(argv_log.read_text(encoding="utf-8"))))
            self.assertEqual(
                [event["type"] for event in state.replay().events[-6:]],
                [
                    "item_started",
                    "worker_completed",
                    "item_verified",
                    "checkpoint_created",
                    "final_audit_passed",
                    "run_finished",
                ],
            )
            self.assertFalse(any(event.get("payload", {}).get("stage") == "finish_run" for event in state.replay().events))

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
            self.assertIn("awaiting_retry_decision", event_types)
            self.assertNotIn("item_verified", event_types)
            self.assertNotIn("checkpoint_created", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") in {"execution_retry", "finish_run"} for event in events))
            self.assertTrue((run_worktree / "src/app.txt").exists())

    def test_verifier_failures_preserve_worktree_without_checkpoint(self) -> None:
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
                self.assertIn("awaiting_retry_decision", event_types)
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
            state.checkpoint_item("TASK-001", evidence="unit ok")

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

    def test_all_verified_auto_keeps_and_releases_active(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
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
            self.assertEqual(finished["outcome"], "kept")
            self.assertEqual(finished["final_checkpoint"], checkpoint)
            self.assertEqual(state.replay().status, "completed")
            self.assertFalse(state.active_file.exists())
            self.assertTrue(run_worktree.is_dir())
            self.assertEqual(git(repo, "rev-parse", "--verify", finished["run_branch"]), checkpoint)
            second = OptimPlansState.initialize(repo, topic="Second", plan_hash="def456")
            self.assertTrue(second.active_file.exists())

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
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            (run_worktree / "src/done.txt").write_text("failed attempt\n", encoding="utf-8")
            state.record_worker_failure("TASK-001", evidence="worker failed")
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
                state.begin_item("TASK-001")
                (run_worktree / "src").mkdir()
                (run_worktree / "src/done.txt").write_text("failed attempt\n", encoding="utf-8")
                state.record_worker_failure("TASK-001", evidence="worker failed visibly")
                nonce = state.request_finish_approval()["nonce"]
                state.record_answer(nonce, outcome)

                finished = state.finish_run(outcome, approval_nonce=nonce)

                self.assertEqual(state.replay().status, expected_status)
                self.assertEqual(finished["outcome"], outcome)
                self.assertEqual(finished["failure_event_type"], "worker_failed")
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
            OptimPlansState,
            execution_manifest_hash,
            json_text,
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

    def test_run_worktree_checkpoint_uses_fixed_identity_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
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
            checkpoint = state.checkpoint_item("TASK-001", evidence="unit ok")

            self.assertEqual(git(repo, "rev-parse", "--verify", "HEAD"), source_head)
            self.assertEqual((repo / "src/app.py").read_text(encoding="utf-8"), "v1\n")
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), checkpoint["commit"])
            self.assertEqual(
                git(run_worktree, "show", "-s", "--format=%an <%ae>", checkpoint["commit"]),
                "Optim Plans <optim-plans@example.invalid>",
            )
            self.assertEqual(git(repo, "status", "--porcelain=v1"), "")
            self.assertEqual(
                [event["type"] for event in state.replay().events[-4:]],
                ["item_started", "worker_completed", "item_verified", "checkpoint_created"],
            )

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

            state.checkpoint_item("TASK-001", evidence="unit ok")
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
            state.checkpoint_item("TASK-001", evidence="unit ok")
            self.assertEqual(state.begin_item("TASK-002")["item_id"], "TASK-002")

    def test_failed_attempt_blocks_dependents_until_single_use_retry_restore(self) -> None:
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
            self.assertTrue(dirty.exists())
            with self.assertRaises(ContractError):
                state.begin_item("TASK-002")
            with self.assertRaises(ContractError):
                state.restore_retry("TASK-001", "missing")

            restored = state.restore_retry("TASK-001", None)
            self.assertTrue(restored["auto_approved"])
            self.assertFalse(dirty.exists())

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            dirty.write_text("second bad attempt\n", encoding="utf-8")
            state.record_worker_failure("TASK-001", evidence="worker failed again")
            with self.assertRaises(ContractError):
                state.restore_retry("TASK-001", None)

            retry = state.request_retry("TASK-001")
            state.record_answer(retry["nonce"], "approve")
            state.restore_retry("TASK-001", retry["nonce"])
            self.assertFalse(dirty.exists())

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            dirty.write_text("good attempt\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            state.checkpoint_item("TASK-001", evidence="unit ok")
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
            state.record_worker_failure("TASK-001", evidence="first failure")
            first_finish = state.request_finish_approval()
            state.record_answer(first_finish["nonce"], "failed")
            first = state.request_retry("TASK-001")
            state.record_answer(first["nonce"], "approve")
            state.restore_retry("TASK-001", first["nonce"])

            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir(exist_ok=True)
            target.write_text("second failure\n", encoding="utf-8")
            state.record_worker_failure("TASK-001", evidence="second failure")
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
            state.record_worker_failure("TASK-001", evidence="worker failed")
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
