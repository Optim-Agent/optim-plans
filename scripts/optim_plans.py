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
        HOST_EXECUTOR_PROMPT_PROTOCOL,
        HOST_EXECUTOR_RESULT_SCHEMA,
        HOST_VALIDATOR_PROMPT_PROTOCOL,
        HOST_VALIDATOR_RESULT_SCHEMA,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        controller_script_path,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        language_renders_chinese,
        latest_preserved_run,
        plan_level,
        read_config_language,
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
        HOST_EXECUTOR_PROMPT_PROTOCOL,
        HOST_EXECUTOR_RESULT_SCHEMA,
        HOST_VALIDATOR_PROMPT_PROTOCOL,
        HOST_VALIDATOR_RESULT_SCHEMA,
        OptimPlansState,
        QuestionLedger,
        cached_smoke_tested_worker,
        controller_script_path,
        host_agent,
        host_executor_prompt_hash,
        json_text,
        language_renders_chinese,
        latest_preserved_run,
        plan_level,
        read_config_language,
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
    init.add_argument("--request-text")
    init.add_argument("--plan-level", default="plan")

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
    answer.add_argument("--model-provider")

    worker_config = sub.add_parser("worker-config")
    worker_config.add_argument("--repo", required=True)
    worker_config.add_argument("--role", required=True, choices=["reviewer", "criticizer", "executor", "validator"])
    worker_config.add_argument("--cwd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--repo", required=True)

    register_plan = sub.add_parser("register-plan")
    register_plan.add_argument("--repo", required=True)
    register_plan.add_argument("--path", required=True)
    register_plan.add_argument("--version", required=True, type=int)

    deep_ref = sub.add_parser("deep-record-ref")
    deep_ref.add_argument("--repo", required=True)
    deep_ref.add_argument("--ref-id", required=True)
    deep_ref.add_argument("--name", required=True)
    deep_ref.add_argument("--url", required=True)
    deep_ref.add_argument("--commit", required=True)
    deep_ref.add_argument("--kind", required=True)
    deep_ref.add_argument("--local-path", required=True)

    deep_graph = sub.add_parser("deep-record-graph")
    deep_graph.add_argument("--repo", required=True)
    deep_graph.add_argument("--ref-id", required=True)
    deep_graph.add_argument("--graph-json-path", required=True)
    deep_graph.add_argument("--coverage", required=True)
    deep_graph.add_argument("--backend", required=True)
    deep_graph.add_argument("--commit")

    deep_analysis = sub.add_parser("deep-record-analysis")
    deep_analysis.add_argument("--repo", required=True)
    deep_analysis.add_argument("--ref-id", required=True)
    deep_analysis.add_argument("--analysis-artifact", required=True)
    deep_analysis.add_argument("--commit")

    deep_waiver = sub.add_parser("deep-record-waiver")
    deep_waiver.add_argument("--repo", required=True)
    deep_waiver.add_argument("--ref-id", required=True)
    deep_waiver.add_argument("--waiver-type", required=True)
    deep_waiver.add_argument("--reason", required=True)
    deep_waiver.add_argument("--coverage", required=True)
    deep_waiver.add_argument("--answer-nonce", required=True)
    deep_waiver.add_argument("--commit")

    deep_waiver_question = sub.add_parser("deep-waiver-question")
    deep_waiver_question.add_argument("--repo", required=True)
    deep_waiver_question.add_argument("--ref-id", required=True)
    deep_waiver_question.add_argument("--prompt")

    deep_adoption = sub.add_parser("deep-adoption-question")
    deep_adoption.add_argument("--repo", required=True)
    deep_adoption.add_argument("--ref-id", required=True)
    deep_adoption.add_argument("--claim", required=True)
    deep_adoption.add_argument("--evidence-path", required=True)
    deep_adoption.add_argument("--prompt")
    deep_adoption.add_argument("--commit")

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

    authorize_resume = sub.add_parser("authorize-resume")
    authorize_resume.add_argument("--repo", required=True)
    authorize_resume.add_argument("--item-id", required=True)
    authorize_resume.add_argument("--assignment-nonce", required=True)
    authorize_resume.add_argument("--prior-agent-handle", required=True)
    authorize_resume.add_argument("--launch-block", required=True)

    authorize_batch = sub.add_parser("authorize-batch-spawn")
    authorize_batch.add_argument("--repo", required=True)
    authorize_batch.add_argument("--batch-id", required=True)
    authorize_batch.add_argument("--assignment-nonce", required=True)
    authorize_batch.add_argument("--launch-block", required=True)

    authorize_batch_resume = sub.add_parser("authorize-batch-resume")
    authorize_batch_resume.add_argument("--repo", required=True)
    authorize_batch_resume.add_argument("--batch-id", required=True)
    authorize_batch_resume.add_argument("--assignment-nonce", required=True)
    authorize_batch_resume.add_argument("--prior-agent-handle", required=True)
    authorize_batch_resume.add_argument("--launch-block", required=True)

    register = sub.add_parser("register-agent")
    register.add_argument("--repo", required=True)
    register.add_argument("--item-id", required=True)
    register.add_argument("--assignment-nonce", required=True)
    register.add_argument("--launch-nonce")
    register.add_argument("--resume-nonce")
    register.add_argument("--agent-handle", required=True)
    register.add_argument("--launch-block", required=True)

    register_batch = sub.add_parser("register-batch-agent")
    register_batch.add_argument("--repo", required=True)
    register_batch.add_argument("--batch-id", required=True)
    register_batch.add_argument("--assignment-nonce", required=True)
    register_batch.add_argument("--launch-nonce")
    register_batch.add_argument("--resume-nonce")
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
    fail.add_argument("--resume-nonce")
    fail.add_argument("--resume-failure-kind", default="resume_or_send")
    fail.add_argument("--evidence", required=True)

    fail_batch = sub.add_parser("fail-batch")
    fail_batch.add_argument("--repo", required=True)
    fail_batch.add_argument("--batch-id", required=True)
    fail_batch.add_argument("--assignment-nonce", required=True)
    fail_batch.add_argument("--agent-handle")
    fail_batch.add_argument("--launch-nonce")
    fail_batch.add_argument("--resume-nonce")
    fail_batch.add_argument("--resume-failure-kind", default="resume_or_send")
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

    authorize_validator_resume = sub.add_parser("authorize-validator-resume")
    authorize_validator_resume.add_argument("--repo", required=True)
    authorize_validator_resume.add_argument("--item-id", required=True)
    authorize_validator_resume.add_argument("--validator-nonce", required=True)
    authorize_validator_resume.add_argument("--prior-agent-handle", required=True)
    authorize_validator_resume.add_argument("--launch-block", required=True)

    authorize_batch_validator = sub.add_parser("authorize-batch-validator-spawn")
    authorize_batch_validator.add_argument("--repo", required=True)
    authorize_batch_validator.add_argument("--batch-id", required=True)
    authorize_batch_validator.add_argument("--validator-nonce", required=True)
    authorize_batch_validator.add_argument("--launch-block", required=True)

    authorize_batch_validator_resume = sub.add_parser("authorize-batch-validator-resume")
    authorize_batch_validator_resume.add_argument("--repo", required=True)
    authorize_batch_validator_resume.add_argument("--batch-id", required=True)
    authorize_batch_validator_resume.add_argument("--validator-nonce", required=True)
    authorize_batch_validator_resume.add_argument("--prior-agent-handle", required=True)
    authorize_batch_validator_resume.add_argument("--launch-block", required=True)

    register_validator = sub.add_parser("register-validator")
    register_validator.add_argument("--repo", required=True)
    register_validator.add_argument("--item-id", required=True)
    register_validator.add_argument("--validator-nonce", required=True)
    register_validator.add_argument("--launch-nonce")
    register_validator.add_argument("--resume-nonce")
    register_validator.add_argument("--agent-handle", required=True)
    register_validator.add_argument("--launch-block", required=True)

    register_batch_validator = sub.add_parser("register-batch-validator")
    register_batch_validator.add_argument("--repo", required=True)
    register_batch_validator.add_argument("--batch-id", required=True)
    register_batch_validator.add_argument("--validator-nonce", required=True)
    register_batch_validator.add_argument("--launch-nonce")
    register_batch_validator.add_argument("--resume-nonce")
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
    fail_validator.add_argument("--resume-nonce")
    fail_validator.add_argument("--resume-failure-kind", default="resume_or_send")
    fail_validator.add_argument("--evidence", default="")

    fail_batch_validator = sub.add_parser("fail-batch-validator")
    fail_batch_validator.add_argument("--repo", required=True)
    fail_batch_validator.add_argument("--batch-id", required=True)
    fail_batch_validator.add_argument("--reason", required=True, choices=["process", "crash", "timeout", "interrupted", "unknown"])
    fail_batch_validator.add_argument("--validator-nonce")
    fail_batch_validator.add_argument("--agent-handle")
    fail_batch_validator.add_argument("--launch-nonce")
    fail_batch_validator.add_argument("--resume-nonce")
    fail_batch_validator.add_argument("--resume-failure-kind", default="resume_or_send")
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


def controller_argv(repo: Path, command: str, *args: str) -> list[str]:
    return ["python3", str(controller_script_path()), command, "--repo", str(repo), *args]


def _host_agent(env: dict[str, str]) -> str:
    return host_agent(env)


def _language(repo: Path) -> str:
    return read_config_language(repo) or "en"


def _t(language: str | None, english: str, chinese: str) -> str:
    return chinese if language_renders_chinese(language) else english


def _language_gate(state: OptimPlansState) -> bool:
    question = state.ensure_language_selection(force=True)
    if question is None:
        return False
    print_json(question)
    return True


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
    if value.get("model_provider") is not None and (
        not isinstance(value.get("model_provider"), str) or not value["model_provider"].strip()
    ):
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
    *, env: dict[str, str] | None = None, role: str = "refinement", language: str | None = None
) -> tuple[tuple[str, str, str], list[tuple[str, str, str]]]:
    env = env or os.environ.copy()
    codex_reason = _t(language, "use detected Codex defaults for model and effort", "使用检测到的 Codex 默认模型和推理强度")
    claude_reason = _t(language, "use detected Claude defaults for model and effort", "使用检测到的 Claude 默认模型和推理强度")
    try:
        from agent_adapters import detect_agents
    except ImportError:  # pragma: no cover - package import path
        from scripts.agent_adapters import detect_agents
    agents = detect_agents(env=env)
    codex = agents.get("codex")
    claude = agents.get("claude")
    if codex and codex.available:
        provider = f" on provider {codex.configured_provider}" if codex.configured_provider else ""
        codex_reason = _t(
            language,
            f"use Codex model {codex.configured_model or 'default'}{provider} with effort {codex.configured_effort or 'default'}",
            f"使用 Codex 模型 {codex.configured_model or 'default'}{provider}，推理强度 {codex.configured_effort or 'default'}",
        )
    if claude and claude.available:
        claude_reason = _t(
            language,
            f"use Claude model {claude.configured_model or 'default'} with effort {claude.configured_effort or 'default'}",
            f"使用 Claude 模型 {claude.configured_model or 'default'}，推理强度 {claude.configured_effort or 'default'}",
        )
    if role == "executor":
        codex_options = [
            ("codex-default", _t(language, "Codex profile defaults", "Codex profile 默认值"), codex_reason),
            (
                "codex-manual",
                _t(language, "Codex profile manual", "Codex profile 手动配置"),
                _t(language, "choose explicit model, provider, and effort for Codex", "为 Codex 选择明确模型、provider 和推理强度"),
            ),
            (
                "codex-cli-default",
                _t(language, "Codex profile defaults", "Codex profile 默认值"),
                _t(language, "legacy alias for Codex profile execution", "Codex profile 执行的旧别名"),
            ),
            (
                "codex-cli-manual",
                _t(language, "Codex profile manual", "Codex profile 手动配置"),
                _t(language, "legacy alias for explicit Codex model, provider, and effort", "明确 Codex 模型、provider 和推理强度的旧别名"),
            ),
        ]
    elif role == "validator":
        codex_options = [
            ("codex-default", _t(language, "Codex validator profile defaults", "Codex 验证器 profile 默认值"), codex_reason),
            (
                "codex-manual",
                _t(language, "Codex validator profile manual", "Codex 验证器 profile 手动配置"),
                _t(language, "choose explicit model, provider, and effort for Codex validation", "为 Codex 验证选择明确模型、provider 和推理强度"),
            ),
            (
                "codex-cli-default",
                _t(language, "Codex validator profile defaults", "Codex 验证器 profile 默认值"),
                _t(language, "legacy alias for Codex validator profile execution", "Codex 验证器 profile 执行的旧别名"),
            ),
            (
                "codex-cli-manual",
                _t(language, "Codex validator profile manual", "Codex 验证器 profile 手动配置"),
                _t(language, "legacy alias for explicit Codex validator model, provider, and effort", "明确 Codex 验证器模型、provider 和推理强度的旧别名"),
            ),
        ]
    else:
        codex_options = [
            ("codex-default", _t(language, "Codex detected defaults", "Codex 检测默认值"), codex_reason),
            (
                "codex-manual",
                _t(language, "Codex manual model/provider/effort", "Codex 手动模型/provider/推理强度"),
                _t(language, "choose explicit --model, provider, and reasoning effort for Codex", "为 Codex 选择明确的 --model、provider 和推理强度"),
            ),
        ]
    claude_options = [
        ("claude-default", _t(language, "Claude detected defaults", "Claude 检测默认值"), claude_reason),
        (
            "claude-manual",
            _t(language, "Claude manual model/effort", "Claude 手动模型/推理强度"),
            _t(language, "choose explicit model and reasoning effort for Claude", "为 Claude 选择明确模型和推理强度"),
        ),
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
    if _language_gate(state):
        return
    role = "executor" if key == "executor_worker" else "validator" if key == "validator_worker" else "refinement"
    language = _language(state.repo)
    recommended, alternatives = _background_model_options(role=role, language=language)
    question = QuestionLedger().ask(prompt, recommended=recommended, alternatives=alternatives, language=language)
    payload = question.to_json(expected_seq=len(state.replay().events) + 1)
    payload.update({"plan_level": level.to_json(), "stage": "background-model", "config_key": key})
    stored = _worker_preference(state.repo, key) if reuse else None
    if stored:
        cli = "-cli" if key in {"executor_worker", "validator_worker"} and stored.get("execution_mode") == "cli-adapter" else ""
        choice = f"{stored['platform']}{cli}-{stored['mode']}"
        extra = {field: stored[field] for field in ("model", "effort", "model_provider") if field in stored}
        _record_default(state, payload, choice, **extra)
    else:
        state.append_event("pending_question", payload)
        print_json(payload)


def _option_tuple(raw: list[str]) -> tuple[str, str, str]:
    return (raw[0].strip(), raw[1].strip(), raw[2].strip())


def cmd_init(args: argparse.Namespace) -> None:
    request_text = args.request_text if args.request_text is not None else args.topic
    state = OptimPlansState.initialize(
        Path(args.repo),
        topic=args.topic,
        plan_hash=sha256_text(args.topic),
        request_text=request_text,
        plan_level_name=args.plan_level,
    )
    state.append_event("initialized", {"topic": args.topic})
    payload = {"run_id": state.run_id, "artifact_dir": str(state.artifact_dir.relative_to(state.repo))}
    question = state.ensure_language_selection()
    if question is not None:
        payload.update(question)
    print_json(payload)


def cmd_ask(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    if _language_gate(state):
        return
    level = plan_level(args.plan_level)
    actual_level = state._run_plan_level_name()
    if actual_level is not None and actual_level != "plan":
        state._require_plan_level(level.name)
    ledger = QuestionLedger()
    language = _language(state.repo)
    generic = args.decision_id is not None or args.recommended_option is not None or bool(args.alternative_option)
    foreground = (
        "foreground",
        _t(language, "Current foreground session", "当前前台会话"),
        _t(language, "continue reviewing, questioning, or criticizing in this session", "在当前会话继续审查、提问或质疑"),
    )
    reviewer = ("reviewer", _t(language, "Reviewer", "审查者"), _t(language, "fresh read-only reviewer session", "新的只读审查会话"))
    criticizer = ("criticizer", _t(language, "Criticizer", "质疑者"), _t(language, "fresh read-only criticizer session", "新的只读质疑会话"))
    jump = (
        "skip-refinement-execute",
        _t(language, "Jump to executor", "跳到执行器"),
        _t(language, "skip refinement; use this choice as direct execution launch approval", "跳过精炼；将此选择作为直接执行启动批准"),
    )
    if generic:
        if args.recommended_option is None or args.decision_id is None:
            raise ContractError("generic question requires --decision-id and --recommended-option")
        recommended = _option_tuple(args.recommended_option)
        alternatives = [_option_tuple(option) for option in args.alternative_option]
        validate_generic_question(args.stage, args.decision_id, [recommended[0], *(option[0] for option in alternatives)])
        question = ledger.ask(args.prompt, recommended=recommended, alternatives=alternatives, language=language)
    elif args.stage == "agent-choice":
        delegated_label = _t(language, "Delegated validator run", "委托验证器运行") if args.role == "validator" else _t(
            language, "Delegated foreground run", "委托前台运行"
        )
        delegated = (
            "background",
            delegated_label,
            _t(language, "choose a standalone sub-agent with visible output", "选择有可见输出的独立子智能体"),
        )
        question = ledger.ask(
            args.prompt,
            recommended=delegated,
            alternatives=[foreground],
            language=language,
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
            language=language,
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
            if args.model_provider and args.model_provider.strip():
                worker["model_provider"] = args.model_provider.strip()
        else:
            worker = {"platform": platform, "mode": "default"}
        if execution_mode is not None:
            worker["execution_mode"] = execution_mode
    payload = state.record_answer(args.nonce, args.choice)
    if pending and pending.get("stage") == "agent-choice" and choice in {"foreground", "background"}:
        config_key = pending.get("config_key", "refinement_worker")
        _save_config(state.repo, config_key, {"choice": choice})
        if config_key == "executor_worker" and _agent_choice_preference(state.repo, "validator_worker") is None:
            language = _language(state.repo)
            question = QuestionLedger().ask(
                _t(language, "Choose validator worker", "选择验证器 worker"),
                recommended=(
                    "background",
                    _t(language, "Delegated validator run", "委托验证器运行"),
                    _t(language, "run the validator in a fresh read-only worker", "在新的只读 worker 中运行验证器"),
                ),
                alternatives=[
                    (
                        "foreground",
                        _t(language, "Current foreground session", "当前前台会话"),
                        _t(language, "run validator recovery or review steps in this session", "在当前会话运行验证器恢复或审查步骤"),
                    )
                ],
                language=language,
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
    if _language_gate(state):
        return
    key = _worker_config_key(args.role)
    language = _language(state.repo)
    role_label = _t(language, args.role, {"reviewer": "审查者", "criticizer": "质疑者", "executor": "执行器", "validator": "验证器"}[args.role])
    if args.role == "executor" and _agent_choice_preference(state.repo, key) == "foreground":
        print_json(
            {
                "mode": "foreground",
                "platform": host_agent(),
                "prompt_protocol": HOST_EXECUTOR_PROMPT_PROTOCOL,
                "prompt_hash": host_executor_prompt_hash(),
                "result_schema": HOST_EXECUTOR_RESULT_SCHEMA,
            }
        )
        return
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
        _worker_question(
            state,
            prompt=_t(language, f"Choose {args.role} model and effort", f"选择{role_label}模型和推理强度"),
            level=plan_level("plan"),
            key=key,
        )
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
            prompt=_t(language, f"Choose {args.role} model and effort", f"选择{role_label}模型和推理强度"),
            level=plan_level("plan"),
            key=key,
            reuse=False,
        )
        return
    info = AgentInfo(
        name=platform,
        available=True,
        version=detected.version,
        path=detected.path,
        configured_model=preference.get("model") if preference["mode"] == "manual" else detected.configured_model,
        configured_effort=preference.get("effort") if preference["mode"] == "manual" else detected.configured_effort,
        auth_state=detected.auth_state,
        configured_provider=preference.get("model_provider") if preference["mode"] == "manual" else detected.configured_provider,
    )
    cwd = Path(args.cwd)
    files: dict[str, str] = {}
    launch_files = worker_launch_files(state.repo) if args.role == "executor" or platform == "codex" else {}
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
    config_files = command.config_files or []
    metadata = command.metadata or {}
    worker = {"adapter": platform, "argv": command.argv, "env": env, "config_files": config_files, **metadata}
    cached = cached_smoke_tested_worker(state.repo, worker)
    print_json(cached if cached is not None else {"adapter": platform, "argv": command.argv, "env": env, "config_files": config_files, **metadata, **files})


def cmd_status(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    replayed = state.replay()
    deep_research = state.deep_research_projection(replayed.events)
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "status": replayed.status,
        "events": len(replayed.events),
        "legacy_active": replayed.status == "legacy_active",
    }
    if deep_research["required"] or deep_research["ref_count"]:
        payload["deep_research"] = {
            "ready": deep_research["ready"],
            "ref_count": deep_research["ref_count"],
            "blockers": deep_research["blockers"],
            "refs": [
                {
                    "ref_id": ref["ref_id"],
                    "name": ref["name"],
                    "graph_json_path": ref["graph_json_path"],
                    "analysis_artifact": ref["analysis_artifact"],
                    "adoption_answer_count": ref["adoption_answer_count"],
                }
                for ref in deep_research["refs"]
            ],
            "trajectory": deep_research["trajectory"],
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
            argv = controller_argv(
                state.repo,
                "start-execution",
                "--approval-nonce",
                str(nonce),
            )
            payload["resume_command"] = shlex.join(argv)
            payload["next_action"] = "fix any clean-worktree blockers, then run resume_command"
        else:
            payload["next_action"] = "approve the execution launch nonce before running start-execution"
    elif replayed.status == "awaiting_retry_decision":
        batch_failure = next(
            (
                event
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
        retry_batch_id = batch_failure.get("payload", {}).get("batch_id") if batch_failure is not None else None
        if isinstance(retry_batch_id, str):
            payload["retry_batch_id"] = retry_batch_id
            retry_seen = any(
                event["type"] == "batch_retry_restored" and event.get("payload", {}).get("batch_id") == retry_batch_id
                for event in replayed.events
            )
            retry_argv = controller_argv(
                state.repo,
                "retry-batch",
                "--batch-id",
                retry_batch_id,
            )
            if state._is_retryable_failure_event(batch_failure):
                payload["retry_command"] = shlex.join(retry_argv)
                payload["resume_command"] = payload["retry_command"]
                payload["next_action"] = "run resume_command for the automatic batch retry, or finish with finish_approval_nonce"
            elif retry_seen:
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
        item_failure = None if isinstance(retry_batch_id, str) else next(
            (
                event
                for event in reversed(replayed.events)
                if event["type"]
                in {"worker_failed", "validator_protocol_rejected", "validator_failed", "verification_failed", "audit_failed"}
                or (event["type"] == "validator_result_recorded" and event.get("payload", {}).get("status") == "fail")
            ),
            None,
        )
        retry_item_id = item_failure.get("payload", {}).get("item_id") if item_failure is not None else None
        if isinstance(retry_item_id, str):
            payload["retry_item_id"] = retry_item_id
            retry_seen = any(
                event["type"] == "retry_restored" and event.get("payload", {}).get("item_id") == retry_item_id
                for event in replayed.events
            )
            retry_argv = controller_argv(
                state.repo,
                "retry-item",
                "--item-id",
                retry_item_id,
            )
            if state._is_retryable_failure_event(item_failure):
                payload["retry_command"] = shlex.join(retry_argv)
                payload["resume_command"] = payload["retry_command"]
                payload["next_action"] = "run resume_command for the automatic retry, or finish with finish_approval_nonce"
            elif retry_seen:
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
    elif replayed.status == "blocked":
        blocked = next(
            (
                event.get("payload", {})
                for event in reversed(replayed.events)
                if event["type"] in {"execution_blocked", "batch_execution_blocked"}
            ),
            {},
        )
        if isinstance(blocked.get("batch_id"), str):
            payload["blocked_batch_id"] = blocked["batch_id"]
            payload["blocked_item_ids"] = blocked.get("item_ids", [])
        elif isinstance(blocked.get("item_id"), str):
            payload["blocked_item_id"] = blocked["item_id"]
        payload["blocked_reason"] = blocked.get("reason", "retry policy blocked execution")
        payload["blocked_evidence"] = blocked.get("evidence", "")
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finish_choices"] = [option["id"] for option in approval["options"]]
        payload["next_action"] = "finish with finish_approval_nonce"
    elif replayed.status == "awaiting_integration":
        approval = state.request_finish_approval()
        payload["events"] = len(state.replay().events)
        payload["finish_approval_nonce"] = approval["nonce"]
        payload["finish_choices"] = [option["id"] for option in approval["options"]]
    active_wait = state.active_registered_wait()
    if active_wait is not None:
        payload.update(active_wait)
    print_json(payload)


def cmd_register_plan(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(state.register_plan(Path(args.path), args.version))


def cmd_deep_record_ref(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.record_deep_ref(
            {
                "ref_id": args.ref_id,
                "name": args.name,
                "url": args.url,
                "commit": args.commit,
                "kind": args.kind,
                "local_path": args.local_path,
            }
        )
    )


def cmd_deep_record_graph(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = {
        "ref_id": args.ref_id,
        "graph_json_path": args.graph_json_path,
        "coverage": args.coverage,
        "backend": args.backend,
    }
    if args.commit:
        payload["commit"] = args.commit
    print_json(state.record_deep_ref_graph(payload))


def cmd_deep_record_analysis(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = {"ref_id": args.ref_id, "analysis_artifact": args.analysis_artifact}
    if args.commit:
        payload["commit"] = args.commit
    print_json(state.record_deep_ref_analysis(payload))


def cmd_deep_record_waiver(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = {
        "ref_id": args.ref_id,
        "waiver_type": args.waiver_type,
        "reason": args.reason,
        "coverage": args.coverage,
        "answer_nonce": args.answer_nonce,
    }
    if args.commit:
        payload["commit"] = args.commit
    print_json(state.record_deep_ref_waiver(payload))


def cmd_deep_waiver_question(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = {"ref_id": args.ref_id}
    if args.prompt:
        payload["prompt"] = args.prompt
    print_json(state.request_deep_ref_waiver(payload))


def cmd_deep_adoption_question(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = {"ref_id": args.ref_id, "claim": args.claim, "evidence_path": args.evidence_path}
    if args.prompt:
        payload["prompt"] = args.prompt
    if args.commit:
        payload["commit"] = args.commit
    print_json(state.request_deep_ref_adoption(payload))


def cmd_previous_run(args: argparse.Namespace) -> None:
    print_json(latest_preserved_run(Path(args.repo)))


def cmd_prepare_execution(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    if _language_gate(state):
        return
    if _worker_preference(state.repo, "executor_worker") is None and _agent_choice_preference(state.repo, "executor_worker") != "foreground":
        _worker_question(
            state,
            prompt=_t(_language(state.repo), "Choose executor model and effort", "选择执行器模型和推理强度"),
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
            prompt=_t(_language(state.repo), "Choose validator model and effort", "选择验证器模型和推理强度"),
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


def cmd_authorize_resume(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_resume(
            args.item_id,
            args.assignment_nonce,
            args.prior_agent_handle,
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


def cmd_authorize_batch_resume(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_batch_resume(
            args.batch_id,
            args.assignment_nonce,
            args.prior_agent_handle,
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
            resume_nonce=args.resume_nonce,
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
            resume_nonce=args.resume_nonce,
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
            resume_nonce=args.resume_nonce,
            resume_failure_kind=args.resume_failure_kind,
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
            resume_nonce=args.resume_nonce,
            resume_failure_kind=args.resume_failure_kind,
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


def cmd_authorize_validator_resume(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_validator_resume(
            args.item_id,
            args.validator_nonce,
            args.prior_agent_handle,
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


def cmd_authorize_batch_validator_resume(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    print_json(
        state.authorize_batch_validator_resume(
            args.batch_id,
            args.validator_nonce,
            args.prior_agent_handle,
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
            resume_nonce=args.resume_nonce,
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
            resume_nonce=args.resume_nonce,
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
            resume_nonce=args.resume_nonce,
            resume_failure_kind=args.resume_failure_kind,
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
            resume_nonce=args.resume_nonce,
            resume_failure_kind=args.resume_failure_kind,
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
            "register-plan": cmd_register_plan,
            "deep-record-ref": cmd_deep_record_ref,
            "deep-record-graph": cmd_deep_record_graph,
            "deep-record-analysis": cmd_deep_record_analysis,
            "deep-record-waiver": cmd_deep_record_waiver,
            "deep-waiver-question": cmd_deep_waiver_question,
            "deep-adoption-question": cmd_deep_adoption_question,
            "previous-run": cmd_previous_run,
            "prepare-execution": cmd_prepare_execution,
            "start-execution": cmd_start_execution,
            "run-item": cmd_run_item,
            "assign-item": cmd_assign_item,
            "assign-batch": cmd_assign_batch,
            "authorize-spawn": cmd_authorize_spawn,
            "authorize-resume": cmd_authorize_resume,
            "authorize-batch-spawn": cmd_authorize_batch_spawn,
            "authorize-batch-resume": cmd_authorize_batch_resume,
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
            "authorize-validator-resume": cmd_authorize_validator_resume,
            "authorize-batch-validator-spawn": cmd_authorize_batch_validator_spawn,
            "authorize-batch-validator-resume": cmd_authorize_batch_validator_resume,
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
