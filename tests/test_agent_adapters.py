from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from helpers import make_executable, prepend_path
except ModuleNotFoundError:
    from tests.helpers import make_executable, prepend_path


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
            validate = build_codex_command(info, role="validator", cwd=Path("/tmp/repo"))
            self.assertEqual(
                review.argv[:8],
                ["codex", "exec", "-s", "read-only", "--ephemeral", "--ignore-rules", "-C", "/tmp/repo"],
            )
            self.assertEqual(validate.argv[validate.argv.index("-s") + 1], "read-only")
            self.assertIn("--ignore-rules", review.argv)
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
                    "--model",
                    "gpt-test",
                    "-c",
                    'model_reasoning_effort="max"',
                ],
            )
            self.assertEqual(execute.env["CODEX_HOME"], str(config_home))

    def test_codex_executor_follows_current_config_home(self) -> None:
        from scripts.agent_adapters import AgentInfo, build_codex_command

        with tempfile.TemporaryDirectory() as raw:
            current_home = Path(raw) / "current-codex-home"
            isolated_home = Path(raw) / "isolated-codex-home"
            info = AgentInfo("codex", True, "0.144.4", None)

            current = build_codex_command(
                info,
                role="executor",
                cwd=Path("/tmp/repo"),
                config_home=current_home,
                env={"CODEX_HOME": str(current_home)},
            )
            self.assertNotIn("--ignore-user-config", current.argv)

            default_home = build_codex_command(
                info,
                role="executor",
                cwd=Path("/tmp/repo"),
                config_home=Path.home() / ".codex",
                env={},
            )
            self.assertNotIn("--ignore-user-config", default_home.argv)

            isolated = build_codex_command(
                info,
                role="executor",
                cwd=Path("/tmp/repo"),
                config_home=isolated_home,
                env={"CODEX_HOME": str(current_home)},
            )
            self.assertIn("--ignore-user-config", isolated.argv)

    def test_claude_commands_include_schema_and_executor_isolation(self) -> None:
        from scripts.agent_adapters import AgentInfo, build_claude_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            info = AgentInfo("claude", True, "2.1.211", "/bin/claude", "opus-test", "high")
            review = build_claude_command(info, role="reviewer", cwd=Path("/tmp/repo"))
            validate = build_claude_command(info, role="validator", cwd=Path("/tmp/repo"))
            self.assertIn("--json-schema", review.argv)
            self.assertIn("--permission-mode", validate.argv)
            self.assertEqual(validate.argv[validate.argv.index("--permission-mode") + 1], "plan")
            self.assertEqual(review.argv[review.argv.index("--model") + 1], "opus-test")
            self.assertEqual(review.argv[review.argv.index("--effort") + 1], "high")
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
            self.assertNotIn("--setting-sources", execute.argv)
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

if __name__ == "__main__":
    unittest.main()
