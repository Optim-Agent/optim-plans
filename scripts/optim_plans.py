#!/usr/bin/env python3
"""Command line control plane for optim-plans."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

try:
    from optim_plans_core import (
        ContractError,
        HOST_VALIDATOR_PROMPT_PROTOCOL,
        HOST_VALIDATOR_RESULT_SCHEMA,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        latest_preserved_run,
        plan_level,
        read_optim_plans_config,
        save_optim_plans_config_value,
        sha256_text,
        validate_generic_question,
        validator_prompt_hash,
        worker_launch_files,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.optim_plans_core import (
        ContractError,
        HOST_VALIDATOR_PROMPT_PROTOCOL,
        HOST_VALIDATOR_RESULT_SCHEMA,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        latest_preserved_run,
        plan_level,
        read_optim_plans_config,
        save_optim_plans_config_value,
        sha256_text,
        validate_generic_question,
        validator_prompt_hash,
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
    ask.add_argument("--stage", default="default")
    ask.add_argument("--role", choices=["refinement", "executor", "validator"], default="refinement")
    ask.add_argument("--decision-id")
    ask.add_argument("--recommended-option", nargs=3, metavar=("ID", "LABEL", "REASON"))
    ask.add_argument("--alternative-option", nargs=3, action="append", default=[], metavar=("ID", "LABEL", "REASON"))

    answer = sub.add_parser("answer")
    answer.add_argument("--repo", required=True)
    answer.add_argument("--nonce", required=True)
    answer.add_argument("--choice", required=True)
    answer.add_argument("--model")
    answer.add_argument("--effort")

    worker_config = sub.add_parser("worker-config")
    worker_config.add_argument("--repo", required=True)
    worker_config.add_argument("--role", required=True, choices=["reviewer", "criticizer", "executor", "validator"])
    worker_config.add_argument("--cwd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--repo", required=True)

    previous = sub.add_parser("previous-run")
    previous.add_argument("--repo", required=True)

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

    assign_batch = sub.add_parser("assign-batch")
    assign_batch.add_argument("--repo", required=True)
    assign_batch.add_argument("--item-id", action="append", dest="item_ids")

    authorize = sub.add_parser("authorize-spawn")
    authorize.add_argument("--repo", required=True)
    authorize.add_argument("--item-id", required=True)
    authorize.add_argument("--assignment-nonce", required=True)
    authorize.add_argument("--launch-block", required=True)

    authorize_batch = sub.add_parser("authorize-batch-spawn")
    authorize_batch.add_argument("--repo", required=True)
    authorize_batch.add_argument("--batch-id", required=True)
    authorize_batch.add_argument("--assignment-nonce", required=True)
    authorize_batch.add_argument("--launch-block", required=True)

    register = sub.add_parser("register-agent")
    register.add_argument("--repo", required=True)
    register.add_argument("--item-id", required=True)
    register.add_argument("--assignment-nonce", required=True)
    register.add_argument("--launch-nonce", required=True)
    register.add_argument("--agent-handle", required=True)
    register.add_argument("--launch-block", required=True)

    register_batch = sub.add_parser("register-batch-agent")
    register_batch.add_argument("--repo", required=True)
    register_batch.add_argument("--batch-id", required=True)
    register_batch.add_argument("--assignment-nonce", required=True)
    register_batch.add_argument("--launch-nonce", required=True)
    register_batch.add_argument("--agent-handle", required=True)
    register_batch.add_argument("--launch-block", required=True)

    complete = sub.add_parser("complete-item")
    complete.add_argument("--repo", required=True)
    complete.add_argument("--item-id", required=True)
    complete.add_argument("--assignment-nonce", required=True)
    complete.add_argument("--agent-handle", required=True)
    complete.add_argument("--evidence", required=True)

    complete_batch = sub.add_parser("complete-batch")
    complete_batch.add_argument("--repo", required=True)
    complete_batch.add_argument("--batch-id", required=True)
    complete_batch.add_argument("--assignment-nonce", required=True)
    complete_batch.add_argument("--agent-handle", required=True)
    complete_batch.add_argument("--evidence", required=True)

    fail = sub.add_parser("fail-item")
    fail.add_argument("--repo", required=True)
    fail.add_argument("--item-id", required=True)
    fail.add_argument("--assignment-nonce", required=True)
    fail.add_argument("--agent-handle")
    fail.add_argument("--launch-nonce")
    fail.add_argument("--evidence", required=True)

    fail_batch = sub.add_parser("fail-batch")
    fail_batch.add_argument("--repo", required=True)
    fail_batch.add_argument("--batch-id", required=True)
    fail_batch.add_argument("--assignment-nonce", required=True)
    fail_batch.add_argument("--agent-handle")
    fail_batch.add_argument("--launch-nonce")
    fail_batch.add_argument("--evidence", required=True)

    advance = sub.add_parser("advance-item")
    advance.add_argument("--repo", required=True)
    advance.add_argument("--item-id", required=True)

    advance_batch = sub.add_parser("advance-batch")
    advance_batch.add_argument("--repo", required=True)
    advance_batch.add_argument("--batch-id", required=True)

    assign_validator = sub.add_parser("assign-validator")
    assign_validator.add_argument("--repo", required=True)
    assign_validator.add_argument("--item-id", required=True)

    assign_batch_validator = sub.add_parser("assign-batch-validator")
    assign_batch_validator.add_argument("--repo", required=True)
    assign_batch_validator.add_argument("--batch-id", required=True)

    authorize_validator = sub.add_parser("authorize-validator-spawn")
    authorize_validator.add_argument("--repo", required=True)
    authorize_validator.add_argument("--item-id", required=True)
    authorize_validator.add_argument("--validator-nonce", required=True)
    authorize_validator.add_argument("--launch-block", required=True)

    authorize_batch_validator = sub.add_parser("authorize-batch-validator-spawn")
    authorize_batch_validator.add_argument("--repo", required=True)
    authorize_batch_validator.add_argument("--batch-id", required=True)
    authorize_batch_validator.add_argument("--validator-nonce", required=True)
    authorize_batch_validator.add_argument("--launch-block", required=True)

    register_validator = sub.add_parser("register-validator")
    register_validator.add_argument("--repo", required=True)
    register_validator.add_argument("--item-id", required=True)
    register_validator.add_argument("--validator-nonce", required=True)
    register_validator.add_argument("--launch-nonce", required=True)
    register_validator.add_argument("--agent-handle", required=True)
    register_validator.add_argument("--launch-block", required=True)

    register_batch_validator = sub.add_parser("register-batch-validator")
    register_batch_validator.add_argument("--repo", required=True)
    register_batch_validator.add_argument("--batch-id", required=True)
    register_batch_validator.add_argument("--validator-nonce", required=True)
    register_batch_validator.add_argument("--launch-nonce", required=True)
    register_batch_validator.add_argument("--agent-handle", required=True)
    register_batch_validator.add_argument("--launch-block", required=True)

    complete_validator = sub.add_parser("complete-validator")
    complete_validator.add_argument("--repo", required=True)
    complete_validator.add_argument("--item-id", required=True)
    complete_validator.add_argument("--validator-nonce", required=True)
    complete_validator.add_argument("--agent-handle")
    complete_validator.add_argument("--result", required=True)

    complete_batch_validator = sub.add_parser("complete-batch-validator")
    complete_batch_validator.add_argument("--repo", required=True)
    complete_batch_validator.add_argument("--batch-id", required=True)
    complete_batch_validator.add_argument("--validator-nonce", required=True)
    complete_batch_validator.add_argument("--agent-handle")
    complete_batch_validator.add_argument("--result", required=True)

    fail_validator = sub.add_parser("fail-validator")
    fail_validator.add_argument("--repo", required=True)
    fail_validator.add_argument("--item-id", required=True)
    fail_validator.add_argument("--reason", required=True, choices=["process", "crash", "timeout", "interrupted", "unknown"])
    fail_validator.add_argument("--validator-nonce")
    fail_validator.add_argument("--agent-handle")
    fail_validator.add_argument("--launch-nonce")
    fail_validator.add_argument("--evidence", default="")

    fail_batch_validator = sub.add_parser("fail-batch-validator")
    fail_batch_validator.add_argument("--repo", required=True)
    fail_batch_validator.add_argument("--batch-id", required=True)
    fail_batch_validator.add_argument("--reason", required=True, choices=["process", "crash", "timeout", "interrupted", "unknown"])
    fail_batch_validator.add_argument("--validator-nonce")
    fail_batch_validator.add_argument("--agent-handle")
    fail_batch_validator.add_argument("--launch-nonce")
    fail_batch_validator.add_argument("--evidence", default="")

    retry = sub.add_parser("retry-item")
    retry.add_argument("--repo", required=True)
    retry.add_argument("--item-id", required=True)
    retry.add_argument("--approval-nonce")

    retry_batch = sub.add_parser("retry-batch")
    retry_batch.add_argument("--repo", required=True)
    retry_batch.add_argument("--batch-id", required=True)
    retry_batch.add_argument("--approval-nonce")

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
    if role == "executor":
        return "executor_worker"
    if role == "validator":
        return "validator_worker"
    return "refinement_worker"


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


def _manifest_requests_validator(manifest_path: Path) -> bool:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read execution manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("execution manifest must be a JSON object")
    return any(key in payload for key in ("schema_version", "protocol_version", "validator_worker"))


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
    elif role == "validator":
        codex_options = [
            ("codex-default", "Codex host validator defaults", codex_reason),
            ("codex-manual", "Codex host validator manual", "choose explicit model and effort for Codex host validation"),
            ("codex-cli-default", "Codex validator CLI fallback defaults", "use Codex CLI subprocess validation with detected defaults"),
            ("codex-cli-manual", "Codex validator CLI fallback manual", "use Codex CLI subprocess validation with explicit model and effort"),
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
    role = "executor" if key == "executor_worker" else "validator" if key == "validator_worker" else "refinement"
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


def _option_tuple(raw: list[str]) -> tuple[str, str, str]:
    return (raw[0].strip(), raw[1].strip(), raw[2].strip())


def cmd_init(args: argparse.Namespace) -> None:
    state = OptimPlansState.initialize(Path(args.repo), topic=args.topic, plan_hash=sha256_text(args.topic))
    state.append_event("initialized", {"topic": args.topic})
    print_json({"run_id": state.run_id, "artifact_dir": str(state.artifact_dir.relative_to(state.repo))})


def cmd_ask(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    level = plan_level(args.plan_level)
    ledger = QuestionLedger()
    generic = args.decision_id is not None or args.recommended_option is not None or bool(args.alternative_option)
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
    if generic:
        if args.recommended_option is None or args.decision_id is None:
            raise ContractError("generic question requires --decision-id and --recommended-option")
        recommended = _option_tuple(args.recommended_option)
        alternatives = [_option_tuple(option) for option in args.alternative_option]
        validate_generic_question(args.stage, args.decision_id, [recommended[0], *(option[0] for option in alternatives)])
        question = ledger.ask(args.prompt, recommended=recommended, alternatives=alternatives)
    elif args.stage == "agent-choice":
        delegated_label = "Delegated validator run" if args.role == "validator" else "Delegated foreground run"
        delegated = ("background", delegated_label, "choose a standalone sub-agent with visible output")
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
    if args.stage not in {"default", "agent-choice", "background-model"} and not generic:
        raise ContractError("custom question stage requires --decision-id and --recommended-option")
    expected_seq = len(state.replay().events) + 1
    payload = question.to_json(expected_seq=expected_seq)
    payload["plan_level"] = level.to_json()
    if generic:
        payload.update({"stage": args.stage, "decision_id": args.decision_id.strip(), "planning_only": True})
    elif args.stage != "default":
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
            if pending.get("config_key") in {"executor_worker", "validator_worker"} and platform == "codex":
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
        config_key = pending.get("config_key", "refinement_worker")
        _save_config(state.repo, config_key, {"choice": choice})
        if config_key == "executor_worker" and _agent_choice_preference(state.repo, "validator_worker") is None:
            question = QuestionLedger().ask(
                "Choose validator worker",
                recommended=("background", "Delegated validator run", "run the validator in a fresh read-only worker"),
                alternatives=[
                    (
                        "foreground",
                        "Current foreground session",
                        "run validator recovery or review steps in this session",
                    )
                ],
            )
            follow_up = question.to_json(expected_seq=len(state.replay().events) + 1)
            follow_up.update({"plan_level": plan_level("plan").to_json(), "stage": "agent-choice", "config_key": "validator_worker"})
            state.append_event("pending_question", follow_up)
            print_json(follow_up)
            return
    elif worker and worker["platform"] == host_agent():
        _save_config(state.repo, pending.get("config_key", "refinement_worker"), worker)
    print_json(payload)


def cmd_worker_config(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    key = _worker_config_key(args.role)
    if args.role == "validator" and _agent_choice_preference(state.repo, key) == "foreground":
        print_json(
            {
                "mode": "foreground",
                "platform": host_agent(),
                "prompt_protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
                "prompt_hash": validator_prompt_hash(),
                "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
            }
        )
        return
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
    if args.role == "validator" and platform == "codex" and preference.get("execution_mode") != "cli-adapter":
        print_json(
            {
                "mode": "host-multi-agent",
                "platform": "codex",
                "agent_type": "optim-plans-validator",
                "model": info.configured_model or "default",
                "reasoning_effort": info.configured_effort or "default",
                "prompt_protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
                "prompt_hash": validator_prompt_hash(),
                "allowed_tools": ["Read", "Bash"],
                "sandbox": "read-only",
                "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
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
    elif replayed.status == "awaiting_approval":
        approval = next(
            (
                event.get("payload", {})
                for event in reversed(replayed.events)
                if event["type"] == "pending_question" and event.get("payload", {}).get("stage") == "execution_launch"
            ),
            {},
        )
        nonce = approval.get("nonce")
        answer = next(
            (
                event.get("payload", {})
                for event in reversed(replayed.events)
                if event["type"] == "answer_recorded" and event.get("payload", {}).get("nonce") == nonce
            ),
            {},
        )
        approved = answer.get("choice") == "approve"
        if nonce:
            payload["execution_approval_nonce"] = nonce
        payload["execution_approved"] = approved
        if approved:
            argv = [
                "python3",
                "scripts/optim_plans.py",
                "start-execution",
                "--repo",
                str(state.repo),
                "--approval-nonce",
                str(nonce),
            ]
            payload["resume_command"] = shlex.join(argv)
            payload["next_action"] = "fix any clean-worktree blockers, then run resume_command"
        else:
            payload["next_action"] = "approve the execution launch nonce before running start-execution"
    elif replayed.status == "awaiting_retry_decision":
        retry_batch_id = next(
            (
                event.get("payload", {}).get("batch_id")
                for event in reversed(replayed.events)
                if event["type"]
                in {
                    "batch_worker_failed",
                    "batch_validator_protocol_rejected",
                    "batch_validator_failed",
                    "batch_verification_failed",
                    "batch_audit_failed",
                }
                or (event["type"] == "batch_validator_result_recorded" and event.get("payload", {}).get("status") == "fail")
            ),
            None,
        )
        if isinstance(retry_batch_id, str):
            payload["retry_batch_id"] = retry_batch_id
            retry_seen = any(
                event["type"] == "batch_retry_restored" and event.get("payload", {}).get("batch_id") == retry_batch_id
                for event in replayed.events
            )
            retry_argv = [
                "python3",
                "scripts/optim_plans.py",
                "retry-batch",
                "--repo",
                str(state.repo),
                "--batch-id",
                retry_batch_id,
            ]
            if retry_seen:
                retry = state.request_batch_retry(retry_batch_id)
                payload["retry_approval_nonce"] = retry["nonce"]
                retry_argv.extend(["--approval-nonce", retry["nonce"]])
                retry_answer = next(
                    (
                        event.get("payload", {})
                        for event in reversed(state.replay().events)
                        if event["type"] == "answer_recorded" and event.get("payload", {}).get("nonce") == retry["nonce"]
                    ),
                    {},
                )
                payload["retry_approved"] = retry_answer.get("choice") == "approve"
                payload["retry_command"] = shlex.join(retry_argv)
                if payload["retry_approved"]:
                    payload["resume_command"] = payload["retry_command"]
            else:
                payload["resume_command"] = shlex.join(retry_argv)
                payload["next_action"] = "run resume_command for the automatic first batch retry, or finish with finish_approval_nonce"
        retry_item_id = None if isinstance(retry_batch_id, str) else next(
            (
                event.get("payload", {}).get("item_id")
                for event in reversed(replayed.events)
                if event["type"]
                in {"worker_failed", "validator_protocol_rejected", "validator_failed", "verification_failed", "audit_failed"}
                or (event["type"] == "validator_result_recorded" and event.get("payload", {}).get("status") == "fail")
            ),
            None,
        )
        if isinstance(retry_item_id, str):
            payload["retry_item_id"] = retry_item_id
            retry_seen = any(
                event["type"] == "retry_restored" and event.get("payload", {}).get("item_id") == retry_item_id
                for event in replayed.events
            )
            retry_argv = [
                "python3",
                "scripts/optim_plans.py",
                "retry-item",
                "--repo",
                str(state.repo),
                "--item-id",
                retry_item_id,
            ]
            if retry_seen:
                retry = state.request_retry(retry_item_id)
                payload["retry_approval_nonce"] = retry["nonce"]
                retry_argv.extend(["--approval-nonce", retry["nonce"]])
                retry_answer = next(
                    (
                        event.get("payload", {})
                        for event in reversed(state.replay().events)
                        if event["type"] == "answer_recorded" and event.get("payload", {}).get("nonce") == retry["nonce"]
                    ),
                    {},
                )
                payload["retry_approved"] = retry_answer.get("choice") == "approve"
                payload["retry_command"] = shlex.join(retry_argv)
                if payload["retry_approved"]:
                    payload["resume_command"] = payload["retry_command"]
            else:
                payload["resume_command"] = shlex.join(retry_argv)
                payload["next_action"] = "run resume_command for the automatic first retry, or finish with finish_approval_nonce"
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finish_choices"] = [option["id"] for option in approval["options"]]
        if payload.get("retry_approved"):
            payload["next_action"] = "run resume_command, or finish with finish_approval_nonce"
        elif "retry_approval_nonce" in payload:
            payload["next_action"] = "approve retry_approval_nonce then run retry_command, or finish with finish_approval_nonce"
        else:
            payload.setdefault("next_action", "finish with finish_approval_nonce")
    elif replayed.status == "awaiting_integration":
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finish_choices"] = [option["id"] for option in approval["options"]]
    print_json(payload)


def cmd_previous_run(args: argparse.Namespace) -> None:
    print_json(latest_preserved_run(Path(args.repo)))


def cmd_prepare_execution(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    if _worker_preference(state.repo, "executor_worker") is None:
        _worker_question(
            state,
            prompt="Choose executor model and effort",
            level=plan_level("plan"),
            key="executor_worker",
        )
        return
    if (
        _manifest_requests_validator(Path(args.manifest))
        and _worker_preference(state.repo, "validator_worker") is None
        and _agent_choice_preference(state.repo, "validator_worker") != "foreground"
    ):
        _worker_question(
            state,
            prompt="Choose validator model and effort",
            level=plan_level("plan"),
            key="validator_worker",
        )
        return
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


def cmd_assign_batch(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.assign_batch(args.item_ids))


def cmd_authorize_spawn(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_spawn(
            args.item_id,
            args.assignment_nonce,
            _json_object_arg(args.launch_block, label="launch block"),
        )
    )


def cmd_authorize_batch_spawn(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_batch_spawn(
            args.batch_id,
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


def cmd_register_batch_agent(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.register_batch_agent(
            args.batch_id,
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


def cmd_complete_batch(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.complete_host_batch(
            args.batch_id,
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


def cmd_fail_batch(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.fail_host_batch(
            args.batch_id,
            assignment_nonce=args.assignment_nonce,
            agent_handle=args.agent_handle,
            launch_nonce=args.launch_nonce,
            evidence=args.evidence,
        )
    )


def cmd_advance_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.advance_item(args.item_id))


def cmd_advance_batch(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.advance_batch(args.batch_id))


def cmd_assign_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.assign_validator(args.item_id))


def cmd_assign_batch_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.assign_batch_validator(args.batch_id))


def cmd_authorize_validator_spawn(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_validator_spawn(
            args.item_id,
            args.validator_nonce,
            _json_object_arg(args.launch_block, label="validator launch block"),
        )
    )


def cmd_authorize_batch_validator_spawn(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_batch_validator_spawn(
            args.batch_id,
            args.validator_nonce,
            _json_object_arg(args.launch_block, label="validator launch block"),
        )
    )


def cmd_register_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.register_validator_agent(
            args.item_id,
            validator_nonce=args.validator_nonce,
            launch_nonce=args.launch_nonce,
            agent_handle=args.agent_handle,
            launch_block=_json_object_arg(args.launch_block, label="validator launch block"),
        )
    )


def cmd_register_batch_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.register_batch_validator_agent(
            args.batch_id,
            validator_nonce=args.validator_nonce,
            launch_nonce=args.launch_nonce,
            agent_handle=args.agent_handle,
            launch_block=_json_object_arg(args.launch_block, label="validator launch block"),
        )
    )


def cmd_complete_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.record_validator_result(
            args.item_id,
            validator_nonce=args.validator_nonce,
            agent_handle=args.agent_handle,
            result=_json_object_arg(args.result, label="validator result"),
        )
    )


def cmd_complete_batch_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.record_batch_validator_result(
            args.batch_id,
            validator_nonce=args.validator_nonce,
            agent_handle=args.agent_handle,
            result=_json_object_arg(args.result, label="validator result"),
        )
    )


def cmd_fail_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.fail_validator(
            args.item_id,
            reason=args.reason,
            validator_nonce=args.validator_nonce,
            agent_handle=args.agent_handle,
            launch_nonce=args.launch_nonce,
            evidence=args.evidence,
        )
    )


def cmd_fail_batch_validator(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.fail_batch_validator(
            args.batch_id,
            reason=args.reason,
            validator_nonce=args.validator_nonce,
            agent_handle=args.agent_handle,
            launch_nonce=args.launch_nonce,
            evidence=args.evidence,
        )
    )


def cmd_retry_item(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.retry_item(args.item_id, args.approval_nonce))


def cmd_retry_batch(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.retry_batch(args.batch_id, args.approval_nonce))


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
            "previous-run": cmd_previous_run,
            "prepare-execution": cmd_prepare_execution,
            "start-execution": cmd_start_execution,
            "run-item": cmd_run_item,
            "assign-item": cmd_assign_item,
            "assign-batch": cmd_assign_batch,
            "authorize-spawn": cmd_authorize_spawn,
            "authorize-batch-spawn": cmd_authorize_batch_spawn,
            "register-agent": cmd_register_agent,
            "register-batch-agent": cmd_register_batch_agent,
            "complete-item": cmd_complete_item,
            "complete-batch": cmd_complete_batch,
            "fail-item": cmd_fail_item,
            "fail-batch": cmd_fail_batch,
            "advance-item": cmd_advance_item,
            "advance-batch": cmd_advance_batch,
            "assign-validator": cmd_assign_validator,
            "assign-batch-validator": cmd_assign_batch_validator,
            "authorize-validator-spawn": cmd_authorize_validator_spawn,
            "authorize-batch-validator-spawn": cmd_authorize_batch_validator_spawn,
            "register-validator": cmd_register_validator,
            "register-batch-validator": cmd_register_batch_validator,
            "complete-validator": cmd_complete_validator,
            "complete-batch-validator": cmd_complete_batch_validator,
            "fail-validator": cmd_fail_validator,
            "fail-batch-validator": cmd_fail_batch_validator,
            "retry-item": cmd_retry_item,
            "retry-batch": cmd_retry_batch,
            "finish-run": cmd_finish_run,
            "run-worker": cmd_run_worker,
        }[args.command](args)
    except ContractError as exc:
        print(f"optim-plans: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
