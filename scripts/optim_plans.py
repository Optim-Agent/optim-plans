#!/usr/bin/env python3
"""Command line control plane for optim-plans."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from optim_plans_core import (
        ContractError,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        plan_level,
        read_optim_plans_config,
        save_optim_plans_config_value,
        sha256_text,
        worker_launch_files,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.optim_plans_core import (
        ContractError,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        plan_level,
        read_optim_plans_config,
        save_optim_plans_config_value,
        sha256_text,
        worker_launch_files,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="optim-plans controller")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--repo", required=True)
    init.add_argument("--topic", required=True)

    ask = sub.add_parser("ask")
    ask.add_argument("--repo", required=True)
    ask.add_argument("--prompt", required=True)
    ask.add_argument("--plan-level", default="plan")
    ask.add_argument("--stage", choices=["default", "agent-choice", "background-model"], default="default")
    ask.add_argument("--role", choices=["refinement", "executor"], default="refinement")

    answer = sub.add_parser("answer")
    answer.add_argument("--repo", required=True)
    answer.add_argument("--nonce", required=True)
    answer.add_argument("--choice", required=True)
    answer.add_argument("--model")
    answer.add_argument("--effort")

    worker_config = sub.add_parser("worker-config")
    worker_config.add_argument("--repo", required=True)
    worker_config.add_argument("--role", required=True, choices=["reviewer", "criticizer", "executor"])
    worker_config.add_argument("--cwd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--repo", required=True)

    manifest = sub.add_parser("prepare-execution")
    manifest.add_argument("--repo", required=True)
    manifest.add_argument("--manifest", required=True)

    start = sub.add_parser("start-execution")
    start.add_argument("--repo", required=True)
    start.add_argument("--approval-nonce", required=True)

    run = sub.add_parser("run-item")
    run.add_argument("--repo", required=True)
    run.add_argument("--item-id", required=True)

    assign = sub.add_parser("assign-item")
    assign.add_argument("--repo", required=True)
    assign.add_argument("--item-id", required=True)

    authorize = sub.add_parser("authorize-spawn")
    authorize.add_argument("--repo", required=True)
    authorize.add_argument("--item-id", required=True)
    authorize.add_argument("--assignment-nonce", required=True)
    authorize.add_argument("--launch-block", required=True)

    register = sub.add_parser("register-agent")
    register.add_argument("--repo", required=True)
    register.add_argument("--item-id", required=True)
    register.add_argument("--assignment-nonce", required=True)
    register.add_argument("--launch-nonce", required=True)
    register.add_argument("--agent-handle", required=True)
    register.add_argument("--launch-block", required=True)

    complete = sub.add_parser("complete-item")
    complete.add_argument("--repo", required=True)
    complete.add_argument("--item-id", required=True)
    complete.add_argument("--assignment-nonce", required=True)
    complete.add_argument("--agent-handle", required=True)
    complete.add_argument("--evidence", required=True)

    fail = sub.add_parser("fail-item")
    fail.add_argument("--repo", required=True)
    fail.add_argument("--item-id", required=True)
    fail.add_argument("--assignment-nonce", required=True)
    fail.add_argument("--agent-handle")
    fail.add_argument("--launch-nonce")
    fail.add_argument("--evidence", required=True)

    advance = sub.add_parser("advance-item")
    advance.add_argument("--repo", required=True)
    advance.add_argument("--item-id", required=True)

    retry = sub.add_parser("retry-item")
    retry.add_argument("--repo", required=True)
    retry.add_argument("--item-id", required=True)
    retry.add_argument("--approval-nonce")

    finish = sub.add_parser("finish-run")
    finish.add_argument("--repo", required=True)
    finish.add_argument("--outcome", required=True, choices=["integrated", "pr-opened", "kept", "discarded", "failed", "aborted"])
    finish.add_argument("--approval-nonce", required=True)
    finish.add_argument("--target-ref")
    finish.add_argument("--pr-url")
    finish.add_argument("--remote")
    finish.add_argument("--remote-ref")
    finish.add_argument("--confirm-discard", action="store_true")
    finish.add_argument("--evidence", default="")

    worker = sub.add_parser("run-worker", add_help=False)
    worker.add_argument("legacy_args", nargs=argparse.REMAINDER)
    return parser


def print_json(payload: dict[str, Any]) -> None:
    print(json_text(payload, pretty=True))


def _host_agent(env: dict[str, str]) -> str:
    return host_agent(env)


def _save_config(repo: Path, key: str, value: dict[str, Any]) -> None:
    existing = read_optim_plans_config(repo).get(key)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(value)
    save_optim_plans_config_value(repo, key, merged)


def _worker_config_key(role: str) -> str:
    return "executor_worker" if role == "executor" else "refinement_worker"


def _agent_choice_preference(repo: Path, key: str) -> str | None:
    value = read_optim_plans_config(repo).get(key)
    if not isinstance(value, dict):
        return None
    choice = value.get("choice")
    return choice if choice in {"background", "foreground"} else None


def _worker_preference(repo: Path, key: str, *, env: dict[str, str] | None = None) -> dict[str, str] | None:
    value = read_optim_plans_config(repo).get(key)
    platform = host_agent(env)
    if not isinstance(value, dict) or value.get("platform") != platform or value.get("mode") not in {"default", "manual"}:
        return None
    if value["mode"] == "manual" and not all(isinstance(value.get(field), str) and value[field].strip() for field in ("model", "effort")):
        return None
    return value


def _background_model_options(
    *, env: dict[str, str] | None = None, role: str = "refinement"
) -> tuple[tuple[str, str, str], list[tuple[str, str, str]]]:
    env = env or os.environ.copy()
    codex_reason = "use detected Codex defaults for model and effort"
    claude_reason = "use detected Claude defaults for model and effort"
    try:
        from agent_adapters import detect_agents
    except ImportError:  # pragma: no cover - package import path
        from scripts.agent_adapters import detect_agents
    agents = detect_agents(env=env)
    codex = agents.get("codex")
    claude = agents.get("claude")
    if codex and codex.available:
        codex_reason = f"use Codex model {codex.configured_model or 'default'} with effort {codex.configured_effort or 'default'}"
    if claude and claude.available:
        claude_reason = f"use Claude model {claude.configured_model or 'default'} with effort {claude.configured_effort or 'default'}"
    if role == "executor":
        codex_options = [
            ("codex-default", "Codex host multi-agent defaults", codex_reason),
            ("codex-manual", "Codex host multi-agent manual", "choose explicit model and effort for Codex host spawning"),
            ("codex-cli-default", "Codex CLI fallback defaults", "use Codex CLI subprocess execution with detected defaults"),
            ("codex-cli-manual", "Codex CLI fallback manual", "use Codex CLI subprocess execution with explicit model and effort"),
        ]
    else:
        codex_options = [
            ("codex-default", "Codex detected defaults", codex_reason),
            ("codex-manual", "Codex manual model/effort", "choose explicit --model and reasoning effort for Codex"),
        ]
    claude_options = [
        ("claude-default", "Claude detected defaults", claude_reason),
        ("claude-manual", "Claude manual model/effort", "choose explicit model and reasoning effort for Claude"),
    ]
    ordered = claude_options if _host_agent(env) == "claude" else codex_options
    return ordered[0], ordered[1:]


def _agent_choice_default(events: list[dict[str, Any]], *, config_key: str | None = None) -> tuple[str, str] | None:
    questions: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload", {})
        if event["type"] == "pending_question" and payload.get("stage") == "agent-choice":
            if config_key is None or payload.get("config_key") == config_key:
                questions[payload["nonce"]] = payload
        elif event["type"] == "answer_recorded" and payload.get("nonce") in questions:
            source_nonce = payload["nonce"]
            choice = payload.get("choice")
            if choice == "auto":
                choice = questions[source_nonce].get("recommended_option_id")
            if choice in {"background", "foreground"}:
                return source_nonce, choice
    return None


def _record_default(state: OptimPlansState, payload: dict[str, Any], choice: str, **result: str) -> None:
    previous = _agent_choice_default(state.replay().events, config_key=payload.get("config_key")) if payload.get("stage") == "agent-choice" else None
    with state.controller_lock():
        state._append_event_locked("pending_question", payload)
        if previous:
            state._append_event_locked(
                "agent_choice_default_applied",
                {"defaulted_nonce": payload["nonce"], "source_nonce": previous[0], "choice": choice},
            )
        answer = state._append_event_locked("answer_recorded", {"nonce": payload["nonce"], "choice": choice})
    print_json({**answer["payload"], **result})


def _worker_question(state: OptimPlansState, *, prompt: str, level: Any, key: str, reuse: bool = True) -> None:
    role = "executor" if key == "executor_worker" else "refinement"
    recommended, alternatives = _background_model_options(role=role)
    question = QuestionLedger().ask(prompt, recommended=recommended, alternatives=alternatives)
    payload = question.to_json(expected_seq=len(state.replay().events) + 1)
    payload.update({"plan_level": level.to_json(), "stage": "background-model", "config_key": key})
    stored = _worker_preference(state.repo, key) if reuse else None
    if stored:
        cli = "-cli" if key == "executor_worker" and stored.get("execution_mode") == "cli-adapter" else ""
        choice = f"{stored['platform']}{cli}-{stored['mode']}"
        extra = {field: stored[field] for field in ("model", "effort") if field in stored}
        _record_default(state, payload, choice, **extra)
    else:
        state.append_event("pending_question", payload)
        print_json(payload)


def cmd_init(args: argparse.Namespace) -> None:
    state = OptimPlansState.initialize(Path(args.repo), topic=args.topic, plan_hash=sha256_text(args.topic))
    state.append_event("initialized", {"topic": args.topic})
    print_json({"run_id": state.run_id, "artifact_dir": str(state.artifact_dir.relative_to(state.repo))})


def cmd_ask(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    level = plan_level(args.plan_level)
    ledger = QuestionLedger()
    foreground = (
        "foreground",
        "Current foreground session",
        "continue reviewing, questioning, or criticizing in this session",
    )
    reviewer = ("reviewer", "Reviewer", "fresh read-only reviewer session")
    criticizer = ("criticizer", "Criticizer", "fresh read-only criticizer session")
    jump = (
        "skip-refinement-execute",
        "Jump to executor",
        "skip refinement; use this choice as direct execution launch approval",
    )
    if args.stage == "agent-choice":
        delegated = ("background", "Delegated foreground run", "choose a standalone sub-agent with visible output")
        question = ledger.ask(
            args.prompt,
            recommended=delegated,
            alternatives=[foreground],
        )
    elif args.stage == "background-model":
        _worker_question(
            state,
            prompt=args.prompt,
            level=level,
            key=_worker_config_key(args.role),
        )
        return
    else:
        question = ledger.ask(
            args.prompt,
            recommended=reviewer,
            alternatives=[criticizer, jump],
            allow_other=False,
        )
    expected_seq = len(state.replay().events) + 1
    payload = question.to_json(expected_seq=expected_seq)
    payload["plan_level"] = level.to_json()
    if args.stage != "default":
        payload["stage"] = args.stage
    if args.stage == "agent-choice":
        payload["config_key"] = _worker_config_key(args.role)
    stored_choice = _agent_choice_preference(state.repo, payload["config_key"]) if args.stage == "agent-choice" else None
    if stored_choice:
        _record_default(state, payload, stored_choice)
    else:
        state.append_event("pending_question", payload)
        print_json(payload)


def cmd_answer(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    pending = next(
        (
            event["payload"]
            for event in state.replay().events
            if event["type"] == "pending_question" and event.get("payload", {}).get("nonce") == args.nonce
        ),
        None,
    )
    choice = args.choice
    if pending and choice == "auto":
        choice = pending.get("recommended_option_id", choice)
    worker: dict[str, Any] | None = None
    if pending and pending.get("stage") == "background-model" and (
        choice.endswith("-manual") or choice.endswith("-default")
    ):
        manual = choice.endswith("-manual")
        suffix = "-manual" if manual else "-default"
        base = choice.removesuffix(suffix)
        execution_mode = None
        if base.endswith("-cli"):
            platform = base.removesuffix("-cli")
            execution_mode = "cli-adapter"
        else:
            platform = base
            if pending.get("config_key") == "executor_worker" and platform == "codex":
                execution_mode = "host-multi-agent"
        if platform not in {"codex", "claude"}:
            raise ContractError(f"invalid worker platform {platform!r}")
        if manual:
            if not args.model or not args.model.strip() or not args.effort or not args.effort.strip():
                raise ContractError("manual worker choice requires non-empty --model and --effort")
            worker = {
                "platform": platform,
                "mode": "manual",
                "model": args.model.strip(),
                "effort": args.effort.strip(),
            }
        else:
            worker = {"platform": platform, "mode": "default"}
        if execution_mode is not None:
            worker["execution_mode"] = execution_mode
    payload = state.record_answer(args.nonce, args.choice)
    if pending and pending.get("stage") == "agent-choice" and choice in {"foreground", "background"}:
        _save_config(state.repo, pending.get("config_key", "refinement_worker"), {"choice": choice})
    elif worker and worker["platform"] == host_agent():
        _save_config(state.repo, pending.get("config_key", "refinement_worker"), worker)
    print_json(payload)


def cmd_worker_config(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    key = _worker_config_key(args.role)
    preference = _worker_preference(state.repo, key)
    if preference is None:
        _worker_question(state, prompt=f"Choose {args.role} model and effort", level=plan_level("plan"), key=key)
        return
    try:
        from agent_adapters import AgentInfo, build_claude_command, build_codex_command, detect_agents
    except ImportError:  # pragma: no cover - package import path
        from scripts.agent_adapters import AgentInfo, build_claude_command, build_codex_command, detect_agents
    platform = preference["platform"]
    detected = detect_agents().get(platform)
    if detected is None or not detected.available:
        _worker_question(
            state,
            prompt=f"Choose {args.role} model and effort",
            level=plan_level("plan"),
            key=key,
            reuse=False,
        )
        return
    info = AgentInfo(
        platform,
        True,
        detected.version,
        detected.path,
        preference.get("model") if preference["mode"] == "manual" else detected.configured_model,
        preference.get("effort") if preference["mode"] == "manual" else detected.configured_effort,
    )
    cwd = Path(args.cwd)
    files: dict[str, str] = {}
    if args.role == "executor" and platform == "codex" and preference.get("execution_mode") != "cli-adapter":
        print_json(
            {
                "mode": "host-multi-agent",
                "platform": "codex",
                "agent_type": "optim-plans-executor",
                "model": info.configured_model or "default",
                "reasoning_effort": info.configured_effort or "default",
                "prompt_protocol": "optim-plans-host-executor-v1",
                "prompt_hash": host_executor_prompt_hash(),
                "allowed_tools": ["Read", "Write", "Edit", "MultiEdit", "Bash"],
                "sandbox": "workspace-write",
                "result_schema": "optim-plans-worker-result-v1",
            }
        )
        return
    launch_files = worker_launch_files(state.repo) if args.role == "executor" else {}
    if platform == "codex":
        config_home = launch_files.get("codex_home")
        command = build_codex_command(info, role=args.role, cwd=cwd, config_home=config_home)
        env = {"CODEX_HOME": str(config_home)} if config_home else {}
    else:
        settings = launch_files.get("claude_settings")
        plugin_dir = launch_files.get("claude_plugin_dir")
        command = build_claude_command(
            info,
            role=args.role,
            cwd=cwd,
            settings=settings if args.role == "executor" else None,
            plugin_dir=plugin_dir if args.role == "executor" else None,
            allowed_tools=["Bash", "Read", "Write", "Edit", "MultiEdit"] if args.role == "executor" else None,
        )
        env = {"PWD": str(cwd)}
        if args.role == "executor":
            files = {"settings": str(settings), "plugin_dir": str(plugin_dir)}
    worker = {"adapter": platform, "argv": command.argv, "env": env, "config_files": []}
    cached = cached_smoke_tested_worker(state.repo, worker)
    print_json(cached if cached is not None else {"adapter": platform, "argv": command.argv, "env": env, **files})


def cmd_status(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    replayed = state.replay()
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "status": replayed.status,
        "events": len(replayed.events),
        "legacy_active": replayed.status == "legacy_active",
    }
    if replayed.status == "legacy_active":
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finalization"] = (
            "answer the finish approval nonce, then run finish-run "
            "--outcome kept|failed|aborted to release the active pointer without deleting evidence"
        )
    elif replayed.status == "awaiting_retry_decision":
        for event in reversed(replayed.events):
            event_payload = event.get("payload", {})
            if event["type"] == "pending_question" and event_payload.get("stage") == "execution_retry":
                payload["retry_approval_nonce"] = event_payload["nonce"]
                break
        for event in reversed(replayed.events):
            event_payload = event.get("payload", {})
            if event["type"] == "pending_question" and event_payload.get("stage") == "finish_run":
                payload["finish_approval_nonce"] = event_payload["nonce"]
                break
    elif replayed.status == "awaiting_integration":
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finish_choices"] = [option["id"] for option in approval["options"]]
    print_json(payload)


def cmd_prepare_execution(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.prepare_execution(Path(args.manifest)))


def cmd_start_execution(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.start_execution(args.approval_nonce))


def cmd_run_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.run_item(args.item_id))


def _json_object_arg(raw: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def cmd_assign_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.assign_item(args.item_id))


def cmd_authorize_spawn(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_spawn(
            args.item_id,
            args.assignment_nonce,
            _json_object_arg(args.launch_block, label="launch block"),
        )
    )


def cmd_register_agent(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.register_agent(
            args.item_id,
            assignment_nonce=args.assignment_nonce,
            launch_nonce=args.launch_nonce,
            agent_handle=args.agent_handle,
            launch_block=_json_object_arg(args.launch_block, label="launch block"),
        )
    )


def cmd_complete_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.complete_host_item(
            args.item_id,
            assignment_nonce=args.assignment_nonce,
            agent_handle=args.agent_handle,
            evidence=args.evidence,
        )
    )


def cmd_fail_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.fail_host_item(
            args.item_id,
            assignment_nonce=args.assignment_nonce,
            agent_handle=args.agent_handle,
            launch_nonce=args.launch_nonce,
            evidence=args.evidence,
        )
    )


def cmd_advance_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.advance_item(args.item_id))


def cmd_retry_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.retry_item(args.item_id, args.approval_nonce))


def cmd_finish_run(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.finish_run(
            args.outcome,
            approval_nonce=args.approval_nonce,
            target_ref=args.target_ref,
            pr_url=args.pr_url,
            remote=args.remote,
            remote_ref=args.remote_ref,
            confirm_discard=args.confirm_discard,
            evidence=args.evidence,
        )
    )


def cmd_run_worker(args: argparse.Namespace) -> None:
    raise ContractError(
        "legacy run-worker is disabled; use the new manifest flow: "
        "prepare-execution, answer, start-execution"
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "run-worker":
        print(
            "optim-plans: legacy run-worker is disabled; use the new manifest flow: "
            "prepare-execution, answer, start-execution",
            file=sys.stderr,
        )
        return 2
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        {
            "init": cmd_init,
            "ask": cmd_ask,
            "answer": cmd_answer,
            "worker-config": cmd_worker_config,
            "status": cmd_status,
            "prepare-execution": cmd_prepare_execution,
            "start-execution": cmd_start_execution,
            "run-item": cmd_run_item,
            "assign-item": cmd_assign_item,
            "authorize-spawn": cmd_authorize_spawn,
            "register-agent": cmd_register_agent,
            "complete-item": cmd_complete_item,
            "fail-item": cmd_fail_item,
            "advance-item": cmd_advance_item,
            "retry-item": cmd_retry_item,
            "finish-run": cmd_finish_run,
            "run-worker": cmd_run_worker,
        }[args.command](args)
    except ContractError as exc:
        print(f"optim-plans: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
