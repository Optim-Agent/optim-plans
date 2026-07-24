from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_executable, prepend_path


class AgentAdapterTests(unittest.TestCase):
    def test_detects_configured_defaults_without_guessing_models(self) -> None:
        from scripts.agent_adapters import detect_agents

        with tempfile.TemporaryDirectory() as raw:
            bin_dir = Path(raw)
            make_executable(
                bin_dir / "codex",
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'codex 0.144.4'; exit 0; fi\n"
                "if [ \"$1\" = \"config\" ]; then echo '{\"model\":\"gpt-test\",\"effort\":\"max\"}'; exit 0; fi\n"
                "echo codex\n",
            )
            make_executable(
                bin_dir / "claude",
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'Claude Code 2.1.211'; exit 0; fi\n"
                "echo claude\n",
            )
            found = detect_agents(env=prepend_path(bin_dir))
            self.assertEqual(found["codex"].configured_model, "gpt-test")
            self.assertEqual(found["codex"].configured_effort, "max")
            self.assertTrue(found["claude"].available)

    def test_commands_include_role_safety_flags(self) -> None:
        from scripts.agent_adapters import AgentInfo, build_codex_command

        with tempfile.TemporaryDirectory() as raw:
            config_home = Path(raw) / "codex-home"
            info = AgentInfo("codex", True, "0.144.4", None, "gpt-test", "max")
            review = build_codex_command(info, role="reviewer", cwd=Path("/tmp/repo"))
            self.assertEqual(
                review.argv[:8],
                ["codex", "exec", "-s", "read-only", "--ephemeral", "--ignore-rules", "-C", "/tmp/repo"],
            )
            self.assertIn("--ignore-rules", review.argv)
            self.assertIn("--output-schema", review.argv)
            self.assertTrue(Path(review.argv[review.argv.index("--output-schema") + 1]).is_file())
            with self.assertRaises(ValueError):
                build_codex_command(info, role="executor", cwd=Path("/tmp/repo"))
            execute = build_codex_command(info, role="executor", cwd=Path("/tmp/repo"), config_home=config_home)
            self.assertEqual(
                execute.argv,
                [
                    "codex",
                    "exec",
                    "-s",
                    "workspace-write",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-C",
                    "/tmp/repo",
                    "--output-schema",
                    str(config_home / "optim-plans-output-schema.json"),
                    "--model",
                    "gpt-test",
                    "--reasoning-effort",
                    "max",
                ],
            )
            self.assertTrue(Path(execute.argv[execute.argv.index("--output-schema") + 1]).is_file())
            self.assertEqual(execute.env["CODEX_HOME"], str(config_home))

    def test_claude_commands_include_schema_and_executor_isolation(self) -> None:
        from scripts.agent_adapters import AgentInfo, build_claude_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            info = AgentInfo("claude", True, "2.1.211", "/bin/claude", "opus-test", "high")
            review = build_claude_command(info, role="reviewer", cwd=Path("/tmp/repo"))
            self.assertIn("--json-schema", review.argv)
            execute = build_claude_command(
                info,
                role="executor",
                cwd=Path("/tmp/repo"),
                settings=tmp / "settings.json",
                plugin_dir=tmp / "plugin",
                allowed_tools=["Write", "Edit"],
            )
            self.assertIn("--plugin-dir", execute.argv)
            self.assertIn("--allowedTools", execute.argv)
            self.assertIn("--settings", execute.argv)
            self.assertEqual(
                json.loads(execute.argv[execute.argv.index("--mcp-config") + 1]),
                {"mcpServers": {}},
            )
            self.assertEqual(execute.argv[execute.argv.index("--agent") + 1], "optim-plans-executor")
            self.assertNotIn("--bg", execute.argv)
            self.assertNotIn("--background", execute.argv)
            agent = json.loads(execute.argv[execute.argv.index("--agents") + 1])["optim-plans-executor"]
            self.assertEqual(agent["tools"], ["Write", "Edit"])
            self.assertNotIn("background", agent)
            self.assertTrue((tmp / "settings.json").is_file())
            self.assertTrue((tmp / "plugin").is_dir())

    def test_optim_plans_env_binding_is_agent_agnostic(self) -> None:
        from scripts.agent_adapters import AgentInfo, bind_optim_plans_env, build_claude_command, build_codex_command

        commands = [
            build_codex_command(
                AgentInfo("codex", True, "0.144.4", "/bin/codex"),
                role="executor",
                cwd=Path("/tmp/repo"),
                config_home=Path("/tmp/codex-home"),
            ),
            build_claude_command(
                AgentInfo("claude", True, "2.1.211", "/bin/claude"),
                role="executor",
                cwd=Path("/tmp/repo"),
                settings=Path("/tmp/settings.json"),
                plugin_dir=Path("/tmp/plugin"),
                allowed_tools=["Write", "Edit"],
            ),
        ]
        for command in commands:
            bound = bind_optim_plans_env(
                command,
                run_id="run1",
                worker_nonce="nonce1",
                state_path=Path("/tmp/state.json"),
                item_ids=["TASK-001"],
                scopes=["src", "tests"],
                result_path=Path("/tmp/result.json"),
            )
            self.assertEqual(bound.argv, command.argv)
            self.assertEqual(bound.env["OPTIM_PLANS_RUN_ID"], "run1")
            self.assertEqual(bound.env["OPTIM_PLANS_WORKER_NONCE"], "nonce1")
            self.assertEqual(bound.env["OPTIM_PLANS_STATE_PATH"], "/tmp/state.json")
            self.assertEqual(bound.env["OPTIM_PLANS_IDS"], "TASK-001")
            self.assertIn("src", bound.env["OPTIM_PLANS_SCOPES"])
            self.assertIn("tests", bound.env["OPTIM_PLANS_SCOPES"])
            self.assertEqual(bound.env["OPTIM_PLANS_RESULT_PATH"], "/tmp/result.json")


if __name__ == "__main__":
    unittest.main()
