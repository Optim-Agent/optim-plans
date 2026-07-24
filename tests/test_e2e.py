from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import git, make_executable, make_repo


ROOT = Path(__file__).resolve().parents[1]


class E2ETests(unittest.TestCase):
    def test_cli_init_question_answer_and_report_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "E2E Plan"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            payload = json.loads(init.stdout)
            artifact_dir = repo / payload["artifact_dir"]
            self.assertTrue(artifact_dir.is_dir())
            self.assertFalse((artifact_dir / "BRAINSTORM.md").exists())
            question = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "ask", "--repo", str(repo), "--prompt", "Choose reviewer"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            q = json.loads(question.stdout)
            self.assertIsInstance(q["expected_seq"], int)
            self.assertEqual(q["recommended_option_id"], "foreground")
            self.assertEqual([option["id"] for option in q["options"]], ["foreground", "reviewer", "criticizer", "other", "auto"])
            self.assertEqual(q["plan_level"]["name"], "plan")
            self.assertEqual(q["plan_level"]["min_questions"], 1)
            self.assertEqual(q["plan_level"]["max_questions"], 5)
            self.assertEqual(q["plan_level"]["max_refinement_rounds"], 3)
            self.assertEqual(q["plan_level"]["refinement_timeout_seconds"], 600)
            answer = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "answer", "--repo", str(repo), "--nonce", q["nonce"], "--choice", q["options"][0]["id"]],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(json.loads(answer.stdout)["choice"], q["options"][0]["id"])

    def test_cli_ask_emits_plan_level_and_rejects_unknown_level(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Plan Level"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            question = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "ask",
                    "--repo",
                    str(repo),
                    "--prompt",
                    "Choose reviewer",
                    "--plan-level",
                    "big-plan",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(
                json.loads(question.stdout)["plan_level"],
                {
                    "name": "big-plan",
                    "min_questions": 5,
                    "max_questions": 10,
                    "min_refinement_rounds": 0,
                    "max_refinement_rounds": 5,
                    "refinement_timeout_seconds": 1800,
                    "max_refinement_comments_or_questions": 5,
                    "direct_execution_option": False,
                    "high_priority_only": True,
                    "websearch_required_in": ["brainstorming"],
                },
            )
            invalid = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "ask",
                    "--repo",
                    str(repo),
                    "--prompt",
                    "Choose reviewer",
                    "--plan-level",
                    "mega-plan",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("unknown plan level", invalid.stderr)

    def test_cli_ask_agent_choice_stage_offers_background_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Agent Choice"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            question = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "ask",
                    "--repo",
                    str(repo),
                    "--prompt",
                    "Choose agent",
                    "--stage",
                    "agent-choice",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            q = json.loads(question.stdout)
            self.assertEqual(q["recommended_option_id"], "foreground")
            self.assertEqual([option["id"] for option in q["options"]], ["foreground", "background", "other", "auto"])
            self.assertEqual(q["options"][1]["label"], "Delegated foreground run")
            self.assertIn("standalone sub-agent", q["options"][1]["reason"])

    def test_cli_ask_background_model_stage_offers_model_effort_choices(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Model Choice"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            question = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "ask",
                    "--repo",
                    str(repo),
                    "--prompt",
                    "Choose model",
                    "--stage",
                    "background-model",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            q = json.loads(question.stdout)
            self.assertEqual(
                [option["id"] for option in q["options"]],
                ["codex-default", "codex-manual", "claude-default", "claude-manual", "other", "auto"],
            )
            self.assertIn("model", q["options"][0]["reason"])
            self.assertIn("effort", q["options"][1]["reason"])

    def test_cli_ask_mini_plan_recommends_execution_skip_option(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Mini Plan"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            question = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "ask",
                    "--repo",
                    str(repo),
                    "--prompt",
                    "Choose refinement",
                    "--plan-level",
                    "mini-plan",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            q = json.loads(question.stdout)
            self.assertEqual(q["recommended_option_id"], "skip-refinement-execute")
            self.assertEqual(
                [option["id"] for option in q["options"]],
                ["skip-refinement-execute", "foreground", "reviewer", "criticizer", "other"],
            )
            self.assertEqual(q["options"][0]["label"], "Skip refinement and execute")
            self.assertIn("explicit execution launch approval", q["options"][0]["reason"])
            self.assertEqual(q["options"][1]["label"], "Current foreground session")
            self.assertTrue(q["plan_level"]["direct_execution_option"])

    def test_cli_run_worker_fails_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            worker = raw_path / "worker.py"
            sentinel = raw_path / "launched"
            worker.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[1]).write_text('launched', encoding='utf-8')\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "run-worker",
                    "--repo",
                    str(repo),
                    "--item-id",
                    "TASK-001",
                    "--scope",
                    "README.md",
                    "--worker-command",
                    f"{sys.executable} {worker} {sentinel}",
                    "--timeout-seconds",
                    "5",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("new manifest flow", result.stderr)
            self.assertFalse(sentinel.exists())
            help_result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "run-worker", "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotIn("--worker-command", help_result.stdout + help_result.stderr)

    def test_cli_run_item_uses_manifest_adapter_and_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            run_worktree = raw_path / "run-worktree"
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "from pathlib import Path\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/cli-run-item.txt').write_text('ok\\n', encoding='utf-8')\n"
                "Path(os.environ['OPTIM_PLANS_RESULT_PATH']).write_text(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'verified',\n"
                "    'evidence': 'worker done',\n"
                "}), encoding='utf-8')\n",
            )
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Run Item"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            run_id = json.loads(init.stdout)["run_id"]
            schema = repo / ".git/optim-plans/runs" / run_id / "executor-config/schema.json"
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": {
                    "adapter": "codex",
                    "argv": [str(worker), "exec", "-C", str(run_worktree), "--output-schema", str(schema)],
                    "timeout_seconds": 5,
                },
                "verification_argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/cli-run-item.txt').read_text() == 'ok\\n'",
                ],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/cli-run-item.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "prepare-execution",
                    "--repo",
                    str(repo),
                    "--manifest",
                    str(manifest_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            nonce = json.loads(prepared.stdout)["nonce"]
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "answer",
                    "--repo",
                    str(repo),
                    "--nonce",
                    nonce,
                    "--choice",
                    "approve",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "start-execution",
                    "--repo",
                    str(repo),
                    "--approval-nonce",
                    nonce,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            run_item = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "run-item",
                    "--repo",
                    str(repo),
                    "--item-id",
                    "TASK-001",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), json.loads(run_item.stdout)["commit"])

    def test_cli_execution_manifest_approval_and_start_flow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Execution Flow"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "items": [{"id": "TASK-001", "allowed_paths": ["src/cli.txt"]}],
            }
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "prepare-execution",
                    "--repo",
                    str(repo),
                    "--manifest",
                    str(manifest_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            q = json.loads(prepared.stdout)
            self.assertEqual(q["manifest"]["plan_hash"], manifest["plan_hash"])
            self.assertEqual(q["manifest"]["source_base"], manifest["source_base"])
            self.assertEqual(q["manifest"]["items"], [{"id": "TASK-001", "allowed_paths": ["src/cli.txt"], "depends_on": []}])
            self.assertEqual([option["id"] for option in q["options"]], ["approve", "stop", "other"])
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "answer",
                    "--repo",
                    str(repo),
                    "--nonce",
                    q["nonce"],
                    "--choice",
                    "approve",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            started = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "start-execution",
                    "--repo",
                    str(repo),
                    "--approval-nonce",
                    q["nonce"],
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            started_payload = json.loads(started.stdout)
            self.assertEqual(started_payload["manifest_hash"], q["manifest_hash"])
            self.assertTrue(Path(started_payload["run_worktree"]).is_dir())

            help_result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertNotIn("checkpoint-item", help_result.stdout)
            self.assertNotIn("complete-item", help_result.stdout)

    def test_cli_lifecycle_rejects_invalid_states_before_mutation_and_finishes_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            run_worktree = raw_path / "run-worktree"
            sentinel = raw_path / "launched"
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('launched', encoding='utf-8')\n"
                "Path('src').mkdir(exist_ok=True)\n"
                "Path('src/cli-life.txt').write_text('ok\\n', encoding='utf-8')\n"
                "Path(os.environ['OPTIM_PLANS_RESULT_PATH']).write_text(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': 'worker done',\n"
                "}), encoding='utf-8')\n",
            )
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Lifecycle"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            run_id = json.loads(init.stdout)["run_id"]
            schema = repo / ".git/optim-plans/runs" / run_id / "executor-config/schema.json"
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": {
                    "adapter": "codex",
                    "argv": [str(worker), "exec", "-C", str(run_worktree), "--output-schema", str(schema)],
                    "timeout_seconds": 5,
                },
                "verification_argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/cli-life.txt').read_text() == 'ok\\n'",
                ],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/cli-life.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "prepare-execution",
                    "--repo",
                    str(repo),
                    "--manifest",
                    str(manifest_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            nonce = json.loads(prepared.stdout)["nonce"]

            premature = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "run-item",
                    "--repo",
                    str(repo),
                    "--item-id",
                    "TASK-001",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(premature.returncode, 2)
            self.assertFalse(schema.exists())
            self.assertFalse(sentinel.exists())

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "answer",
                    "--repo",
                    str(repo),
                    "--nonce",
                    nonce,
                    "--choice",
                    "approve",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "start-execution",
                    "--repo",
                    str(repo),
                    "--approval-nonce",
                    nonce,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            too_early_finish = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "finish-run",
                    "--repo",
                    str(repo),
                    "--outcome",
                    "kept",
                    "--approval-nonce",
                    nonce,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(too_early_finish.returncode, 2)

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "run-item",
                    "--repo",
                    str(repo),
                    "--item-id",
                    "TASK-001",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            status = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "status", "--repo", str(repo)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["status"], "awaiting_integration")

            from scripts.optim_plans_core import OptimPlansState

            state = OptimPlansState.load_active(repo)
            finish_nonce = next(
                event["payload"]["nonce"]
                for event in state.replay().events
                if event["type"] == "pending_question" and event["payload"].get("stage") == "finish_run"
            )
            self.assertEqual(status_payload["finish_approval_nonce"], finish_nonce)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "answer",
                    "--repo",
                    str(repo),
                    "--nonce",
                    finish_nonce,
                    "--choice",
                    "kept",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "finish-run",
                    "--repo",
                    str(repo),
                    "--outcome",
                    "kept",
                    "--approval-nonce",
                    finish_nonce,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            second = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Second"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("run_id", json.loads(second.stdout))

            help_result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("retry-item", help_result.stdout)
            self.assertIn("finish-run", help_result.stdout)
            self.assertNotIn("restore-retry", help_result.stdout)
            self.assertNotIn("final-audit", help_result.stdout)

    def test_status_reports_legacy_active_run_without_deleting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            from scripts.optim_plans_core import OptimPlansState

            state = OptimPlansState.initialize(repo, topic="Legacy", plan_hash="abc123")
            state.append_event("worker_result_recorded", {"status": "verified", "evidence": "old run"})

            status = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "status", "--repo", str(repo)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            payload = json.loads(status.stdout)
            self.assertTrue(payload["legacy_active"])
            self.assertEqual(payload["status"], "legacy_active")
            self.assertIn("finish-run", payload["finalization"])
            self.assertTrue(state.active_file.exists())


if __name__ == "__main__":
    unittest.main()
