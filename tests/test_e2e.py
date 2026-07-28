from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

try:
    from helpers import git, make_executable, make_repo
except ModuleNotFoundError:
    from tests.helpers import git, make_executable, make_repo


ROOT = Path(__file__).resolve().parents[1]


def controller_json(*args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/optim_plans.py"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=True,
    )
    return json.loads(result.stdout)


def init_controller(repo: Path, topic: str) -> None:
    controller_json("init", "--repo", str(repo), "--topic", topic)


def ask_agent_choice(repo: Path, prompt: str = "Choose agent", role: str = "refinement") -> dict[str, Any]:
    return controller_json("ask", "--repo", str(repo), "--prompt", prompt, "--stage", "agent-choice", "--role", role)


def answer_choice(repo: Path, nonce: str, choice: str, *extra: str) -> dict[str, Any]:
    return controller_json("answer", "--repo", str(repo), "--nonce", nonce, "--choice", choice, *extra)


def config_path(repo: Path) -> Path:
    from scripts.optim_plans_core import git_common_dir

    return git_common_dir(repo) / "optim-plans" / "config.json"


def write_executor_worker_config(repo: Path) -> None:
    from scripts.optim_plans_core import host_agent

    path = config_path(repo)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": 1}
    config["schema"] = 1
    config["executor_worker"] = {"platform": host_agent(os.environ), "mode": "default"}
    config["validator_worker"] = {"platform": host_agent(os.environ), "mode": "default"}
    path.write_text(json.dumps(config), encoding="utf-8")


def fake_agent_env(raw_path: Path, platform: str) -> dict[str, str]:
    bin_dir = raw_path / "bin"
    bin_dir.mkdir()
    make_executable(
        bin_dir / platform,
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('fake agent 1.0')\n"
        "elif len(sys.argv) >= 2 and sys.argv[1] == 'config':\n"
        "    print(json.dumps({'model': 'detected-model', 'effort': 'detected-effort'}))\n",
    )
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def add_passing_full_proof_files(repo: Path) -> None:
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
            write_executor_worker_config(repo)
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

    def test_cli_ask_agent_choice_defaults_by_worker_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Agent Choice Roles")
            q1 = ask_agent_choice(repo)
            answer_choice(repo, q1["nonce"], "foreground")
            same_role = ask_agent_choice(repo, "Choose refinement again")
            executor = ask_agent_choice(repo, "Choose executor", role="executor")
            validator_question = answer_choice(repo, executor["nonce"], "background")
            answer_choice(repo, validator_question["nonce"], "background")
            validator = ask_agent_choice(repo, "Choose validator again", role="validator")

            config = json.loads(config_path(repo).read_text(encoding="utf-8"))
            self.assertEqual(same_role["choice"], "foreground")
            self.assertNotIn("options", same_role)
            self.assertIn("options", executor)
            self.assertEqual(validator["choice"], "background")
            self.assertEqual(config["refinement_worker"]["choice"], "foreground")
            self.assertEqual(config["executor_worker"]["choice"], "background")
            self.assertEqual(config["validator_worker"]["choice"], "background")

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

    def test_agent_choice_is_persisted_in_git_common_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Persist Agent Choice")
            question = ask_agent_choice(repo)
            answer_choice(repo, question["nonce"], "foreground")

            path = config_path(repo)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema": 1, "refinement_worker": {"choice": "foreground"}},
            )
            self.assertEqual(ask_agent_choice(repo)["choice"], "foreground")

    def test_invalid_agent_config_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Invalid Config")
            path = config_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json", encoding="utf-8")

            self.assertIn("options", ask_agent_choice(repo))

    def test_incomplete_agent_config_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Incomplete Config")
            path = config_path(repo)
            path.write_text(json.dumps({"schema": 1, "refinement_worker": []}), encoding="utf-8")

            self.assertIn("options", ask_agent_choice(repo))

    def test_legacy_global_agent_choice_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Legacy Agent Choice")
            config_path(repo).write_text(json.dumps({"schema": 1, "agent_choice": {"choice": "foreground"}}), encoding="utf-8")

            self.assertIn("options", ask_agent_choice(repo))

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

    def test_manual_refinement_worker_is_persisted_and_reused(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Manual Worker")
            platform = host_agent(os.environ)
            question = controller_json(
                "ask", "--repo", str(repo), "--prompt", "Choose model", "--stage", "background-model"
            )
            answer_choice(
                repo,
                question["nonce"],
                f"{platform}-manual",
                "--model",
                "model-test",
                "--effort",
                "high",
            )

            reused = controller_json(
                "ask", "--repo", str(repo), "--prompt", "Choose model again", "--stage", "background-model"
            )
            path = config_path(repo)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["refinement_worker"],
                {"platform": platform, "mode": "manual", "model": "model-test", "effort": "high"},
            )
            self.assertEqual(reused["choice"], f"{platform}-manual")
            self.assertEqual((reused["model"], reused["effort"]), ("model-test", "high"))
            self.assertNotIn("options", reused)

    def test_background_model_preserves_agent_choice(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Preserve Worker Choice")
            platform = host_agent(os.environ)
            q1 = ask_agent_choice(repo)
            answer_choice(repo, q1["nonce"], "background")
            question = controller_json(
                "ask", "--repo", str(repo), "--prompt", "Choose model", "--stage", "background-model"
            )
            answer_choice(repo, question["nonce"], f"{platform}-default")

            self.assertEqual(
                json.loads(config_path(repo).read_text(encoding="utf-8"))["refinement_worker"],
                {"choice": "background", "platform": platform, "mode": "default"},
            )

    def test_incompatible_worker_config_is_treated_as_missing(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Host Switch")
            platform = host_agent(os.environ)
            other = "claude" if platform == "codex" else "codex"
            path = config_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"schema": 1, "refinement_worker": {"platform": other, "mode": "default"}}),
                encoding="utf-8",
            )

            question = controller_json(
                "ask", "--repo", str(repo), "--prompt", "Choose model", "--stage", "background-model"
            )
            self.assertIn("options", question)
            self.assertEqual([option["id"] for option in question["options"][:2]], [f"{platform}-default", f"{platform}-manual"])

    def test_worker_config_executor_uses_stored_manual_values(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init_controller(repo, "Executor Worker")
            platform = host_agent(os.environ)
            env = fake_agent_env(raw_path, platform)
            question = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            self.assertEqual(question["config_key"], "executor_worker")
            answer_choice(
                repo,
                question["nonce"],
                f"{platform}-manual",
                "--model",
                "model-test",
                "--effort",
                "high",
            )

            resolved = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            self.assertEqual(resolved["mode"], "host-multi-agent")
            self.assertEqual(resolved["platform"], platform)
            self.assertEqual(resolved["model"], "model-test")
            self.assertEqual(resolved["reasoning_effort"], "high")
            self.assertEqual(resolved["prompt_protocol"], "optim-plans-host-executor-v1")
            config = json.loads(config_path(repo).read_text(encoding="utf-8"))
            self.assertNotIn("worker_launch_files", config)

            second_repo = raw_path / "repo-2"
            git(repo, "worktree", "add", "--detach", str(second_repo), "HEAD")
            init_controller(second_repo, "Executor Worker 2")
            reused = controller_json(
                "worker-config", "--repo", str(second_repo), "--role", "executor", "--cwd", str(second_repo), env=env
            )
            self.assertEqual(reused["mode"], "host-multi-agent")
            self.assertEqual(reused["model"], "model-test")

    def test_worker_config_validator_uses_validator_worker_and_read_only_config(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init_controller(repo, "Validator Worker")
            platform = host_agent(os.environ)
            env = fake_agent_env(raw_path, platform)
            question = controller_json(
                "worker-config", "--repo", str(repo), "--role", "validator", "--cwd", str(repo), env=env
            )
            self.assertEqual(question["config_key"], "validator_worker")
            answer_choice(repo, question["nonce"], f"{platform}-manual", "--model", "model-test", "--effort", "high")

            resolved = controller_json(
                "worker-config", "--repo", str(repo), "--role", "validator", "--cwd", str(repo), env=env
            )

            if platform == "codex":
                self.assertEqual(resolved["mode"], "host-multi-agent")
                self.assertEqual(resolved["agent_type"], "optim-plans-validator")
                self.assertEqual(resolved["sandbox"], "read-only")
                self.assertNotIn("Write", resolved["allowed_tools"])
            else:
                self.assertEqual(resolved["adapter"], platform)
                self.assertIn("plan", resolved["argv"])
            config = json.loads(config_path(repo).read_text(encoding="utf-8"))
            self.assertEqual(config["validator_worker"]["model"], "model-test")
            self.assertNotIn("executor_worker", config)

    def test_worker_config_validator_foreground_has_no_adapter(self) -> None:
        from scripts.optim_plans_core import HOST_VALIDATOR_RESULT_SCHEMA, host_agent

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            init_controller(repo, "Foreground Validator")
            question = ask_agent_choice(repo, "Choose validator", role="validator")
            answer_choice(repo, question["nonce"], "foreground")

            resolved = controller_json("worker-config", "--repo", str(repo), "--role", "validator", "--cwd", str(repo))

            self.assertEqual(resolved["mode"], "foreground")
            self.assertEqual(resolved["platform"], host_agent())
            self.assertEqual(resolved["result_schema"], HOST_VALIDATOR_RESULT_SCHEMA)
            self.assertNotIn("argv", resolved)

    def test_prepare_execution_executor_worker_required_before_manifest_recording(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init = controller_json("init", "--repo", str(repo), "--topic", "Executor Required")
            platform = host_agent(os.environ)
            config_path(repo).write_text(
                json.dumps({"schema": 1, "refinement_worker": {"platform": platform, "mode": "default"}}),
                encoding="utf-8",
            )
            sentinel = raw_path / "smoked"
            worker = make_executable(
                raw_path / platform,
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                f"    Path({str(sentinel)!r}).write_text('smoked', encoding='utf-8')\n"
                "    print(json.dumps({'status': 'valid'}))\n",
            )
            argv = [str(worker), "exec", "-C", str(repo)]
            if platform == "claude":
                argv = [str(worker), "-p", "prompt", "--json-schema", str(raw_path / "schema.json")]
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "worker": {
                    "adapter": platform,
                    "argv": argv,
                    "smoke": {"argv": [*argv, "--optim-plans-smoke"]},
                    "timeout_seconds": 5,
                },
                "items": [{"id": "TASK-001", "allowed_paths": ["src/cli.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            question = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))

            self.assertEqual(question["stage"], "background-model")
            self.assertEqual(question["config_key"], "executor_worker")
            self.assertFalse(sentinel.exists())
            events = [
                json.loads(line)
                for line in (config_path(repo).parent / "runs" / init["run_id"] / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertNotIn("execution_manifest_created", [event["type"] for event in events])

    def test_prepare_execution_validator_worker_required_before_manifest_recording(self) -> None:
        from scripts.optim_plans_core import EXECUTION_PROTOCOL, EXECUTION_SCHEMA_VERSION, host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init = controller_json("init", "--repo", str(repo), "--topic", "Validator Required")
            platform = host_agent(os.environ)
            config_path(repo).write_text(
                json.dumps({"schema": 1, "executor_worker": {"platform": platform, "mode": "default"}}),
                encoding="utf-8",
            )
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": EXECUTION_SCHEMA_VERSION,
                        "protocol_version": EXECUTION_PROTOCOL,
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/cli.txt"]}],
                    }
                ),
                encoding="utf-8",
            )

            question = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))

            self.assertEqual(question["stage"], "background-model")
            self.assertEqual(question["config_key"], "validator_worker")
            events = [
                json.loads(line)
                for line in (config_path(repo).parent / "runs" / init["run_id"] / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertNotIn("execution_manifest_created", [event["type"] for event in events])

    def test_prepare_execution_legacy_manifest_does_not_require_validator_worker(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init_controller(repo, "Legacy Validator Not Required")
            platform = host_agent(os.environ)
            config_path(repo).write_text(
                json.dumps({"schema": 1, "executor_worker": {"platform": platform, "mode": "default"}}),
                encoding="utf-8",
            )
            worker = make_executable(
                raw_path / platform,
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    print(json.dumps({'status': 'valid'}))\n",
            )
            argv = [str(worker), "exec", "-C", str(repo)]
            if platform == "claude":
                argv = [str(worker), "-p", "prompt", "--json-schema", str(raw_path / "schema.json")]
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "plan_hash": "abc123",
                        "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                        "integration_destination": "main",
                        "worker": {
                            "adapter": platform,
                            "argv": argv,
                            "smoke": {"argv": [*argv, "--optim-plans-smoke"]},
                        },
                        "verification_argv": [sys.executable, "-c", "pass"],
                        "items": [{"id": "TASK-001", "allowed_paths": ["src/cli.txt"]}],
                    }
                ),
                encoding="utf-8",
            )

            approval = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))

            self.assertEqual(approval["stage"], "execution_launch")
            self.assertNotEqual(approval.get("config_key"), "validator_worker")
            manifest = approval["manifest"]
            self.assertNotIn("validator_worker", manifest)

    def test_worker_config_executor_cli_fallback_uses_stored_manual_values(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init_controller(repo, "Executor CLI Worker")
            platform = host_agent(os.environ)
            env = fake_agent_env(raw_path, platform)
            question = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            answer_choice(
                repo,
                question["nonce"],
                f"{platform}-cli-manual",
                "--model",
                "model-test",
                "--effort",
                "high",
            )

            resolved = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            self.assertEqual(resolved["adapter"], platform)
            self.assertIn("model-test", resolved["argv"])
            self.assertIn("high", " ".join(resolved["argv"]))
            config = json.loads(config_path(repo).read_text(encoding="utf-8"))
            self.assertEqual(config["executor_worker"]["execution_mode"], "cli-adapter")
            launch_files = config["worker_launch_files"]
            root = config_path(repo).parent.resolve() / "launch-files"
            self.assertEqual(launch_files["codex_home"], str(root / "codex-home"))
            self.assertEqual(resolved["env"]["CODEX_HOME"], launch_files["codex_home"])

    def test_worker_config_reuses_cached_smoke_tested_worker_block(self) -> None:
        from scripts.optim_plans_core import host_agent

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            init_controller(repo, "Cached Worker")
            platform = host_agent(os.environ)
            env = fake_agent_env(raw_path, platform)
            question = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            answer_choice(
                repo,
                question["nonce"],
                f"{platform}-cli-manual",
                "--model",
                "model-test",
                "--effort",
                "high",
            )
            resolved = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )
            cached = {
                "adapter": resolved["adapter"],
                "argv": resolved["argv"],
                "env": resolved["env"],
                "config_files": [],
                "smoke": {"argv": [*resolved["argv"], "--optim-plans-smoke"], "env": {}, "timeout_seconds": 10.0},
            }
            config_path(repo).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "executor_worker": {
                            "platform": platform,
                            "mode": "manual",
                            "model": "model-test",
                            "effort": "high",
                            "execution_mode": "cli-adapter",
                        },
                        "smoke_tested_workers": [cached],
                    }
                ),
                encoding="utf-8",
            )

            reused = controller_json(
                "worker-config", "--repo", str(repo), "--role", "executor", "--cwd", str(repo), env=env
            )

            self.assertEqual(reused, cached)

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
                "import json, os, sys\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    print(json.dumps({'status': 'valid', 'evidence': 'adapter smoke ok'}))\n"
                "    raise SystemExit(0)\n"
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
                    "smoke": {"argv": [str(worker), "exec", "-C", str(run_worktree), "--optim-plans-smoke"]},
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
            write_executor_worker_config(repo)
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
            run_payload = json.loads(run_item.stdout)
            if run_payload.get("phase") == "awaiting_execution_summary":
                answer_choice(repo, run_payload["question"]["nonce"], "skip-summary")
                run_payload = controller_json("run-item", "--repo", str(repo), "--item-id", "TASK-001")
            self.assertEqual(git(run_worktree, "rev-parse", "--verify", "HEAD"), run_payload["commit"])

    def test_cli_host_workflow_assigns_registers_completes_and_advances(self) -> None:
        from scripts.optim_plans_core import host_executor_prompt_hash

        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            add_passing_full_proof_files(repo)
            run_worktree = raw_path / "host-run-worktree"
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "init", "--repo", str(repo), "--topic", "Host CLI"],
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
                },
                "verification_argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('src/host-cli.txt').read_text() == 'ok\\n'",
                ],
                "items": [{"id": "TASK-001", "allowed_paths": ["src/host-cli.txt"]}],
            }
            manifest_path = raw_path / "host-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            write_executor_worker_config(repo)
            prepared = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))
            answer_choice(repo, prepared["nonce"], "approve")
            controller_json("start-execution", "--repo", str(repo), "--approval-nonce", prepared["nonce"])

            assignment = controller_json("assign-item", "--repo", str(repo), "--item-id", "TASK-001")
            launch_block = json.dumps(assignment["launch_block"], sort_keys=True)
            authorized = controller_json(
                "authorize-spawn",
                "--repo",
                str(repo),
                "--item-id",
                "TASK-001",
                "--assignment-nonce",
                assignment["assignment_nonce"],
                "--launch-block",
                launch_block,
            )
            controller_json(
                "register-agent",
                "--repo",
                str(repo),
                "--item-id",
                "TASK-001",
                "--assignment-nonce",
                assignment["assignment_nonce"],
                "--launch-nonce",
                authorized["launch_nonce"],
                "--agent-handle",
                "agent-cli",
                "--launch-block",
                launch_block,
            )
            (run_worktree / "src").mkdir()
            (run_worktree / "src/host-cli.txt").write_text("ok\n", encoding="utf-8")
            controller_json(
                "complete-item",
                "--repo",
                str(repo),
                "--item-id",
                "TASK-001",
                "--assignment-nonce",
                assignment["assignment_nonce"],
                "--agent-handle",
                "agent-cli",
                "--evidence",
                "wait_agent completed",
            )
            advanced = controller_json("advance-item", "--repo", str(repo), "--item-id", "TASK-001")
            if advanced.get("phase") == "awaiting_execution_summary":
                answer_choice(repo, advanced["question"]["nonce"], "skip-summary")
                advanced = controller_json("advance-item", "--repo", str(repo), "--item-id", "TASK-001")
            self.assertIn("commit", advanced)

            events = [
                json.loads(line)
                for line in (repo / ".git" / "optim-plans" / "runs" / run_id / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertIn("host_spawn_authorized", [event["type"] for event in events])
            self.assertIn("host_agent_registered", [event["type"] for event in events])
            self.assertIn("run_finished", [event["type"] for event in events])

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
            write_executor_worker_config(repo)
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
            self.assertIn("complete-item", help_result.stdout)
            self.assertIn("previous-run", help_result.stdout)

    def test_status_surfaces_execution_approval_resume_only_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            run_worktree = raw_path / "run-worktree"
            init_controller(repo, "Approval Resume")
            manifest = {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "run_worktree_path": str(run_worktree),
                "items": [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}],
            }
            manifest_path = raw_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            write_executor_worker_config(repo)
            prepared = controller_json("prepare-execution", "--repo", str(repo), "--manifest", str(manifest_path))

            pending = controller_json("status", "--repo", str(repo))
            self.assertEqual(pending["status"], "awaiting_approval")
            self.assertEqual(pending["execution_approval_nonce"], prepared["nonce"])
            self.assertFalse(pending["execution_approved"])
            self.assertNotIn("resume_command", pending)

            answer_choice(repo, prepared["nonce"], "approve")
            (repo / "dirty.tmp").write_text("dirty\n", encoding="utf-8")
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/optim_plans.py"),
                    "start-execution",
                    "--repo",
                    str(repo),
                    "--approval-nonce",
                    prepared["nonce"],
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("source worktree must be clean", blocked.stderr)

            resumable = controller_json("status", "--repo", str(repo))
            self.assertTrue(resumable["execution_approved"])
            self.assertIn(prepared["nonce"], resumable["resume_command"])
            (repo / "dirty.tmp").unlink()
            resumed = subprocess.run(
                shlex.split(resumable["resume_command"]),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(json.loads(resumed.stdout)["approval_nonce"], prepared["nonce"])
            self.assertTrue(run_worktree.is_dir())

    def test_previous_run_reports_latest_preserved_without_active_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            from scripts.optim_plans_core import git_common_dir

            runs_dir = git_common_dir(repo) / "optim-plans" / "runs"

            def write_run(run_id: str, terminal_time: str, *, preserved: bool = True, malformed: bool = False) -> None:
                run_dir = runs_dir / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "run.json").write_text(
                    json.dumps({"schema": 1, "run_id": run_id, "artifact_dir": f"docs/optim-plans/{run_id}"}),
                    encoding="utf-8",
                )
                if malformed:
                    (run_dir / "events.jsonl").write_text("{not-json}\n", encoding="utf-8")
                    return
                events = [
                    {"schema": 1, "seq": 1, "time": "2026-07-28T00:00:00Z", "type": "pending_question", "payload": {"stage": "x"}},
                    {
                        "schema": 1,
                        "seq": 2,
                        "time": terminal_time,
                        "type": "run_finished",
                        "payload": {"outcome": "kept", "preserved": preserved},
                    },
                ]
                (run_dir / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            write_run("older", "2026-07-28T00:00:01Z")
            write_run("latest", "2026-07-28T00:00:02Z")
            write_run("newer-not-preserved", "2026-07-28T00:00:03Z", preserved=False)
            write_run("bad", "2026-07-28T00:00:04Z", malformed=True)

            status = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "status", "--repo", str(repo)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(status.returncode, 2)
            previous = controller_json("previous-run", "--repo", str(repo))
            self.assertEqual(previous["candidate"]["run_id"], "latest")
            self.assertEqual(previous["candidate"]["terminal_time"], "2026-07-28T00:00:02Z")
            self.assertEqual(previous["candidate"]["artifact_dir"], "docs/optim-plans/latest")

    def test_status_surfaces_retry_recovery_actions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            from scripts.optim_plans_core import OptimPlansState

            state = OptimPlansState.initialize(repo, topic="Retry Resume", plan_hash="abc123")
            run_worktree = raw_path / "run-worktree"
            base = git(repo, "rev-parse", "--verify", "HEAD")
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": base,
                    "integration_destination": "main",
                    "run_worktree_path": str(run_worktree),
                    "items": [{"id": "TASK-001", "allowed_paths": ["src/done.txt"]}],
                }
            )
            approval = state.request_execution_approval()
            state.record_answer(approval["nonce"], "approve")
            state.start_execution(approval["nonce"])
            item_started = {
                "item_id": "TASK-001",
                "attempt": 1,
                "base_commit": base,
                "run_worktree": str(run_worktree),
                "run_branch": f"optim-plans/run/{state.run_id}",
                "allowed_paths": ["src/done.txt"],
            }
            state.append_event("item_started", item_started)
            state.append_event("worker_failed", {"item_id": "TASK-001", "base_commit": base, "run_worktree": str(run_worktree), "evidence": "failed"})

            first = controller_json("status", "--repo", str(repo))
            self.assertEqual(first["status"], "awaiting_retry_decision")
            self.assertEqual(first["retry_item_id"], "TASK-001")
            self.assertIn("retry-item", first["resume_command"])
            self.assertNotIn("retry_approval_nonce", first)
            self.assertIn("finish_approval_nonce", first)

            state.append_event("retry_restored", {"item_id": "TASK-001", "approval_nonce": None, "auto_approved": True, "restored_to": base, "run_worktree": str(run_worktree)})
            state.append_event("item_started", {**item_started, "attempt": 2})
            state.append_event("worker_failed", {"item_id": "TASK-001", "base_commit": base, "run_worktree": str(run_worktree), "evidence": "failed again"})

            second = controller_json("status", "--repo", str(repo))
            self.assertEqual(second["status"], "awaiting_retry_decision")
            self.assertEqual(second["retry_item_id"], "TASK-001")
            self.assertIn("retry_approval_nonce", second)
            self.assertIn("--approval-nonce", second["retry_command"])
            self.assertIn("finish_approval_nonce", second)

    def test_cli_lifecycle_rejects_invalid_states_before_mutation_and_finishes_integrated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo = make_repo(raw_path)
            add_passing_full_proof_files(repo)
            run_worktree = raw_path / "run-worktree"
            sentinel = raw_path / "launched"
            worker = make_executable(
                raw_path / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "if '--optim-plans-smoke' in sys.argv:\n"
                "    print(json.dumps({'status': 'valid', 'evidence': 'adapter smoke ok'}))\n"
                "    raise SystemExit(0)\n"
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
                    "smoke": {"argv": [str(worker), "exec", "-C", str(run_worktree), "--optim-plans-smoke"]},
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
            write_executor_worker_config(repo)
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

            completed = subprocess.run(
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
            completed_payload = json.loads(completed.stdout)
            if completed_payload.get("phase") == "awaiting_execution_summary":
                answer_choice(repo, completed_payload["question"]["nonce"], "skip-summary")
                completed_payload = controller_json("run-item", "--repo", str(repo), "--item-id", "TASK-001")
            self.assertIn("commit", completed_payload)

            from scripts.optim_plans_core import ContractError, OptimPlansState

            with self.assertRaises(ContractError):
                OptimPlansState.load_active(repo)
            events = [
                json.loads(line)
                for line in (repo / ".git" / "optim-plans" / "runs" / run_id / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            event_types = [event["type"] for event in events]
            self.assertIn("final_audit_passed", event_types)
            self.assertIn("run_finished", event_types)
            self.assertNotIn("awaiting_integration", event_types)
            self.assertFalse(any(event.get("payload", {}).get("stage") == "finish_run" for event in events))
            finished = next(event["payload"] for event in events if event["type"] == "run_finished")
            self.assertEqual(finished["outcome"], "integrated")
            self.assertEqual(git(repo, "rev-parse", "--verify", "main"), finished["final_checkpoint"])
            self.assertIn("integration verification exited 0", finished["integration_verification"]["evidence"])
            self.assertTrue(run_worktree.is_dir())

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
            self.assertIn("fail-validator", help_result.stdout)
            self.assertNotIn("restore-retry", help_result.stdout)
            self.assertNotIn("final-audit", help_result.stdout)
            fail_validator_help = subprocess.run(
                [sys.executable, str(ROOT / "scripts/optim_plans.py"), "fail-validator", "--help"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("--reason", fail_validator_help.stdout)

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
