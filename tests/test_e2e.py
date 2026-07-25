from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import git, make_executable, make_repo


ROOT = Path(__file__).resolve().parents[1]


def controller_json(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/optim_plans.py"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def init_controller(repo: Path, topic: str) -> None:
    controller_json("init", "--repo", str(repo), "--topic", topic)


def ask_agent_choice(repo: Path, prompt: str = "Choose agent") -> dict[str, Any]:
    return controller_json("ask", "--repo", str(repo), "--prompt", prompt, "--stage", "agent-choice")


def answer_choice(repo: Path, nonce: str, choice: str) -> dict[str, Any]:
    return controller_json("answer", "--repo", str(repo), "--nonce", nonce, "--choice", choice)


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
            self.assertEqual(q["recommended_option_id"], "reviewer")
            self.assertEqual([option["id"] for option in q["options"]], ["reviewer", "criticizer", "skip-refinement-execute", "auto"])
            self.assertEqual(q["options"][2]["label"], "Jump to executor")
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

    def test_cli_jump_to_executor_answer_directly_approves_execution_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            run_worktree = raw_path / "run-worktree"
            init_controller(repo, "Jump To Executor")
            q = controller_json("ask", "--repo", str(repo), "--prompt", "Choose refinement")
            answer_choice(repo, q["nonce"], "skip-refinement-execute")
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "items": [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            prepared = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))

            started = controller_json("start-execution", "--repo", str(repo), "--approval-nonce", prepared["nonce"])

            self.assertEqual(started["approval_nonce"], prepared["nonce"])
            self.assertTrue(run_worktree.is_dir())

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
            init_controller(repo, "Agent Choice")
            q = ask_agent_choice(repo)
            self.assertEqual(q["recommended_option_id"], "background")
            self.assertEqual([option["id"] for option in q["options"]], ["background", "foreground", "other", "auto"])
            self.assertEqual(q["stage"], "agent-choice")
            self.assertEqual(q["options"][0]["label"], "Delegated foreground run")
            self.assertIn("standalone sub-agent", q["options"][0]["reason"])

    def test_cli_ask_agent_choice_defaults_from_first_background_or_foreground_answer(self) -> None:
        for choice in ("background", "foreground"):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                init_controller(repo, f"Agent Choice {choice}")
                q1 = ask_agent_choice(repo)
                answer_choice(repo, q1["nonce"], choice)
                q2 = ask_agent_choice(repo, "Choose agent again")

                self.assertEqual(q2["choice"], choice)
                self.assertNotIn("options", q2)
                self.assertNotEqual(q2["nonce"], q1["nonce"])

    def test_cli_ask_agent_choice_auto_defaults_to_recommended_background(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Agent Choice Auto")
            q1 = ask_agent_choice(repo)
            answer_choice(repo, q1["nonce"], "auto")

            self.assertEqual(ask_agent_choice(repo, "Choose agent again")["choice"], "background")

    def test_cli_ask_agent_choice_other_does_not_default_next_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Agent Choice Other")
            q1 = ask_agent_choice(repo)
            answer_choice(repo, q1["nonce"], "other")
            q2 = ask_agent_choice(repo, "Choose agent again")

            self.assertEqual(q2["recommended_option_id"], "background")
            self.assertEqual([option["id"] for option in q2["options"]], ["background", "foreground", "other", "auto"])
            self.assertEqual(q2["stage"], "agent-choice")

    def test_cli_ask_agent_choice_default_records_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Agent Choice Metadata")
            q1 = ask_agent_choice(repo)
            answer_choice(repo, q1["nonce"], "background")
            q2 = ask_agent_choice(repo, "Choose agent again")

            from scripts.optim_plans_core import OptimPlansState

            events = OptimPlansState.load_active(repo).replay().events
            default_events = [event for event in events if event["type"] == "agent_choice_default_applied"]
            self.assertEqual(len(default_events), 1)
            self.assertEqual(
                default_events[0]["payload"],
                {"defaulted_nonce": q2["nonce"], "source_nonce": q1["nonce"], "choice": "background"},
            )
            self.assertTrue(
                any(
                    event["type"] == "answer_recorded"
                    and event["payload"] == {"nonce": q2["nonce"], "choice": "background"}
                    for event in events
                )
            )

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
                ["codex-default", "codex-manual", "other", "auto"],
            )
            self.assertIn("model", q["options"][0]["reason"])
            self.assertIn("effort", q["options"][1]["reason"])

    def test_background_model_stage_defaults_to_same_platform_worker(self) -> None:
        from scripts.optim_plans import _background_model_options

        recommended, alternatives = _background_model_options(env={"PATH": "", "CLAUDE_PLUGIN_ROOT": "/tmp/plugin"})
        self.assertEqual(recommended[0], "claude-default")
        self.assertEqual(
            [recommended[0], *(option[0] for option in alternatives)],
            ["claude-default", "claude-manual"],
        )

        recommended, alternatives = _background_model_options(env={"PATH": "", "CODEX_PLUGIN_ROOT": "/tmp/plugin"})
        self.assertEqual(recommended[0], "codex-default")
        self.assertEqual(
            [recommended[0], *(option[0] for option in alternatives)],
            ["codex-default", "codex-manual"],
        )

    def test_cli_ask_mini_plan_uses_same_refinement_mode_prompt(self) -> None:
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
            self.assertEqual(q["recommended_option_id"], "reviewer")
            self.assertEqual(
                [option["id"] for option in q["options"]],
                ["reviewer", "criticizer", "skip-refinement-execute", "auto"],
            )
            self.assertEqual(q["options"][2]["label"], "Jump to executor")
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
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'verified',\n"
                "    'evidence': 'worker done',\n"
                "}))\n",
            )
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Run Item"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            run_id = json.loads(init.stdout)["run_id"]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": {
                    "adapter": "codex",
                    "argv": [str(worker), "exec", "-C", str(run_worktree)],
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
                "print(json.dumps({\n"
                "    'nonce': os.environ['OPTIM_PLANS_WORKER_NONCE'],\n"
                "    'item_id': os.environ['OPTIM_PLANS_IDS'],\n"
                "    'status': 'completed',\n"
                "    'evidence': 'worker done',\n"
                "}))\n",
            )
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Lifecycle"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            run_id = json.loads(init.stdout)["run_id"]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "worker": {
                    "adapter": "codex",
                    "argv": [str(worker), "exec", "-C", str(run_worktree)],
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
