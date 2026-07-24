#!/usr/bin/env python3
"""Run-scoped optim-plans hook guard.

Hooks are guards only: no event writes, worker launches, Stop handling, or state
mutation happens here.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import PurePosixPath
from typing import Any


RESERVED_GIT = {
    "commit",
    "reset",
    "clean",
    "worktree",
    "update-ref",
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


def codex_pre_tool(decision: str, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    return payload


def codex_session_context(context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


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
    context = f"optim-plans worker scope: ids={ids or 'none'} scopes={scopes or 'repository'}"
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


def handle_pre_tool_use(event: dict[str, Any]) -> dict[str, Any]:
    tool = event.get("tool")
    if tool in {"Shell", "Bash"} and is_reserved_git(str(event.get("command", ""))):
        reason = "optim-plans controller owns Git checkpoint commands"
        return codex_pre_tool("block", reason) if codex() else {"action": "deny", "reason": reason}
    if tool in {"Write", "Edit", "MultiEdit"}:
        raw_path = event.get("path") or event.get("file_path")
        if not isinstance(raw_path, str):
            reason = "structured write event is missing a path"
            return codex_pre_tool("block", reason) if codex() else {"action": "deny", "reason": reason}
        if not in_scope(raw_path):
            reason = "write path is outside optim-plans assigned scopes"
            return codex_pre_tool("block", reason) if codex() else {"action": "deny", "reason": reason}
    return codex_pre_tool("allow") if codex() else {"action": "allow"}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        reason = "invalid hook JSON"
        output(codex_pre_tool("block", reason) if codex() else {"action": "deny", "reason": reason})
        return 0
    if not owned():
        if event.get("event") == "PreToolUse" and codex():
            output(codex_pre_tool("allow"))
        elif not codex():
            output({"action": "noop"})
        return 0
    state_error = validate_owned_state()
    if state_error:
        output(codex_pre_tool("block", state_error) if codex() else {"action": "deny", "reason": state_error})
        return 0
    event_name = event.get("event")
    if event_name == "SessionStart":
        output(handle_session_start())
    elif event_name == "PreToolUse":
        output(handle_pre_tool_use(event))
    else:
        if not codex():
            output({"action": "noop"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
