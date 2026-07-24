#!/usr/bin/env python3
"""Claude and Codex discovery plus conservative command construction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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


@dataclass(frozen=True)
class AgentCommand:
    argv: list[str]
    env: dict[str, str]


WORKER_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["nonce", "item_id", "status", "evidence"],
    "properties": {
        "nonce": {"type": "string"},
        "item_id": {"type": "string"},
        "status": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "additionalProperties": True,
}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


def _write_worker_schema(path: Path) -> None:
    _write_json_file(path, WORKER_RESULT_SCHEMA)


def bind_optim_plans_env(
    command: AgentCommand,
    *,
    run_id: str,
    worker_nonce: str,
    state_path: Path,
    item_ids: list[str],
    scopes: list[str],
    result_path: Path,
) -> AgentCommand:
    env = {
        **command.env,
        "OPTIM_PLANS_RUN_ID": run_id,
        "OPTIM_PLANS_WORKER_NONCE": worker_nonce,
        "OPTIM_PLANS_STATE_PATH": str(state_path),
        "OPTIM_PLANS_IDS": os.pathsep.join(item_ids),
        "OPTIM_PLANS_SCOPES": os.pathsep.join(scopes),
        "OPTIM_PLANS_RESULT_PATH": str(result_path),
    }
    return AgentCommand(list(command.argv), env)


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


def _codex_defaults(path: str, env: dict[str, str]) -> tuple[str | None, str | None]:
    raw = _run([path, "config", "show", "--json"], env=env) or _run([path, "config"], env=env)
    if not raw:
        return None, None
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return (
        payload.get("model") if isinstance(payload.get("model"), str) else None,
        payload.get("effort") if isinstance(payload.get("effort"), str) else None,
    )


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
        if name == "codex":
            model, effort = _codex_defaults(path, env)
        found[name] = AgentInfo(name, True, version, path, model, effort, "unknown")
    return found


def build_codex_command(
    info: AgentInfo,
    *,
    role: str,
    cwd: Path,
    config_home: Path | None = None,
) -> AgentCommand:
    if not info.available:
        raise ValueError("codex is unavailable")
    if role == "executor" and config_home is None:
        raise ValueError("executor requires isolated CODEX_HOME")
    executable = info.path or "codex"
    sandbox = "read-only" if role in {"reviewer", "criticizer", "verifier"} else "workspace-write"
    schema_path = (
        (config_home / "optim-plans-output-schema.json")
        if config_home is not None
        else Path(tempfile.gettempdir()) / "optim-plans-output-schema.json"
    )
    _write_worker_schema(schema_path)
    argv = [
        executable,
        "exec",
        "-s",
        sandbox,
        "--ephemeral",
        "--ignore-rules",
        "-C",
        str(cwd),
        "--output-schema",
        str(schema_path),
    ]
    if info.configured_model:
        argv.extend(["--model", info.configured_model])
    if info.configured_effort:
        argv.extend(["--reasoning-effort", info.configured_effort])
    env = os.environ.copy()
    if config_home is not None:
        env["CODEX_HOME"] = str(config_home)
    return AgentCommand(argv, env)


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
    if role in {"reviewer", "criticizer", "verifier"}:
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
                    "prompt": "Complete only the assigned optim-plans item, stay inside the allowed scopes, and write the required result JSON.",
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
            "--setting-sources",
            "",
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
    return AgentCommand(argv, {**os.environ, "PWD": str(cwd)})
