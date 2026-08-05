#!/usr/bin/env python3
"""Run-scoped optim-plans hook guard.

Hooks are guards only: no event writes, worker launches, Stop handling, or state
mutation happens here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


RESERVED_GIT = {
    "commit",
    "reset",
    "clean",
    "worktree",
    "update-ref",
}

POLLING_CONTROLLER_COMMANDS = {"status", "advance-item", "advance-batch"}

WAIT_START_EVENTS = {
    "host_agent_registered": "executor_item",
    "batch_agent_registered": "executor_batch",
    "validator_agent_registered": "validator_item",
    "batch_validator_agent_registered": "validator_batch",
}


def owned() -> bool:
    return bool(
        os.environ.get("OPTIM_PLANS_RUN_ID")
        or os.environ.get("OPTIM_PLANS_WORKER_NONCE")
        or os.environ.get("OPTIM_PLANS_STATE_PATH")
    )


def validate_owned_state() -> str | None:
    if not owned():
        return None
    state_path = os.environ.get("OPTIM_PLANS_STATE_PATH")
    if not state_path:
        return "owned worker is missing OPTIM_PLANS_STATE_PATH"
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return "owned worker has invalid optim-plans state"
    if state.get("run_id") != os.environ.get("OPTIM_PLANS_RUN_ID"):
        return "run ID does not match optim-plans state"
    if state.get("worker_nonce") != os.environ.get("OPTIM_PLANS_WORKER_NONCE"):
        return "worker nonce does not match optim-plans state"
    return None


def output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def codex() -> bool:
    return bool(os.environ.get("PLUGIN_DATA") or os.environ.get("OPTIM_PLANS_PLUGIN_ROOT"))


def pre_tool_output(decision: str, reason: str | None = None) -> dict[str, Any]:
    if decision == "allow":
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason or "",
        }
    }


def codex_session_context(context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def normalize_hook_event(raw: dict[str, Any]) -> dict[str, Any]:
    event = dict(raw)
    if not isinstance(event.get("event"), str) and isinstance(raw.get("hook_event_name"), str):
        event["event"] = raw["hook_event_name"]
    if not isinstance(event.get("tool"), str) and isinstance(raw.get("tool_name"), str):
        event["tool"] = raw["tool_name"]
    tool_input = raw.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path"):
            if key not in event and isinstance(tool_input.get(key), str):
                event[key] = tool_input[key]
    return event


def in_scope(path: str) -> bool:
    scopes = [item for item in os.environ.get("OPTIM_PLANS_SCOPES", "").split(os.pathsep) if item]
    if not scopes:
        return False
    target = PurePosixPath(path.replace("\\", "/"))
    if target.is_absolute() or ".." in target.parts:
        return False
    for scope in scopes:
        scope_path = PurePosixPath(scope.replace("\\", "/"))
        if target == scope_path or scope_path in target.parents:
            return True
    return False


def handle_session_start() -> dict[str, Any]:
    ids = os.environ.get("OPTIM_PLANS_IDS", "")
    scopes = os.environ.get("OPTIM_PLANS_SCOPES", "")
    ignored = os.environ.get("OPTIM_PLANS_IGNORED_AUDIT_NOISE", "")
    context = f"optim-plans worker scope: ids={ids or 'none'} scopes={scopes or 'repository'}"
    if ignored:
        context += f" ignored_audit_noise={ignored}"
    if codex():
        return codex_session_context(context)
    return {"action": "inject", "context": context}


def is_reserved_git(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 2:
        return False
    if parts[0] != "git":
        return False
    index = 1
    options_with_values = {"-C", "-c", "--git-dir", "--work-tree"}
    while index < len(parts) and parts[index].startswith("-"):
        index += 2 if parts[index] in options_with_values else 1
    return index < len(parts) and parts[index] in RESERVED_GIT


def controller_invocation(command: str) -> tuple[str, str] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 4 or not Path(parts[0]).name.startswith("python"):
        return None
    index = 1
    while index < len(parts) and parts[index].startswith("-"):
        if parts[index] in {"-c", "-m"}:
            return None
        index += 1
    if index + 1 >= len(parts):
        return None
    script = PurePosixPath(parts[index].replace("\\", "/"))
    if script.name != "optim_plans.py" or script.parent.name != "scripts":
        return None
    repo = option_value(parts[index + 2 :], "--repo")
    if repo is None:
        return None
    return parts[index + 1], repo


def option_value(parts: list[str], name: str) -> str | None:
    for index, part in enumerate(parts):
        if part == name and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None


def git_common_dir(repo: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    return (common if common.is_absolute() else repo / common).resolve()


def active_events_file(repo: Path) -> Path | None:
    repo = repo.absolute()
    common = git_common_dir(repo)
    if common is None:
        return None
    worktree_id = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    root = common / "optim-plans"
    try:
        active = json.loads((root / "worktrees" / worktree_id / "active.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    run_id = active.get("run_id")
    if not isinstance(run_id, str):
        return None
    return root / "runs" / run_id / "events.jsonl"


def wait_finished(kind: str, wait: dict[str, Any], event_type: str, payload: dict[str, Any]) -> bool:
    if kind == "executor_item":
        return payload.get("item_id") == wait.get("item_id") and event_type in {
            "worker_completed",
            "worker_failed",
            "retry_restored",
            "checkpoint_created",
        }
    if kind == "executor_batch":
        return payload.get("batch_id") == wait.get("batch_id") and event_type in {
            "batch_completed",
            "batch_worker_failed",
            "batch_retry_restored",
            "batch_checkpoint_created",
        }
    if kind == "validator_item":
        return payload.get("item_id") == wait.get("item_id") and event_type in {
            "validator_result_recorded",
            "validator_protocol_rejected",
            "validator_failed",
            "retry_restored",
            "checkpoint_created",
        }
    return payload.get("batch_id") == wait.get("batch_id") and event_type in {
        "batch_validator_result_recorded",
        "batch_validator_protocol_rejected",
        "batch_validator_failed",
        "batch_retry_restored",
        "batch_checkpoint_created",
    }


def active_registered_wait_exists(repo: str) -> bool:
    events_file = active_events_file(Path(repo))
    if events_file is None:
        return False
    active: tuple[str, dict[str, Any]] | None = None
    try:
        lines = events_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if event_type in WAIT_START_EVENTS:
            active = (WAIT_START_EVENTS[event_type], payload)
        elif active is not None and isinstance(event_type, str) and wait_finished(active[0], active[1], event_type, payload):
            active = None
    return active is not None


def controller_polling_reason(command: str) -> str | None:
    invocation = controller_invocation(command)
    if invocation is None:
        return None
    controller_command, repo = invocation
    if controller_command not in POLLING_CONTROLLER_COMMANDS:
        return None
    if not active_registered_wait_exists(repo):
        return None
    return "optim-plans is waiting for a registered executor or validator; complete/fail it before polling"


def handle_pre_tool_use(event: dict[str, Any], *, worker_owned: bool) -> dict[str, Any]:
    tool = event.get("tool")
    if tool in {"Shell", "Bash"}:
        command = str(event.get("command", ""))
        reason = controller_polling_reason(command)
        if reason:
            return pre_tool_output("deny", reason)
        if worker_owned and is_reserved_git(command):
            return pre_tool_output("deny", "optim-plans controller owns Git checkpoint commands")
    if worker_owned and tool in {"Write", "Edit", "MultiEdit"}:
        raw_path = event.get("path") or event.get("file_path")
        if not isinstance(raw_path, str):
            return pre_tool_output("deny", "structured write event is missing a path")
        if not in_scope(raw_path):
            return pre_tool_output("deny", "write path is outside optim-plans assigned scopes")
    return pre_tool_output("allow")


def main() -> int:
    try:
        event = normalize_hook_event(json.load(sys.stdin))
    except json.JSONDecodeError:
        reason = "invalid hook JSON"
        output(pre_tool_output("deny", reason) if codex() else {"action": "deny", "reason": reason})
        return 0
    event_name = event.get("event")
    worker_owned = owned()
    if event_name == "PreToolUse":
        if worker_owned:
            state_error = validate_owned_state()
            if state_error:
                output(pre_tool_output("deny", state_error))
                return 0
        output(handle_pre_tool_use(event, worker_owned=worker_owned))
        return 0
    if not owned():
        if not codex():
            output({"action": "noop"})
        return 0
    state_error = validate_owned_state()
    if state_error:
        output({"action": "deny", "reason": state_error})
        return 0
    if event_name == "SessionStart":
        output(handle_session_start())
    else:
        if not codex():
            output({"action": "noop"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
