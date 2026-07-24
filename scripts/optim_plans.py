#!/usr/bin/env python3
"""Command line control plane for optim-plans."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from optim_plans_core import (
        ContractError,
        OptimPlansState,
        QuestionLedger,
        json_text,
        plan_level,
        sha256_text,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.optim_plans_core import (
        ContractError,
        OptimPlansState,
        QuestionLedger,
        json_text,
        plan_level,
        sha256_text,
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

    answer = sub.add_parser("answer")
    answer.add_argument("--repo", required=True)
    answer.add_argument("--nonce", required=True)
    answer.add_argument("--choice", required=True)

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

    retry = sub.add_parser("retry-item")
    retry.add_argument("--repo", required=True)
    retry.add_argument("--item-id", required=True)
    retry.add_argument("--approval-nonce", required=True)

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


def _background_model_options() -> tuple[tuple[str, str, str], list[tuple[str, str, str]]]:
    codex_reason = "use detected Codex defaults for model and effort"
    claude_reason = "use detected Claude defaults for model and effort"
    try:
        from agent_adapters import detect_agents
    except ImportError:  # pragma: no cover - package import path
        from scripts.agent_adapters import detect_agents
    agents = detect_agents()
    codex = agents.get("codex")
    claude = agents.get("claude")
    if codex and codex.available:
        codex_reason = f"use Codex model {codex.configured_model or 'default'} with effort {codex.configured_effort or 'default'}"
    if claude and claude.available:
        claude_reason = f"use Claude model {claude.configured_model or 'default'} with effort {claude.configured_effort or 'default'}"
    return (
        ("codex-default", "Codex detected defaults", codex_reason),
        [
            ("codex-manual", "Codex manual model/effort", "choose explicit --model and reasoning effort for Codex"),
            ("claude-default", "Claude detected defaults", claude_reason),
            ("claude-manual", "Claude manual model/effort", "choose explicit model and reasoning effort for Claude"),
        ],
    )


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
    if args.stage == "agent-choice":
        question = ledger.ask(
            args.prompt,
            recommended=foreground,
            alternatives=[("background", "Delegated foreground run", "choose a standalone sub-agent with visible output")],
        )
    elif args.stage == "background-model":
        recommended, alternatives = _background_model_options()
        question = ledger.ask(args.prompt, recommended=recommended, alternatives=alternatives)
    elif level.direct_execution_option:
        question = ledger.ask(
            args.prompt,
            recommended=(
                "skip-refinement-execute",
                "Skip refinement and execute",
                "skip optional refinement; this human choice is explicit execution launch approval",
            ),
            alternatives=[foreground, reviewer, criticizer],
            allow_auto_complete=False,
        )
    else:
        question = ledger.ask(
            args.prompt,
            recommended=foreground,
            alternatives=[reviewer, criticizer],
        )
    expected_seq = len(state.replay().events) + 1
    payload = question.to_json(expected_seq=expected_seq)
    payload["plan_level"] = level.to_json()
    state.append_event("pending_question", payload)
    print_json(payload)


def cmd_answer(args: argparse.Namespace) -> None:
    state = OptimPlansState.load_active(Path(args.repo))
    payload = state.record_answer(args.nonce, args.choice)
    print_json(payload)


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
    elif replayed.status in {"awaiting_retry_decision", "awaiting_integration"}:
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
            "status": cmd_status,
            "prepare-execution": cmd_prepare_execution,
            "start-execution": cmd_start_execution,
            "run-item": cmd_run_item,
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
