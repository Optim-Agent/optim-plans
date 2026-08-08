#!/usr/bin/env python3
"""Claude and Codex discovery plus conservative command construction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentInfo:
    name: str
    available: bool
    version: str | None
    path: str | None
    configured_model: str | None = None
    configured_effort: str | None = None
    auth_state: str = "unknown"
    configured_provider: str | None = None


@dataclass(frozen=True)
class AgentCommand:
    argv: list[str]
    env: dict[str, str]
    config_files: list[dict[str, str]] | None = None
    metadata: dict[str, str] | None = None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


def _run(argv: list[str], *, env: dict[str, str]) -> str | None:
    try:
        result = subprocess.run(
            argv,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _toml_string_assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()
        key = key.strip()
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError:
            parsed = value.strip("'\"")
        if isinstance(parsed, str):
            values[key] = parsed
    return values


def _toml_table(text: str, table: str) -> str | None:
    wanted = f"[{table}]"
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        if raw.strip() != wanted:
            continue
        end = index + 1
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            end += 1
        return "\n".join(lines[index:end]).strip() + "\n"
    return None


def _codex_config_file(env: dict[str, str]) -> Path:
    return _codex_home(env) / "config.toml"


def _read_codex_config(env: dict[str, str]) -> str:
    try:
        return _codex_config_file(env).read_text(encoding="utf-8")
    except OSError:
        return ""


def _codex_defaults(path: str, env: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    raw = _run([path, "config", "show", "--json"], env=env) or _run([path, "config"], env=env)
    model = effort = provider = None
    if not raw:
        payload = {}
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
    if isinstance(payload, dict):
        model = payload.get("model") if isinstance(payload.get("model"), str) else None
        effort = payload.get("effort") if isinstance(payload.get("effort"), str) else None
        effort = payload.get("model_reasoning_effort") if isinstance(payload.get("model_reasoning_effort"), str) else effort
        provider = payload.get("model_provider") if isinstance(payload.get("model_provider"), str) else None
    config = _toml_string_assignments(_read_codex_config(env))
    return (
        model or config.get("model"),
        effort or config.get("model_reasoning_effort"),
        provider or config.get("model_provider"),
    )


def _codex_home(env: dict[str, str]) -> Path:
    raw = env.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def detect_agents(*, env: dict[str, str] | None = None) -> dict[str, AgentInfo]:
    env = env or os.environ.copy()
    found: dict[str, AgentInfo] = {}
    for name in ("codex", "claude"):
        path = shutil.which(name, path=env.get("PATH"))
        if path is None:
            found[name] = AgentInfo(name, False, None, None, auth_state="unavailable")
            continue
        version = _run([path, "--version"], env=env)
        model = effort = None
        provider = None
        if name == "codex":
            model, effort, provider = _codex_defaults(path, env)
        found[name] = AgentInfo(name, True, version, path, model, effort, "unknown", provider)
    return found


def _codex_role_instructions(role: str) -> str:
    if role == "executor":
        return (
            "Complete only the assigned optim-plans item in the controller-owned worktree, "
            "write only inside allowed scopes, and print the required JSON result."
        )
    if role == "validator":
        return "Validate the assigned optim-plans result read-only and print the required JSON verdict."
    if role == "criticizer":
        return "Criticize the plan for correctness, missing edge cases, and avoidable complexity without editing files."
    return "Review the plan for correctness, security, and missing tests without editing files."


def _toml_string(key: str, value: str) -> str:
    return f"{key} = {json.dumps(value)}\n"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_codex_profile_files(
    info: AgentInfo,
    *,
    role: str,
    config_home: Path,
    env: dict[str, str],
    sandbox: str,
) -> tuple[str, list[dict[str, str]], dict[str, str]]:
    config_home.mkdir(parents=True, exist_ok=True)
    profile = f"optim-plans-{role}"
    base_text = ""
    if info.configured_provider:
        table = _toml_table(_read_codex_config(env), f"model_providers.{info.configured_provider}")
        if table:
            base_text = table
    base_config = config_home / "config.toml"
    profile_config = config_home / f"{profile}.config.toml"
    base_config.write_text(base_text, encoding="utf-8")
    text = (
        _toml_string("sandbox_mode", sandbox)
        + _toml_string("developer_instructions", _codex_role_instructions(role))
    )
    if info.configured_model:
        text += _toml_string("model", info.configured_model)
    if info.configured_effort:
        text += _toml_string("model_reasoning_effort", info.configured_effort)
    if info.configured_provider:
        text += _toml_string("model_provider", info.configured_provider)
    profile_config.write_text(text, encoding="utf-8")
    files = [
        {"path": str(base_config), "sha256": _sha256_file(base_config)},
        {"path": str(profile_config), "sha256": _sha256_file(profile_config)},
    ]
    metadata = {"profile": profile}
    if info.configured_model:
        metadata["model"] = info.configured_model
    if info.configured_effort:
        metadata["reasoning_effort"] = info.configured_effort
    if info.configured_provider:
        metadata["model_provider"] = info.configured_provider
    return profile, files, metadata


def build_codex_command(
    info: AgentInfo,
    *,
    role: str,
    cwd: Path,
    config_home: Path | None = None,
    env: dict[str, str] | None = None,
) -> AgentCommand:
    if not info.available:
        raise ValueError("codex is unavailable")
    if role == "executor" and config_home is None:
        raise ValueError("executor requires isolated CODEX_HOME")
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    executable = info.path or "codex"
    sandbox = "read-only" if role in {"reviewer", "criticizer", "validator", "verifier"} else "workspace-write"
    config_files: list[dict[str, str]] = []
    metadata: dict[str, str] = {}
    profile_args: list[str] = []
    if config_home is not None:
        source_env = dict(command_env)
        command_env["CODEX_HOME"] = str(config_home)
        profile, config_files, metadata = _write_codex_profile_files(
            info,
            role=role,
            config_home=config_home,
            env=source_env,
            sandbox=sandbox,
        )
        profile_args = ["--profile", profile]
    argv = [
        executable,
        "exec",
        "-s",
        sandbox,
        "--ephemeral",
        *profile_args,
        "--ignore-rules",
        "-C",
        str(cwd),
    ]
    if info.configured_model:
        argv.extend(["--model", info.configured_model])
    if info.configured_effort:
        argv.extend(["-c", f"model_reasoning_effort={json.dumps(info.configured_effort)}"])
    return AgentCommand(argv, command_env, config_files, metadata)


def build_claude_command(
    info: AgentInfo,
    *,
    role: str,
    cwd: Path,
    settings: Path | None = None,
    plugin_dir: Path | None = None,
    allowed_tools: list[str] | None = None,
) -> AgentCommand:
    if not info.available or not info.path:
        raise ValueError("claude is unavailable")
    if role in {"reviewer", "criticizer", "validator", "verifier"}:
        argv = [
            info.path,
            "-p",
            "--permission-mode",
            "plan",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--json-schema",
            "{}",
        ]
    else:
        if settings is None:
            raise ValueError("executor requires isolated settings")
        if plugin_dir is None:
            raise ValueError("executor requires plugin_dir")
        if not allowed_tools:
            raise ValueError("executor requires explicit allowed tools")
        _write_json_file(settings, {})
        plugin_dir.mkdir(parents=True, exist_ok=True)
        agent_name = "optim-plans-executor"
        agents = json.dumps(
            {
                agent_name: {
                    "description": "Executes approved optim-plans manifest items in a controller-owned worktree.",
                    "prompt": (
                        "Complete only the assigned optim-plans item, inspect the run worktree as needed, "
                        "read ignored worktree files when useful but write only inside allowed scopes, "
                        "leave ignored audit noise such as .xsw/, .pytest_cache/, __pycache__/, and *.pyc untouched, "
                        "and print the required result JSON to stdout."
                    ),
                    "tools": allowed_tools,
                    "permissionMode": "acceptEdits",
                }
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        argv = [
            info.path,
            "-p",
            "--agents",
            agents,
            "--agent",
            agent_name,
            "--settings",
            str(settings),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--plugin-dir",
            str(plugin_dir),
            "--allowedTools",
            ",".join(allowed_tools),
            "--permission-mode",
            "acceptEdits",
            "--no-session-persistence",
            "--json-schema",
            "{}",
        ]
    if info.configured_model:
        argv.extend(["--model", info.configured_model])
    if info.configured_effort:
        argv.extend(["--effort", info.configured_effort])
    return AgentCommand(argv, {**os.environ, "PWD": str(cwd)})
