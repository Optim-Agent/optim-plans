#!/usr/bin/env python3
"""Strict state, artifact, interaction, and Git primitives for optim-plans."""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXECUTION_SCHEMA_VERSION = "0.1.2"
EXECUTION_PROTOCOL = "optim-plans-execution-v0.1.2"
MAX_EVIDENCE_CHARS = 4096
TIMEOUT_KILL_GRACE_SECONDS = 1.0
FULL_INTEGRATION_VERIFICATION_TIMEOUT_SECONDS = 300.0
ADAPTER_NAMES = {"claude", "codex"}
FINISH_OUTCOMES = {"integrated", "pr-opened", "kept", "discarded", "failed", "aborted"}
SMOKE_TESTED_WORKERS_CONFIG_KEY = "smoke_tested_workers"
WORKER_LAUNCH_FILES_CONFIG_KEY = "worker_launch_files"
EXECUTION_SUMMARY_CONFIG_KEY = "execution_summary"
EXECUTION_SUMMARY_FILE = "EXECUTION_SUMMARY.md"
LANGUAGE_CONFIG_KEY = "language"
LANGUAGE_SELECTION_STAGE = "language-selection"
HOST_EXECUTOR_PROMPT_PROTOCOL = "optim-plans-host-executor-v1"
HOST_EXECUTOR_RESULT_SCHEMA = "optim-plans-worker-result-v1"
HOST_VALIDATOR_PROMPT_PROTOCOL = "optim-plans-host-validator-v1"
HOST_VALIDATOR_RESULT_SCHEMA = "optim-plans-validator-result-v1"
ITEM_RETRYABLE_FAILURE_EVENTS = {
    "worker_failed",
    "validator_result_recorded",
    "validator_protocol_rejected",
    "validator_failed",
    "verification_failed",
    "audit_failed",
}
BATCH_RETRYABLE_FAILURE_EVENTS = {
    "batch_worker_failed",
    "batch_validator_result_recorded",
    "batch_validator_protocol_rejected",
    "batch_validator_failed",
    "batch_verification_failed",
    "batch_audit_failed",
}
RETRYABLE_FAILURE_EVENTS = ITEM_RETRYABLE_FAILURE_EVENTS | BATCH_RETRYABLE_FAILURE_EVENTS
IGNORED_AUDIT_NOISE_PATTERNS = [".xsw/", ".pytest_cache/", "__pycache__/", "*.pyc"]
PLAN_CONTEXT_REQUIRED_SECTIONS = (
    "Requirements",
    "Acceptance Criteria",
    "Implementation Items",
    "Verifier Checklist",
    "Non-Goals",
    "Constraints",
)
PLAN_CONTEXT_CRITICAL_SECTIONS = (
    "Requirements",
    "Acceptance Criteria",
    "Implementation Items",
    "Verifier Checklist",
)
PLAN_CONTEXT_SECTION_CHAR_LIMIT = 6000
PLAN_CONTEXT_TOTAL_CHAR_LIMIT = 24000
PLAN_CONTEXT_FILE_RE = re.compile(r"^PLAN_v([0-9]+)\.md$")
HOST_EXECUTOR_PROMPT_CONTRACT = {
    "instructions": [
        "Modify only the assigned run worktree.",
        "Leave ignored audit noise untouched; the controller ignores .xsw/, .pytest_cache/, __pycache__/, and *.pyc.",
        "Keep pursuing the assigned goal until complete or genuinely blocked.",
        "Return concise completion evidence to the host.",
        "The controller, not the worker, performs verification, audit, checkpoint, retry, and finalization.",
    ],
    "required_result_fields": ["status", "evidence"],
}
VALIDATOR_PROMPT_CONTRACT = {
    "instructions": [
        "Validate the executor delta before controller verification.",
        "Aim for the best complete implementation.",
        "Consider the problem from multiple angles.",
        "Avoid accepting an MVP-only task completion.",
        "Return only the required validator result JSON.",
    ],
    "required_result_fields": ["status", "evidence", "feedback_for_executor", "checked_items"],
}
LEGACY_ACTIVE_EVENT_TYPES = {"item_completed", "execution_completed", "run_completed", "worker_result_recorded"}
LIFECYCLE_EVENT_TYPES = {
    "execution_manifest_created",
    "source_snapshot_committed",
    "execution_started",
    "item_started",
    "batch_started",
    "host_spawn_authorized",
    "batch_host_spawn_authorized",
    "host_agent_registered",
    "batch_agent_registered",
    "worker_completed",
    "batch_completed",
    "worker_failed",
    "batch_worker_failed",
    "validator_assigned",
    "batch_validator_assigned",
    "validator_spawn_authorized",
    "batch_validator_spawn_authorized",
    "validator_agent_registered",
    "batch_validator_agent_registered",
    "validator_result_recorded",
    "batch_validator_result_recorded",
    "validator_protocol_rejected",
    "batch_validator_protocol_rejected",
    "validator_failed",
    "batch_validator_failed",
    "context_integrity_recovery",
    "batch_context_integrity_recovery",
    "verification_failed",
    "batch_verification_failed",
    "audit_failed",
    "batch_audit_failed",
    "awaiting_retry_decision",
    "execution_blocked",
    "batch_execution_blocked",
    "retry_restored",
    "batch_retry_restored",
    "item_verified",
    "checkpoint_prepared",
    "batch_checkpoint_prepared",
    "checkpoint_created",
    "batch_checkpoint_created",
    "final_audit_passed",
    "awaiting_integration",
    "integration_verification_failed",
    "run_finished",
}


class ContractError(RuntimeError):
    """A user-actionable optim-plans contract violation."""


def host_agent(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    if any(key.startswith("CLAUDE") for key in env):
        return "claude"
    if any(key.startswith("CODEX") for key in env):
        return "codex"
    return "codex"


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number {value!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ContractError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def parse_json_strict(text: str, *, source: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"Invalid JSON in {source}: {exc}") from exc


def json_text(value: Any, *, pretty: bool = False) -> str:
    kwargs: dict[str, Any] = {"ensure_ascii": True, "allow_nan": False, "sort_keys": True}
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs)


def bounded_evidence(text: str, *, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[truncated]\n"
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json_text(payload, pretty=True) + "\n").encode()
    try:
        with tmp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def append_json_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json_text(payload) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def git_maybe(repo: Path, *args: str) -> str | None:
    try:
        return git(repo, *args)
    except subprocess.CalledProcessError:
        return None


_UNSAFE_COMMIT_SUBJECT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _collapse_commit_subject_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _manifest_commit_subject_text(value: Any) -> str | None:
    if not isinstance(value, str) or "\0" in value or _UNSAFE_COMMIT_SUBJECT_CONTROL.search(value):
        return None
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return _collapse_commit_subject_text(stripped)
    return None


def _safe_commit_subject_path(path: str) -> str:
    safe = _UNSAFE_COMMIT_SUBJECT_CONTROL.sub(" ", path)
    return _collapse_commit_subject_text(safe) or "changed path"


def _checkpoint_commit_subject(item: dict[str, Any], changed_files: list[str]) -> str:
    for key in ("commit_message", "summary", "description"):
        subject = _manifest_commit_subject_text(item.get(key))
        if subject:
            return subject
    if len(changed_files) == 1:
        return f"Update {_safe_commit_subject_path(changed_files[0])}"
    if changed_files:
        return f"Update {len(changed_files)} files"
    return "Record empty checkpoint"


def git_common_dir(repo: Path) -> Path:
    common = git(repo, "rev-parse", "--git-common-dir")
    path = Path(common)
    if not path.is_absolute():
        path = repo / path
    return path.absolute()


def optim_plans_state_dir(repo: Path) -> Path:
    return (git_common_dir(repo) / "optim-plans").resolve()


def optim_plans_config_path(repo: Path) -> Path:
    return optim_plans_state_dir(repo) / "config.json"


def read_optim_plans_config(repo: Path) -> dict[str, Any]:
    try:
        payload = json.loads(optim_plans_config_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("schema") == 1 else {}


def save_optim_plans_config_value(repo: Path, key: str, value: Any) -> None:
    config = read_optim_plans_config(repo)
    config.update({"schema": 1, key: value})
    write_json_atomic(optim_plans_config_path(repo), config)


_LANGUAGE_TAG_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")


def normalize_language_tag(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value != value.strip():
        return None
    tag = value
    if tag.lower() in {"auto", "other"} or not _LANGUAGE_TAG_RE.fullmatch(tag):
        return None
    parts = tag.split("-")
    if len(parts) > 8:
        return None
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def read_config_language(repo: Path) -> str | None:
    return normalize_language_tag(read_optim_plans_config(repo).get(LANGUAGE_CONFIG_KEY))


def language_renders_chinese(language: str | None) -> bool:
    normalized = normalize_language_tag(language)
    return bool(normalized and normalized.split("-", 1)[0] == "zh")


def _text(language: str | None, english: str, chinese: str) -> str:
    return chinese if language_renders_chinese(language) else english


def recommended_language_option(request_text: str) -> str:
    body = re.sub(r"`[^`]*`", " ", request_text)
    body = re.sub(r"\b[\w./-]+\.(?:py|md|json|txt|ya?ml|toml|ini|cfg|sh)\b", " ", body)
    chinese = len(re.findall(r"[\u3400-\u9fff]", body))
    english = len(re.findall(r"[A-Za-z]+", body))
    return "zh-hans" if chinese and chinese / max(chinese + english, 1) > 0.6 else "en"


def _language_selection_options(language: str | None, recommended: str) -> list[dict[str, Any]]:
    language_options = [
        {
            "id": "zh-hans",
            "label": _text(language, "Simplified Chinese", "简体中文"),
            "reason": _text(language, "render controller text and artifacts in Chinese", "控制器文本和产物使用中文"),
            "language_value": "zh-Hans",
        },
        {
            "id": "en",
            "label": "English",
            "reason": _text(language, "render controller text and artifacts in English", "控制器文本和产物使用英文"),
            "language_value": "en",
        },
        {
            "id": "zh-hant",
            "label": _text(language, "Traditional Chinese", "繁体中文"),
            "reason": _text(language, "render controller text and artifacts in Chinese", "控制器文本和产物使用中文"),
            "language_value": "zh-Hant",
        },
    ]
    ordered = [option for option in language_options if option["id"] == recommended]
    ordered.extend(option for option in language_options if option["id"] != recommended)
    ordered.extend(
        [
            {
                "id": "other",
                "label": _text(language, "Other", "其他"),
                "reason": _text(language, "provide a supported language choice to continue", "提供可支持的语言选择后继续"),
            },
            {
                "id": "auto",
                "label": _text(language, "Auto-complete", "自动完成"),
                "reason": _text(language, "use the recommended language", "使用推荐语言"),
            },
        ]
    )
    return ordered


def language_selection_question_payload(request_text: str, *, expected_seq: int | None = None) -> dict[str, Any]:
    recommended = recommended_language_option(request_text)
    language = "zh-Hans" if recommended == "zh-hans" else "en"
    payload: dict[str, Any] = {
        "nonce": uuid.uuid4().hex,
        "prompt": _text(language, "Choose the optim-plans language.", "选择 optim-plans 输出语言。"),
        "options": _language_selection_options(language, recommended),
        "recommended_option_id": recommended,
        "free_form": {"option_id": "other", "required": False},
        "stage": LANGUAGE_SELECTION_STAGE,
    }
    if expected_seq is not None:
        payload["expected_seq"] = expected_seq
    return payload


def language_value_for_choice(question: dict[str, Any], choice: str) -> str | None:
    if choice == "auto":
        choice = str(question.get("recommended_option_id", ""))
    for option in question.get("options", []):
        if option.get("id") == choice:
            return normalize_language_tag(option.get("language_value"))
    return None


def _default_worker_launch_files(repo: Path) -> dict[str, Path]:
    root = optim_plans_state_dir(repo) / "launch-files"
    return {
        "codex_home": root / "codex-home",
        "claude_settings": root / "claude-settings" / "settings.json",
        "claude_plugin_dir": root / "claude-plugin",
    }


def worker_launch_files(repo: Path) -> dict[str, Path]:
    defaults = _default_worker_launch_files(repo)
    expected = {key: str(path) for key, path in defaults.items()}
    raw = read_optim_plans_config(repo).get(WORKER_LAUNCH_FILES_CONFIG_KEY)
    if raw is None:
        save_optim_plans_config_value(repo, WORKER_LAUNCH_FILES_CONFIG_KEY, expected)
        return defaults
    if not isinstance(raw, dict):
        raise ContractError("worker_launch_files config must be an object")
    merged = dict(raw)
    changed = False
    for key, value in expected.items():
        configured = raw.get(key)
        if configured is None:
            merged[key] = value
            changed = True
        elif configured != value:
            raise ContractError(f"worker_launch_files.{key} must be {value}")
    if changed:
        save_optim_plans_config_value(repo, WORKER_LAUNCH_FILES_CONFIG_KEY, merged)
    return defaults


def _smoke_worker_payload(worker: dict[str, Any]) -> dict[str, Any]:
    return _json_clone(
        {
            "adapter": worker.get("adapter"),
            "argv": worker.get("argv"),
            "env": worker.get("env", {}),
            "config_files": worker.get("config_files", []),
            "smoke": worker.get("smoke"),
        },
        source="smoke-tested worker config",
    )


def _smoke_worker_identity(worker: dict[str, Any]) -> dict[str, Any]:
    return _json_clone(
        {
            "adapter": worker.get("adapter"),
            "argv": worker.get("argv"),
            "env": worker.get("env", {}),
            "config_files": worker.get("config_files", []),
        },
        source="worker config identity",
    )


def _smoke_tested_workers(repo: Path) -> list[dict[str, Any]]:
    entries = read_optim_plans_config(repo).get(SMOKE_TESTED_WORKERS_CONFIG_KEY, [])
    if not isinstance(entries, list):
        return []
    return [_json_clone(entry, source="smoke-tested worker entry") for entry in entries if isinstance(entry, dict)]


def smoke_tested_worker_is_cached(repo: Path, worker: dict[str, Any]) -> bool:
    return _smoke_worker_payload(worker) in _smoke_tested_workers(repo)


def cached_smoke_tested_worker(repo: Path, worker: dict[str, Any]) -> dict[str, Any] | None:
    identity = _smoke_worker_identity(worker)
    for entry in _smoke_tested_workers(repo):
        if _smoke_worker_identity(entry) == identity:
            return entry
    return None


def remember_smoke_tested_worker(repo: Path, worker: dict[str, Any]) -> None:
    entry = _smoke_worker_payload(worker)
    entries = _smoke_tested_workers(repo)
    if entry in entries:
        return
    save_optim_plans_config_value(repo, SMOKE_TESTED_WORKERS_CONFIG_KEY, [*entries, entry])


def canonical_worktree_id(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]


def slugify_topic(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug or "plan"


def create_artifact_dir(repo: Path, topic: str, *, date: str | None = None) -> Path:
    date = date or _dt.date.today().isoformat()
    root = repo / "docs" / "optim-plans"
    root.mkdir(parents=True, exist_ok=True)
    base = f"{date}-{slugify_topic(topic)}"
    candidate = root / base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def _json_clone(value: Any, *, source: str) -> Any:
    try:
        return parse_json_strict(json_text(value), source=source)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Invalid JSON in {source}: {exc}") from exc


def canonical_execution_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    canonical = _json_clone(manifest, source="execution manifest")
    if not isinstance(canonical, dict):
        raise ContractError("execution manifest must be a JSON object")
    items = canonical.get("items")
    if not isinstance(items, list) or not items:
        raise ContractError("execution manifest items are required")

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    deps_by_id: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ContractError("execution manifest items must be JSON objects")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id != item_id.strip():
            raise ContractError("execution manifest item ids must be non-empty unique strings")
        if item_id in by_id:
            raise ContractError(f"duplicate execution manifest item id {item_id!r}")
        depends_on = item.get("depends_on", [])
        if depends_on is None:
            depends_on = []
        if not isinstance(depends_on, list):
            raise ContractError(f"depends_on for {item_id} must be a list")
        deps: list[str] = []
        for dep in depends_on:
            if not isinstance(dep, str) or not dep.strip():
                raise ContractError(f"dependency for {item_id} must be a non-empty string")
            if dep == item_id:
                raise ContractError(f"execution manifest item {item_id} depends on itself")
            deps.append(dep)
        item["depends_on"] = deps
        by_id[item_id] = item
        order.append(item_id)
        deps_by_id[item_id] = deps

    for item_id, deps in deps_by_id.items():
        for dep in deps:
            if dep not in by_id:
                raise ContractError(f"unknown dependency {dep!r} for {item_id}")

    remaining = set(order)
    ordered: list[str] = []
    # ponytail: O(n^2) topo scan, heap by original index only if manifests get large.
    while remaining:
        progressed = False
        for item_id in order:
            if item_id in remaining and all(dep not in remaining for dep in deps_by_id[item_id]):
                ordered.append(item_id)
                remaining.remove(item_id)
                progressed = True
        if not progressed:
            raise ContractError("execution manifest item dependencies contain a cycle")
    canonical["items"] = [by_id[item_id] for item_id in ordered]
    return canonical


def execution_manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json_text(canonical_execution_manifest(manifest)).encode()).hexdigest()


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def _git_with_env(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def host_executor_prompt_hash() -> str:
    return stable_json_hash(HOST_EXECUTOR_PROMPT_CONTRACT)


def validator_prompt_hash() -> str:
    return stable_json_hash(VALIDATOR_PROMPT_CONTRACT)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_plan_context_sections() -> dict[str, dict[str, Any]]:
    return {
        section: {
            "present": False,
            "status": "missing",
            "content": "",
            "source_chars": 0,
            "included_chars": 0,
            "truncated": False,
        }
        for section in PLAN_CONTEXT_REQUIRED_SECTIONS
    }


def _plan_context_base(
    artifact_dir: Path,
    *,
    section_char_limit: int,
    total_char_limit: int,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "source_path": None,
        "source_hash": None,
        "source_version": None,
        "artifact_dir": str(artifact_dir),
        "limits": {"section_chars": section_char_limit, "total_chars": total_char_limit},
        "required_sections": list(PLAN_CONTEXT_REQUIRED_SECTIONS),
        "critical_sections": list(PLAN_CONTEXT_CRITICAL_SECTIONS),
        "truncated": False,
        "truncation": {
            "whole": False,
            "sections": [],
            "audit_breaking": False,
            "audit_breaking_critical_sections": [],
        },
        "sections": _empty_plan_context_sections(),
    }


def _highest_plan_path(artifact_dir: Path) -> tuple[int, Path] | None:
    if not artifact_dir.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for path in artifact_dir.iterdir():
        match = PLAN_CONTEXT_FILE_RE.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        candidate = (int(match.group(1)), path)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _markdown_h2_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        match = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if match is not None:
            current = match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "".join(lines).strip("\n") for name, lines in sections.items()}


def plan_context(
    artifact_dir: Path | str,
    *,
    section_char_limit: int = PLAN_CONTEXT_SECTION_CHAR_LIMIT,
    total_char_limit: int = PLAN_CONTEXT_TOTAL_CHAR_LIMIT,
) -> dict[str, Any]:
    artifact = Path(artifact_dir)
    context = _plan_context_base(artifact, section_char_limit=section_char_limit, total_char_limit=total_char_limit)
    selected = _highest_plan_path(artifact)
    if selected is None:
        context["unavailable_reason"] = "no PLAN_vN.md found"
        return context
    version, path = selected
    context.update({"source_path": str(path), "source_version": version})
    try:
        text = path.read_text(encoding="utf-8")
        source_hash = _hash_file(path)
    except (OSError, UnicodeError) as exc:
        context["unavailable_reason"] = str(exc)
        return context

    context.update({"status": "available", "source_hash": source_hash, "source_chars": len(text)})
    raw_sections = _markdown_h2_sections(text)
    remaining = total_char_limit
    truncated_sections: list[str] = []
    critical_truncated: list[str] = []
    whole_truncated = False
    sections: dict[str, dict[str, Any]] = {}
    for name in PLAN_CONTEXT_REQUIRED_SECTIONS:
        if name not in raw_sections:
            sections[name] = context["sections"][name]
            continue
        raw = raw_sections[name]
        source_chars = len(raw)
        content = raw[:section_char_limit]
        truncated = source_chars > len(content)
        if len(content) > remaining:
            content = content[: max(0, remaining)]
            truncated = True
            whole_truncated = True
        remaining -= len(content)
        if truncated:
            truncated_sections.append(name)
            if name in PLAN_CONTEXT_CRITICAL_SECTIONS:
                critical_truncated.append(name)
        status = "empty" if source_chars == 0 else "truncated" if truncated else "available"
        sections[name] = {
            "present": True,
            "status": status,
            "content": content,
            "source_chars": source_chars,
            "included_chars": len(content),
            "truncated": truncated,
        }
    context["sections"] = sections
    context["truncated"] = bool(truncated_sections or whole_truncated)
    context["included_chars"] = sum(section["included_chars"] for section in sections.values())
    context["truncation"] = {
        "whole": whole_truncated,
        "sections": truncated_sections,
        "audit_breaking": bool(critical_truncated),
        "audit_breaking_critical_sections": critical_truncated,
    }
    return context


def _path_signature(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"type": "missing"}
    info = os.lstat(path)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"type": "symlink", "mode": mode, "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {"type": "dir", "mode": mode}
    if stat.S_ISREG(info.st_mode):
        return {"type": "file", "mode": mode, "sha256": _hash_file(path)}
    return {"type": "special", "mode": mode}


def _tree_signature(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for parent, dirs, files in os.walk(root):
        names = sorted(dirs + files)
        for name in names:
            path = Path(parent) / name
            rel = path.relative_to(root).as_posix()
            out[rel] = _path_signature(path)
    return out


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _protected_refs_snapshot(repo: Path, *, run_branch: str) -> dict[str, str]:
    excluded = f"refs/heads/{run_branch}"
    refs: dict[str, str] = {}
    output = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    for line in output.splitlines():
        if not line:
            continue
        ref, commit = line.split(" ", 1)
        if ref != excluded:
            refs[ref] = commit
    return refs


def _worktree_registry_snapshot(repo: Path, *, run_worktree: Path) -> list[str]:
    raw = git(repo, "worktree", "list", "--porcelain")
    records: list[str] = []
    for block in raw.strip().split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        registered = Path(lines[0].removeprefix("worktree ")).resolve()
        if registered == run_worktree.resolve():
            lines = ["HEAD <controller-managed>" if line.startswith("HEAD ") else line for line in lines]
        records.extend(lines + [""])
    return records


def _config_snapshot(common: Path) -> dict[str, dict[str, Any]]:
    paths = [common / "config", common / "config.worktree"]
    worktrees = common / "worktrees"
    if worktrees.exists():
        paths.extend(worktrees.glob("*/config.worktree"))
    return {
        path.relative_to(common).as_posix(): _path_signature(path)
        for path in sorted(paths)
        if path.exists() or path.is_symlink()
    }


def _protected_metadata_snapshot(repo: Path, *, run_branch: str, run_worktree: Path) -> dict[str, Any]:
    common = git_common_dir(repo)
    return {
        "source_ref": git_maybe(repo, "symbolic-ref", "-q", "HEAD") or "DETACHED",
        "source_head": git(repo, "rev-parse", "--verify", "HEAD"),
        "refs": _protected_refs_snapshot(repo, run_branch=run_branch),
        "config": _config_snapshot(common),
        "hooks": _tree_signature(common / "hooks"),
        "worktrees": _worktree_registry_snapshot(repo, run_worktree=run_worktree),
    }


def _status_entries(repo: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    fields = result.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        status_code = raw[:2]
        path = raw[3:].rstrip("/")
        if "R" in status_code or "C" in status_code:
            original_path = fields[index].rstrip("/")
            index += 1
            if original_path:
                entries.append((status_code, original_path))
        if path:
            entries.append((status_code, path))
    return entries


def _pathspec_exclusions(repo: Path, ignored_paths: list[Path] | None) -> list[str]:
    root = repo.resolve()
    exclusions: list[str] = []
    for path in ignored_paths or []:
        try:
            relative = Path(path).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        exclusions.append(f":(exclude){relative}")
    return exclusions


def _source_status_entries(repo: Path, *, ignored_paths: list[Path] | None = None) -> list[tuple[str, str]]:
    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
            *_pathspec_exclusions(repo, ignored_paths),
        ],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    fields = result.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        status_code = raw[:2]
        path = raw[3:].rstrip("/")
        if "R" in status_code or "C" in status_code:
            original_path = fields[index].rstrip("/")
            index += 1
            if original_path:
                entries.append((status_code, original_path))
        if path:
            entries.append((status_code, path))
    return entries


def _is_allowed_ignored_audit_noise(path: str) -> bool:
    return (
        path == ".xsw"
        or path.startswith(".xsw/")
        or path == ".pytest_cache"
        or path.startswith(".pytest_cache/")
        or path == "__pycache__"
        or path.endswith("/__pycache__")
        or "/__pycache__/" in path
        or path.endswith(".pyc")
    )


def ignored_audit_noise_policy() -> dict[str, Any]:
    return {"action": "leave_untouched", "patterns": list(IGNORED_AUDIT_NOISE_PATTERNS)}


def _diff_paths(repo: Path, base_commit: str, head_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", base_commit, head_commit],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [path.rstrip("/") for path in result.stdout.split("\0") if path]


def _normalize_scope(scope: str) -> str:
    path = Path(scope)
    if scope.startswith("/") or ".." in path.parts:
        raise ContractError(f"path scope escapes repository: {scope}")
    normalized = path.as_posix().rstrip("/")
    return "." if normalized in ("", ".") else normalized


def _path_in_scope(path: str, scopes: list[str]) -> bool:
    if "." in scopes:
        return True
    return any(path == scope or path.startswith(f"{scope}/") for scope in scopes)


def _ls_tree_modes(repo: Path, commit: str, path: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-rz", commit, "--", path],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    modes = []
    for entry in result.stdout.split("\0"):
        if entry:
            modes.append(entry.split(" ", 1)[0])
    return modes


def _index_modes(repo: Path, path: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", path],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [line.split(" ", 1)[0] for line in result.stdout.splitlines() if line]


def _contains_nested_repo(repo: Path, path: str) -> bool:
    root = repo.resolve()
    candidate = (root / path).resolve()
    probe = candidate if candidate.is_dir() else candidate.parent
    while probe != root and root in probe.parents:
        if (probe / ".git").exists():
            return True
        probe = probe.parent
    return False


def audit_git_delta(
    repo: Path,
    *,
    allowed_paths: list[str],
    base_commit: str | None = None,
    head_commit: str | None = None,
) -> dict[str, Any]:
    scopes = [_normalize_scope(scope) for scope in allowed_paths]
    if not scopes:
        raise ContractError("allowed paths are required for Git delta audit")
    resolve_path_scopes(repo, scopes)

    paths: dict[str, str] = {}
    for status_code, path in _status_entries(repo):
        if status_code == "!!":
            if _is_allowed_ignored_audit_noise(path):
                continue
            raise ContractError(f"ignored change is not allowed: {path}")
        paths[path] = "status"
    if base_commit is not None:
        head = head_commit or git(repo, "rev-parse", "--verify", "HEAD")
        for path in _diff_paths(repo, base_commit, head):
            paths.setdefault(path, "committed")

    for path, source in sorted(paths.items()):
        if not _path_in_scope(path, scopes):
            raise ContractError(f"{source} change is out of scope: {path}")
        candidate = repo / path
        if candidate.is_symlink():
            raise ContractError(f"symlink change is not allowed: {path}")
        if _contains_nested_repo(repo, path):
            raise ContractError(f"nested repository change is not allowed: {path}")
        modes = set(_index_modes(repo, path))
        if base_commit is not None:
            modes.update(_ls_tree_modes(repo, base_commit, path))
        if head_commit is not None:
            modes.update(_ls_tree_modes(repo, head_commit, path))
        if "120000" in modes:
            raise ContractError(f"symlink change is not allowed: {path}")
        if "160000" in modes:
            raise ContractError(f"gitlink change is not allowed: {path}")
    return {"status": "passed", "changed_files": sorted(paths)}


def checkpoint_delta_fingerprint(repo: Path, changed_files: list[str]) -> str:
    status = sorted(
        (status_code, path)
        for status_code, path in _status_entries(repo)
        if status_code != "!!" or not _is_allowed_ignored_audit_noise(path)
    )
    return stable_json_hash(
        {
            "head": git(repo, "rev-parse", "--verify", "HEAD"),
            "status": status,
            "paths": {path: _path_signature(repo / path) for path in sorted(changed_files)},
        }
    )


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    launch_error: str | None = None

    def ok(self) -> bool:
        return not self.timed_out and self.launch_error is None and self.returncode == 0

    def evidence(self, label: str, *, timeout_seconds: float | None) -> str:
        if self.launch_error is not None:
            head = f"{label} launch failed: {self.launch_error}"
        elif self.timed_out:
            head = f"{label} timed out after {timeout_seconds:g}s"
        else:
            head = f"{label} exited {self.returncode}"
        body = "\n".join(
            [
                head,
                f"argv: {json_text(self.argv)}",
                f"stdout:\n{self.stdout}",
                f"stderr:\n{self.stderr}",
            ]
        )
        return bounded_evidence(body)


def _terminate_process_group(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return proc.communicate(timeout=TIMEOUT_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return proc.communicate()


def run_process_group(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float | None,
) -> ProcessResult:
    if not argv or not all(isinstance(part, str) for part in argv) or not argv[0]:
        raise ContractError("subprocess argv must have a non-empty executable and string arguments")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ContractError("timeout_seconds must be positive")
    try:
        proc: subprocess.Popen[str] = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
        )
    except OSError as exc:
        return ProcessResult(list(argv), None, launch_error=str(exc))
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return ProcessResult(list(argv), proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process_group(proc)
        return ProcessResult(list(argv), proc.returncode, stdout or "", stderr or "", timed_out=True)


@dataclass(frozen=True)
class ReplayState:
    events: list[dict[str, Any]]
    status: str
    status_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetryPolicyDecision:
    action: str
    failure_signature: dict[str, Any] | None = None
    equivalent_failures: int = 0
    total_failures: int = 0
    reason: str = ""


def is_legacy_active_run(events: list[dict[str, Any]]) -> bool:
    has_new_lifecycle = any(
        event["type"] in LIFECYCLE_EVENT_TYPES
        or (
            event["type"] == "pending_question"
            and event.get("payload", {}).get("stage") in {"execution_launch", "execution_retry"}
        )
        for event in events
    )
    return not has_new_lifecycle and any(event["type"] in LEGACY_ACTIVE_EVENT_TYPES for event in events)


def lifecycle_status(events: list[dict[str, Any]]) -> str:
    if is_legacy_active_run(events):
        return "legacy_active"
    status = "planning"
    for event in events:
        event_type = event["type"]
        payload = event.get("payload", {})
        if event_type == "run_finished":
            outcome = payload.get("outcome")
            if outcome == "failed":
                return "failed"
            elif outcome == "aborted":
                return "aborted"
            return "completed"
        elif event_type == "awaiting_integration":
            status = "awaiting_integration"
        elif event_type == "integration_verification_failed":
            status = "awaiting_integration"
        elif event_type == "awaiting_retry_decision":
            status = "awaiting_retry_decision"
        elif event_type in {"context_integrity_recovery", "batch_context_integrity_recovery"}:
            status = "context_integrity_recovery"
        elif event_type in {"execution_blocked", "batch_execution_blocked"}:
            status = "blocked"
        elif event_type in {
            "worker_failed",
            "batch_worker_failed",
            "validator_protocol_rejected",
            "batch_validator_protocol_rejected",
            "validator_failed",
            "batch_validator_failed",
            "verification_failed",
            "batch_verification_failed",
            "audit_failed",
            "batch_audit_failed",
        }:
            status = "awaiting_retry_decision"
        elif event_type in {"validator_result_recorded", "batch_validator_result_recorded"}:
            status = "verifying" if payload.get("status") == "pass" else "awaiting_retry_decision"
        elif event_type in {"worker_completed", "batch_completed"}:
            status = "validating"
        elif event_type == "pending_question" and payload.get("stage") == "execution_launch":
            status = "awaiting_approval"
        elif event_type == "execution_manifest_created":
            status = "awaiting_approval"
        elif event_type in {
            "execution_started",
            "item_started",
            "batch_started",
            "host_spawn_authorized",
            "batch_host_spawn_authorized",
            "host_agent_registered",
            "batch_agent_registered",
            "validator_assigned",
            "batch_validator_assigned",
            "validator_spawn_authorized",
            "batch_validator_spawn_authorized",
            "validator_agent_registered",
            "batch_validator_agent_registered",
            "retry_restored",
            "batch_retry_restored",
            "item_verified",
            "checkpoint_prepared",
            "batch_checkpoint_prepared",
            "checkpoint_created",
            "batch_checkpoint_created",
            "final_audit_passed",
        }:
            status = "executing"
    return status


def _context_integrity_projection(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("plan_context")
    context = context if isinstance(context, dict) else {}
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "item_id": payload.get("item_id"),
        "batch_id": payload.get("batch_id"),
        "item_ids": payload.get("item_ids", []),
        "source_path": context.get("source_path"),
        "source_hash": context.get("source_hash"),
        "truncated": context.get("truncated", False),
        "truncation": context.get("truncation", {}),
    }


def _context_integrity_summary(payload: dict[str, Any]) -> str:
    projected = _context_integrity_projection(payload)
    return (
        f"reason={projected['reason']}; status={projected['status']}; "
        f"source_path={projected['source_path']}; source_hash={projected['source_hash']}; "
        f"truncation={json_text(projected['truncation'])}"
    )


def lifecycle_status_details(events: list[dict[str, Any]]) -> dict[str, Any]:
    if lifecycle_status(events) != "context_integrity_recovery":
        return {}
    for event in reversed(events):
        if event["type"] in {"context_integrity_recovery", "batch_context_integrity_recovery"}:
            return {"context_integrity_recovery": _context_integrity_projection(event.get("payload", {}))}
    return {}


def latest_preserved_run(repo: Path | str) -> dict[str, Any]:
    repo = Path(repo).absolute()
    runs_dir = git_common_dir(repo) / "optim-plans" / "runs"
    best: tuple[str, str, dict[str, Any]] | None = None
    if not runs_dir.exists():
        return {"candidate": None}
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            run = parse_json_strict((run_dir / "run.json").read_text(encoding="utf-8"), source=str(run_dir / "run.json"))
            events = []
            for line_number, line in enumerate((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip():
                    event = parse_json_strict(line, source=f"{run_dir / 'events.jsonl'}:{line_number}")
                    expected = len(events) + 1
                    if event.get("schema") != SCHEMA_VERSION or event.get("seq") != expected:
                        raise ContractError("event sequence gap or schema mismatch")
                    if not isinstance(event.get("type"), str):
                        raise ContractError("event type missing")
                    events.append(event)
        except (ContractError, OSError):
            continue
        run_id = run.get("run_id")
        if not isinstance(run_id, str):
            continue
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("type") == "run_finished" and event.get("payload", {}).get("preserved") is True
            ),
            None,
        )
        if terminal is None:
            continue
        last_event = events[-1] if events else terminal
        candidate = {
            "run_id": run_id,
            "status": lifecycle_status(events),
            "artifact_dir": run.get("artifact_dir"),
            "terminal_time": terminal.get("time", ""),
            "outcome": terminal.get("payload", {}).get("outcome"),
            "last_event_type": last_event.get("type"),
            "last_event_time": last_event.get("time", ""),
            "next_action": "inspect the artifact_dir or run worktree; this command does not restore an active pointer",
        }
        key = (str(candidate["terminal_time"]), run_id, candidate)
        if best is None or key[:2] > best[:2]:
            best = key
    return {"candidate": None if best is None else best[2]}


@dataclass(frozen=True)
class OptimPlansState:
    repo: Path
    run_id: str
    root: Path
    run_dir: Path
    run_file: Path
    events_file: Path
    runtime_file: Path
    lock_file: Path
    active_file: Path
    artifact_dir: Path

    @classmethod
    def initialize(
        cls,
        repo: Path | str,
        *,
        topic: str,
        plan_hash: str,
        request_text: str | None = None,
    ) -> "OptimPlansState":
        repo = Path(repo).absolute()
        common = git_common_dir(repo)
        root = common / "optim-plans"
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        worktree_id = canonical_worktree_id(repo)
        active_file = root / "worktrees" / worktree_id / "active.json"
        if active_file.exists():
            active = parse_json_strict(active_file.read_text(), source=str(active_file))
            run_dir = root / "runs" / active["run_id"]
            if run_dir.exists():
                raise ContractError(f"active run already exists for worktree: {active['run_id']}")

        run_id = uuid.uuid4().hex
        run_dir = root / "runs" / run_id
        artifact_dir = create_artifact_dir(repo, topic)
        state = cls(
            repo=repo,
            run_id=run_id,
            root=root,
            run_dir=run_dir,
            run_file=run_dir / "run.json",
            events_file=run_dir / "events.jsonl",
            runtime_file=run_dir / "runtime.json",
            lock_file=run_dir / "controller.lock",
            active_file=active_file,
            artifact_dir=artifact_dir,
        )
        payload = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now(),
            "repo": str(repo),
            "worktree_id": worktree_id,
            "topic": topic,
            "plan_hash": plan_hash,
            "artifact_dir": str(artifact_dir.relative_to(repo)),
        }
        if request_text is not None:
            payload["request_text"] = request_text
        write_json_atomic(state.run_file, payload)
        state.events_file.parent.mkdir(parents=True, exist_ok=True)
        state.events_file.touch(mode=0o600)
        write_json_atomic(state.active_file, {"run_id": run_id, "artifact_dir": payload["artifact_dir"]})
        write_json_atomic(state.runtime_file, {"status": "initialized", "last_seq": 0})
        return state

    @classmethod
    def load_active(cls, repo: Path | str) -> "OptimPlansState":
        repo = Path(repo).absolute()
        root = git_common_dir(repo) / "optim-plans"
        active_file = root / "worktrees" / canonical_worktree_id(repo) / "active.json"
        if not active_file.exists():
            raise ContractError("no active optim-plans run for this worktree")
        active = parse_json_strict(active_file.read_text(), source=str(active_file))
        run_dir = root / "runs" / active["run_id"]
        run = parse_json_strict((run_dir / "run.json").read_text(), source="run.json")
        return cls(
            repo=repo,
            run_id=active["run_id"],
            root=root,
            run_dir=run_dir,
            run_file=run_dir / "run.json",
            events_file=run_dir / "events.jsonl",
            runtime_file=run_dir / "runtime.json",
            lock_file=run_dir / "controller.lock",
            active_file=active_file,
            artifact_dir=repo / run["artifact_dir"],
        )

    @classmethod
    def load(cls, repo: Path | str) -> "OptimPlansState":
        return cls.load_active(repo)

    @contextmanager
    def controller_lock(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def source_prepare_lock(self):
        path = self.root / "source-prepare.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.controller_lock():
            return self._append_event_locked(event_type, payload)

    def _run_language_request_text(self, *, force: bool = False) -> str | None:
        try:
            run = parse_json_strict(self.run_file.read_text(encoding="utf-8"), source=str(self.run_file))
        except OSError:
            return None
        request_text = run.get("request_text")
        if isinstance(request_text, str):
            return request_text
        topic = run.get("topic")
        return topic if force and isinstance(topic, str) else None

    def _pending_language_selection(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        answered = {
            event.get("payload", {}).get("nonce")
            for event in events
            if event["type"] == "answer_recorded"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if (
                event["type"] == "pending_question"
                and payload.get("stage") == LANGUAGE_SELECTION_STAGE
                and payload.get("nonce") not in answered
            ):
                return payload
        return None

    def _language_selection_question_payload_locked(
        self,
        events: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        pending = self._pending_language_selection(events)
        if pending is not None:
            return pending
        if read_config_language(self.repo) is not None:
            return None
        request_text = self._run_language_request_text(force=force)
        if request_text is None:
            return None
        payload = language_selection_question_payload(request_text, expected_seq=len(events) + 1)
        return self._append_event_locked("pending_question", payload)["payload"]

    def ensure_language_selection(self, *, force: bool = False) -> dict[str, Any] | None:
        with self.controller_lock():
            return self._language_selection_question_payload_locked(self.replay().events, force=force)

    def _controller_language(self) -> str:
        return read_config_language(self.repo) or "en"

    def _plan_context(self) -> dict[str, Any]:
        return plan_context(self.artifact_dir)

    def _append_event_locked(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        replayed = self.replay()
        event = {
            "schema": SCHEMA_VERSION,
            "seq": len(replayed.events) + 1,
            "time": utc_now(),
            "type": event_type,
            "payload": payload,
        }
        append_json_line(self.events_file, event)
        events = [*replayed.events, event]
        write_json_atomic(
            self.runtime_file,
            {"status": lifecycle_status(events), "status_details": lifecycle_status_details(events), "last_seq": event["seq"]},
        )
        if event_type in {
            "checkpoint_created",
            "batch_checkpoint_created",
            "final_audit_passed",
            "awaiting_integration",
            "integration_verification_failed",
            "run_finished",
        }:
            self._maybe_render_execution_summary_locked(events)
        return event

    def record_answer(self, nonce: str, choice: str, *, expected_seq: int | None = None) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            if expected_seq is not None and len(replayed.events) != expected_seq:
                raise ContractError("question answer expected sequence does not match state")
            found: dict[str, Any] | None = None
            for event in replayed.events:
                payload = event.get("payload", {})
                if event["type"] == "pending_question" and payload.get("nonce") == nonce:
                    found = payload
                if event["type"] == "answer_recorded" and payload.get("nonce") == nonce:
                    raise ContractError("stale or replayed question nonce")
            if found is None:
                raise ContractError("unknown question nonce")
            pending_expected_seq = found.get("expected_seq")
            if isinstance(pending_expected_seq, int) and len(replayed.events) != pending_expected_seq:
                raise ContractError("question answer expected sequence does not match state")
            choices = {option["id"] for option in found["options"]}
            if choice not in choices:
                raise ContractError(f"invalid answer choice {choice!r}")
            if found.get("stage") == LANGUAGE_SELECTION_STAGE:
                language = language_value_for_choice(found, choice)
                if language is None and choice != "other":
                    raise ContractError(f"invalid language choice {choice!r}")
                if language is not None:
                    existing_language = read_config_language(self.repo)
                    if existing_language is None:
                        save_optim_plans_config_value(self.repo, LANGUAGE_CONFIG_KEY, language)
                    elif existing_language != language:
                        raise ContractError("language-selection retry conflicts with persisted language")
            if found.get("stage") == "execution_summary" and choice == "always-skip-summary":
                save_optim_plans_config_value(self.repo, EXECUTION_SUMMARY_CONFIG_KEY, {"mode": "always-skip"})
            event = self._append_event_locked("answer_recorded", {"nonce": nonce, "choice": choice})
            return event["payload"]

    def _canonicalize_execution_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        canonical = _json_clone(manifest, source="execution manifest")
        if isinstance(canonical.get("worker"), dict):
            worker_item = {"id": "__manifest_worker__", "worker": canonical["worker"], "allowed_paths": []}
            canonical["worker"] = self._worker_config(canonical, worker_item)
        for item in canonical.get("items", []):
            if isinstance(item, dict) and isinstance(item.get("worker"), dict):
                item["worker"] = self._worker_config(canonical, item)
        if self._is_current_execution_manifest(canonical):
            self._require_current_execution_manifest(canonical)
            canonical["validator_worker"] = self._validator_config(canonical)
            canonical["validator_prompt"] = self._validator_prompt_config(canonical)
            for item in canonical["items"]:
                item["validator"] = {"check_ids": self._validator_check_ids(item)}
        return canonical

    def _is_current_execution_manifest(self, manifest: dict[str, Any]) -> bool:
        return "schema_version" in manifest or "protocol_version" in manifest or "validator_worker" in manifest

    def _require_current_execution_manifest(self, manifest: dict[str, Any]) -> None:
        if manifest.get("schema_version") != EXECUTION_SCHEMA_VERSION:
            raise ContractError(f"execution manifest schema_version must be {EXECUTION_SCHEMA_VERSION}")
        if manifest.get("protocol_version") != EXECUTION_PROTOCOL:
            raise ContractError(f"execution manifest protocol_version must be {EXECUTION_PROTOCOL}")
        retry_limit = manifest.get("validator_retry_limit")
        if not isinstance(retry_limit, int) or retry_limit < 0:
            raise ContractError("execution manifest validator_retry_limit must be a non-negative integer")

    def _validator_config(self, manifest: dict[str, Any]) -> dict[str, Any]:
        raw = manifest.get("validator_worker")
        if not isinstance(raw, dict):
            raise ContractError("execution manifest validator_worker config is required")
        if raw.get("mode") == "foreground":
            return self._foreground_validator_config(raw)
        config = self._worker_config({**manifest, "worker": raw}, {"id": "__manifest_validator__", "allowed_paths": []})
        if config.get("mode") == "host-multi-agent":
            if config.get("sandbox") != "read-only":
                raise ContractError("host validator sandbox must be read-only")
            if any(tool in {"Write", "Edit", "MultiEdit"} for tool in config.get("allowed_tools", [])):
                raise ContractError("host validator allowed_tools must be read-only")
        else:
            timeout = raw.get("timeout_seconds", manifest.get("validator_timeout_seconds", 300))
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ContractError("validator timeout_seconds must be positive")
            config["timeout_seconds"] = float(timeout)
        return config

    def _foreground_validator_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        platform = raw.get("platform", host_agent())
        if platform not in ADAPTER_NAMES:
            raise ContractError("foreground validator platform must be claude or codex")
        if raw.get("prompt_protocol") != HOST_VALIDATOR_PROMPT_PROTOCOL:
            raise ContractError(f"foreground validator prompt_protocol must be {HOST_VALIDATOR_PROMPT_PROTOCOL}")
        if raw.get("prompt_hash") != validator_prompt_hash():
            raise ContractError("foreground validator prompt_hash does not match the controller validator contract")
        if raw.get("result_schema") != HOST_VALIDATOR_RESULT_SCHEMA:
            raise ContractError(f"foreground validator result_schema must be {HOST_VALIDATOR_RESULT_SCHEMA}")
        return {
            "mode": "foreground",
            "platform": platform,
            "prompt_protocol": HOST_VALIDATOR_PROMPT_PROTOCOL,
            "prompt_hash": validator_prompt_hash(),
            "result_schema": HOST_VALIDATOR_RESULT_SCHEMA,
        }

    def _validator_prompt_config(self, manifest: dict[str, Any]) -> dict[str, Any]:
        raw = manifest.get("validator_prompt")
        if not isinstance(raw, dict):
            raise ContractError("execution manifest validator_prompt is required")
        protocol = raw.get("protocol")
        prompt_hash = raw.get("hash")
        contract = raw.get("contract")
        if protocol != HOST_VALIDATOR_PROMPT_PROTOCOL:
            raise ContractError(f"validator_prompt.protocol must be {HOST_VALIDATOR_PROMPT_PROTOCOL}")
        if contract != VALIDATOR_PROMPT_CONTRACT:
            raise ContractError("validator_prompt.contract must match the controller validator contract")
        if prompt_hash != validator_prompt_hash():
            raise ContractError("validator_prompt.hash does not match the controller validator contract")
        return {"protocol": protocol, "hash": prompt_hash, "contract": contract}

    def _validator_check_ids(self, item: dict[str, Any]) -> list[str]:
        raw = item.get("validator")
        check_ids = raw.get("check_ids") if isinstance(raw, dict) else item.get("validator_check_ids")
        if not isinstance(check_ids, list) or not check_ids:
            raise ContractError(f"validator.check_ids for {item['id']} must be a non-empty list")
        if not all(isinstance(check_id, str) and check_id.strip() for check_id in check_ids):
            raise ContractError(f"validator.check_ids for {item['id']} must contain non-empty strings")
        if len(set(check_ids)) != len(check_ids):
            raise ContractError(f"validator.check_ids for {item['id']} must not contain duplicates")
        return list(check_ids)

    def _temporary_source_index_tree(self, *, head: str) -> str:
        fd, raw_index = tempfile.mkstemp(prefix="source-snapshot-", dir=self.run_dir)
        os.close(fd)
        index = Path(raw_index)
        try:
            index.unlink()
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(index)
            _git_with_env(self.repo, env, "read-tree", head)
            _git_with_env(
                self.repo,
                env,
                "add",
                "-A",
                "--",
                ".",
                *_pathspec_exclusions(self.repo, [self.artifact_dir]),
            )
            return _git_with_env(self.repo, env, "write-tree")
        finally:
            if index.exists():
                index.unlink()

    def _source_snapshot(self) -> dict[str, Any] | None:
        entries = sorted(_source_status_entries(self.repo, ignored_paths=[self.artifact_dir]))
        if not entries:
            return None
        try:
            head = git(self.repo, "rev-parse", "--verify", "HEAD")
        except subprocess.CalledProcessError as exc:
            raise ContractError("source base commit is required before source auto-commit") from exc
        tree = self._temporary_source_index_tree(head=head)
        fingerprint = stable_json_hash({"head": head, "tree": tree, "status": entries})
        return {
            "head": head,
            "tree": tree,
            "fingerprint": fingerprint,
            "status": [[status, path] for status, path in entries],
        }

    def _matching_source_auto_commit_question(
        self,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None] | None:
        answers = {
            event.get("payload", {}).get("nonce"): event.get("payload", {}).get("choice")
            for event in events
            if event["type"] == "answer_recorded"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if (
                event["type"] == "pending_question"
                and payload.get("stage") == "source_auto_commit"
                and payload.get("source_head") == snapshot["head"]
                and payload.get("source_snapshot_fingerprint") == snapshot["fingerprint"]
            ):
                return payload, answers.get(payload.get("nonce"))
        return None

    def _source_auto_commit_question_payload_locked(
        self,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, "Commit dirty source snapshot before execution?", "执行前提交脏的源码快照？"),
            "options": [
                {
                    "id": "approve",
                    "label": _text(language, "Approve commit", "批准提交"),
                    "reason": _text(language, "create one controller source snapshot commit", "创建一个控制器源码快照提交"),
                },
                {
                    "id": "stop",
                    "label": _text(language, "Stop", "停止"),
                    "reason": _text(language, "do not create an execution manifest", "不创建执行清单"),
                },
                {
                    "id": "other",
                    "label": _text(language, "Other", "其他"),
                    "reason": _text(language, "free-form answer", "自由回答"),
                },
            ],
            "recommended_option_id": "approve",
            "free_form": {"option_id": "other", "required": False},
            "expected_seq": len(events) + 1,
            "stage": "source_auto_commit",
            "source_head": snapshot["head"],
            "source_tree": snapshot["tree"],
            "source_snapshot_fingerprint": snapshot["fingerprint"],
            "source_status": snapshot["status"],
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def _source_auto_commit_decision_locked(
        self,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        match = self._matching_source_auto_commit_question(events, snapshot)
        if match is None:
            return {"action": "ask", "question": self._source_auto_commit_question_payload_locked(events, snapshot)}
        question, choice = match
        if choice is None:
            return {"action": "ask", "question": question}
        if choice != "approve":
            raise ContractError("source auto-commit was not approved")
        return {"action": "commit", "question": question}

    def _commit_source_snapshot_locked(self, snapshot: dict[str, Any], *, approval_nonce: str) -> str:
        current = self._source_snapshot()
        if (
            current is None
            or current["head"] != snapshot["head"]
            or current["fingerprint"] != snapshot["fingerprint"]
        ):
            raise ContractError("approved source snapshot changed")
        commit = git(
            self.repo,
            "commit-tree",
            current["tree"],
            "-p",
            current["head"],
            "-m",
            "optim-plans source snapshot",
        )
        git(self.repo, "update-ref", "-m", "optim-plans source snapshot", "HEAD", commit, current["head"])
        git(self.repo, "reset", "-q", commit, "--", ".", *_pathspec_exclusions(self.repo, [self.artifact_dir]))
        self._append_event_locked(
            "source_snapshot_committed",
            {
                "approval_nonce": approval_nonce,
                "parent": current["head"],
                "commit": commit,
                "tree": current["tree"],
                "source_snapshot_fingerprint": current["fingerprint"],
            },
        )
        return commit

    def _same_source_snapshot(self, left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        return (
            left is not None
            and right is not None
            and left["head"] == right["head"]
            and left["fingerprint"] == right["fingerprint"]
        )

    def _manifest_with_source_base(self, manifest: dict[str, Any], source_base: str) -> dict[str, Any]:
        updated = _json_clone(manifest, source="execution manifest")
        updated["source_base"] = source_base
        updated["base_commit"] = source_base
        return updated

    def _persist_execution_manifest_locked(self, canonical: dict[str, Any]) -> dict[str, Any]:
        replayed = self.replay()
        self._require_lifecycle_locked(replayed.events, {"planning"}, "prepare-execution")
        if any(event["type"] == "execution_manifest_created" for event in replayed.events):
            raise ContractError("execution manifest is write-once")
        payload = {"manifest": canonical, "manifest_hash": execution_manifest_hash(canonical)}
        return self._append_event_locked("execution_manifest_created", payload)["payload"]

    def persist_execution_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        canonical = self._canonicalize_execution_manifest(canonical_execution_manifest(manifest))
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"planning"}, "prepare-execution")
            if any(event["type"] == "execution_manifest_created" for event in replayed.events):
                raise ContractError("execution manifest is write-once")
        self._smoke_execution_manifest(canonical)
        with self.controller_lock():
            return self._persist_execution_manifest_locked(canonical)

    def prepare_execution(self, manifest_path: Path) -> dict[str, Any]:
        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(f"cannot read execution manifest {manifest_path}: {exc}") from exc
        manifest = parse_json_strict(raw, source=str(manifest_path))
        if not isinstance(manifest, dict):
            raise ContractError("execution manifest must be a JSON object")
        if self._is_current_execution_manifest(manifest):
            self._require_current_execution_manifest(manifest)
        approved_snapshot: dict[str, Any] | None = None
        approved_question: dict[str, Any] | None = None
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"planning"}, "prepare-execution")
            if any(event["type"] == "execution_manifest_created" for event in replayed.events):
                raise ContractError("execution manifest is write-once")
            snapshot = self._source_snapshot()
            if snapshot is not None:
                decision = self._source_auto_commit_decision_locked(replayed.events, snapshot)
                if decision["action"] == "ask":
                    return decision["question"]
                approved_snapshot = snapshot
                approved_question = decision["question"]
            else:
                source_base = self._manifest_source_base(manifest)
                current_base = git(self.repo, "rev-parse", "--verify", "HEAD")
                if source_base != current_base:
                    raise ContractError("execution manifest source_base does not match current HEAD")

        self._smoke_execution_manifest(self._canonicalize_execution_manifest(canonical_execution_manifest(manifest)))

        with self.source_prepare_lock():
            with self.controller_lock():
                replayed = self.replay()
                self._require_lifecycle_locked(replayed.events, {"planning"}, "prepare-execution")
                if any(event["type"] == "execution_manifest_created" for event in replayed.events):
                    raise ContractError("execution manifest is write-once")
                snapshot = self._source_snapshot()
                if approved_snapshot is not None:
                    if not self._same_source_snapshot(snapshot, approved_snapshot):
                        if snapshot is None:
                            raise ContractError("approved source snapshot changed")
                        return self._source_auto_commit_question_payload_locked(replayed.events, snapshot)
                    assert approved_question is not None
                    commit = self._commit_source_snapshot_locked(snapshot, approval_nonce=approved_question["nonce"])
                    manifest = self._manifest_with_source_base(manifest, commit)
                elif snapshot is not None:
                    decision = self._source_auto_commit_decision_locked(replayed.events, snapshot)
                    if decision["action"] == "ask":
                        return decision["question"]
                    raise ContractError("source changed during execution manifest preparation")
                else:
                    source_base = self._manifest_source_base(manifest)
                    current_base = git(self.repo, "rev-parse", "--verify", "HEAD")
                    if source_base != current_base:
                        raise ContractError("execution manifest source_base does not match current HEAD")
                canonical = self._canonicalize_execution_manifest(canonical_execution_manifest(manifest))
                self._persist_execution_manifest_locked(canonical)
                return self._execution_approval_question_payload_locked(self.replay().events)

    def _execution_manifest_record(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        records = [event.get("payload", {}) for event in events if event["type"] == "execution_manifest_created"]
        if not records:
            records = self._legacy_execution_manifest_records(events)
        if not records:
            raise ContractError("execution manifest has not been recorded")
        if len(records) > 1:
            raise ContractError("execution manifest is write-once")
        payload = records[0]
        manifest = payload.get("manifest")
        manifest_hash = payload.get("manifest_hash")
        if not isinstance(manifest, dict) or not isinstance(manifest_hash, str):
            raise ContractError("execution manifest event is invalid")
        canonical = self._canonicalize_execution_manifest(canonical_execution_manifest(manifest))
        if manifest != canonical or execution_manifest_hash(canonical) != manifest_hash:
            raise ContractError("execution manifest hash mismatch")
        return {"manifest": canonical, "manifest_hash": manifest_hash}

    def _legacy_execution_manifest_records(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        legacy = [
            event.get("payload", {})
            for event in events
            if event["type"] in {"execution_manifest_written", "execution_manifest_superseded"}
        ]
        if not legacy:
            return []
        payload = legacy[-1]
        manifest_path = payload.get("new_manifest_path", payload.get("manifest_path"))
        manifest_hash = payload.get("new_manifest_hash", payload.get("manifest_hash"))
        if not isinstance(manifest_path, str) or not isinstance(manifest_hash, str):
            raise ContractError("legacy execution manifest event is invalid")
        path = Path(manifest_path)
        if not path.is_absolute():
            path = self.repo / path
        try:
            raw = path.read_bytes()
            manifest = parse_json_strict(raw.decode("utf-8"), source=str(path))
        except OSError as exc:
            raise ContractError(f"cannot read legacy execution manifest {path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ContractError("legacy execution manifest must be a JSON object")
        canonical = self._canonicalize_execution_manifest(canonical_execution_manifest(manifest))
        canonical_hash = execution_manifest_hash(canonical)
        if manifest_hash not in {canonical_hash, hashlib.sha256(raw).hexdigest()}:
            raise ContractError("legacy execution manifest hash mismatch")
        return [{"manifest": canonical, "manifest_hash": canonical_hash}]

    def _require_lifecycle_locked(self, events: list[dict[str, Any]], allowed: set[str], command: str) -> str:
        status = lifecycle_status(events)
        if status not in allowed:
            raise ContractError(f"{command} is not allowed while lifecycle is {status}")
        return status

    def _finish_question_payload_locked(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        language_gate = self._language_selection_question_payload_locked(events)
        if language_gate is not None:
            return language_gate
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] == "pending_question" and payload.get("stage") == "finish_run":
                answer_seq = next(
                    (
                        candidate["seq"]
                        for candidate in events
                        if candidate["type"] == "answer_recorded"
                        and candidate.get("payload", {}).get("nonce") == payload.get("nonce")
                    ),
                    None,
                )
                if answer_seq is None or not any(
                    candidate["type"] in {"retry_restored", "batch_retry_restored"} and candidate["seq"] > answer_seq
                    for candidate in events
                ):
                    return payload
                break
        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, "Approve finish outcome?", "确认完成结果？"),
            "options": [
                {
                    "id": "integrated",
                    "label": _text(language, "Integrated", "已集成"),
                    "reason": _text(language, "local destination ref contains final checkpoint", "本地目标引用包含最终检查点"),
                },
                {
                    "id": "pr-opened",
                    "label": _text(language, "PR opened", "已打开 PR"),
                    "reason": _text(language, "remote ref and PR URL contain final checkpoint", "远端引用和 PR URL 包含最终检查点"),
                },
                {
                    "id": "kept",
                    "label": _text(language, "Keep", "保留"),
                    "reason": _text(language, "preserve run worktree and branch", "保留运行工作树和分支"),
                },
                {
                    "id": "discarded",
                    "label": _text(language, "Discard", "丢弃"),
                    "reason": _text(language, "remove validated controller-owned worktree and branch", "移除已验证的控制器工作树和分支"),
                },
                {
                    "id": "failed",
                    "label": _text(language, "Failed", "失败"),
                    "reason": _text(language, "preserve failure evidence", "保留失败证据"),
                },
                {
                    "id": "aborted",
                    "label": _text(language, "Aborted", "已中止"),
                    "reason": _text(language, "preserve abort evidence", "保留中止证据"),
                },
            ],
            "recommended_option_id": "kept",
            "free_form": {"option_id": "other", "required": False},
            "stage": "finish_run",
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def _retry_question_payload_locked(
        self,
        events: list[dict[str, Any]],
        *,
        item_id: str,
        failed_base_commit: str,
    ) -> dict[str, Any]:
        language_gate = self._language_selection_question_payload_locked(events)
        if language_gate is not None:
            return language_gate
        consumed_nonces = {
            event.get("payload", {}).get("approval_nonce")
            for event in events
            if event["type"] == "retry_restored"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if (
                event["type"] == "pending_question"
                and payload.get("stage") == "execution_retry"
                and payload.get("item_id") == item_id
                and payload.get("failed_base_commit") == failed_base_commit
                and payload.get("nonce") not in consumed_nonces
            ):
                return payload
        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, f"Approve retry restore for {item_id}?", f"确认为 {item_id} 恢复重试？"),
            "options": [
                {
                    "id": "approve",
                    "label": _text(language, "Approve retry", "批准重试"),
                    "reason": _text(language, "restore failed run worktree once", "恢复失败的运行工作树一次"),
                },
                {
                    "id": "stop",
                    "label": _text(language, "Stop", "停止"),
                    "reason": _text(language, "preserve failed attempt", "保留失败尝试"),
                },
                {
                    "id": "other",
                    "label": _text(language, "Other", "其他"),
                    "reason": _text(language, "free-form answer", "自由回答"),
                },
            ],
            "recommended_option_id": "approve",
            "free_form": {"option_id": "other", "required": False},
            "stage": "execution_retry",
            "item_id": item_id,
            "failed_base_commit": failed_base_commit,
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def _batch_retry_question_payload_locked(
        self,
        events: list[dict[str, Any]],
        *,
        batch_id: str,
        item_ids: list[str],
        failed_base_commit: str,
    ) -> dict[str, Any]:
        language_gate = self._language_selection_question_payload_locked(events)
        if language_gate is not None:
            return language_gate
        consumed_nonces = {
            event.get("payload", {}).get("approval_nonce")
            for event in events
            if event["type"] == "batch_retry_restored"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if (
                event["type"] == "pending_question"
                and payload.get("stage") == "execution_batch_retry"
                and payload.get("batch_id") == batch_id
                and payload.get("item_ids") == item_ids
                and payload.get("failed_base_commit") == failed_base_commit
                and payload.get("nonce") not in consumed_nonces
            ):
                return payload
        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, f"Approve retry restore for {batch_id}?", f"确认为 {batch_id} 恢复重试？"),
            "options": [
                {
                    "id": "approve",
                    "label": _text(language, "Approve retry", "批准重试"),
                    "reason": _text(language, "restore failed batch worktree once", "恢复失败的批量工作树一次"),
                },
                {
                    "id": "stop",
                    "label": _text(language, "Stop", "停止"),
                    "reason": _text(language, "preserve failed batch attempt", "保留失败的批量尝试"),
                },
                {
                    "id": "other",
                    "label": _text(language, "Other", "其他"),
                    "reason": _text(language, "free-form answer", "自由回答"),
                },
            ],
            "recommended_option_id": "approve",
            "free_form": {"option_id": "other", "required": False},
            "stage": "execution_batch_retry",
            "batch_id": batch_id,
            "item_ids": list(item_ids),
            "failed_base_commit": failed_base_commit,
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def _direct_execution_source_nonce(self, events: list[dict[str, Any]]) -> str | None:
        answers = {
            event.get("payload", {}).get("nonce"): event.get("payload", {}).get("choice")
            for event in events
            if event["type"] == "answer_recorded"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "pending_question" or answers.get(payload.get("nonce")) != "skip-refinement-execute":
                continue
            if any(option.get("id") == "skip-refinement-execute" for option in payload.get("options", [])):
                return payload["nonce"]
        return None

    def _answer_choice(self, events: list[dict[str, Any]], nonce: str) -> str | None:
        for event in events:
            payload = event.get("payload", {})
            if event["type"] == "answer_recorded" and payload.get("nonce") == nonce:
                return payload.get("choice")
        return None

    def _execution_summary_decision(self, events: list[dict[str, Any]]) -> str | None:
        config = read_optim_plans_config(self.repo).get(EXECUTION_SUMMARY_CONFIG_KEY)
        if isinstance(config, dict) and config.get("mode") == "always-skip":
            return "always-skip-summary"
        questions = {
            event.get("payload", {}).get("nonce")
            for event in events
            if event["type"] == "pending_question" and event.get("payload", {}).get("stage") == "execution_summary"
        }
        for event in events:
            payload = event.get("payload", {})
            if event["type"] == "answer_recorded" and payload.get("nonce") in questions:
                choice = payload.get("choice")
                if choice in {"generate-summary", "skip-summary", "always-skip-summary"}:
                    return choice
        return None

    def _execution_summary_question_payload_locked(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self._execution_summary_decision(events) is not None:
            return None
        language_gate = self._language_selection_question_payload_locked(events)
        if language_gate is not None:
            return language_gate
        answered = {
            event.get("payload", {}).get("nonce")
            for event in events
            if event["type"] == "answer_recorded"
        }
        for event in reversed(events):
            payload = event.get("payload", {})
            if (
                event["type"] == "pending_question"
                and payload.get("stage") == "execution_summary"
                and payload.get("nonce") not in answered
            ):
                return payload
        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, "Generate execution summary artifact?", "生成执行摘要产物？"),
            "options": [
                {
                    "id": "generate-summary",
                    "label": _text(language, "Generate summary", "生成摘要"),
                    "reason": _text(language, "write EXECUTION_SUMMARY.md from controller events", "根据控制器事件写入执行摘要"),
                },
                {
                    "id": "skip-summary",
                    "label": _text(language, "Skip summary", "跳过摘要"),
                    "reason": _text(language, "do not write a public execution summary for this run", "本次运行不写公开执行摘要"),
                },
                {
                    "id": "always-skip-summary",
                    "label": _text(language, "Always skip summary", "总是跳过摘要"),
                    "reason": _text(language, "skip this artifact for this and future runs in this repo", "本仓库当前和后续运行都跳过此产物"),
                },
            ],
            "recommended_option_id": "generate-summary",
            "expected_seq": len(events) + 1,
            "stage": "execution_summary",
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def request_finish_approval(self) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(
                replayed.events,
                {"context_integrity_recovery", "awaiting_retry_decision", "awaiting_integration", "blocked", "legacy_active"},
                "finish approval",
            )
            return self._finish_question_payload_locked(replayed.events)

    def _execution_approval_question_payload_locked(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        record = self._execution_manifest_record(events)
        language_gate = self._language_selection_question_payload_locked(events)
        if language_gate is not None:
            return language_gate
        existing = [
            event.get("payload", {})
            for event in events
            if event["type"] == "pending_question"
            and event.get("payload", {}).get("stage") == "execution_launch"
        ]
        if len(existing) > 1:
            raise ContractError("multiple execution approval questions recorded")
        if existing:
            question = existing[0]
            if question.get("manifest") != record["manifest"] or question.get("manifest_hash") != record["manifest_hash"]:
                raise ContractError("execution approval question is not bound to the manifest")
            source_nonce = self._direct_execution_source_nonce(events)
            if source_nonce and self._answer_choice(events, question["nonce"]) is None:
                self._append_event_locked(
                    "answer_recorded",
                    {"nonce": question["nonce"], "choice": "approve", "source_nonce": source_nonce},
                )
                question = dict(question)
                question["choice"] = "approve"
                question["approval_source_nonce"] = source_nonce
            return question

        language = self._controller_language()
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": _text(language, "Approve execution?", "确认执行？"),
            "options": [
                {
                    "id": "approve",
                    "label": _text(language, "Approve execution", "批准执行"),
                    "reason": _text(language, "launch exactly this manifest", "严格按此清单启动"),
                },
                {
                    "id": "stop",
                    "label": _text(language, "Stop", "停止"),
                    "reason": _text(language, "do not launch execution", "不启动执行"),
                },
                {
                    "id": "other",
                    "label": _text(language, "Other", "其他"),
                    "reason": _text(language, "free-form answer", "自由回答"),
                },
            ],
            "recommended_option_id": "approve",
            "free_form": {"option_id": "other", "required": False},
            "expected_seq": len(events) + 1,
            "stage": "execution_launch",
            "manifest": record["manifest"],
            "manifest_hash": record["manifest_hash"],
        }
        question = self._append_event_locked("pending_question", payload)["payload"]
        source_nonce = self._direct_execution_source_nonce(events)
        if source_nonce:
            self._append_event_locked(
                "answer_recorded",
                {"nonce": question["nonce"], "choice": "approve", "source_nonce": source_nonce},
            )
            question = dict(question)
            question["choice"] = "approve"
            question["approval_source_nonce"] = source_nonce
        return question

    def request_execution_approval(self) -> dict[str, Any]:
        with self.controller_lock():
            return self._execution_approval_question_payload_locked(self.replay().events)

    def _manifest_source_base(self, manifest: dict[str, Any]) -> str:
        source_base = manifest.get("source_base", manifest.get("base_commit"))
        if not isinstance(source_base, str) or not source_base.strip():
            raise ContractError("execution manifest source_base is required")
        return source_base

    def _run_branch(self, manifest: dict[str, Any]) -> str:
        branch = manifest.get("run_branch")
        if branch is None:
            return f"optim-plans/run/{self.run_id}"
        if not isinstance(branch, str) or not branch.strip() or branch.startswith("-"):
            raise ContractError("execution manifest run_branch is invalid")
        return branch

    def _run_worktree(self, manifest: dict[str, Any]) -> Path:
        raw = manifest.get("run_worktree_path", manifest.get("worktree_path"))
        if raw is None:
            return self.root / "run-worktrees" / self.run_id
        if not isinstance(raw, str) or not raw.strip():
            raise ContractError("execution manifest worktree path is invalid")
        path = Path(raw)
        if not path.is_absolute():
            path = self.repo / path
        return path.absolute()

    def _worktree_is_exact(self, worktree: Path, *, run_branch: str, source_base: str) -> bool:
        if git_common_dir(worktree).resolve() != git_common_dir(self.repo).resolve():
            return False
        if Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree.resolve():
            return False
        if git_maybe(worktree, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{run_branch}":
            return False
        if git(worktree, "rev-parse", "--verify", "HEAD") != source_base:
            return False
        return not _status_entries(worktree)

    def _ensure_run_worktree(self, manifest: dict[str, Any], *, source_base: str) -> dict[str, Any]:
        run_branch = self._run_branch(manifest)
        run_worktree = self._run_worktree(manifest)
        if run_worktree.resolve() == self.repo.resolve():
            raise ContractError("run worktree must be separate from the source worktree")

        branch_ref = f"refs/heads/{run_branch}"
        branch_commit = git_maybe(self.repo, "rev-parse", "--verify", branch_ref)
        path_exists = run_worktree.exists()
        adopted = False
        created = False

        if path_exists:
            try:
                adopted = self._worktree_is_exact(run_worktree, run_branch=run_branch, source_base=source_base)
            except (subprocess.CalledProcessError, ContractError, OSError):
                adopted = False
            if not adopted:
                raise ContractError("existing run worktree is not the exact clean controller-owned worktree")
        elif branch_commit is not None and branch_commit != source_base:
            raise ContractError("existing run branch is not at the approved source base")
        else:
            run_worktree.parent.mkdir(parents=True, exist_ok=True)
            if branch_commit is None:
                git(self.repo, "worktree", "add", "-b", run_branch, str(run_worktree), source_base)
            else:
                git(self.repo, "worktree", "add", str(run_worktree), run_branch)
            created = True

        return {
            "run_branch": run_branch,
            "run_worktree": str(run_worktree),
            "run_worktree_created": created,
            "run_worktree_adopted": adopted,
        }

    def _execution_started_record(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        starts = [event.get("payload", {}) for event in events if event["type"] == "execution_started"]
        if not starts:
            raise ContractError("execution has not been started")
        if len(starts) > 1:
            raise ContractError("execution start event is not single-use")
        payload = dict(starts[0])
        if "source_base" not in payload and isinstance(payload.get("base_commit"), str):
            payload["source_base"] = payload["base_commit"]
        if "protected_metadata" not in payload:
            payload["protected_metadata"] = {}
        for key in ("run_branch", "run_worktree", "protected_metadata"):
            if key not in payload:
                raise ContractError("execution start event is missing run isolation metadata")
        return payload

    def _require_protected_metadata_clean(self, started: dict[str, Any]) -> None:
        expected = started.get("protected_metadata")
        actual = _protected_metadata_snapshot(
            self.repo,
            run_branch=started["run_branch"],
            run_worktree=Path(started["run_worktree"]),
        )
        if actual != expected:
            raise ContractError("protected metadata drift detected")
        require_clean_source(self.repo, ignored_paths=[self.artifact_dir])

    def _latest_checkpoint(self, events: list[dict[str, Any]], started: dict[str, Any]) -> str:
        for event in reversed(events):
            if event["type"] in {"checkpoint_created", "batch_checkpoint_created"}:
                return event["payload"]["commit"]
        return started["source_base"]

    def _require_run_worktree(
        self,
        started: dict[str, Any],
        *,
        expected_head: str,
        clean: bool,
    ) -> Path:
        run_worktree = Path(started["run_worktree"])
        try:
            if git_common_dir(run_worktree).resolve() != git_common_dir(self.repo).resolve():
                raise ContractError("run worktree no longer belongs to the source repository")
            if Path(git(run_worktree, "rev-parse", "--show-toplevel")).resolve() != run_worktree.resolve():
                raise ContractError("run worktree path no longer identifies its registered root")
            if git_maybe(run_worktree, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{started['run_branch']}":
                raise ContractError("run worktree is no longer on the controller-owned branch")
            if git(run_worktree, "rev-parse", "--verify", "HEAD") != expected_head:
                raise ContractError("run worktree HEAD is not the latest controller checkpoint")
            if clean and any(
                status_code != "!!" or not _is_allowed_ignored_audit_noise(path)
                for status_code, path in _status_entries(run_worktree)
            ):
                raise ContractError("run worktree is not clean at the latest controller checkpoint")
        except (subprocess.CalledProcessError, OSError) as exc:
            raise ContractError("run worktree ownership validation failed") from exc
        return run_worktree

    def _manifest_item(self, manifest: dict[str, Any], item_id: str) -> dict[str, Any]:
        for item in manifest["items"]:
            if item["id"] == item_id:
                return item
        raise ContractError(f"unknown execution item {item_id}")

    def _item_allowed_paths(self, item: dict[str, Any]) -> list[str]:
        raw = item.get("allowed_paths", item.get("scopes", []))
        if raw is None:
            raw = []
        if not isinstance(raw, list) or not all(isinstance(path, str) for path in raw):
            raise ContractError(f"allowed_paths for {item['id']} must be a list of strings")
        return list(raw)

    def _worker_config(self, manifest: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("worker", manifest.get("worker", manifest.get("adapter")))
        if not isinstance(raw, dict):
            raise ContractError("execution manifest worker adapter config is required")
        if raw.get("mode") == "host-multi-agent":
            return self._host_worker_config(raw)
        adapter = raw.get("adapter", raw.get("name"))
        argv = raw.get("argv")
        env = raw.get("env", {})
        if adapter not in ADAPTER_NAMES:
            raise ContractError("worker adapter must be claude or codex")
        host = host_agent()
        if adapter != host:
            raise ContractError(f"cross-platform delegated worker is not allowed: {host} host cannot launch {adapter} worker")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv) or not argv[0]:
            raise ContractError("worker adapter argv must have a non-empty executable and string arguments")
        if Path(argv[0]).name != adapter:
            raise ContractError("worker adapter argv executable does not match adapter")
        if adapter == "codex" and "exec" not in argv[1:]:
            raise ContractError("codex worker argv must be an exec adapter command")
        if adapter == "claude" and "-p" not in argv[1:]:
            raise ContractError("claude worker argv must be a print-mode adapter command")
        if adapter == "claude" and "--json-schema" not in argv:
            raise ContractError("claude worker argv must include a JSON schema")
        if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ContractError("worker adapter env must be a string map")
        config_files = raw.get("config_files", [])
        if config_files is None:
            config_files = []
        if not isinstance(config_files, list):
            raise ContractError("worker adapter config_files must be a list")
        smoke = raw.get("smoke")
        if not isinstance(smoke, dict):
            raise ContractError("worker adapter smoke config is required before manifest recording")
        smoke_argv = smoke.get("argv")
        if (
            not isinstance(smoke_argv, list)
            or not smoke_argv
            or not all(isinstance(part, str) and part for part in smoke_argv)
        ):
            raise ContractError("worker adapter smoke argv must be a non-empty argv array")
        if smoke_argv[: len(argv)] != argv:
            raise ContractError("worker adapter smoke argv must start with the worker adapter argv")
        if Path(smoke_argv[0]).name != adapter:
            raise ContractError("worker adapter smoke argv executable does not match adapter")
        if adapter == "codex" and "exec" not in smoke_argv[1:]:
            raise ContractError("codex worker smoke argv must be an exec adapter command")
        if adapter == "claude" and "-p" not in smoke_argv[1:]:
            raise ContractError("claude worker smoke argv must be a print-mode adapter command")
        smoke_env = smoke.get("env", {})
        if not isinstance(smoke_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in smoke_env.items()
        ):
            raise ContractError("worker adapter smoke env must be a string map")
        smoke_timeout = smoke.get("timeout_seconds", 10)
        if not isinstance(smoke_timeout, (int, float)) or smoke_timeout <= 0:
            raise ContractError("worker adapter smoke timeout_seconds must be positive")
        legacy_timeout = raw.get("timeout_seconds", manifest.get("worker_timeout_seconds"))
        if legacy_timeout is not None and (not isinstance(legacy_timeout, (int, float)) or legacy_timeout <= 0):
            raise ContractError("worker timeout_seconds must be positive")
        return {
            "adapter": adapter,
            "argv": list(argv),
            "env": dict(env),
            "config_files": list(config_files),
            "smoke": {"argv": list(smoke_argv), "env": dict(smoke_env), "timeout_seconds": float(smoke_timeout)},
            "timeout_seconds": None,
        }

    def _host_worker_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        platform = raw.get("platform")
        if platform != "codex":
            raise ContractError("host multi-agent worker platform must be codex")
        host = host_agent()
        if platform != host:
            raise ContractError(f"cross-platform delegated worker is not allowed: {host} host cannot launch {platform} worker")
        required = {
            "agent_type",
            "model",
            "reasoning_effort",
            "prompt_protocol",
            "prompt_hash",
            "allowed_tools",
            "sandbox",
            "result_schema",
        }
        missing = sorted(key for key in required if key not in raw)
        if missing:
            raise ContractError(f"host multi-agent worker config missing {missing[0]}")
        for key in ("agent_type", "model", "reasoning_effort", "prompt_protocol", "prompt_hash", "sandbox", "result_schema"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ContractError(f"host multi-agent worker {key} must be a non-empty string")
        service_tier = raw.get("service_tier")
        if service_tier is not None and (not isinstance(service_tier, str) or not service_tier.strip()):
            raise ContractError("host multi-agent worker service_tier must be a string")
        allowed_tools = raw.get("allowed_tools")
        if not isinstance(allowed_tools, list) or not allowed_tools or not all(
            isinstance(tool, str) and tool.strip() for tool in allowed_tools
        ):
            raise ContractError("host multi-agent worker allowed_tools must be a non-empty string list")
        if len(set(allowed_tools)) != len(allowed_tools):
            raise ContractError("host multi-agent worker allowed_tools must not contain duplicates")
        config = {
            "mode": "host-multi-agent",
            "platform": platform,
            "agent_type": raw["agent_type"],
            "model": raw["model"],
            "reasoning_effort": raw["reasoning_effort"],
            "prompt_protocol": raw["prompt_protocol"],
            "prompt_hash": raw["prompt_hash"],
            "allowed_tools": list(allowed_tools),
            "sandbox": raw["sandbox"],
            "result_schema": raw["result_schema"],
        }
        if service_tier is not None:
            config["service_tier"] = service_tier
        return config

    def _verification_config(self, manifest: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("verification", manifest.get("verification"))
        argv = item.get("verification_argv", manifest.get("verification_argv"))
        timeout_seconds = item.get("verification_timeout_seconds", manifest.get("verification_timeout_seconds", 300))
        env: dict[str, str] = {}
        if isinstance(raw, dict):
            argv = raw.get("argv", argv)
            timeout_seconds = raw.get("timeout_seconds", timeout_seconds)
            raw_env = raw.get("env", {})
            if not isinstance(raw_env, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
            ):
                raise ContractError("verification env must be a string map")
            env = dict(raw_env)
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise ContractError("verification argv must be a non-empty argv array")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ContractError("verification timeout_seconds must be positive")
        return {"argv": list(argv), "env": env, "timeout_seconds": float(timeout_seconds)}

    def _owned_generated_path(self, target: Path) -> bool:
        return _path_is_under(target, self.run_dir) or _path_is_under(
            target, optim_plans_state_dir(self.repo) / "launch-files"
        )

    def _write_manifest_config_files(self, files: list[Any], *, write: bool = True) -> None:
        for entry in files:
            if not isinstance(entry, dict):
                raise ContractError("worker adapter config file entries must be objects")
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                raise ContractError("worker adapter config file path is required")
            content = entry.get("content", "")
            target = Path(path)
            if not target.is_absolute():
                target = self.run_dir / target
            if not self._owned_generated_path(target):
                raise ContractError("worker adapter config files must live under the controller run directory or launch-files state")
            if isinstance(content, (dict, list)):
                text = json_text(content, pretty=True) + "\n"
            elif isinstance(content, str):
                text = content
            else:
                raise ContractError("worker adapter config file content must be JSON or text")
            if not write:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def _owned_launch_path(self, raw: str, *, flag: str, canonical_key: str | None = None) -> Path:
        target = Path(raw)
        if not target.is_absolute():
            raise ContractError(f"{flag} path must be absolute")
        if _path_is_under(target, self.run_dir):
            return target
        if canonical_key is not None:
            expected = worker_launch_files(self.repo)[canonical_key]
            if target.resolve(strict=False) == expected.resolve(strict=False):
                return target
        raise ContractError(f"{flag} path must live under the controller run directory or canonical launch-files state")

    def _refresh_launch_dir(self, target: Path) -> None:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ContractError(f"launch path must be a directory: {target}")
        if target.exists():
            for child in target.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        target.mkdir(parents=True, exist_ok=True)

    def _refresh_launch_json(self, target: Path) -> None:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ContractError(f"launch path must be a file: {target}")
        write_json_atomic(target, {})

    def _ensure_adapter_launch_files(self, config: dict[str, Any], *, write: bool = True) -> None:
        argv = config["argv"]
        self._write_manifest_config_files(config["config_files"], write=write)
        if config["adapter"] == "codex" and config["env"].get("CODEX_HOME"):
            target = self._owned_launch_path(config["env"]["CODEX_HOME"], flag="CODEX_HOME", canonical_key="codex_home")
            if write:
                self._refresh_launch_dir(target)
        for flag in ("--settings",):
            if flag not in argv:
                continue
            index = argv.index(flag) + 1
            if index >= len(argv):
                raise ContractError(f"{flag} requires a path")
            target = self._owned_launch_path(argv[index], flag=flag, canonical_key="claude_settings")
            if write:
                self._refresh_launch_json(target)
        if "--plugin-dir" in argv:
            index = argv.index("--plugin-dir") + 1
            if index >= len(argv):
                raise ContractError("--plugin-dir requires a path")
            target = self._owned_launch_path(argv[index], flag="--plugin-dir", canonical_key="claude_plugin_dir")
            if write:
                self._refresh_launch_dir(target)

    def _smoke_execution_manifest(self, manifest: dict[str, Any]) -> None:
        seen: set[str] = set()
        configs: list[dict[str, Any]] = []
        for item in manifest["items"]:
            raw = item.get("worker", manifest.get("worker", manifest.get("adapter")))
            if not isinstance(raw, dict):
                continue
            configs.append(self._worker_config(manifest, item))
        if self._is_current_execution_manifest(manifest):
            configs.append(self._validator_config(manifest))
        for config in configs:
            if config.get("mode") in {"host-multi-agent", "foreground"}:
                key = json_text(config)
                if key in seen:
                    continue
                seen.add(key)
                continue
            key = json_text(
                {
                    "argv": config["argv"],
                    "config_files": config["config_files"],
                    "env": config["env"],
                    "smoke": config["smoke"],
                }
            )
            if key in seen:
                continue
            seen.add(key)
            self._ensure_adapter_launch_files(config)
            if smoke_tested_worker_is_cached(self.repo, config):
                continue
            env = os.environ.copy()
            env.update(config["env"])
            env.update(config["smoke"]["env"])
            timeout_seconds = config["smoke"]["timeout_seconds"]
            result = run_process_group(
                config["smoke"]["argv"],
                cwd=self.run_dir,
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if not result.ok():
                raise ContractError(result.evidence("worker adapter smoke", timeout_seconds=timeout_seconds))
            if not result.stdout.strip():
                raise ContractError("worker adapter smoke stdout result is missing")
            payload = parse_json_strict(result.stdout.strip(), source="worker adapter smoke stdout")
            if not isinstance(payload, dict):
                raise ContractError("worker adapter smoke result must be a JSON object")
            if payload.get("status") != "valid":
                raise ContractError("worker adapter smoke result status must be valid")
            remember_smoke_tested_worker(self.repo, config)

    def _retry_evidence_class(self, evidence: str) -> str:
        for line in bounded_evidence(evidence).splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:240]
        return ""

    def _retry_failure_signature(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "status": payload.get("status") if "validator_result_recorded" in event_type else None,
            "item_id": payload.get("item_id"),
            "batch_id": payload.get("batch_id"),
            "evidence_class": self._retry_evidence_class(str(payload.get("evidence", ""))),
            "delta_fingerprint": payload.get("delta_fingerprint"),
        }

    def _is_retryable_failure_event(self, event: dict[str, Any]) -> bool:
        event_type = event["type"]
        payload = event.get("payload", {})
        if payload.get("retryable") is False:
            return False
        if event_type not in RETRYABLE_FAILURE_EVENTS:
            return False
        if event_type in {"validator_result_recorded", "batch_validator_result_recorded"}:
            return payload.get("status") == "fail"
        return True

    def _retryable_failures_since_checkpoint(
        self,
        events: list[dict[str, Any]],
        *,
        item_id: str | None = None,
        batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for event in events:
            payload = event.get("payload", {})
            if item_id is not None:
                if event["type"] == "checkpoint_created" and payload.get("item_id") == item_id:
                    failures.clear()
                elif self._is_retryable_failure_event(event) and payload.get("item_id") == item_id:
                    failures.append(event)
            elif batch_id is not None:
                if event["type"] == "batch_checkpoint_created" and payload.get("batch_id") == batch_id:
                    failures.clear()
                elif self._is_retryable_failure_event(event) and payload.get("batch_id") == batch_id:
                    failures.append(event)
        return failures

    def _retry_policy_decision_locked(self, events: list[dict[str, Any]], failure_event: dict[str, Any]) -> RetryPolicyDecision:
        if not self._is_retryable_failure_event(failure_event):
            return RetryPolicyDecision("manual")
        payload = failure_event.get("payload", {})
        item_id = payload.get("item_id")
        batch_id = payload.get("batch_id")
        failures = self._retryable_failures_since_checkpoint(
            events,
            item_id=item_id if isinstance(item_id, str) else None,
            batch_id=batch_id if isinstance(batch_id, str) else None,
        )
        signature = self._retry_failure_signature(failure_event["type"], payload)
        equivalent = 0
        for event in reversed(failures):
            if self._retry_failure_signature(event["type"], event.get("payload", {})) != signature:
                break
            equivalent += 1
        if equivalent >= 3:
            return RetryPolicyDecision(
                "blocked",
                failure_signature=signature,
                equivalent_failures=equivalent,
                total_failures=len(failures),
                reason="three consecutive equivalent retryable failures",
            )
        if len(failures) >= 5:
            return RetryPolicyDecision(
                "blocked",
                failure_signature=signature,
                equivalent_failures=equivalent,
                total_failures=len(failures),
                reason="five retryable failures since latest checkpoint",
            )
        return RetryPolicyDecision("auto_retry", failure_signature=signature, equivalent_failures=equivalent, total_failures=len(failures))

    def _item_auto_retry_delta_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
        *,
        expected_delta_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        started = self._execution_started_record(events)
        self._require_protected_metadata_clean(started)
        delta = self._item_delta_locked(events, manifest, started, item, start)
        if expected_delta_fingerprint is not None and delta["delta_fingerprint"] != expected_delta_fingerprint:
            raise ContractError("delta fingerprint changed before retry restore")
        return delta

    def _batch_auto_retry_delta_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        start: dict[str, Any],
        *,
        expected_delta_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        started = self._execution_started_record(events)
        self._require_protected_metadata_clean(started)
        delta = self._batch_delta_locked(events, manifest, started, start)
        if expected_delta_fingerprint is not None and delta["delta_fingerprint"] != expected_delta_fingerprint:
            raise ContractError("delta fingerprint changed before retry restore")
        return delta

    def _auto_retry_restore_preflight_locked(self, events: list[dict[str, Any]], start: dict[str, Any]) -> Path:
        started = self._execution_started_record(events)
        self._require_protected_metadata_clean(started)
        run_worktree = self._require_run_worktree(started, expected_head=start["base_commit"], clean=False)
        if Path(start["run_worktree"]).resolve() != run_worktree.resolve():
            raise ContractError("failed attempt is not bound to the controller-owned worktree")
        return run_worktree

    def _append_attempt_manual_recovery_locked(
        self,
        event_type: str,
        item_id: str,
        *,
        evidence: str,
        start: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "item_id": item_id,
            "evidence": bounded_evidence(evidence),
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
        }
        if extra:
            payload.update(extra)
        self._append_event_locked(event_type, payload)
        self._append_event_locked(
            "awaiting_retry_decision",
            {
                "item_id": item_id,
                "failure_event": event_type,
                "base_commit": start["base_commit"],
                "run_worktree": start["run_worktree"],
            },
        )
        return payload

    def _append_batch_manual_recovery_locked(
        self,
        event_type: str,
        batch_id: str,
        *,
        evidence: str,
        start: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "batch_id": batch_id,
            "item_ids": list(start["item_ids"]),
            "attempt": start["attempt"],
            "evidence": bounded_evidence(evidence),
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
        }
        if extra:
            payload.update(extra)
        self._append_event_locked(event_type, payload)
        self._append_event_locked(
            "awaiting_retry_decision",
            {
                "batch_id": batch_id,
                "item_ids": list(start["item_ids"]),
                "failure_event": event_type,
                "base_commit": start["base_commit"],
                "run_worktree": start["run_worktree"],
            },
        )
        return payload

    def _record_auto_retry_audit_failure_locked(self, item_id: str, *, evidence: str, start: dict[str, Any]) -> None:
        self._append_attempt_manual_recovery_locked(
            "audit_failed",
            item_id,
            evidence=f"audit failed: {evidence}",
            start=start,
            extra={"retryable": False},
        )

    def _record_batch_auto_retry_audit_failure_locked(self, batch_id: str, *, evidence: str, start: dict[str, Any]) -> None:
        self._append_batch_manual_recovery_locked(
            "batch_audit_failed",
            batch_id,
            evidence=f"audit failed: {evidence}",
            start=start,
            extra={"retryable": False},
        )

    def _restore_item_auto_retry_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
        failure_event: dict[str, Any],
    ) -> dict[str, Any]:
        payload = failure_event["payload"]
        try:
            if failure_event["type"] == "audit_failed":
                self._auto_retry_restore_preflight_locked(events, start)
            else:
                self._item_auto_retry_delta_locked(
                    events,
                    manifest,
                    item,
                    start,
                    expected_delta_fingerprint=payload.get("delta_fingerprint"),
                )
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_auto_retry_audit_failure_locked(item["id"], evidence=str(exc), start=start)
            raise
        run_worktree = Path(start["run_worktree"])
        git(run_worktree, "reset", "--hard", start["base_commit"])
        git(run_worktree, "clean", "-fdx")
        retry_payload = {
            "item_id": item["id"],
            "approval_nonce": None,
            "auto_approved": True,
            "auto_retry": True,
            "auto_validator_retry": failure_event["type"]
            in {"validator_result_recorded", "validator_protocol_rejected", "validator_failed"},
            "failure_event": failure_event["type"],
            "restored_to": start["base_commit"],
            "run_worktree": str(run_worktree),
            "evidence": payload.get("evidence", ""),
        }
        for key in ("validator_nonce", "feedback_for_executor", "checked_items"):
            if key in payload:
                retry_payload[key] = payload[key]
        return self._append_event_locked("retry_restored", retry_payload)["payload"]

    def _restore_batch_auto_retry_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        start: dict[str, Any],
        failure_event: dict[str, Any],
    ) -> dict[str, Any]:
        payload = failure_event["payload"]
        batch_id = start["batch_id"]
        try:
            if failure_event["type"] == "batch_audit_failed":
                self._auto_retry_restore_preflight_locked(events, start)
            else:
                self._batch_auto_retry_delta_locked(
                    events,
                    manifest,
                    start,
                    expected_delta_fingerprint=payload.get("delta_fingerprint"),
                )
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_batch_auto_retry_audit_failure_locked(batch_id, evidence=str(exc), start=start)
            raise
        run_worktree = Path(start["run_worktree"])
        git(run_worktree, "reset", "--hard", start["base_commit"])
        git(run_worktree, "clean", "-fdx")
        retry_payload = {
            "batch_id": batch_id,
            "item_ids": list(start["item_ids"]),
            "approval_nonce": None,
            "auto_approved": True,
            "auto_retry": True,
            "auto_validator_retry": failure_event["type"]
            in {"batch_validator_result_recorded", "batch_validator_protocol_rejected", "batch_validator_failed"},
            "failure_event": failure_event["type"],
            "restored_to": start["base_commit"],
            "run_worktree": str(run_worktree),
            "evidence": payload.get("evidence", ""),
        }
        for key in ("validator_nonce", "feedback_for_executor", "checked_items"):
            if key in payload:
                retry_payload[key] = payload[key]
        return self._append_event_locked("batch_retry_restored", retry_payload)["payload"]

    def _record_attempt_failure_locked(
        self,
        event_type: str,
        item_id: str,
        *,
        evidence: str,
        start: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._execution_manifest_record(self.replay().events)
        item = self._manifest_item(record["manifest"], item_id)
        payload = {
            "item_id": item_id,
            "evidence": bounded_evidence(evidence),
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
        }
        if extra:
            payload.update(extra)
        if payload.get("retryable") is False or event_type not in ITEM_RETRYABLE_FAILURE_EVENTS or (
            event_type == "validator_result_recorded" and payload.get("status") != "fail"
        ):
            return self._append_attempt_manual_recovery_locked(event_type, item_id, evidence=evidence, start=start, extra=extra)
        payload["retryable"] = True
        try:
            if event_type == "audit_failed":
                self._auto_retry_restore_preflight_locked(self.replay().events, start)
            else:
                delta = self._item_auto_retry_delta_locked(self.replay().events, record["manifest"], item, start)
                payload.update({"changed_files": delta["changed_files"], "delta_fingerprint": delta["delta_fingerprint"]})
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            payload["retryable"] = False
            self._append_event_locked(event_type, payload)
            self._record_auto_retry_audit_failure_locked(item_id, evidence=str(exc), start=start)
            raise
        failure_event = self._append_event_locked(event_type, payload)
        events = [*self.replay().events]
        decision = self._retry_policy_decision_locked(events, failure_event)
        if decision.action == "blocked":
            blocked = {
                "item_id": item_id,
                "failure_event": event_type,
                "base_commit": start["base_commit"],
                "run_worktree": start["run_worktree"],
                "reason": decision.reason,
                "failure_signature": decision.failure_signature,
                "equivalent_failures": decision.equivalent_failures,
                "total_failures": decision.total_failures,
                "evidence": payload["evidence"],
            }
            return self._append_event_locked("execution_blocked", blocked)["payload"]
        return self._restore_item_auto_retry_locked(events, record["manifest"], item, start, failure_event)

    def _record_batch_attempt_failure_locked(
        self,
        event_type: str,
        batch_id: str,
        *,
        evidence: str,
        start: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._execution_manifest_record(self.replay().events)
        payload = {
            "batch_id": batch_id,
            "item_ids": list(start["item_ids"]),
            "attempt": start["attempt"],
            "evidence": bounded_evidence(evidence),
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
        }
        if extra:
            payload.update(extra)
        if payload.get("retryable") is False or event_type not in BATCH_RETRYABLE_FAILURE_EVENTS or (
            event_type == "batch_validator_result_recorded" and payload.get("status") != "fail"
        ):
            return self._append_batch_manual_recovery_locked(event_type, batch_id, evidence=evidence, start=start, extra=extra)
        payload["retryable"] = True
        try:
            if event_type == "batch_audit_failed":
                self._auto_retry_restore_preflight_locked(self.replay().events, start)
            else:
                delta = self._batch_auto_retry_delta_locked(self.replay().events, record["manifest"], start)
                payload.update({"changed_files": delta["changed_files"], "delta_fingerprint": delta["delta_fingerprint"]})
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            payload["retryable"] = False
            self._append_event_locked(event_type, payload)
            self._record_batch_auto_retry_audit_failure_locked(batch_id, evidence=str(exc), start=start)
            raise
        failure_event = self._append_event_locked(event_type, payload)
        events = [*self.replay().events]
        decision = self._retry_policy_decision_locked(events, failure_event)
        if decision.action == "blocked":
            blocked = {
                "batch_id": batch_id,
                "item_ids": list(start["item_ids"]),
                "failure_event": event_type,
                "base_commit": start["base_commit"],
                "run_worktree": start["run_worktree"],
                "reason": decision.reason,
                "failure_signature": decision.failure_signature,
                "equivalent_failures": decision.equivalent_failures,
                "total_failures": decision.total_failures,
                "evidence": payload["evidence"],
            }
            return self._append_event_locked("batch_execution_blocked", blocked)["payload"]
        return self._restore_batch_auto_retry_locked(events, record["manifest"], start, failure_event)

    def record_attempt_failure(
        self,
        event_type: str,
        item_id: str,
        *,
        evidence: str,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        if event_type not in {"verification_failed", "audit_failed"}:
            raise ContractError(f"unsupported failure event {event_type!r}")
        if not evidence.strip():
            raise ContractError("failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "record-attempt-failure")
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] not in {"in_progress", "completed", "validated"}:
                raise ContractError(f"{item_id} has no active attempt to fail")
            start = self._latest_item_start(replayed.events, item_id)
            extra = None if retryable is None else {"retryable": retryable}
            return self._record_attempt_failure_locked(event_type, item_id, evidence=evidence, start=start, extra=extra)

    def record_batch_attempt_failure(self, event_type: str, batch_id: str, *, evidence: str) -> dict[str, Any]:
        if event_type not in {"batch_verification_failed", "batch_audit_failed"}:
            raise ContractError(f"unsupported failure event {event_type!r}")
        if not evidence.strip():
            raise ContractError("failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            start = self._latest_batch_start(replayed.events, batch_id)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if any(statuses.get(item_id) not in {"in_progress", "completed", "validated"} for item_id in start["item_ids"]):
                raise ContractError(f"{batch_id} has no active attempt to fail")
            return self._record_batch_attempt_failure_locked(event_type, batch_id, evidence=evidence, start=start)

    def _worker_result_evidence(self, item_id: str, *, stdout: str, worker_nonce: str) -> str:
        if not stdout.strip():
            raise ContractError("worker stdout result is missing")
        payload = parse_json_strict(stdout.strip(), source="worker stdout")
        if not isinstance(payload, dict):
            raise ContractError("worker result must be a JSON object")
        for key in ("nonce", "item_id", "status", "evidence"):
            if key not in payload:
                raise ContractError(f"worker result is missing {key!r}")
        if payload["nonce"] != worker_nonce:
            raise ContractError("worker result nonce does not match assignment")
        if payload["item_id"] != item_id:
            raise ContractError("worker result item_id does not match assignment")
        if not isinstance(payload["status"], str) or not payload["status"].strip():
            raise ContractError("worker result status is required")
        if not isinstance(payload["evidence"], str) or not payload["evidence"].strip():
            raise ContractError("worker result evidence is required")
        return bounded_evidence(payload["evidence"])

    def _require_host_worker_config(self, manifest: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        config = self._worker_config(manifest, item)
        if config.get("mode") != "host-multi-agent":
            raise ContractError("item is not configured for host-multi-agent execution; use run-item for CLI adapter workers")
        return config

    def _require_host_batch_worker_config(self, manifest: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        configs = [self._require_host_worker_config(manifest, item) for item in items]
        first = stable_json_hash(configs[0])
        if any(stable_json_hash(config) != first for config in configs[1:]):
            raise ContractError("batch items must share one approved host worker config")
        return configs[0]

    def _host_launch_block(self, *, item_id: str, start: dict[str, Any], worker_config: dict[str, Any]) -> dict[str, Any]:
        events = self.replay().events
        block = {
            "run_id": self.run_id,
            "item_id": item_id,
            "attempt": start["attempt"],
            "assignment_nonce": start["assignment_nonce"],
            "base_commit": start["base_commit"],
            "cwd": start["run_worktree"],
            "allowed_paths": list(start["allowed_paths"]),
            "ignored_audit_noise": ignored_audit_noise_policy(),
            "worker": worker_config,
            "plan_context": start.get("plan_context") or self._plan_context(),
        }
        block.update(
            self._prior_context(
                events,
                role="executor",
                config_hash=stable_json_hash(worker_config),
                exclude_nonce=start["assignment_nonce"],
            )
        )
        feedback = self._latest_validator_feedback(events, item_id)
        if feedback is not None:
            block["validator_feedback"] = feedback
        retry_feedback = self._latest_retry_feedback(events, item_id)
        if retry_feedback is not None:
            block["retry_feedback"] = retry_feedback
        return block

    def _host_batch_launch_block(
        self,
        *,
        batch_id: str,
        item_ids: list[str],
        start: dict[str, Any],
        worker_config: dict[str, Any],
    ) -> dict[str, Any]:
        events = self.replay().events
        block = {
            "run_id": self.run_id,
            "batch_id": batch_id,
            "item_ids": list(item_ids),
            "attempt": start["attempt"],
            "assignment_nonce": start["assignment_nonce"],
            "base_commit": start["base_commit"],
            "cwd": start["run_worktree"],
            "allowed_paths": list(start["allowed_paths"]),
            "ignored_audit_noise": ignored_audit_noise_policy(),
            "worker": worker_config,
            "plan_context": start.get("plan_context") or self._plan_context(),
        }
        block.update(
            self._prior_context(
                events,
                role="executor",
                config_hash=stable_json_hash(worker_config),
                exclude_nonce=start["assignment_nonce"],
            )
        )
        feedback = self._latest_batch_validator_feedback(events, batch_id)
        if feedback is not None:
            block["validator_feedback"] = feedback
        retry_feedback = self._latest_batch_retry_feedback(events, batch_id)
        if retry_feedback is not None:
            block["retry_feedback"] = retry_feedback
        return block

    def _manifest_uses_validator(self, manifest: dict[str, Any]) -> bool:
        return manifest.get("schema_version") == EXECUTION_SCHEMA_VERSION and manifest.get("protocol_version") == EXECUTION_PROTOCOL

    def _item_validator_check_ids(self, item: dict[str, Any]) -> list[str]:
        raw = item.get("validator", {})
        check_ids = raw.get("check_ids") if isinstance(raw, dict) else None
        if not isinstance(check_ids, list):
            raise ContractError(f"validator.check_ids for {item['id']} must be recorded")
        return list(check_ids)

    def _batch_items(self, manifest: dict[str, Any], item_ids: list[str]) -> list[dict[str, Any]]:
        return [self._manifest_item(manifest, item_id) for item_id in item_ids]

    def _batch_allowed_paths(self, items: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for item in items:
            for path in self._item_allowed_paths(item):
                if path not in seen:
                    paths.append(path)
                    seen.add(path)
        return paths

    def _batch_validator_check_ids(self, items: list[dict[str, Any]]) -> list[str]:
        check_ids: list[str] = []
        for item in items:
            check_ids.extend(self._item_validator_check_ids(item))
        return check_ids

    def _item_is_high_risk(self, item: dict[str, Any]) -> bool:
        if item.get("high_risk") is True:
            return True
        raw = item.get("risk", item.get("validation_risk", item.get("batch")))
        return isinstance(raw, str) and raw.lower() in {"high", "high-risk", "single", "solo"}

    def _ready_batch_prefix(
        self,
        manifest: dict[str, Any],
        statuses: dict[str, str],
        *,
        limit: int = 6,
    ) -> list[str]:
        pending_index = next(
            (index for index, item in enumerate(manifest["items"]) if statuses[item["id"]] == "pending"),
            None,
        )
        if pending_index is None:
            return []
        selected: list[str] = []
        for item in manifest["items"][pending_index:]:
            item_id = item["id"]
            if statuses[item_id] != "pending":
                break
            if any(statuses.get(dependency) != "verified" for dependency in item.get("depends_on", [])):
                break
            if self._item_is_high_risk(item):
                if not selected:
                    selected.append(item_id)
                break
            selected.append(item_id)
            if len(selected) >= limit:
                break
        return selected

    def _select_batch_item_ids(
        self,
        manifest: dict[str, Any],
        statuses: dict[str, str],
        requested_item_ids: list[str] | None = None,
    ) -> list[str]:
        auto = self._ready_batch_prefix(manifest, statuses)
        if not auto:
            raise ContractError("no ready execution batch is available")
        if requested_item_ids is None:
            return auto
        if not requested_item_ids:
            raise ContractError("batch item_ids are required")
        if len(requested_item_ids) > 6:
            raise ContractError("batch may contain at most 6 items")
        if requested_item_ids != auto[: len(requested_item_ids)]:
            raise ContractError("batch item_ids must be a continuous ready prefix from manifest order")
        if len(requested_item_ids) < 3 and len(auto) >= 3 and not self._item_is_high_risk(self._manifest_item(manifest, requested_item_ids[0])):
            raise ContractError("batch size below 3 is allowed only for tail, dependency, or high-risk exceptions")
        return list(requested_item_ids)

    def _event_item_ids(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("item_ids")
        if isinstance(raw, list):
            return [item_id for item_id in raw if isinstance(item_id, str)]
        item_id = payload.get("item_id")
        return [item_id] if isinstance(item_id, str) else []

    def _latest_batch_start(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_started":
                return payload
            if event["type"] in {"batch_checkpoint_created", "batch_retry_restored"}:
                break
        raise ContractError(f"{batch_id} has not been started")

    def _active_batch_for_item(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if item_id not in self._event_item_ids(payload):
                continue
            if event["type"] == "batch_started":
                return payload
            if event["type"] in {"batch_checkpoint_created", "batch_retry_restored"}:
                return None
        return None

    def _reject_item_command_if_batch_member(self, events: list[dict[str, Any]], item_id: str, command: str) -> None:
        batch = self._active_batch_for_item(events, item_id)
        if batch is not None:
            raise ContractError(f"{item_id} belongs to active batch {batch['batch_id']}; use batch commands")

    def _latest_batch_failure_event(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] in {
                "batch_worker_failed",
                "batch_validator_result_recorded",
                "batch_validator_protocol_rejected",
                "batch_validator_failed",
                "batch_verification_failed",
                "batch_audit_failed",
            }:
                if event["type"] != "batch_validator_result_recorded" or payload.get("status") == "fail":
                    return event
            if event["type"] in {"batch_checkpoint_created", "batch_retry_restored", "batch_started"}:
                break
        raise ContractError(f"{batch_id} has no failed attempt to retry")

    def _latest_batch_failure(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
        return self._latest_batch_failure_event(events, batch_id)["payload"]

    def _pending_retry_batch(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] == "batch_started":
                return None
            if event["type"] == "batch_retry_restored":
                return payload
        return None

    def _pending_retry_item(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "item_started":
                return None
            if event["type"] == "retry_restored":
                return payload
        return None

    def _latest_batch_checkpoint_created(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_checkpoint_created":
                return payload
            if event["type"] == "batch_retry_restored":
                return None
        return None

    def _latest_batch_checkpoint_prepared(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_checkpoint_prepared":
                return payload
            if event["type"] in {"batch_checkpoint_created", "batch_retry_restored"}:
                return None
        return None

    def _controller_command(self, command: str, *args: str) -> str:
        return shlex.join(["python3", "scripts/optim_plans.py", command, "--repo", str(self.repo), *args])

    def _wait_next_action(self, kind: str, payload: dict[str, Any], *, checked_items: list[str] | None = None) -> str:
        handle = payload["agent_handle"]
        if kind == "executor_item":
            complete = self._controller_command(
                "complete-item",
                "--item-id",
                payload["item_id"],
                "--assignment-nonce",
                payload["assignment_nonce"],
                "--agent-handle",
                handle,
                "--evidence",
                "<evidence>",
            )
            fail = self._controller_command(
                "fail-item",
                "--item-id",
                payload["item_id"],
                "--assignment-nonce",
                payload["assignment_nonce"],
                "--agent-handle",
                handle,
                "--evidence",
                "<evidence>",
            )
            return f"use host wait_agent on registered handle {handle}, then run {complete}; on failure run {fail}"
        if kind == "executor_batch":
            complete = self._controller_command(
                "complete-batch",
                "--batch-id",
                payload["batch_id"],
                "--assignment-nonce",
                payload["assignment_nonce"],
                "--agent-handle",
                handle,
                "--evidence",
                "<evidence>",
            )
            fail = self._controller_command(
                "fail-batch",
                "--batch-id",
                payload["batch_id"],
                "--assignment-nonce",
                payload["assignment_nonce"],
                "--agent-handle",
                handle,
                "--evidence",
                "<evidence>",
            )
            return f"use host wait_agent on registered handle {handle}, then run {complete}; on failure run {fail}"
        if kind == "validator_item":
            result = json_text(
                {
                    "run_id": self.run_id,
                    "item_id": payload["item_id"],
                    "attempt": payload["attempt"],
                    "nonce": payload["validator_nonce"],
                    "validator_config_hash": payload["validator_config_hash"],
                    "validator_prompt_hash": payload["validator_prompt_hash"],
                    "delta_fingerprint": payload["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "<evidence>",
                    "feedback_for_executor": "",
                    "checked_items": checked_items or [],
                }
            )
            complete = self._controller_command(
                "complete-validator",
                "--item-id",
                payload["item_id"],
                "--validator-nonce",
                payload["validator_nonce"],
                "--agent-handle",
                handle,
                "--result",
                result,
            )
            fail = self._controller_command(
                "fail-validator",
                "--item-id",
                payload["item_id"],
                "--validator-nonce",
                payload["validator_nonce"],
                "--agent-handle",
                handle,
                "--reason",
                "unknown",
                "--evidence",
                "<evidence>",
            )
            return (
                f"use host wait_agent on registered handle {handle}, then run {complete} "
                f"(result nonce is validator_nonce; status is pass or fail); on agent failure run {fail}"
            )
        if kind == "validator_batch":
            result = json_text(
                {
                    "run_id": self.run_id,
                    "batch_id": payload["batch_id"],
                    "item_ids": list(payload["item_ids"]),
                    "attempt": payload["attempt"],
                    "assignment_nonce": payload["assignment_nonce"],
                    "nonce": payload["validator_nonce"],
                    "validator_config_hash": payload["validator_config_hash"],
                    "validator_prompt_hash": payload["validator_prompt_hash"],
                    "delta_fingerprint": payload["delta_fingerprint"],
                    "status": "pass",
                    "evidence": "<evidence>",
                    "feedback_for_executor": "",
                    "checked_items": checked_items or [],
                }
            )
            complete = self._controller_command(
                "complete-batch-validator",
                "--batch-id",
                payload["batch_id"],
                "--validator-nonce",
                payload["validator_nonce"],
                "--agent-handle",
                handle,
                "--result",
                result,
            )
            fail = self._controller_command(
                "fail-batch-validator",
                "--batch-id",
                payload["batch_id"],
                "--validator-nonce",
                payload["validator_nonce"],
                "--agent-handle",
                handle,
                "--reason",
                "unknown",
                "--evidence",
                "<evidence>",
            )
            return (
                f"use host wait_agent on registered handle {handle}, then run {complete} "
                f"(result nonce is validator_nonce; status is pass or fail); on agent failure run {fail}"
            )
        raise AssertionError(f"unknown wait kind {kind}")

    def _active_wait_payload(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        active = {
            "command": "host wait_agent",
            "agent_handle": payload["agent_handle"],
            "attempt": payload["attempt"],
            "role": "validator" if kind.startswith("validator") else "executor",
            "target_kind": "batch" if kind.endswith("batch") else "item",
        }
        if kind.endswith("batch"):
            active.update({"batch_id": payload["batch_id"], "item_ids": list(payload["item_ids"])})
        else:
            active["item_id"] = payload["item_id"]
        if kind.startswith("validator"):
            active["validator_nonce"] = payload["validator_nonce"]
            if "assignment_nonce" in payload:
                active["assignment_nonce"] = payload["assignment_nonce"]
        else:
            active["assignment_nonce"] = payload["assignment_nonce"]
        return active

    def _is_active_wait_finished(self, kind: str, wait: dict[str, Any], event_type: str, payload: dict[str, Any]) -> bool:
        if kind == "executor_item":
            return payload.get("item_id") == wait.get("item_id") and event_type in {"worker_completed", "worker_failed", "retry_restored", "checkpoint_created"}
        if kind == "executor_batch":
            return payload.get("batch_id") == wait.get("batch_id") and event_type in {"batch_completed", "batch_worker_failed", "batch_retry_restored", "batch_checkpoint_created"}
        if kind == "validator_item":
            return payload.get("item_id") == wait.get("item_id") and event_type in {
                "validator_result_recorded",
                "validator_protocol_rejected",
                "validator_failed",
                "context_integrity_recovery",
                "retry_restored",
                "checkpoint_created",
            }
        return payload.get("batch_id") == wait.get("batch_id") and event_type in {
            "batch_validator_result_recorded",
            "batch_validator_protocol_rejected",
            "batch_validator_failed",
            "batch_context_integrity_recovery",
            "batch_retry_restored",
            "batch_checkpoint_created",
        }

    def _latest_active_wait(self, events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
        active: tuple[str, dict[str, Any]] | None = None
        for event in events:
            event_type = event["type"]
            payload = event.get("payload", {})
            if event_type == "host_agent_registered":
                active = ("executor_item", payload)
            elif event_type == "batch_agent_registered":
                active = ("executor_batch", payload)
            elif event_type == "validator_agent_registered":
                active = ("validator_item", payload)
            elif event_type == "batch_validator_agent_registered":
                active = ("validator_batch", payload)
            elif active is not None and self._is_active_wait_finished(active[0], active[1], event_type, payload):
                active = None
        return active

    def active_registered_wait(self) -> dict[str, Any] | None:
        replayed = self.replay()
        active = self._latest_active_wait(replayed.events)
        if active is None:
            return None
        kind, payload = active
        record = self._execution_manifest_record(replayed.events)
        checked_items = None
        if kind == "validator_item":
            checked_items = self._item_validator_check_ids(self._manifest_item(record["manifest"], payload["item_id"]))
        elif kind == "validator_batch":
            checked_items = self._batch_validator_check_ids(self._batch_items(record["manifest"], list(payload["item_ids"])))
        return {
            "active_wait": self._active_wait_payload(kind, payload),
            "next_action": self._wait_next_action(kind, payload, checked_items=checked_items),
        }

    def _prior_context(
        self,
        events: list[dict[str, Any]],
        *,
        role: str,
        config_hash: str,
        prompt_hash: str | None = None,
        exclude_nonce: str | None = None,
    ) -> dict[str, Any]:
        handle: str | None = None
        snippets: list[str] = []
        registration_types = {
            "executor": {"host_agent_registered", "batch_agent_registered"},
            "validator": {"validator_agent_registered", "batch_validator_agent_registered"},
        }[role]
        result_types = {
            "executor": {"worker_completed", "batch_completed"},
            "validator": {"validator_result_recorded", "batch_validator_result_recorded"},
        }[role]
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("assignment_nonce") == exclude_nonce or payload.get("validator_nonce") == exclude_nonce:
                continue
            if event["type"] in registration_types and payload.get(f"{'worker' if role == 'executor' else 'validator'}_config_hash") == config_hash:
                if role == "validator" and prompt_hash is not None and payload.get("validator_prompt_hash") != prompt_hash:
                    continue
                if isinstance(payload.get("agent_handle"), str):
                    handle = payload["agent_handle"]
                    break
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] not in result_types:
                continue
            ids = self._event_item_ids(payload)
            if not ids:
                continue
            evidence = payload.get("evidence")
            if isinstance(evidence, str) and evidence:
                snippets.append(f"{','.join(ids)}: {evidence}")
            if len(snippets) >= 3:
                break
        out: dict[str, Any] = {}
        if handle:
            out[f"prior_{role}_agent_handle"] = handle
        if snippets:
            out["prior_context"] = bounded_evidence("\n".join(reversed(snippets)))
        return out

    def _latest_validator_feedback(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        saw_validator_retry = False
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "retry_restored":
                if not payload.get("auto_validator_retry"):
                    return None
                saw_validator_retry = True
                continue
            if saw_validator_retry and event["type"] == "validator_result_recorded" and payload.get("status") == "fail":
                return {
                    "attempt": payload.get("attempt"),
                    "evidence": payload.get("evidence", ""),
                    "feedback_for_executor": payload.get("feedback_for_executor", ""),
                    "checked_items": list(payload.get("checked_items", [])),
                }
            if event["type"] == "checkpoint_created":
                return None
        return None

    def _latest_batch_validator_feedback(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
        saw_validator_retry = False
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_retry_restored":
                if not payload.get("auto_validator_retry"):
                    return None
                saw_validator_retry = True
                continue
            if saw_validator_retry and event["type"] == "batch_validator_result_recorded" and payload.get("status") == "fail":
                return {
                    "batch_id": batch_id,
                    "item_ids": list(payload.get("item_ids", [])),
                    "attempt": payload.get("attempt"),
                    "evidence": payload.get("evidence", ""),
                    "feedback_for_executor": payload.get("feedback_for_executor", ""),
                    "checked_items": list(payload.get("checked_items", [])),
                }
            if event["type"] == "batch_checkpoint_created":
                return None
        return None

    def _latest_retry_feedback(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "retry_restored":
                feedback = {
                    "failure_event": payload.get("failure_event"),
                    "evidence": payload.get("evidence", ""),
                }
                if payload.get("feedback_for_executor"):
                    feedback["feedback_for_executor"] = payload["feedback_for_executor"]
                return feedback
            if event["type"] == "checkpoint_created":
                return None
        return None

    def _latest_batch_retry_feedback(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_retry_restored":
                feedback = {
                    "failure_event": payload.get("failure_event"),
                    "evidence": payload.get("evidence", ""),
                }
                if payload.get("feedback_for_executor"):
                    feedback["feedback_for_executor"] = payload["feedback_for_executor"]
                return feedback
            if event["type"] == "batch_checkpoint_created":
                return None
        return None

    def _assignment_plan_context(self, assignment: dict[str, Any]) -> dict[str, Any]:
        raw = assignment.get("plan_context")
        return raw if isinstance(raw, dict) else self._plan_context()

    def _plan_context_integrity_issue(self, context: dict[str, Any]) -> dict[str, Any] | None:
        if context.get("status") != "available":
            reason = str(context.get("unavailable_reason") or "plan_context unavailable")
            return {"reason": "plan_context_unavailable", "evidence": f"plan_context unavailable: {reason}"}
        critical = context.get("truncation", {}).get("audit_breaking_critical_sections", [])
        critical = [section for section in critical if isinstance(section, str)]
        if critical:
            return {
                "reason": "plan_context_audit_breaking_truncation",
                "evidence": f"plan_context critical sections truncated: {', '.join(critical)}",
            }
        return None

    def _record_context_integrity_recovery_locked(
        self,
        item_id: str,
        *,
        assignment: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any] | None:
        context = self._assignment_plan_context(assignment)
        issue = self._plan_context_integrity_issue(context)
        if issue is None:
            return None
        payload = {
            "item_id": item_id,
            "attempt": assignment["attempt"],
            "validator_nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
            "status": "recovery_required",
            "reason": issue["reason"],
            "evidence": bounded_evidence(issue["evidence"]),
            "plan_context": context,
        }
        return self._append_event_locked("context_integrity_recovery", payload)["payload"]

    def _record_batch_context_integrity_recovery_locked(
        self,
        batch_id: str,
        *,
        assignment: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any] | None:
        context = self._assignment_plan_context(assignment)
        issue = self._plan_context_integrity_issue(context)
        if issue is None:
            return None
        payload = {
            "batch_id": batch_id,
            "item_ids": list(assignment["item_ids"]),
            "attempt": assignment["attempt"],
            "assignment_nonce": assignment["assignment_nonce"],
            "validator_nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
            "status": "recovery_required",
            "reason": issue["reason"],
            "evidence": bounded_evidence(issue["evidence"]),
            "plan_context": context,
        }
        return self._append_event_locked("batch_context_integrity_recovery", payload)["payload"]

    def _latest_validator_assignment(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "validator_assigned":
                return payload
            if event["type"] in {
                "validator_result_recorded",
                "validator_protocol_rejected",
                "validator_failed",
                "context_integrity_recovery",
                "retry_restored",
                "checkpoint_created",
            }:
                return None
        return None

    def _latest_batch_validator_assignment(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_validator_assigned":
                return payload
            if event["type"] in {
                "batch_validator_result_recorded",
                "batch_validator_protocol_rejected",
                "batch_validator_failed",
                "batch_context_integrity_recovery",
                "batch_retry_restored",
                "batch_checkpoint_created",
            }:
                return None
        return None

    def _latest_validator_host_registration(
        self,
        events: list[dict[str, Any]],
        *,
        validator_nonce: str,
        agent_handle: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "validator_agent_registered" or payload.get("validator_nonce") != validator_nonce:
                continue
            if agent_handle is None or payload.get("agent_handle") == agent_handle:
                return payload
        return None

    def _latest_validator_host_authorization(
        self,
        events: list[dict[str, Any]],
        *,
        validator_nonce: str,
        launch_nonce: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "validator_spawn_authorized" or payload.get("validator_nonce") != validator_nonce:
                continue
            if launch_nonce is None or payload.get("launch_nonce") == launch_nonce:
                return payload
        return None

    def _latest_batch_validator_host_registration(
        self,
        events: list[dict[str, Any]],
        *,
        validator_nonce: str,
        agent_handle: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "batch_validator_agent_registered" or payload.get("validator_nonce") != validator_nonce:
                continue
            if agent_handle is None or payload.get("agent_handle") == agent_handle:
                return payload
        return None

    def _latest_batch_validator_host_authorization(
        self,
        events: list[dict[str, Any]],
        *,
        validator_nonce: str,
        launch_nonce: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "batch_validator_spawn_authorized" or payload.get("validator_nonce") != validator_nonce:
                continue
            if launch_nonce is None or payload.get("launch_nonce") == launch_nonce:
                return payload
        return None

    def _item_delta_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        started: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any]:
        run_worktree = self._require_run_worktree(started, expected_head=start["base_commit"], clean=False)
        allowed_paths = self._item_allowed_paths(item)
        audit = audit_git_delta(
            run_worktree,
            allowed_paths=allowed_paths,
            base_commit=start["base_commit"],
            head_commit=start["base_commit"],
        )
        return {
            "run_worktree": str(run_worktree),
            "allowed_paths": allowed_paths,
            "changed_files": audit["changed_files"],
            "delta_fingerprint": checkpoint_delta_fingerprint(run_worktree, audit["changed_files"]),
        }

    def _batch_delta_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        started: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any]:
        run_worktree = self._require_run_worktree(started, expected_head=start["base_commit"], clean=False)
        allowed_paths = self._batch_allowed_paths(self._batch_items(manifest, list(start["item_ids"])))
        audit = audit_git_delta(
            run_worktree,
            allowed_paths=allowed_paths,
            base_commit=start["base_commit"],
            head_commit=start["base_commit"],
        )
        return {
            "run_worktree": str(run_worktree),
            "allowed_paths": allowed_paths,
            "changed_files": audit["changed_files"],
            "delta_fingerprint": checkpoint_delta_fingerprint(run_worktree, audit["changed_files"]),
        }

    def _validator_launch_block(
        self,
        *,
        assignment: dict[str, Any],
        validator_config: dict[str, Any],
        validator_prompt: dict[str, Any],
        check_ids: list[str],
    ) -> dict[str, Any]:
        block = {
            "run_id": self.run_id,
            "item_id": assignment["item_id"],
            "attempt": assignment["attempt"],
            "validator_nonce": assignment["validator_nonce"],
            "base_commit": assignment["base_commit"],
            "cwd": assignment["run_worktree"],
            "allowed_paths": list(assignment["allowed_paths"]),
            "changed_files": list(assignment["changed_files"]),
            "check_ids": list(check_ids),
            "delta_fingerprint": assignment["delta_fingerprint"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "validator": validator_config,
            "validator_prompt": validator_prompt,
            "plan_context": assignment.get("plan_context") or self._plan_context(),
        }
        block.update(
            self._prior_context(
                self.replay().events,
                role="validator",
                config_hash=assignment["validator_config_hash"],
                prompt_hash=assignment["validator_prompt_hash"],
                exclude_nonce=assignment["validator_nonce"],
            )
        )
        return block

    def _batch_validator_launch_block(
        self,
        *,
        assignment: dict[str, Any],
        validator_config: dict[str, Any],
        validator_prompt: dict[str, Any],
        check_ids: list[str],
    ) -> dict[str, Any]:
        block = {
            "run_id": self.run_id,
            "batch_id": assignment["batch_id"],
            "item_ids": list(assignment["item_ids"]),
            "attempt": assignment["attempt"],
            "assignment_nonce": assignment["assignment_nonce"],
            "validator_nonce": assignment["validator_nonce"],
            "base_commit": assignment["base_commit"],
            "cwd": assignment["run_worktree"],
            "allowed_paths": list(assignment["allowed_paths"]),
            "changed_files": list(assignment["changed_files"]),
            "check_ids": list(check_ids),
            "delta_fingerprint": assignment["delta_fingerprint"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "validator": validator_config,
            "validator_prompt": validator_prompt,
            "plan_context": assignment.get("plan_context") or self._plan_context(),
        }
        block.update(
            self._prior_context(
                self.replay().events,
                role="validator",
                config_hash=assignment["validator_config_hash"],
                prompt_hash=assignment["validator_prompt_hash"],
                exclude_nonce=assignment["validator_nonce"],
            )
        )
        return block

    def _validator_assignment_response_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        assignment: dict[str, Any],
        validator_config: dict[str, Any],
    ) -> dict[str, Any]:
        check_ids = self._item_validator_check_ids(item)
        validator_prompt = manifest["validator_prompt"]
        launch_block = self._validator_launch_block(
            assignment=assignment,
            validator_config=validator_config,
            validator_prompt=validator_prompt,
            check_ids=check_ids,
        )
        phase = "validator_assigned"
        next_action = None
        registration = self._latest_validator_host_registration(events, validator_nonce=assignment["validator_nonce"])
        if registration is not None:
            phase = "validator_agent_registered"
            next_action = self._wait_next_action("validator_item", registration, checked_items=check_ids)
        elif self._latest_validator_host_authorization(events, validator_nonce=assignment["validator_nonce"]) is not None:
            phase = "validator_spawn_authorized"
        response = {
            **assignment,
            "phase": phase,
            "validator": validator_config,
            "validator_launch_block": launch_block,
            "validator_launch_block_hash": stable_json_hash(launch_block),
        }
        if next_action is not None:
            response["next_action"] = next_action
        return response

    def _assign_validator_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self._latest_validator_assignment(events, item["id"])
        validator_config = self._validator_config(manifest)
        if existing is not None:
            current = self._item_delta_locked(events, manifest, self._execution_started_record(events), item, start)
            if current["delta_fingerprint"] != existing.get("delta_fingerprint"):
                self._record_attempt_failure_locked(
                    "audit_failed",
                    item["id"],
                    evidence="audit failed: delta fingerprint changed before validator assignment",
                    start=start,
                    extra={"retryable": False},
                )
                raise ContractError("delta fingerprint changed before validator assignment")
            return self._validator_assignment_response_locked(events, manifest, item, existing, validator_config)

        started = self._execution_started_record(events)
        try:
            self._require_protected_metadata_clean(started)
            delta = self._item_delta_locked(events, manifest, started, item, start)
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_attempt_failure_locked("audit_failed", item["id"], evidence=f"audit failed: {exc}", start=start)
            raise
        prompt_hash = manifest["validator_prompt"]["hash"]
        assignment = {
            "item_id": item["id"],
            "attempt": start["attempt"],
            "base_commit": start["base_commit"],
            "run_worktree": delta["run_worktree"],
            "run_branch": started["run_branch"],
            "allowed_paths": delta["allowed_paths"],
            "changed_files": delta["changed_files"],
            "validator_nonce": uuid.uuid4().hex,
            "validator_config_hash": stable_json_hash(validator_config),
            "validator_prompt_hash": prompt_hash,
            "delta_fingerprint": delta["delta_fingerprint"],
            "plan_context": start.get("plan_context") or self._plan_context(),
        }
        payload = self._append_event_locked("validator_assigned", assignment)["payload"]
        return self._validator_assignment_response_locked(
            [*events, {"type": "validator_assigned", "payload": payload}],
            manifest,
            item,
            payload,
            validator_config,
        )

    def _batch_validator_assignment_response_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        assignment: dict[str, Any],
        validator_config: dict[str, Any],
    ) -> dict[str, Any]:
        check_ids = self._batch_validator_check_ids(self._batch_items(manifest, list(assignment["item_ids"])))
        validator_prompt = manifest["validator_prompt"]
        launch_block = self._batch_validator_launch_block(
            assignment=assignment,
            validator_config=validator_config,
            validator_prompt=validator_prompt,
            check_ids=check_ids,
        )
        phase = "validator_assigned"
        next_action = None
        registration = self._latest_batch_validator_host_registration(events, validator_nonce=assignment["validator_nonce"])
        if registration is not None:
            phase = "validator_agent_registered"
            next_action = self._wait_next_action("validator_batch", registration, checked_items=check_ids)
        elif self._latest_batch_validator_host_authorization(events, validator_nonce=assignment["validator_nonce"]) is not None:
            phase = "validator_spawn_authorized"
        response = {
            **assignment,
            "phase": phase,
            "validator": validator_config,
            "validator_launch_block": launch_block,
            "validator_launch_block_hash": stable_json_hash(launch_block),
        }
        if next_action is not None:
            response["next_action"] = next_action
        return response

    def _assign_batch_validator_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        start: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self._latest_batch_validator_assignment(events, start["batch_id"])
        validator_config = self._validator_config(manifest)
        if existing is not None:
            current = self._batch_delta_locked(events, manifest, self._execution_started_record(events), start)
            if current["delta_fingerprint"] != existing.get("delta_fingerprint"):
                self._record_batch_attempt_failure_locked(
                    "batch_audit_failed",
                    start["batch_id"],
                    evidence="audit failed: delta fingerprint changed before validator assignment",
                    start=start,
                    extra={"retryable": False},
                )
                raise ContractError("delta fingerprint changed before validator assignment")
            return self._batch_validator_assignment_response_locked(events, manifest, existing, validator_config)

        started = self._execution_started_record(events)
        try:
            self._require_protected_metadata_clean(started)
            delta = self._batch_delta_locked(events, manifest, started, start)
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_batch_attempt_failure_locked("batch_audit_failed", start["batch_id"], evidence=f"audit failed: {exc}", start=start)
            raise
        prompt_hash = manifest["validator_prompt"]["hash"]
        assignment = {
            "batch_id": start["batch_id"],
            "item_ids": list(start["item_ids"]),
            "attempt": start["attempt"],
            "assignment_nonce": start["assignment_nonce"],
            "base_commit": start["base_commit"],
            "run_worktree": delta["run_worktree"],
            "run_branch": started["run_branch"],
            "allowed_paths": delta["allowed_paths"],
            "changed_files": delta["changed_files"],
            "validator_nonce": uuid.uuid4().hex,
            "validator_config_hash": stable_json_hash(validator_config),
            "validator_prompt_hash": prompt_hash,
            "delta_fingerprint": delta["delta_fingerprint"],
            "plan_context": start.get("plan_context") or self._plan_context(),
        }
        payload = self._append_event_locked("batch_validator_assigned", assignment)["payload"]
        return self._batch_validator_assignment_response_locked(
            [*events, {"type": "batch_validator_assigned", "payload": payload}],
            manifest,
            payload,
            validator_config,
        )

    def _latest_host_registration(
        self,
        events: list[dict[str, Any]],
        *,
        assignment_nonce: str,
        agent_handle: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "host_agent_registered" or payload.get("assignment_nonce") != assignment_nonce:
                continue
            if agent_handle is None or payload.get("agent_handle") == agent_handle:
                return payload
        return None

    def _latest_host_authorization(
        self,
        events: list[dict[str, Any]],
        *,
        assignment_nonce: str,
        launch_nonce: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "host_spawn_authorized" or payload.get("assignment_nonce") != assignment_nonce:
                continue
            if launch_nonce is None or payload.get("launch_nonce") == launch_nonce:
                return payload
        return None

    def _host_assignment_response_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
        worker_config: dict[str, Any],
    ) -> dict[str, Any]:
        launch_block = self._host_launch_block(item_id=item["id"], start=start, worker_config=worker_config)
        statuses = self._item_statuses(events, manifest)
        phase = statuses[item["id"]]
        next_action = None
        if phase == "in_progress":
            registration = self._latest_host_registration(events, assignment_nonce=start["assignment_nonce"])
            if registration is not None:
                phase = "agent_registered"
                next_action = self._wait_next_action("executor_item", registration)
            elif self._latest_host_authorization(events, assignment_nonce=start["assignment_nonce"]) is not None:
                phase = "spawn_authorized"
            else:
                phase = "assigned"
        elif phase == "completed":
            phase = "worker_completed"
        elif phase == "verified":
            phase = "checkpointed"
        response = {
            **start,
            "phase": phase,
            "worker": worker_config,
            "worker_config_hash": stable_json_hash(worker_config),
            "launch_block": launch_block,
            "launch_block_hash": stable_json_hash(launch_block),
        }
        if next_action is not None:
            response["next_action"] = next_action
        return response

    def assign_item(self, item_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "assign-item")
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "assign-item")
            worker_config = self._require_host_worker_config(record["manifest"], item)
            worker_config_hash = stable_json_hash(worker_config)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] == "in_progress":
                start = self._latest_item_start(replayed.events, item_id)
                if not isinstance(start.get("assignment_nonce"), str) or not start["assignment_nonce"]:
                    raise ContractError(f"{item_id} active attempt is not a host assignment")
                if start.get("worker_config_hash") != worker_config_hash:
                    raise ContractError(f"{item_id} active assignment is not bound to the approved host worker config")
                return self._host_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    item,
                    start,
                    worker_config,
                )
            if statuses[item_id] != "pending":
                raise ContractError(f"{item_id} is not ready for host assignment; current status is {statuses[item_id]}")
            blocked = [
                current_id
                for current_id, status in statuses.items()
                if status in {"in_progress", "completed", "validating", "validated", "prepared", "failed", "blocked"}
            ]
            if blocked:
                raise ContractError(f"another item attempt must be resolved first: {blocked[0]}")
            next_item = next(
                (current["id"] for current in record["manifest"]["items"] if statuses[current["id"]] == "pending"),
                None,
            )
            if next_item != item_id:
                raise ContractError(f"{item_id} is not next in the approved serial order")
            for dependency in item.get("depends_on", []):
                if statuses.get(dependency) != "verified":
                    raise ContractError(f"{item_id} dependency {dependency} is not verified")
            run_worktree = self._require_run_worktree(
                started,
                expected_head=self._latest_checkpoint(replayed.events, started),
                clean=True,
            )
            start = {
                "item_id": item_id,
                "attempt": sum(
                    1
                    for event in replayed.events
                    if event["type"] == "item_started" and event.get("payload", {}).get("item_id") == item_id
                )
                + 1,
                "base_commit": git(run_worktree, "rev-parse", "--verify", "HEAD"),
                "run_worktree": str(run_worktree),
                "run_branch": started["run_branch"],
                "allowed_paths": self._item_allowed_paths(item),
                "assignment_nonce": uuid.uuid4().hex,
                "worker_config_hash": worker_config_hash,
                "plan_context": self._plan_context(),
            }
            payload = self._append_event_locked("item_started", start)["payload"]
            return self._host_assignment_response_locked(
                [*replayed.events, {"type": "item_started", "payload": payload}],
                record["manifest"],
                item,
                payload,
                worker_config,
            )

    def _latest_batch_host_registration(
        self,
        events: list[dict[str, Any]],
        *,
        assignment_nonce: str,
        agent_handle: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "batch_agent_registered" or payload.get("assignment_nonce") != assignment_nonce:
                continue
            if agent_handle is None or payload.get("agent_handle") == agent_handle:
                return payload
        return None

    def _latest_batch_host_authorization(
        self,
        events: list[dict[str, Any]],
        *,
        assignment_nonce: str,
        launch_nonce: str | None = None,
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] != "batch_host_spawn_authorized" or payload.get("assignment_nonce") != assignment_nonce:
                continue
            if launch_nonce is None or payload.get("launch_nonce") == launch_nonce:
                return payload
        return None

    def _batch_assignment_response_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        start: dict[str, Any],
        worker_config: dict[str, Any],
    ) -> dict[str, Any]:
        item_ids = list(start["item_ids"])
        launch_block = self._host_batch_launch_block(
            batch_id=start["batch_id"],
            item_ids=item_ids,
            start=start,
            worker_config=worker_config,
        )
        statuses = self._item_statuses(events, manifest)
        phases = {statuses[item_id] for item_id in item_ids}
        phase = phases.pop() if len(phases) == 1 else "mixed"
        next_action = None
        if phase == "in_progress":
            registration = self._latest_batch_host_registration(events, assignment_nonce=start["assignment_nonce"])
            if registration is not None:
                phase = "agent_registered"
                next_action = self._wait_next_action("executor_batch", registration)
            elif self._latest_batch_host_authorization(events, assignment_nonce=start["assignment_nonce"]) is not None:
                phase = "spawn_authorized"
            else:
                phase = "assigned"
        elif phase == "completed":
            phase = "worker_completed"
        elif phase == "verified":
            phase = "checkpointed"
        response = {
            **start,
            "phase": phase,
            "worker": worker_config,
            "worker_config_hash": stable_json_hash(worker_config),
            "launch_block": launch_block,
            "launch_block_hash": stable_json_hash(launch_block),
        }
        if next_action is not None:
            response["next_action"] = next_action
        return response

    def _begin_batch_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        started: dict[str, Any],
        item_ids: list[str],
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        items = self._batch_items(manifest, item_ids)
        worker_config = self._require_host_batch_worker_config(manifest, items)
        worker_config_hash = stable_json_hash(worker_config)
        run_worktree = self._require_run_worktree(
            started,
            expected_head=self._latest_checkpoint(events, started),
            clean=True,
        )
        batch_id = batch_id or f"B-{uuid.uuid4().hex[:12]}"
        start = {
            "batch_id": batch_id,
            "item_ids": list(item_ids),
            "attempt": sum(
                1
                for event in events
                if event["type"] == "batch_started" and event.get("payload", {}).get("batch_id") == batch_id
            )
            + 1,
            "base_commit": git(run_worktree, "rev-parse", "--verify", "HEAD"),
            "run_worktree": str(run_worktree),
            "run_branch": started["run_branch"],
            "allowed_paths": self._batch_allowed_paths(items),
            "assignment_nonce": uuid.uuid4().hex,
            "worker_config_hash": worker_config_hash,
            "plan_context": self._plan_context(),
        }
        payload = self._append_event_locked("batch_started", start)["payload"]
        return self._batch_assignment_response_locked(
            [*events, {"type": "batch_started", "payload": payload}],
            manifest,
            payload,
            worker_config,
        )

    def assign_batch(self, item_ids: list[str] | None = None) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "assign-batch")
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            pending_retry = self._pending_retry_batch(replayed.events)
            if pending_retry is not None:
                selected_ids = list(pending_retry["item_ids"])
                if item_ids is not None and item_ids != selected_ids:
                    raise ContractError("retry batch item_ids must match the failed batch")
                return self._begin_batch_locked(
                    replayed.events,
                    record["manifest"],
                    started,
                    selected_ids,
                    batch_id=pending_retry["batch_id"],
                )
            active = next(
                (
                    event.get("payload", {})
                    for event in reversed(replayed.events)
                    if event["type"] == "batch_started"
                    and any(statuses.get(current) in {"in_progress", "completed", "validating", "validated", "prepared", "failed", "blocked"} for current in event.get("payload", {}).get("item_ids", []))
                ),
                None,
            )
            if active is not None:
                if item_ids is not None and item_ids != active.get("item_ids"):
                    raise ContractError(f"another batch attempt must be resolved first: {active['batch_id']}")
                if all(statuses.get(current) == "in_progress" for current in active.get("item_ids", [])):
                    items = self._batch_items(record["manifest"], list(active["item_ids"]))
                    worker_config = self._require_host_batch_worker_config(record["manifest"], items)
                    return self._batch_assignment_response_locked(
                        replayed.events,
                        record["manifest"],
                        active,
                        worker_config,
                    )
                raise ContractError(f"another batch attempt must be resolved first: {active['batch_id']}")
            blocked = [
                current_id
                for current_id, status in statuses.items()
                if status in {"in_progress", "completed", "validating", "validated", "prepared", "failed", "blocked"}
            ]
            if blocked:
                raise ContractError(f"another item attempt must be resolved first: {blocked[0]}")
            selected_ids = self._select_batch_item_ids(record["manifest"], statuses, item_ids)
            return self._begin_batch_locked(replayed.events, record["manifest"], started, selected_ids)

    def _require_host_batch_assignment_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        batch_id: str,
        assignment_nonce: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(assignment_nonce, str) or not assignment_nonce.strip():
            raise ContractError("assignment nonce is required")
        start = self._latest_batch_start(events, batch_id)
        statuses = self._item_statuses(events, manifest)
        if any(statuses.get(item_id) != "in_progress" for item_id in start["item_ids"]):
            raise ContractError(f"{batch_id} does not have an active host assignment")
        if start.get("assignment_nonce") != assignment_nonce:
            raise ContractError("assignment nonce does not match active batch assignment")
        worker_config = self._require_host_batch_worker_config(manifest, self._batch_items(manifest, list(start["item_ids"])))
        if start.get("worker_config_hash") != stable_json_hash(worker_config):
            raise ContractError("active batch assignment is not bound to the approved host worker config")
        return start, worker_config

    def authorize_batch_spawn(self, batch_id: str, assignment_nonce: str, launch_block: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(launch_block, dict):
            raise ContractError("launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "authorize-batch-spawn")
            record = self._execution_manifest_record(replayed.events)
            start, worker_config = self._require_host_batch_assignment_locked(replayed.events, record["manifest"], batch_id, assignment_nonce)
            expected = self._host_batch_launch_block(
                batch_id=batch_id,
                item_ids=list(start["item_ids"]),
                start=start,
                worker_config=worker_config,
            )
            if launch_block != expected:
                raise ContractError("host launch block does not match the approved batch assignment")
            if self._latest_batch_host_registration(replayed.events, assignment_nonce=assignment_nonce) is not None:
                raise ContractError("host agent is already registered for this batch assignment")
            launch_block_hash = stable_json_hash(launch_block)
            existing = self._latest_batch_host_authorization(replayed.events, assignment_nonce=assignment_nonce)
            if existing is not None:
                if existing.get("launch_block_hash") != launch_block_hash:
                    raise ContractError("active batch host spawn authorization is bound to a different launch block")
                return existing
            payload = {
                "batch_id": batch_id,
                "item_ids": list(start["item_ids"]),
                "attempt": start["attempt"],
                "assignment_nonce": assignment_nonce,
                "launch_nonce": uuid.uuid4().hex,
                "worker_config_hash": stable_json_hash(worker_config),
                "launch_block_hash": launch_block_hash,
                "launch_block": launch_block,
            }
            return self._append_event_locked("batch_host_spawn_authorized", payload)["payload"]

    def register_batch_agent(
        self,
        batch_id: str,
        *,
        assignment_nonce: str,
        launch_nonce: str,
        agent_handle: str,
        launch_block: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("agent handle is required")
        if not isinstance(launch_nonce, str) or not launch_nonce.strip():
            raise ContractError("launch nonce is required")
        if not isinstance(launch_block, dict):
            raise ContractError("launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "register-batch-agent")
            record = self._execution_manifest_record(replayed.events)
            start, worker_config = self._require_host_batch_assignment_locked(replayed.events, record["manifest"], batch_id, assignment_nonce)
            expected = self._host_batch_launch_block(
                batch_id=batch_id,
                item_ids=list(start["item_ids"]),
                start=start,
                worker_config=worker_config,
            )
            if launch_block != expected:
                raise ContractError("host launch block does not match the approved batch assignment")
            authorization = self._latest_batch_host_authorization(
                replayed.events,
                assignment_nonce=assignment_nonce,
                launch_nonce=launch_nonce,
            )
            if authorization is None:
                raise ContractError("unknown or stale host launch nonce")
            if authorization.get("launch_block_hash") != stable_json_hash(launch_block):
                raise ContractError("host launch nonce is not bound to this launch block")
            if any(
                event["type"] == "batch_agent_registered"
                and event.get("payload", {}).get("launch_nonce") == launch_nonce
                for event in replayed.events
            ):
                raise ContractError("host launch nonce is stale or already used")
            payload = {
                "batch_id": batch_id,
                "item_ids": list(start["item_ids"]),
                "attempt": start["attempt"],
                "assignment_nonce": assignment_nonce,
                "launch_nonce": launch_nonce,
                "agent_handle": agent_handle,
                "worker_config_hash": stable_json_hash(worker_config),
                "launch_block_hash": stable_json_hash(launch_block),
            }
            registered = self._append_event_locked("batch_agent_registered", payload)["payload"]
            return {**registered, "next_action": self._wait_next_action("executor_batch", registered)}

    def _require_registered_batch_agent_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        batch_id: str,
        *,
        assignment_nonce: str,
        agent_handle: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("agent handle is required")
        start, _worker_config = self._require_host_batch_assignment_locked(events, manifest, batch_id, assignment_nonce)
        registration = self._latest_batch_host_registration(
            events,
            assignment_nonce=assignment_nonce,
            agent_handle=agent_handle,
        )
        if registration is None:
            raise ContractError("registered host agent handle does not match active batch assignment")
        return start, registration

    def complete_host_batch(
        self,
        batch_id: str,
        *,
        assignment_nonce: str,
        agent_handle: str,
        evidence: str,
    ) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker completion evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            start, registration = self._require_registered_batch_agent_locked(
                replayed.events,
                record["manifest"],
                batch_id,
                assignment_nonce=assignment_nonce,
                agent_handle=agent_handle,
            )
            payload = {
                "batch_id": batch_id,
                "item_ids": list(start["item_ids"]),
                "attempt": start["attempt"],
                "assignment_nonce": assignment_nonce,
                "agent_handle": agent_handle,
                "launch_nonce": registration["launch_nonce"],
                "evidence": bounded_evidence(evidence),
            }
            return self._append_event_locked("batch_completed", payload)["payload"]

    def fail_host_batch(
        self,
        batch_id: str,
        *,
        assignment_nonce: str,
        agent_handle: str | None = None,
        launch_nonce: str | None = None,
        evidence: str,
    ) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            if agent_handle is not None:
                start, registration = self._require_registered_batch_agent_locked(
                    replayed.events,
                    record["manifest"],
                    batch_id,
                    assignment_nonce=assignment_nonce,
                    agent_handle=agent_handle,
                )
                extra = {
                    "assignment_nonce": assignment_nonce,
                    "agent_handle": agent_handle,
                    "launch_nonce": registration["launch_nonce"],
                }
            else:
                if not isinstance(launch_nonce, str) or not launch_nonce.strip():
                    raise ContractError("launch nonce is required when failing a host batch without an agent handle")
                start, _worker_config = self._require_host_batch_assignment_locked(
                    replayed.events,
                    record["manifest"],
                    batch_id,
                    assignment_nonce,
                )
                authorization = self._latest_batch_host_authorization(
                    replayed.events,
                    assignment_nonce=assignment_nonce,
                    launch_nonce=launch_nonce,
                )
                if authorization is None:
                    raise ContractError("unknown or stale host launch nonce")
                if any(
                    event["type"] == "batch_agent_registered"
                    and event.get("payload", {}).get("launch_nonce") == launch_nonce
                    for event in replayed.events
                ):
                    raise ContractError("agent handle is required after host launch nonce registration")
                extra = {
                    "assignment_nonce": assignment_nonce,
                    "launch_nonce": launch_nonce,
                    "agent_handle_lost": True,
                }
            return self._record_batch_attempt_failure_locked(
                "batch_worker_failed",
                batch_id,
                evidence=evidence,
                start=start,
                extra=extra,
            )

    def _require_validator_assignment_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        validator_nonce: str,
    ) -> dict[str, Any]:
        if not isinstance(validator_nonce, str) or not validator_nonce.strip():
            raise ContractError("validator nonce is required")
        statuses = self._item_statuses(events, manifest)
        if statuses[item["id"]] != "validating":
            raise ContractError(f"{item['id']} does not have an active validator assignment")
        assignment = self._latest_validator_assignment(events, item["id"])
        if assignment is None or assignment.get("validator_nonce") != validator_nonce:
            raise ContractError("validator nonce does not match active validator assignment")
        validator_config = self._validator_config(manifest)
        if assignment.get("validator_config_hash") != stable_json_hash(validator_config):
            raise ContractError("active validator assignment is not bound to the approved validator config")
        if assignment.get("validator_prompt_hash") != manifest["validator_prompt"]["hash"]:
            raise ContractError("active validator assignment is not bound to the approved validator prompt")
        return assignment

    def authorize_validator_spawn(self, item_id: str, validator_nonce: str, launch_block: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(launch_block, dict):
            raise ContractError("validator launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing", "validating"}, "authorize-validator-spawn")
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "authorize-validator-spawn")
            validator_config = self._validator_config(record["manifest"])
            if validator_config.get("mode") != "host-multi-agent":
                raise ContractError("validator is not configured for host-multi-agent execution")
            assignment = self._require_validator_assignment_locked(replayed.events, record["manifest"], item, validator_nonce)
            expected = self._validator_launch_block(
                assignment=assignment,
                validator_config=validator_config,
                validator_prompt=record["manifest"]["validator_prompt"],
                check_ids=self._item_validator_check_ids(item),
            )
            if launch_block != expected:
                raise ContractError("validator launch block does not match the approved validator assignment")
            if self._latest_validator_host_registration(replayed.events, validator_nonce=validator_nonce) is not None:
                raise ContractError("validator agent is already registered for this assignment")
            launch_block_hash = stable_json_hash(launch_block)
            existing = self._latest_validator_host_authorization(replayed.events, validator_nonce=validator_nonce)
            if existing is not None:
                if existing.get("launch_block_hash") != launch_block_hash:
                    raise ContractError("active validator spawn authorization is bound to a different launch block")
                return existing
            payload = {
                "item_id": item_id,
                "attempt": assignment["attempt"],
                "validator_nonce": validator_nonce,
                "launch_nonce": uuid.uuid4().hex,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "launch_block_hash": launch_block_hash,
                "launch_block": launch_block,
            }
            return self._append_event_locked("validator_spawn_authorized", payload)["payload"]

    def register_validator_agent(
        self,
        item_id: str,
        *,
        validator_nonce: str,
        launch_nonce: str,
        agent_handle: str,
        launch_block: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("validator agent handle is required")
        if not isinstance(launch_nonce, str) or not launch_nonce.strip():
            raise ContractError("validator launch nonce is required")
        if not isinstance(launch_block, dict):
            raise ContractError("validator launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing", "validating"}, "register-validator")
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "register-validator")
            validator_config = self._validator_config(record["manifest"])
            assignment = self._require_validator_assignment_locked(replayed.events, record["manifest"], item, validator_nonce)
            expected = self._validator_launch_block(
                assignment=assignment,
                validator_config=validator_config,
                validator_prompt=record["manifest"]["validator_prompt"],
                check_ids=self._item_validator_check_ids(item),
            )
            if launch_block != expected:
                raise ContractError("validator launch block does not match the approved validator assignment")
            authorization = self._latest_validator_host_authorization(
                replayed.events,
                validator_nonce=validator_nonce,
                launch_nonce=launch_nonce,
            )
            if authorization is None:
                raise ContractError("unknown or stale validator launch nonce")
            if authorization.get("launch_block_hash") != stable_json_hash(launch_block):
                raise ContractError("validator launch nonce is not bound to this launch block")
            if any(
                event["type"] == "validator_agent_registered"
                and event.get("payload", {}).get("launch_nonce") == launch_nonce
                for event in replayed.events
            ):
                raise ContractError("validator launch nonce is stale or already used")
            payload = {
                "item_id": item_id,
                "attempt": assignment["attempt"],
                "validator_nonce": validator_nonce,
                "launch_nonce": launch_nonce,
                "agent_handle": agent_handle,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "launch_block_hash": stable_json_hash(launch_block),
            }
            registered = self._append_event_locked("validator_agent_registered", payload)["payload"]
            return {
                **registered,
                "next_action": self._wait_next_action("validator_item", registered, checked_items=self._item_validator_check_ids(item)),
            }

    def _require_batch_validator_assignment_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        batch_id: str,
        validator_nonce: str,
    ) -> dict[str, Any]:
        if not isinstance(validator_nonce, str) or not validator_nonce.strip():
            raise ContractError("validator nonce is required")
        start = self._latest_batch_start(events, batch_id)
        statuses = self._item_statuses(events, manifest)
        if any(statuses.get(item_id) != "validating" for item_id in start["item_ids"]):
            raise ContractError(f"{batch_id} does not have an active validator assignment")
        assignment = self._latest_batch_validator_assignment(events, batch_id)
        if assignment is None or assignment.get("validator_nonce") != validator_nonce:
            raise ContractError("validator nonce does not match active batch validator assignment")
        validator_config = self._validator_config(manifest)
        if assignment.get("validator_config_hash") != stable_json_hash(validator_config):
            raise ContractError("active batch validator assignment is not bound to the approved validator config")
        if assignment.get("validator_prompt_hash") != manifest["validator_prompt"]["hash"]:
            raise ContractError("active batch validator assignment is not bound to the approved validator prompt")
        return assignment

    def authorize_batch_validator_spawn(self, batch_id: str, validator_nonce: str, launch_block: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(launch_block, dict):
            raise ContractError("validator launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing", "validating"}, "authorize-batch-validator-spawn")
            record = self._execution_manifest_record(replayed.events)
            validator_config = self._validator_config(record["manifest"])
            if validator_config.get("mode") != "host-multi-agent":
                raise ContractError("validator is not configured for host-multi-agent execution")
            assignment = self._require_batch_validator_assignment_locked(replayed.events, record["manifest"], batch_id, validator_nonce)
            expected = self._batch_validator_launch_block(
                assignment=assignment,
                validator_config=validator_config,
                validator_prompt=record["manifest"]["validator_prompt"],
                check_ids=self._batch_validator_check_ids(self._batch_items(record["manifest"], list(assignment["item_ids"]))),
            )
            if launch_block != expected:
                raise ContractError("validator launch block does not match the approved batch validator assignment")
            if self._latest_batch_validator_host_registration(replayed.events, validator_nonce=validator_nonce) is not None:
                raise ContractError("validator agent is already registered for this batch assignment")
            launch_block_hash = stable_json_hash(launch_block)
            existing = self._latest_batch_validator_host_authorization(replayed.events, validator_nonce=validator_nonce)
            if existing is not None:
                if existing.get("launch_block_hash") != launch_block_hash:
                    raise ContractError("active batch validator spawn authorization is bound to a different launch block")
                return existing
            payload = {
                "batch_id": batch_id,
                "item_ids": list(assignment["item_ids"]),
                "attempt": assignment["attempt"],
                "assignment_nonce": assignment["assignment_nonce"],
                "validator_nonce": validator_nonce,
                "launch_nonce": uuid.uuid4().hex,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "launch_block_hash": launch_block_hash,
                "launch_block": launch_block,
            }
            return self._append_event_locked("batch_validator_spawn_authorized", payload)["payload"]

    def register_batch_validator_agent(
        self,
        batch_id: str,
        *,
        validator_nonce: str,
        launch_nonce: str,
        agent_handle: str,
        launch_block: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("validator agent handle is required")
        if not isinstance(launch_nonce, str) or not launch_nonce.strip():
            raise ContractError("validator launch nonce is required")
        if not isinstance(launch_block, dict):
            raise ContractError("validator launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing", "validating"}, "register-batch-validator")
            record = self._execution_manifest_record(replayed.events)
            validator_config = self._validator_config(record["manifest"])
            assignment = self._require_batch_validator_assignment_locked(replayed.events, record["manifest"], batch_id, validator_nonce)
            expected = self._batch_validator_launch_block(
                assignment=assignment,
                validator_config=validator_config,
                validator_prompt=record["manifest"]["validator_prompt"],
                check_ids=self._batch_validator_check_ids(self._batch_items(record["manifest"], list(assignment["item_ids"]))),
            )
            if launch_block != expected:
                raise ContractError("validator launch block does not match the approved batch validator assignment")
            authorization = self._latest_batch_validator_host_authorization(
                replayed.events,
                validator_nonce=validator_nonce,
                launch_nonce=launch_nonce,
            )
            if authorization is None:
                raise ContractError("unknown or stale validator launch nonce")
            if authorization.get("launch_block_hash") != stable_json_hash(launch_block):
                raise ContractError("validator launch nonce is not bound to this launch block")
            if any(
                event["type"] == "batch_validator_agent_registered"
                and event.get("payload", {}).get("launch_nonce") == launch_nonce
                for event in replayed.events
            ):
                raise ContractError("validator launch nonce is stale or already used")
            payload = {
                "batch_id": batch_id,
                "item_ids": list(assignment["item_ids"]),
                "attempt": assignment["attempt"],
                "assignment_nonce": assignment["assignment_nonce"],
                "validator_nonce": validator_nonce,
                "launch_nonce": launch_nonce,
                "agent_handle": agent_handle,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "launch_block_hash": stable_json_hash(launch_block),
            }
            registered = self._append_event_locked("batch_validator_agent_registered", payload)["payload"]
            return {
                **registered,
                "next_action": self._wait_next_action(
                    "validator_batch",
                    registered,
                    checked_items=self._batch_validator_check_ids(self._batch_items(record["manifest"], list(assignment["item_ids"]))),
                ),
            }

    def _reject_validator_result_locked(
        self,
        item_id: str,
        *,
        evidence: str,
        assignment: dict[str, Any],
        start: dict[str, Any],
        retryable: bool = True,
    ) -> dict[str, Any]:
        return self._record_attempt_failure_locked(
            "validator_protocol_rejected",
            item_id,
            evidence=evidence,
            start=start,
            extra={
                "attempt": assignment["attempt"],
                "validator_nonce": assignment["validator_nonce"],
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "retryable": retryable,
            },
        )

    def _validator_result_payload(
        self,
        result: dict[str, Any],
        *,
        assignment: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any]:
        for key in (
            "run_id",
            "item_id",
            "attempt",
            "nonce",
            "validator_config_hash",
            "validator_prompt_hash",
            "delta_fingerprint",
            "status",
            "evidence",
            "feedback_for_executor",
            "checked_items",
        ):
            if key not in result:
                raise ContractError(f"validator result is missing {key!r}")
        expected = {
            "run_id": self.run_id,
            "item_id": assignment["item_id"],
            "attempt": assignment["attempt"],
            "nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
        }
        for key, value in expected.items():
            if result.get(key) != value:
                raise ContractError(f"validator result {key} does not match assignment")
        if result["status"] not in {"pass", "fail"}:
            raise ContractError("validator result status must be pass or fail")
        if not isinstance(result["evidence"], str) or not result["evidence"].strip():
            raise ContractError("validator result evidence is required")
        if not isinstance(result["feedback_for_executor"], str):
            raise ContractError("validator result feedback_for_executor must be a string")
        checked_items = result["checked_items"]
        if checked_items != self._item_validator_check_ids(item):
            raise ContractError("validator result checked_items does not match item check IDs")
        return {
            "item_id": assignment["item_id"],
            "attempt": assignment["attempt"],
            "validator_nonce": assignment["validator_nonce"],
            "status": result["status"],
            "evidence": bounded_evidence(result["evidence"]),
            "feedback_for_executor": bounded_evidence(result["feedback_for_executor"]),
            "checked_items": list(checked_items),
            "base_commit": assignment["base_commit"],
            "run_worktree": assignment["run_worktree"],
            "changed_files": list(assignment["changed_files"]),
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
        }

    def _reject_batch_validator_result_locked(
        self,
        batch_id: str,
        *,
        evidence: str,
        assignment: dict[str, Any],
        start: dict[str, Any],
        retryable: bool = True,
    ) -> dict[str, Any]:
        return self._record_batch_attempt_failure_locked(
            "batch_validator_protocol_rejected",
            batch_id,
            evidence=evidence,
            start=start,
            extra={
                "attempt": assignment["attempt"],
                "assignment_nonce": assignment["assignment_nonce"],
                "validator_nonce": assignment["validator_nonce"],
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
                "retryable": retryable,
            },
        )

    def _batch_validator_result_payload(
        self,
        result: dict[str, Any],
        *,
        assignment: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for key in (
            "run_id",
            "batch_id",
            "item_ids",
            "attempt",
            "assignment_nonce",
            "nonce",
            "validator_config_hash",
            "validator_prompt_hash",
            "delta_fingerprint",
            "status",
            "evidence",
            "feedback_for_executor",
            "checked_items",
        ):
            if key not in result:
                raise ContractError(f"validator result is missing {key!r}")
        expected = {
            "run_id": self.run_id,
            "batch_id": assignment["batch_id"],
            "item_ids": assignment["item_ids"],
            "attempt": assignment["attempt"],
            "assignment_nonce": assignment["assignment_nonce"],
            "nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
        }
        for key, value in expected.items():
            if result.get(key) != value:
                raise ContractError(f"validator result {key} does not match assignment")
        if result["status"] not in {"pass", "fail"}:
            raise ContractError("validator result status must be pass or fail")
        if not isinstance(result["evidence"], str) or not result["evidence"].strip():
            raise ContractError("validator result evidence is required")
        if not isinstance(result["feedback_for_executor"], str):
            raise ContractError("validator result feedback_for_executor must be a string")
        checked_items = result["checked_items"]
        expected_checks = self._batch_validator_check_ids(items)
        if checked_items != expected_checks:
            raise ContractError("validator result checked_items does not match batch check IDs")
        return {
            "batch_id": assignment["batch_id"],
            "item_ids": list(assignment["item_ids"]),
            "attempt": assignment["attempt"],
            "assignment_nonce": assignment["assignment_nonce"],
            "validator_nonce": assignment["validator_nonce"],
            "status": result["status"],
            "evidence": bounded_evidence(result["evidence"]),
            "feedback_for_executor": bounded_evidence(result["feedback_for_executor"]),
            "checked_items": list(checked_items),
            "base_commit": assignment["base_commit"],
            "run_worktree": assignment["run_worktree"],
            "changed_files": list(assignment["changed_files"]),
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
        }
    def _auto_restore_validator_retry_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        start: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        failure_event = {"type": "validator_result_recorded", "payload": result_payload}
        decision = self._retry_policy_decision_locked(events, failure_event)
        if decision.action == "blocked":
            return self._append_event_locked(
                "execution_blocked",
                {
                    "item_id": item["id"],
                    "failure_event": "validator_result_recorded",
                    "base_commit": start["base_commit"],
                    "run_worktree": start["run_worktree"],
                    "reason": decision.reason,
                    "failure_signature": decision.failure_signature,
                    "equivalent_failures": decision.equivalent_failures,
                    "total_failures": decision.total_failures,
                    "evidence": result_payload["evidence"],
                },
            )["payload"]
        return self._restore_item_auto_retry_locked(events, manifest, item, start, failure_event)

    def _auto_restore_batch_validator_retry_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        start: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        batch_id = start["batch_id"]
        failure_event = {"type": "batch_validator_result_recorded", "payload": result_payload}
        decision = self._retry_policy_decision_locked(events, failure_event)
        if decision.action == "blocked":
            return self._append_event_locked(
                "batch_execution_blocked",
                {
                    "batch_id": batch_id,
                    "item_ids": list(start["item_ids"]),
                    "failure_event": "batch_validator_result_recorded",
                    "base_commit": start["base_commit"],
                    "run_worktree": start["run_worktree"],
                    "reason": decision.reason,
                    "failure_signature": decision.failure_signature,
                    "equivalent_failures": decision.equivalent_failures,
                    "total_failures": decision.total_failures,
                    "evidence": result_payload["evidence"],
                },
            )["payload"]
        return self._restore_batch_auto_retry_locked(events, manifest, start, failure_event)

    def record_validator_result(
        self,
        item_id: str,
        *,
        validator_nonce: str,
        result: dict[str, Any],
        agent_handle: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ContractError("validator result must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "complete-validator")
            assignment = self._require_validator_assignment_locked(replayed.events, record["manifest"], item, validator_nonce)
            start = self._latest_item_start(replayed.events, item_id)
            validator_config = self._validator_config(record["manifest"])
            if validator_config.get("mode") == "host-multi-agent":
                if agent_handle is None:
                    raise ContractError("validator agent handle is required for host validator results")
                registration = self._latest_validator_host_registration(
                    replayed.events,
                    validator_nonce=validator_nonce,
                    agent_handle=agent_handle,
                )
                if registration is None:
                    raise ContractError("registered validator agent handle does not match active assignment")
            else:
                registration = None
            recovery = self._record_context_integrity_recovery_locked(item_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            try:
                current = self._item_delta_locked(
                    replayed.events,
                    record["manifest"],
                    self._execution_started_record(replayed.events),
                    item,
                    start,
                )
                if current["delta_fingerprint"] != assignment["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed before validator result receipt")
            except ContractError as exc:
                self._reject_validator_result_locked(
                    item_id,
                    evidence=f"validator result rejected: {exc}",
                    assignment=assignment,
                    start=start,
                    retryable=False,
                )
                raise
            try:
                payload = self._validator_result_payload(result, assignment=assignment, item=item)
            except ContractError as exc:
                return self._reject_validator_result_locked(
                    item_id,
                    evidence=f"validator result rejected: {exc}",
                    assignment=assignment,
                    start=start,
                )
            if registration is not None:
                payload.update({"agent_handle": agent_handle, "launch_nonce": registration["launch_nonce"]})
            if payload["status"] == "fail":
                payload["retryable"] = True
            recorded = self._append_event_locked("validator_result_recorded", payload)["payload"]
            if recorded["status"] == "fail":
                return self._auto_restore_validator_retry_locked(
                    [*replayed.events, {"type": "validator_result_recorded", "payload": recorded}],
                    record["manifest"],
                    item,
                    start,
                    recorded,
                )
            return recorded

    def record_batch_validator_result(
        self,
        batch_id: str,
        *,
        validator_nonce: str,
        result: dict[str, Any],
        agent_handle: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ContractError("validator result must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            assignment = self._require_batch_validator_assignment_locked(replayed.events, record["manifest"], batch_id, validator_nonce)
            start = self._latest_batch_start(replayed.events, batch_id)
            items = self._batch_items(record["manifest"], list(assignment["item_ids"]))
            validator_config = self._validator_config(record["manifest"])
            if validator_config.get("mode") == "host-multi-agent":
                if agent_handle is None:
                    raise ContractError("validator agent handle is required for host validator results")
                registration = self._latest_batch_validator_host_registration(
                    replayed.events,
                    validator_nonce=validator_nonce,
                    agent_handle=agent_handle,
                )
                if registration is None:
                    raise ContractError("registered validator agent handle does not match active assignment")
            else:
                registration = None
            recovery = self._record_batch_context_integrity_recovery_locked(batch_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            try:
                current = self._batch_delta_locked(
                    replayed.events,
                    record["manifest"],
                    self._execution_started_record(replayed.events),
                    start,
                )
                if current["delta_fingerprint"] != assignment["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed before validator result receipt")
            except ContractError as exc:
                self._reject_batch_validator_result_locked(
                    batch_id,
                    evidence=f"validator result rejected: {exc}",
                    assignment=assignment,
                    start=start,
                    retryable=False,
                )
                raise
            try:
                payload = self._batch_validator_result_payload(result, assignment=assignment, items=items)
            except ContractError as exc:
                return self._reject_batch_validator_result_locked(
                    batch_id,
                    evidence=f"validator result rejected: {exc}",
                    assignment=assignment,
                    start=start,
                )
            if registration is not None:
                payload.update({"agent_handle": agent_handle, "launch_nonce": registration["launch_nonce"]})
            if payload["status"] == "fail":
                payload["retryable"] = True
            recorded = self._append_event_locked("batch_validator_result_recorded", payload)["payload"]
            if recorded["status"] == "fail":
                return self._auto_restore_batch_validator_retry_locked(
                    [*replayed.events, {"type": "batch_validator_result_recorded", "payload": recorded}],
                    record["manifest"],
                    start,
                    recorded,
                )
            return recorded

    def fail_validator(
        self,
        item_id: str,
        *,
        reason: str,
        validator_nonce: str | None = None,
        agent_handle: str | None = None,
        launch_nonce: str | None = None,
        evidence: str = "",
    ) -> dict[str, Any]:
        if reason not in {"process", "crash", "timeout", "interrupted", "unknown"}:
            raise ContractError("validator failure reason must be process, crash, timeout, interrupted, or unknown")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "fail-validator")
            assignment = self._latest_validator_assignment(replayed.events, item_id)
            if assignment is None:
                raise ContractError(f"{item_id} has no active validator assignment")
            if validator_nonce is not None and assignment.get("validator_nonce") != validator_nonce:
                raise ContractError("validator nonce does not match active validator assignment")
            start = self._latest_item_start(replayed.events, item_id)
            extra = {
                "attempt": assignment["attempt"],
                "validator_nonce": assignment["validator_nonce"],
                "reason": reason,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
            }
            if agent_handle is not None:
                registration = self._latest_validator_host_registration(
                    replayed.events,
                    validator_nonce=assignment["validator_nonce"],
                    agent_handle=agent_handle,
                )
                if registration is None:
                    raise ContractError("registered validator agent handle does not match active assignment")
                extra.update({"agent_handle": agent_handle, "launch_nonce": registration["launch_nonce"]})
            elif launch_nonce is not None:
                authorization = self._latest_validator_host_authorization(
                    replayed.events,
                    validator_nonce=assignment["validator_nonce"],
                    launch_nonce=launch_nonce,
                )
                if authorization is None:
                    raise ContractError("unknown or stale validator launch nonce")
                extra.update({"launch_nonce": launch_nonce, "agent_handle_lost": True})
            recovery = self._record_context_integrity_recovery_locked(item_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            return self._record_attempt_failure_locked(
                "validator_failed",
                item_id,
                evidence=evidence or f"validator {reason}",
                start=start,
                extra=extra,
            )

    def fail_batch_validator(
        self,
        batch_id: str,
        *,
        reason: str,
        validator_nonce: str | None = None,
        agent_handle: str | None = None,
        launch_nonce: str | None = None,
        evidence: str = "",
    ) -> dict[str, Any]:
        if reason not in {"process", "crash", "timeout", "interrupted", "unknown"}:
            raise ContractError("validator failure reason must be process, crash, timeout, interrupted, or unknown")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            assignment = self._latest_batch_validator_assignment(replayed.events, batch_id)
            if assignment is None:
                raise ContractError(f"{batch_id} has no active validator assignment")
            if validator_nonce is not None and assignment.get("validator_nonce") != validator_nonce:
                raise ContractError("validator nonce does not match active validator assignment")
            start = self._latest_batch_start(replayed.events, batch_id)
            extra = {
                "attempt": assignment["attempt"],
                "assignment_nonce": assignment["assignment_nonce"],
                "validator_nonce": assignment["validator_nonce"],
                "reason": reason,
                "validator_config_hash": assignment["validator_config_hash"],
                "validator_prompt_hash": assignment["validator_prompt_hash"],
                "delta_fingerprint": assignment["delta_fingerprint"],
            }
            if agent_handle is not None:
                registration = self._latest_batch_validator_host_registration(
                    replayed.events,
                    validator_nonce=assignment["validator_nonce"],
                    agent_handle=agent_handle,
                )
                if registration is None:
                    raise ContractError("registered validator agent handle does not match active assignment")
                extra.update({"agent_handle": agent_handle, "launch_nonce": registration["launch_nonce"]})
            elif launch_nonce is not None:
                authorization = self._latest_batch_validator_host_authorization(
                    replayed.events,
                    validator_nonce=assignment["validator_nonce"],
                    launch_nonce=launch_nonce,
                )
                if authorization is None:
                    raise ContractError("unknown or stale validator launch nonce")
                extra.update({"launch_nonce": launch_nonce, "agent_handle_lost": True})
            recovery = self._record_batch_context_integrity_recovery_locked(batch_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            # record["manifest"] is read above to keep the manifest event validated before failure recording.
            _ = record
            return self._record_batch_attempt_failure_locked(
                "batch_validator_failed",
                batch_id,
                evidence=evidence or f"validator {reason}",
                start=start,
                extra=extra,
            )

    def assign_validator(self, item_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            if not self._manifest_uses_validator(record["manifest"]):
                raise ContractError("execution manifest does not use validator workers")
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "assign-validator")
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] == "validating":
                assignment = self._latest_validator_assignment(replayed.events, item_id)
                if assignment is None:
                    raise ContractError(f"{item_id} validator assignment is missing")
                return self._validator_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    item,
                    assignment,
                    self._validator_config(record["manifest"]),
                )
            if statuses[item_id] != "completed":
                raise ContractError(f"{item_id} is not ready for validator assignment; current status is {statuses[item_id]}")
            start = self._latest_item_start(replayed.events, item_id)
            return self._assign_validator_locked(replayed.events, record["manifest"], item, start)

    def assign_batch_validator(self, batch_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            if not self._manifest_uses_validator(record["manifest"]):
                raise ContractError("execution manifest does not use validator workers")
            start = self._latest_batch_start(replayed.events, batch_id)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if all(statuses.get(item_id) == "validating" for item_id in start["item_ids"]):
                assignment = self._latest_batch_validator_assignment(replayed.events, batch_id)
                if assignment is None:
                    raise ContractError(f"{batch_id} validator assignment is missing")
                return self._batch_validator_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    assignment,
                    self._validator_config(record["manifest"]),
                )
            if any(statuses.get(item_id) != "completed" for item_id in start["item_ids"]):
                raise ContractError(f"{batch_id} is not ready for validator assignment")
            return self._assign_batch_validator_locked(replayed.events, record["manifest"], start)

    def reject_validator_protocol(self, item_id: str, *, validator_nonce: str, evidence: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "complete-validator")
            assignment = self._require_validator_assignment_locked(replayed.events, record["manifest"], item, validator_nonce)
            start = self._latest_item_start(replayed.events, item_id)
            recovery = self._record_context_integrity_recovery_locked(item_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            return self._reject_validator_result_locked(item_id, evidence=evidence, assignment=assignment, start=start)

    def reject_batch_validator_protocol(self, batch_id: str, *, validator_nonce: str, evidence: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            assignment = self._require_batch_validator_assignment_locked(replayed.events, record["manifest"], batch_id, validator_nonce)
            start = self._latest_batch_start(replayed.events, batch_id)
            recovery = self._record_batch_context_integrity_recovery_locked(batch_id, assignment=assignment, start=start)
            if recovery is not None:
                return recovery
            return self._reject_batch_validator_result_locked(batch_id, evidence=evidence, assignment=assignment, start=start)

    def run_validator(self, item_id: str) -> dict[str, Any]:
        assignment = self.assign_validator(item_id)
        validator_config = assignment["validator"]
        if validator_config.get("mode") in {"host-multi-agent", "foreground"}:
            return assignment
        self._ensure_adapter_launch_files(validator_config, write=False)
        run_worktree = Path(assignment["run_worktree"])
        state_path = self.run_dir / "validator-states" / f"{item_id}-{assignment['attempt']}.json"
        validator_state = {
            "run_id": self.run_id,
            "item_id": item_id,
            "attempt": assignment["attempt"],
            "validator_nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
            "checked_items": self._item_validator_check_ids(self._manifest_item(self._execution_manifest_record(self.replay().events)["manifest"], item_id)),
            "validator_prompt": self._execution_manifest_record(self.replay().events)["manifest"]["validator_prompt"],
            "plan_context": assignment["validator_launch_block"]["plan_context"],
        }
        write_json_atomic(state_path, validator_state)
        env = os.environ.copy()
        env.update(validator_config["env"])
        env.update(
            {
                "OPTIM_PLANS_RUN_ID": self.run_id,
                "OPTIM_PLANS_ITEM_ID": item_id,
                "OPTIM_PLANS_ATTEMPT": str(assignment["attempt"]),
                "OPTIM_PLANS_VALIDATOR_NONCE": assignment["validator_nonce"],
                "OPTIM_PLANS_VALIDATOR_CONFIG_HASH": assignment["validator_config_hash"],
                "OPTIM_PLANS_VALIDATOR_PROMPT_HASH": assignment["validator_prompt_hash"],
                "OPTIM_PLANS_DELTA_FINGERPRINT": assignment["delta_fingerprint"],
                "OPTIM_PLANS_VALIDATOR_STATE_PATH": str(state_path),
                "OPTIM_PLANS_CHECK_IDS": json_text(validator_state["checked_items"]),
                "OPTIM_PLANS_PLAN_CONTEXT": json_text(validator_state["plan_context"]),
            }
        )
        validator = run_process_group(
            validator_config["argv"],
            cwd=run_worktree,
            env=env,
            timeout_seconds=validator_config["timeout_seconds"],
        )
        if not validator.ok():
            reason = "timeout" if validator.timed_out else "process"
            evidence = validator.evidence("validator", timeout_seconds=validator_config["timeout_seconds"])
            self.fail_validator(item_id, reason=reason, validator_nonce=assignment["validator_nonce"], evidence=evidence)
            raise ContractError(evidence)
        if not validator.stdout.strip():
            evidence = "validator result rejected: validator stdout result is missing"
            return self.reject_validator_protocol(item_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        try:
            result = parse_json_strict(validator.stdout.strip(), source="validator stdout")
        except ContractError as exc:
            evidence = f"validator result rejected: {exc}"
            return self.reject_validator_protocol(item_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        if not isinstance(result, dict):
            evidence = "validator result rejected: validator result must be a JSON object"
            return self.reject_validator_protocol(item_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        return self.record_validator_result(item_id, validator_nonce=assignment["validator_nonce"], result=result)

    def _require_host_assignment_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        assignment_nonce: str,
    ) -> dict[str, Any]:
        if not isinstance(assignment_nonce, str) or not assignment_nonce.strip():
            raise ContractError("assignment nonce is required")
        statuses = self._item_statuses(events, manifest)
        if statuses[item["id"]] != "in_progress":
            raise ContractError(f"{item['id']} does not have an active host assignment")
        start = self._latest_item_start(events, item["id"])
        if start.get("assignment_nonce") != assignment_nonce:
            raise ContractError("assignment nonce does not match active item assignment")
        worker_config = self._require_host_worker_config(manifest, item)
        if start.get("worker_config_hash") != stable_json_hash(worker_config):
            raise ContractError("active assignment is not bound to the approved host worker config")
        return start

    def authorize_spawn(self, item_id: str, assignment_nonce: str, launch_block: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(launch_block, dict):
            raise ContractError("launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "authorize-spawn")
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "authorize-spawn")
            worker_config = self._require_host_worker_config(record["manifest"], item)
            start = self._require_host_assignment_locked(replayed.events, record["manifest"], item, assignment_nonce)
            expected = self._host_launch_block(item_id=item_id, start=start, worker_config=worker_config)
            if launch_block != expected:
                raise ContractError("host launch block does not match the approved item assignment")
            existing_registration = self._latest_host_registration(replayed.events, assignment_nonce=assignment_nonce)
            if existing_registration is not None:
                raise ContractError("host agent is already registered for this assignment")
            launch_block_hash = stable_json_hash(launch_block)
            existing = self._latest_host_authorization(replayed.events, assignment_nonce=assignment_nonce)
            if existing is not None:
                if existing.get("launch_block_hash") != launch_block_hash:
                    raise ContractError("active host spawn authorization is bound to a different launch block")
                return existing
            payload = {
                "item_id": item_id,
                "attempt": start["attempt"],
                "assignment_nonce": assignment_nonce,
                "launch_nonce": uuid.uuid4().hex,
                "worker_config_hash": stable_json_hash(worker_config),
                "launch_block_hash": launch_block_hash,
                "launch_block": launch_block,
            }
            return self._append_event_locked("host_spawn_authorized", payload)["payload"]

    def register_agent(
        self,
        item_id: str,
        *,
        assignment_nonce: str,
        launch_nonce: str,
        agent_handle: str,
        launch_block: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("agent handle is required")
        if not isinstance(launch_nonce, str) or not launch_nonce.strip():
            raise ContractError("launch nonce is required")
        if not isinstance(launch_block, dict):
            raise ContractError("launch block must be a JSON object")
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "register-agent")
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "register-agent")
            worker_config = self._require_host_worker_config(record["manifest"], item)
            start = self._require_host_assignment_locked(replayed.events, record["manifest"], item, assignment_nonce)
            expected = self._host_launch_block(item_id=item_id, start=start, worker_config=worker_config)
            if launch_block != expected:
                raise ContractError("host launch block does not match the approved item assignment")
            authorization = self._latest_host_authorization(
                replayed.events,
                assignment_nonce=assignment_nonce,
                launch_nonce=launch_nonce,
            )
            if authorization is None:
                raise ContractError("unknown or stale host launch nonce")
            if authorization.get("launch_block_hash") != stable_json_hash(launch_block):
                raise ContractError("host launch nonce is not bound to this launch block")
            if any(
                event["type"] == "host_agent_registered"
                and event.get("payload", {}).get("launch_nonce") == launch_nonce
                for event in replayed.events
            ):
                raise ContractError("host launch nonce is stale or already used")
            payload = {
                "item_id": item_id,
                "attempt": start["attempt"],
                "assignment_nonce": assignment_nonce,
                "launch_nonce": launch_nonce,
                "agent_handle": agent_handle,
                "worker_config_hash": stable_json_hash(worker_config),
                "launch_block_hash": stable_json_hash(launch_block),
            }
            registered = self._append_event_locked("host_agent_registered", payload)["payload"]
            return {**registered, "next_action": self._wait_next_action("executor_item", registered)}

    def _require_registered_host_agent_locked(
        self,
        events: list[dict[str, Any]],
        manifest: dict[str, Any],
        item: dict[str, Any],
        *,
        assignment_nonce: str,
        agent_handle: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(agent_handle, str) or not agent_handle.strip():
            raise ContractError("agent handle is required")
        start = self._require_host_assignment_locked(events, manifest, item, assignment_nonce)
        registration = self._latest_host_registration(
            events,
            assignment_nonce=assignment_nonce,
            agent_handle=agent_handle,
        )
        if registration is None:
            raise ContractError("registered host agent handle does not match active assignment")
        return start, registration

    def complete_host_item(
        self,
        item_id: str,
        *,
        assignment_nonce: str,
        agent_handle: str,
        evidence: str,
    ) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker completion evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "complete-item")
            _start, registration = self._require_registered_host_agent_locked(
                replayed.events,
                record["manifest"],
                item,
                assignment_nonce=assignment_nonce,
                agent_handle=agent_handle,
            )
            payload = {
                "item_id": item_id,
                "assignment_nonce": assignment_nonce,
                "agent_handle": agent_handle,
                "launch_nonce": registration["launch_nonce"],
                "evidence": bounded_evidence(evidence),
            }
            return self._append_event_locked("worker_completed", payload)["payload"]

    def fail_host_item(
        self,
        item_id: str,
        *,
        assignment_nonce: str,
        agent_handle: str | None = None,
        launch_nonce: str | None = None,
        evidence: str,
    ) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "fail-item")
            if agent_handle is not None:
                start, registration = self._require_registered_host_agent_locked(
                    replayed.events,
                    record["manifest"],
                    item,
                    assignment_nonce=assignment_nonce,
                    agent_handle=agent_handle,
                )
                extra = {
                    "assignment_nonce": assignment_nonce,
                    "agent_handle": agent_handle,
                    "launch_nonce": registration["launch_nonce"],
                }
            else:
                if not isinstance(launch_nonce, str) or not launch_nonce.strip():
                    raise ContractError("launch nonce is required when failing a host item without an agent handle")
                start = self._require_host_assignment_locked(replayed.events, record["manifest"], item, assignment_nonce)
                authorization = self._latest_host_authorization(
                    replayed.events,
                    assignment_nonce=assignment_nonce,
                    launch_nonce=launch_nonce,
                )
                if authorization is None:
                    raise ContractError("unknown or stale host launch nonce")
                if any(
                    event["type"] == "host_agent_registered"
                    and event.get("payload", {}).get("launch_nonce") == launch_nonce
                    for event in replayed.events
                ):
                    raise ContractError("agent handle is required after host launch nonce registration")
                extra = {
                    "assignment_nonce": assignment_nonce,
                    "launch_nonce": launch_nonce,
                    "agent_handle_lost": True,
                }
            return self._record_attempt_failure_locked(
                "worker_failed",
                item_id,
                evidence=evidence,
                start=start,
                extra=extra,
            )

    def run_batch_validator(self, batch_id: str) -> dict[str, Any]:
        assignment = self.assign_batch_validator(batch_id)
        validator_config = assignment["validator"]
        if validator_config.get("mode") in {"host-multi-agent", "foreground"}:
            return assignment
        self._ensure_adapter_launch_files(validator_config, write=False)
        run_worktree = Path(assignment["run_worktree"])
        state_path = self.run_dir / "validator-states" / f"{batch_id}-{assignment['attempt']}.json"
        item_ids = list(assignment["item_ids"])
        checked_items = self._batch_validator_check_ids(
            self._batch_items(self._execution_manifest_record(self.replay().events)["manifest"], item_ids)
        )
        validator_state = {
            "run_id": self.run_id,
            "batch_id": batch_id,
            "item_ids": item_ids,
            "attempt": assignment["attempt"],
            "assignment_nonce": assignment["assignment_nonce"],
            "validator_nonce": assignment["validator_nonce"],
            "validator_config_hash": assignment["validator_config_hash"],
            "validator_prompt_hash": assignment["validator_prompt_hash"],
            "delta_fingerprint": assignment["delta_fingerprint"],
            "checked_items": checked_items,
            "validator_prompt": self._execution_manifest_record(self.replay().events)["manifest"]["validator_prompt"],
            "plan_context": assignment["validator_launch_block"]["plan_context"],
            "prior_context": assignment["validator_launch_block"].get("prior_context", ""),
        }
        write_json_atomic(state_path, validator_state)
        env = os.environ.copy()
        env.update(validator_config["env"])
        env.update(
            {
                "OPTIM_PLANS_RUN_ID": self.run_id,
                "OPTIM_PLANS_BATCH_ID": batch_id,
                "OPTIM_PLANS_ITEM_IDS": json_text(item_ids),
                "OPTIM_PLANS_ATTEMPT": str(assignment["attempt"]),
                "OPTIM_PLANS_ASSIGNMENT_NONCE": assignment["assignment_nonce"],
                "OPTIM_PLANS_VALIDATOR_NONCE": assignment["validator_nonce"],
                "OPTIM_PLANS_VALIDATOR_CONFIG_HASH": assignment["validator_config_hash"],
                "OPTIM_PLANS_VALIDATOR_PROMPT_HASH": assignment["validator_prompt_hash"],
                "OPTIM_PLANS_DELTA_FINGERPRINT": assignment["delta_fingerprint"],
                "OPTIM_PLANS_VALIDATOR_STATE_PATH": str(state_path),
                "OPTIM_PLANS_CHECK_IDS": json_text(checked_items),
                "OPTIM_PLANS_PLAN_CONTEXT": json_text(validator_state["plan_context"]),
            }
        )
        if validator_state["prior_context"]:
            env["OPTIM_PLANS_PRIOR_CONTEXT"] = str(validator_state["prior_context"])
        validator = run_process_group(
            validator_config["argv"],
            cwd=run_worktree,
            env=env,
            timeout_seconds=validator_config["timeout_seconds"],
        )
        if not validator.ok():
            reason = "timeout" if validator.timed_out else "process"
            evidence = validator.evidence("validator", timeout_seconds=validator_config["timeout_seconds"])
            self.fail_batch_validator(batch_id, reason=reason, validator_nonce=assignment["validator_nonce"], evidence=evidence)
            raise ContractError(evidence)
        if not validator.stdout.strip():
            evidence = "validator result rejected: validator stdout result is missing"
            return self.reject_batch_validator_protocol(batch_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        try:
            result = parse_json_strict(validator.stdout.strip(), source="validator stdout")
        except ContractError as exc:
            evidence = f"validator result rejected: {exc}"
            return self.reject_batch_validator_protocol(batch_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        if not isinstance(result, dict):
            evidence = "validator result rejected: validator result must be a JSON object"
            return self.reject_batch_validator_protocol(batch_id, validator_nonce=assignment["validator_nonce"], evidence=evidence)
        return self.record_batch_validator_result(batch_id, validator_nonce=assignment["validator_nonce"], result=result)

    def advance_batch(self, batch_id: str) -> dict[str, Any]:
        retry_batch_item_ids: list[str] | None = None
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            start = self._latest_batch_start(replayed.events, batch_id)
            item_ids = list(start["item_ids"])
            worker_config = self._require_host_batch_worker_config(record["manifest"], self._batch_items(record["manifest"], item_ids))
            statuses = self._item_statuses(replayed.events, record["manifest"])
            phases = {statuses[item_id] for item_id in item_ids}
            status = phases.pop() if len(phases) == 1 else "mixed"
            uses_validator = self._manifest_uses_validator(record["manifest"])
            if status == "pending":
                pending_retry = self._pending_retry_batch(replayed.events)
                if pending_retry is not None and pending_retry.get("batch_id") == batch_id:
                    retry_batch_item_ids = item_ids
                else:
                    return {"batch_id": batch_id, "item_ids": item_ids, "phase": "pending"}
            elif status == "in_progress":
                return self._batch_assignment_response_locked(replayed.events, record["manifest"], start, worker_config)
            elif status == "validating":
                assignment = self._latest_batch_validator_assignment(replayed.events, batch_id)
                if assignment is None:
                    raise ContractError(f"{batch_id} validator assignment is missing")
                return self._batch_validator_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    assignment,
                    self._validator_config(record["manifest"]),
                )
            elif status == "failed":
                return {"batch_id": batch_id, "item_ids": item_ids, "phase": "failed"}
            elif status == "verified":
                all_verified = all(current == "verified" for current in statuses.values())
                checkpoint = next(
                    event["payload"]
                    for event in reversed(replayed.events)
                    if event["type"] == "batch_checkpoint_created" and event.get("payload", {}).get("batch_id") == batch_id
                )
                if not all_verified:
                    return {"batch_id": batch_id, "item_ids": item_ids, "phase": "checkpointed", **checkpoint}
            elif status == "prepared":
                pass
            elif status == "completed" and uses_validator:
                pass
            elif status not in {"completed", "validated"}:
                raise ContractError(f"{batch_id} cannot be advanced from status {status}")

        if status == "pending" and retry_batch_item_ids is not None:
            return self.assign_batch(retry_batch_item_ids)
        if status == "completed" and uses_validator:
            validated = self.run_batch_validator(batch_id)
            if validated.get("status") == "pass":
                return self.advance_batch(batch_id)
            if validated.get("auto_validator_retry"):
                return self.assign_batch(item_ids)
            return validated
        if status in {"completed", "validated"}:
            self._assert_batch_protected_metadata_before_verification(batch_id)
            start = self._latest_batch_start(self.replay().events, batch_id)
            if uses_validator:
                self._assert_batch_delta_fingerprint_before_verification(batch_id)
            evidence_parts: list[str] = []
            run_worktree = Path(start["run_worktree"])
            for item in self._batch_items(self._execution_manifest_record(self.replay().events)["manifest"], list(start["item_ids"])):
                verification_config = self._verification_config(self._execution_manifest_record(self.replay().events)["manifest"], item)
                verifier_env = os.environ.copy()
                verifier_env.update(verification_config["env"])
                verifier = run_process_group(
                    verification_config["argv"],
                    cwd=run_worktree,
                    env=verifier_env,
                    timeout_seconds=verification_config["timeout_seconds"],
                )
                verifier_evidence = verifier.evidence(
                    f"verification {item['id']}",
                    timeout_seconds=verification_config["timeout_seconds"],
                )
                evidence_parts.append(verifier_evidence)
                if not verifier.ok():
                    failed = self.record_batch_attempt_failure("batch_verification_failed", batch_id, evidence=verifier_evidence)
                    if failed.get("auto_retry"):
                        return self.assign_batch(item_ids)
                    raise ContractError(verifier_evidence)
            if uses_validator:
                self._assert_batch_delta_fingerprint_after_verification(batch_id)
            checkpoint = self.checkpoint_batch(batch_id, evidence=bounded_evidence("\n\n".join(evidence_parts)))
        elif status == "prepared":
            checkpoint = self.checkpoint_batch(batch_id, evidence="prepared checkpoint")
        else:
            checkpoint = None
        if checkpoint is not None and checkpoint.get("phase") == "awaiting_execution_summary":
            return checkpoint

        try:
            final = self.final_audit()
            payload: dict[str, Any] = {"batch_id": batch_id, "item_ids": item_ids, "phase": "finalized", "final_audit": final}
        except ContractError:
            if lifecycle_status(self.replay().events) == "awaiting_retry_decision":
                raise
            payload = {"batch_id": batch_id, "item_ids": item_ids, "phase": "checkpointed"}
        if checkpoint is not None:
            payload.update(checkpoint)
        return payload

    def advance_item(self, item_id: str) -> dict[str, Any]:
        retry_item_id: str | None = None
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "advance-item")
            worker_config = self._require_host_worker_config(record["manifest"], item)
            verification_config = self._verification_config(record["manifest"], item)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            status = statuses[item_id]
            uses_validator = self._manifest_uses_validator(record["manifest"])
            if status == "pending":
                if self._pending_retry_item(replayed.events, item_id) is not None:
                    retry_item_id = item_id
                else:
                    return {"item_id": item_id, "phase": "pending"}
            elif status == "in_progress":
                start = self._latest_item_start(replayed.events, item_id)
                return self._host_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    item,
                    start,
                    worker_config,
                )
            elif status == "validating":
                assignment = self._latest_validator_assignment(replayed.events, item_id)
                if assignment is None:
                    raise ContractError(f"{item_id} validator assignment is missing")
                return self._validator_assignment_response_locked(
                    replayed.events,
                    record["manifest"],
                    item,
                    assignment,
                    self._validator_config(record["manifest"]),
                )
            elif status == "failed":
                return {"item_id": item_id, "phase": "failed"}
            elif status == "verified":
                all_verified = all(current == "verified" for current in statuses.values())
                checkpoint = next(
                    event["payload"]
                    for event in reversed(replayed.events)
                    if event["type"] == "checkpoint_created" and event.get("payload", {}).get("item_id") == item_id
                )
                if not all_verified:
                    return {"item_id": item_id, "phase": "checkpointed", **checkpoint}
            elif status == "prepared":
                pass
            elif status == "completed" and uses_validator:
                pass
            elif status not in {"completed", "validated"}:
                raise ContractError(f"{item_id} cannot be advanced from status {status}")

        if status == "pending" and retry_item_id is not None:
            return self.assign_item(retry_item_id)
        if status == "completed" and uses_validator:
            validated = self.run_validator(item_id)
            if validated.get("status") == "pass":
                return self.advance_item(item_id)
            if validated.get("auto_validator_retry"):
                return self.assign_item(item_id)
            return validated
        if status in {"completed", "validated"}:
            self._assert_protected_metadata_before_verification(item_id)
            start = self._latest_item_start(self.replay().events, item_id)
            if uses_validator:
                self._assert_delta_fingerprint_before_verification(item_id)
            verifier_env = os.environ.copy()
            verifier_env.update(verification_config["env"])
            verifier = run_process_group(
                verification_config["argv"],
                cwd=Path(start["run_worktree"]),
                env=verifier_env,
                timeout_seconds=verification_config["timeout_seconds"],
            )
            verifier_evidence = verifier.evidence(
                "verification",
                timeout_seconds=verification_config["timeout_seconds"],
            )
            if not verifier.ok():
                failed = self.record_attempt_failure("verification_failed", item_id, evidence=verifier_evidence)
                if failed.get("auto_retry"):
                    return self.assign_item(item_id)
                raise ContractError(verifier_evidence)
            if uses_validator:
                self._assert_delta_fingerprint_after_verification(item_id)
            checkpoint = self.checkpoint_item(item_id, evidence=verifier_evidence)
        elif status == "prepared":
            checkpoint = self.checkpoint_item(item_id, evidence="prepared checkpoint")
        else:
            checkpoint = None
        if checkpoint is not None and checkpoint.get("phase") == "awaiting_execution_summary":
            return checkpoint

        try:
            final = self.final_audit()
            payload: dict[str, Any] = {"item_id": item_id, "phase": "finalized", "final_audit": final}
        except ContractError:
            if lifecycle_status(self.replay().events) == "awaiting_retry_decision":
                raise
            payload = {"item_id": item_id, "phase": "checkpointed"}
        if checkpoint is not None:
            payload.update(checkpoint)
        return payload

    def _assert_protected_metadata_before_verification(self, item_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                started = self._execution_started_record(replayed.events)
                self._require_protected_metadata_clean(started)
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self.record_attempt_failure("audit_failed", item_id, evidence=f"audit failed: {exc}", retryable=False)
            raise

    def _latest_validator_pass(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "validator_result_recorded" and payload.get("status") == "pass":
                return payload
            if event["type"] in {"retry_restored", "checkpoint_created", "item_started"}:
                break
        raise ContractError(f"{item_id} does not have a passing validator result")

    def _assert_delta_fingerprint_before_verification(self, item_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                record = self._execution_manifest_record(replayed.events)
                item = self._manifest_item(record["manifest"], item_id)
                started = self._execution_started_record(replayed.events)
                start = self._latest_item_start(replayed.events, item_id)
                validator = self._latest_validator_pass(replayed.events, item_id)
                delta = self._item_delta_locked(replayed.events, record["manifest"], started, item, start)
                if delta["delta_fingerprint"] != validator["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed before verification")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self.record_attempt_failure("audit_failed", item_id, evidence=f"audit failed: {exc}", retryable=False)
            raise

    def _assert_delta_fingerprint_after_verification(self, item_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                record = self._execution_manifest_record(replayed.events)
                item = self._manifest_item(record["manifest"], item_id)
                started = self._execution_started_record(replayed.events)
                start = self._latest_item_start(replayed.events, item_id)
                validator = self._latest_validator_pass(replayed.events, item_id)
                delta = self._item_delta_locked(replayed.events, record["manifest"], started, item, start)
                if delta["delta_fingerprint"] != validator["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed after verification")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self.record_attempt_failure("audit_failed", item_id, evidence=f"audit failed: {exc}", retryable=False)
            raise

    def _latest_batch_validator_pass(self, events: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("batch_id") != batch_id:
                continue
            if event["type"] == "batch_validator_result_recorded" and payload.get("status") == "pass":
                return payload
            if event["type"] in {"batch_retry_restored", "batch_checkpoint_created", "batch_started"}:
                break
        raise ContractError(f"{batch_id} does not have a passing validator result")

    def _assert_batch_protected_metadata_before_verification(self, batch_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                started = self._execution_started_record(replayed.events)
                self._require_protected_metadata_clean(started)
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            start = self._latest_batch_start(self.replay().events, batch_id)
            with self.controller_lock():
                self._record_batch_attempt_failure_locked(
                    "batch_audit_failed",
                    batch_id,
                    evidence=f"audit failed: {exc}",
                    start=start,
                    extra={"retryable": False},
                )
            raise

    def _assert_batch_delta_fingerprint_before_verification(self, batch_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                record = self._execution_manifest_record(replayed.events)
                started = self._execution_started_record(replayed.events)
                start = self._latest_batch_start(replayed.events, batch_id)
                validator = self._latest_batch_validator_pass(replayed.events, batch_id)
                delta = self._batch_delta_locked(replayed.events, record["manifest"], started, start)
                if delta["delta_fingerprint"] != validator["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed before verification")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            start = self._latest_batch_start(self.replay().events, batch_id)
            with self.controller_lock():
                self._record_batch_attempt_failure_locked(
                    "batch_audit_failed",
                    batch_id,
                    evidence=f"audit failed: {exc}",
                    start=start,
                    extra={"retryable": False},
                )
            raise

    def _assert_batch_delta_fingerprint_after_verification(self, batch_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                record = self._execution_manifest_record(replayed.events)
                started = self._execution_started_record(replayed.events)
                start = self._latest_batch_start(replayed.events, batch_id)
                validator = self._latest_batch_validator_pass(replayed.events, batch_id)
                delta = self._batch_delta_locked(replayed.events, record["manifest"], started, start)
                if delta["delta_fingerprint"] != validator["delta_fingerprint"]:
                    raise ContractError("delta fingerprint changed after verification")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            start = self._latest_batch_start(self.replay().events, batch_id)
            with self.controller_lock():
                self._record_batch_attempt_failure_locked(
                    "batch_audit_failed",
                    batch_id,
                    evidence=f"audit failed: {exc}",
                    start=start,
                    extra={"retryable": False},
                )
            raise

    def run_item(self, item_id: str) -> dict[str, Any]:
        replayed = self.replay()
        self._require_lifecycle_locked(replayed.events, {"executing", "validating", "verifying"}, "run-item")
        record = self._execution_manifest_record(replayed.events)
        item = self._manifest_item(record["manifest"], item_id)
        self._reject_item_command_if_batch_member(replayed.events, item_id, "run-item")
        statuses = self._item_statuses(replayed.events, record["manifest"])
        uses_validator = self._manifest_uses_validator(record["manifest"])
        if statuses[item_id] == "prepared":
            checkpoint = self.checkpoint_item(item_id, evidence="prepared checkpoint")
            if checkpoint.get("phase") == "awaiting_execution_summary":
                return checkpoint
            try:
                self.final_audit()
            except ContractError:
                if lifecycle_status(self.replay().events) == "awaiting_retry_decision":
                    raise
            return checkpoint
        if statuses[item_id] in {"completed", "validating"} and uses_validator:
            validated = self.run_validator(item_id)
            if validated.get("status") == "pass":
                statuses = self._item_statuses(self.replay().events, record["manifest"])
            elif validated.get("auto_validator_retry"):
                return self.run_item(item_id)
            else:
                return validated
        worker_config = self._worker_config(record["manifest"], item)
        if worker_config.get("mode") == "host-multi-agent":
            raise ContractError(
                "host-multi-agent workers require assign-item, authorize-spawn, register-agent, "
                "complete-item or fail-item, and advance-item; run-item is CLI adapter fallback only"
            )
        verification_config = self._verification_config(record["manifest"], item)
        self._ensure_adapter_launch_files(worker_config, write=False)

        if statuses[item_id] != "validated":
            started = self.begin_item(item_id)
            self._ensure_adapter_launch_files(worker_config)
            run_worktree = Path(started["run_worktree"])
            worker_nonce = uuid.uuid4().hex
            state_path = self.run_dir / "worker-states" / f"{item_id}-{started['attempt']}.json"
            worker_state: dict[str, Any] = {
                "run_id": self.run_id,
                "worker_nonce": worker_nonce,
                "plan_context": started["plan_context"],
            }
            feedback = self._latest_validator_feedback(self.replay().events, item_id)
            if feedback is not None:
                worker_state["validator_feedback"] = feedback
            retry_feedback = self._latest_retry_feedback(self.replay().events, item_id)
            if retry_feedback is not None:
                worker_state["retry_feedback"] = retry_feedback
            write_json_atomic(state_path, worker_state)
            env = os.environ.copy()
            env.update(worker_config["env"])
            env.update(
                {
                    "OPTIM_PLANS_RUN_ID": self.run_id,
                    "OPTIM_PLANS_WORKER_NONCE": worker_nonce,
                    "OPTIM_PLANS_STATE_PATH": str(state_path),
                    "OPTIM_PLANS_IDS": item_id,
                    "OPTIM_PLANS_SCOPES": os.pathsep.join(started["allowed_paths"]),
                    "OPTIM_PLANS_PLAN_CONTEXT": json_text(worker_state["plan_context"]),
                }
            )
            if feedback is not None:
                env["OPTIM_PLANS_VALIDATOR_FEEDBACK"] = feedback["feedback_for_executor"]
                env["OPTIM_PLANS_VALIDATOR_EVIDENCE"] = feedback["evidence"]
            if retry_feedback is not None:
                env["OPTIM_PLANS_RETRY_FEEDBACK"] = json_text(retry_feedback)
            worker = run_process_group(
                worker_config["argv"],
                cwd=run_worktree,
                env=env,
                timeout_seconds=worker_config["timeout_seconds"],
            )
            if not worker.ok():
                evidence = worker.evidence("worker", timeout_seconds=worker_config["timeout_seconds"])
                failed = self.record_worker_failure(item_id, evidence=evidence)
                if failed.get("auto_retry"):
                    return self.run_item(item_id)
                raise ContractError(evidence)
            try:
                worker_evidence = self._worker_result_evidence(item_id, stdout=worker.stdout, worker_nonce=worker_nonce)
            except ContractError as exc:
                failed = self.record_worker_failure(item_id, evidence=f"worker result rejected: {exc}")
                if failed.get("auto_retry"):
                    return self.run_item(item_id)
                raise
            self.record_worker_completion(item_id, evidence=worker_evidence)
            if uses_validator:
                validated = self.run_validator(item_id)
                if validated.get("status") == "pass":
                    pass
                elif validated.get("auto_validator_retry"):
                    return self.run_item(item_id)
                else:
                    return validated
        start = self._latest_item_start(self.replay().events, item_id)
        run_worktree = Path(start["run_worktree"])

        self._assert_protected_metadata_before_verification(item_id)
        if uses_validator:
            self._assert_delta_fingerprint_before_verification(item_id)
        verifier_env = os.environ.copy()
        verifier_env.update(verification_config["env"])
        verifier = run_process_group(
            verification_config["argv"],
            cwd=run_worktree,
            env=verifier_env,
            timeout_seconds=verification_config["timeout_seconds"],
        )
        verifier_evidence = verifier.evidence("verification", timeout_seconds=verification_config["timeout_seconds"])
        if not verifier.ok():
            failed = self.record_attempt_failure("verification_failed", item_id, evidence=verifier_evidence)
            if failed.get("auto_retry"):
                return self.run_item(item_id)
            raise ContractError(verifier_evidence)
        if uses_validator:
            self._assert_delta_fingerprint_after_verification(item_id)
        checkpoint = self.checkpoint_item(item_id, evidence=verifier_evidence)
        if checkpoint.get("phase") == "awaiting_execution_summary":
            return checkpoint
        try:
            self.final_audit()
        except ContractError:
            if lifecycle_status(self.replay().events) == "awaiting_retry_decision":
                raise
        return checkpoint

    def _item_statuses(self, events: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, str]:
        statuses = {item["id"]: "pending" for item in manifest["items"]}
        for event in events:
            payload = event.get("payload", {})
            batch_status: str | None = None
            if event["type"] == "batch_started":
                batch_status = "in_progress"
            elif event["type"] == "batch_completed":
                batch_status = "completed"
            elif event["type"] in {"batch_validator_assigned", "batch_validator_spawn_authorized", "batch_validator_agent_registered"}:
                batch_status = "validating"
            elif event["type"] == "batch_validator_result_recorded":
                batch_status = "validated" if payload.get("status") == "pass" else "failed"
            elif event["type"] in {
                "batch_worker_failed",
                "batch_validator_protocol_rejected",
                "batch_validator_failed",
                "batch_context_integrity_recovery",
                "batch_verification_failed",
                "batch_audit_failed",
            }:
                batch_status = "failed"
            elif event["type"] == "batch_execution_blocked":
                batch_status = "blocked"
            elif event["type"] == "batch_retry_restored":
                batch_status = "pending"
            elif event["type"] == "batch_checkpoint_prepared":
                batch_status = "prepared"
            elif event["type"] == "batch_checkpoint_created":
                batch_status = "verified"
            if batch_status is not None:
                for current_id in self._event_item_ids(payload):
                    if current_id in statuses:
                        statuses[current_id] = batch_status
                continue
            item_id = payload.get("item_id")
            if item_id not in statuses:
                continue
            if event["type"] == "item_started":
                statuses[item_id] = "in_progress"
            elif event["type"] == "worker_completed":
                statuses[item_id] = "completed"
            elif event["type"] in {"validator_assigned", "validator_spawn_authorized", "validator_agent_registered"}:
                statuses[item_id] = "validating"
            elif event["type"] == "validator_result_recorded":
                statuses[item_id] = "validated" if payload.get("status") == "pass" else "failed"
            elif event["type"] in {
                "worker_failed",
                "validator_protocol_rejected",
                "validator_failed",
                "context_integrity_recovery",
                "verification_failed",
                "audit_failed",
            }:
                statuses[item_id] = "failed"
            elif event["type"] == "execution_blocked":
                statuses[item_id] = "blocked"
            elif event["type"] == "retry_restored":
                statuses[item_id] = "pending"
            elif event["type"] == "checkpoint_prepared":
                statuses[item_id] = "prepared"
            elif event["type"] == "checkpoint_created":
                statuses[item_id] = "verified"
        return statuses

    def _latest_item_start(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "item_started":
                return payload
            if event["type"] in {"checkpoint_created", "retry_restored"}:
                break
        raise ContractError(f"{item_id} has not been started")

    def _latest_failure_event(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] in {
                "worker_failed",
                "validator_result_recorded",
                "validator_protocol_rejected",
                "validator_failed",
                "verification_failed",
                "audit_failed",
            }:
                if event["type"] != "validator_result_recorded" or payload.get("status") == "fail":
                    return event
            if event["type"] in {"checkpoint_created", "retry_restored", "item_started"}:
                break
        raise ContractError(f"{item_id} has no failed attempt to retry")

    def _latest_failure(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        return self._latest_failure_event(events, item_id)["payload"]

    def start_execution(self, approval_nonce: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_approval"}, "start-execution")
            record = self._execution_manifest_record(replayed.events)
            if any(event["type"] == "execution_started" for event in replayed.events):
                raise ContractError("execution approval nonce is stale or already used")

            question: dict[str, Any] | None = None
            answer: dict[str, Any] | None = None
            for event in replayed.events:
                payload = event.get("payload", {})
                if (
                    event["type"] == "pending_question"
                    and payload.get("stage") == "execution_launch"
                    and payload.get("nonce") == approval_nonce
                ):
                    question = payload
                if event["type"] == "answer_recorded" and payload.get("nonce") == approval_nonce:
                    answer = payload
            if question is None:
                raise ContractError("unknown execution approval nonce")
            if question.get("manifest") != record["manifest"] or question.get("manifest_hash") != record["manifest_hash"]:
                raise ContractError("execution approval question is not bound to the manifest")
            if answer is None or answer.get("choice") != "approve":
                raise ContractError("execution approval has not been granted")

            source_base = self._manifest_source_base(record["manifest"])
            try:
                current_base = git(self.repo, "rev-parse", "--verify", "HEAD")
            except subprocess.CalledProcessError as exc:
                raise ContractError("source base commit is required before execution") from exc
            if current_base != source_base:
                raise ContractError("source base changed since execution approval")
            require_clean_source(self.repo, ignored_paths=[self.artifact_dir])
            isolation = self._ensure_run_worktree(record["manifest"], source_base=source_base)
            require_clean_source(self.repo, ignored_paths=[self.artifact_dir])
            payload = {
                "approval_nonce": approval_nonce,
                "manifest_hash": record["manifest_hash"],
                "source_base": current_base,
                "source_clean": True,
                **isolation,
                "protected_metadata": _protected_metadata_snapshot(
                    self.repo,
                    run_branch=isolation["run_branch"],
                    run_worktree=Path(isolation["run_worktree"]),
                ),
            }
            return self._append_event_locked("execution_started", payload)["payload"]

    def begin_item(self, item_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"executing"}, "run-item")
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            item = self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "run-item")
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "pending":
                raise ContractError(f"{item_id} is not ready for execution")
            blocked = [
                current_id
                for current_id, status in statuses.items()
                if status in {"in_progress", "completed", "validating", "validated", "prepared", "failed", "blocked"}
            ]
            if blocked:
                raise ContractError(f"another item attempt must be resolved first: {blocked[0]}")
            next_item = next(
                (current["id"] for current in record["manifest"]["items"] if statuses[current["id"]] == "pending"),
                None,
            )
            if next_item != item_id:
                raise ContractError(f"{item_id} is not next in the approved serial order")
            for dependency in item.get("depends_on", []):
                if statuses.get(dependency) != "verified":
                    raise ContractError(f"{item_id} dependency {dependency} is not verified")
            run_worktree = self._require_run_worktree(
                started,
                expected_head=self._latest_checkpoint(replayed.events, started),
                clean=True,
            )
            payload = {
                "item_id": item_id,
                "attempt": sum(
                    1
                    for event in replayed.events
                    if event["type"] == "item_started" and event.get("payload", {}).get("item_id") == item_id
                )
                + 1,
                "base_commit": git(run_worktree, "rev-parse", "--verify", "HEAD"),
                "run_worktree": str(run_worktree),
                "run_branch": started["run_branch"],
                "allowed_paths": self._item_allowed_paths(item),
                "plan_context": self._plan_context(),
            }
            return self._append_event_locked("item_started", payload)["payload"]

    def record_worker_completion(self, item_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker completion evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "complete-item")
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "in_progress":
                raise ContractError(f"{item_id} is not in progress")
            payload = {"item_id": item_id, "evidence": evidence}
            return self._append_event_locked("worker_completed", payload)["payload"]

    def record_worker_failure(self, item_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "fail-item")
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "in_progress":
                raise ContractError(f"{item_id} is not in progress")
            start = self._latest_item_start(replayed.events, item_id)
            return self._record_attempt_failure_locked("worker_failed", item_id, evidence=evidence, start=start)

    def _latest_checkpoint_created(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "checkpoint_created":
                return payload
            if event["type"] == "retry_restored":
                return None
        return None

    def _latest_checkpoint_prepared(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] == "checkpoint_prepared":
                return payload
            if event["type"] in {"checkpoint_created", "retry_restored"}:
                return None
        return None

    def _prepare_checkpoint_locked(
        self,
        events: list[dict[str, Any]],
        item_id: str,
        *,
        evidence: str,
    ) -> dict[str, Any]:
        existing = self._latest_checkpoint_prepared(events, item_id)
        if existing is not None:
            return existing
        record = self._execution_manifest_record(events)
        started = self._execution_started_record(events)
        item = self._manifest_item(record["manifest"], item_id)
        statuses = self._item_statuses(events, record["manifest"])
        if self._manifest_uses_validator(record["manifest"]) and statuses[item_id] != "validated":
            raise ContractError(f"{item_id} is not validated and ready for checkpoint")
        if statuses[item_id] not in {"completed", "validated"}:
            raise ContractError(f"{item_id} is not completed and ready for checkpoint")
        start = self._latest_item_start(events, item_id)
        try:
            self._require_protected_metadata_clean(started)
            run_worktree = self._require_run_worktree(
                started,
                expected_head=start["base_commit"],
                clean=False,
            )
            allowed_paths = self._item_allowed_paths(item)
            audit = audit_git_delta(
                run_worktree,
                allowed_paths=allowed_paths,
                base_commit=start["base_commit"],
                head_commit=start["base_commit"],
            )
            fingerprint = checkpoint_delta_fingerprint(run_worktree, audit["changed_files"])
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_attempt_failure_locked("audit_failed", item_id, evidence=f"audit failed: {exc}", start=start)
            raise
        verified = {
            "item_id": item_id,
            "evidence": bounded_evidence(evidence),
            "worker_evidence": next(
                event.get("payload", {}).get("evidence")
                for event in reversed(events)
                if event["type"] == "worker_completed" and event.get("payload", {}).get("item_id") == item_id
            ),
            "changed_files": audit["changed_files"],
        }
        self._append_event_locked("item_verified", verified)
        payload = {
            **verified,
            "base_commit": start["base_commit"],
            "run_worktree": str(run_worktree),
            "run_branch": started["run_branch"],
            "allowed_paths": allowed_paths,
            "attempt": start["attempt"],
            "head_commit": git(run_worktree, "rev-parse", "--verify", "HEAD"),
            "delta_fingerprint": fingerprint,
        }
        return self._append_event_locked("checkpoint_prepared", payload)["payload"]

    def _commit_prepared_checkpoint_locked(
        self,
        events: list[dict[str, Any]],
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        item_id = prepared["item_id"]
        record = self._execution_manifest_record(events)
        started = self._execution_started_record(events)
        item = self._manifest_item(record["manifest"], item_id)
        start = self._latest_item_start(events, item_id)
        try:
            self._require_protected_metadata_clean(started)
            run_worktree = self._require_run_worktree(
                started,
                expected_head=prepared["head_commit"],
                clean=False,
            )
            if Path(prepared["run_worktree"]).resolve() != run_worktree.resolve():
                raise ContractError("prepared checkpoint is bound to a different run worktree")
            if prepared["base_commit"] != start["base_commit"]:
                raise ContractError("prepared checkpoint base no longer matches the active attempt")
            allowed_paths = list(prepared["allowed_paths"])
            audit = audit_git_delta(
                run_worktree,
                allowed_paths=allowed_paths,
                base_commit=prepared["base_commit"],
                head_commit=prepared["head_commit"],
            )
            if audit["changed_files"] != prepared["changed_files"]:
                raise ContractError("run worktree changed since checkpoint preparation")
            if checkpoint_delta_fingerprint(run_worktree, audit["changed_files"]) != prepared["delta_fingerprint"]:
                raise ContractError("run worktree changed since checkpoint preparation")
            for path in audit["changed_files"]:
                git(run_worktree, "add", "-A", "--", path)
            subject = _checkpoint_commit_subject(item, audit["changed_files"])
            body = f"optim-plans run: {self.run_id}\nitem: {item_id}\nattempt: {prepared['attempt']}"
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--allow-empty",
                    "-m",
                    subject,
                    "-m",
                    body,
                ],
                cwd=run_worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            commit = git(run_worktree, "rev-parse", "--verify", "HEAD")
            clean_audit = audit_git_delta(run_worktree, allowed_paths=allowed_paths)
            if clean_audit["changed_files"]:
                raise ContractError("run worktree is not clean after checkpoint")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_attempt_failure_locked(
                "audit_failed",
                item_id,
                evidence=f"checkpoint failed: {exc}",
                start=start,
                extra={"retryable": False},
            )
            raise
        payload = {"item_id": item_id, "commit": commit, "changed_files": audit["changed_files"]}
        return self._append_event_locked("checkpoint_created", payload)["payload"]

    def checkpoint_item(self, item_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("verification evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            self._reject_item_command_if_batch_member(replayed.events, item_id, "checkpoint-item")
            existing = self._latest_checkpoint_created(replayed.events, item_id)
            if existing is not None:
                return existing
            prepared = self._prepare_checkpoint_locked(replayed.events, item_id, evidence=evidence)
            replayed = self.replay()
            question = self._execution_summary_question_payload_locked(replayed.events)
            if question is not None:
                return {"item_id": item_id, "phase": "awaiting_execution_summary", "question": question}
            return self._commit_prepared_checkpoint_locked(self.replay().events, prepared)

    def _prepare_batch_checkpoint_locked(
        self,
        events: list[dict[str, Any]],
        batch_id: str,
        *,
        evidence: str,
    ) -> dict[str, Any]:
        existing = self._latest_batch_checkpoint_prepared(events, batch_id)
        if existing is not None:
            return existing
        record = self._execution_manifest_record(events)
        started = self._execution_started_record(events)
        start = self._latest_batch_start(events, batch_id)
        statuses = self._item_statuses(events, record["manifest"])
        item_ids = list(start["item_ids"])
        if self._manifest_uses_validator(record["manifest"]) and any(statuses[item_id] != "validated" for item_id in item_ids):
            raise ContractError(f"{batch_id} is not validated and ready for checkpoint")
        if any(statuses[item_id] not in {"completed", "validated"} for item_id in item_ids):
            raise ContractError(f"{batch_id} is not completed and ready for checkpoint")
        try:
            self._require_protected_metadata_clean(started)
            run_worktree = self._require_run_worktree(
                started,
                expected_head=start["base_commit"],
                clean=False,
            )
            allowed_paths = self._batch_allowed_paths(self._batch_items(record["manifest"], item_ids))
            audit = audit_git_delta(
                run_worktree,
                allowed_paths=allowed_paths,
                base_commit=start["base_commit"],
                head_commit=start["base_commit"],
            )
            fingerprint = checkpoint_delta_fingerprint(run_worktree, audit["changed_files"])
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_batch_attempt_failure_locked("batch_audit_failed", batch_id, evidence=f"audit failed: {exc}", start=start)
            raise
        payload = {
            "batch_id": batch_id,
            "item_ids": item_ids,
            "evidence": bounded_evidence(evidence),
            "worker_evidence": next(
                (
                    event.get("payload", {}).get("evidence")
                    for event in reversed(events)
                    if event["type"] == "batch_completed" and event.get("payload", {}).get("batch_id") == batch_id
                ),
                None,
            ),
            "changed_files": audit["changed_files"],
            "base_commit": start["base_commit"],
            "run_worktree": str(run_worktree),
            "run_branch": started["run_branch"],
            "allowed_paths": allowed_paths,
            "attempt": start["attempt"],
            "assignment_nonce": start["assignment_nonce"],
            "head_commit": git(run_worktree, "rev-parse", "--verify", "HEAD"),
            "delta_fingerprint": fingerprint,
        }
        return self._append_event_locked("batch_checkpoint_prepared", payload)["payload"]

    def _commit_prepared_batch_checkpoint_locked(
        self,
        events: list[dict[str, Any]],
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        batch_id = prepared["batch_id"]
        started = self._execution_started_record(events)
        start = self._latest_batch_start(events, batch_id)
        try:
            self._require_protected_metadata_clean(started)
            run_worktree = self._require_run_worktree(
                started,
                expected_head=prepared["head_commit"],
                clean=False,
            )
            if Path(prepared["run_worktree"]).resolve() != run_worktree.resolve():
                raise ContractError("prepared checkpoint is bound to a different run worktree")
            if prepared["base_commit"] != start["base_commit"]:
                raise ContractError("prepared checkpoint base no longer matches the active batch attempt")
            allowed_paths = list(prepared["allowed_paths"])
            audit = audit_git_delta(
                run_worktree,
                allowed_paths=allowed_paths,
                base_commit=prepared["base_commit"],
                head_commit=prepared["head_commit"],
            )
            if audit["changed_files"] != prepared["changed_files"]:
                raise ContractError("run worktree changed since checkpoint preparation")
            if checkpoint_delta_fingerprint(run_worktree, audit["changed_files"]) != prepared["delta_fingerprint"]:
                raise ContractError("run worktree changed since checkpoint preparation")
            for path in audit["changed_files"]:
                git(run_worktree, "add", "-A", "--", path)
            subject = f"Update batch {', '.join(prepared['item_ids'])}"
            body = (
                f"optim-plans run: {self.run_id}\n"
                f"batch: {batch_id}\n"
                f"items: {', '.join(prepared['item_ids'])}\n"
                f"attempt: {prepared['attempt']}"
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--allow-empty",
                    "-m",
                    subject,
                    "-m",
                    body,
                ],
                cwd=run_worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            commit = git(run_worktree, "rev-parse", "--verify", "HEAD")
            clean_audit = audit_git_delta(run_worktree, allowed_paths=allowed_paths)
            if clean_audit["changed_files"]:
                raise ContractError("run worktree is not clean after checkpoint")
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self._record_batch_attempt_failure_locked(
                "batch_audit_failed",
                batch_id,
                evidence=f"checkpoint failed: {exc}",
                start=start,
                extra={"retryable": False},
            )
            raise
        payload = {
            "batch_id": batch_id,
            "item_ids": list(prepared["item_ids"]),
            "commit": commit,
            "changed_files": audit["changed_files"],
        }
        return self._append_event_locked("batch_checkpoint_created", payload)["payload"]

    def checkpoint_batch(self, batch_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("verification evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            existing = self._latest_batch_checkpoint_created(replayed.events, batch_id)
            if existing is not None:
                return existing
            prepared = self._prepare_batch_checkpoint_locked(replayed.events, batch_id, evidence=evidence)
            replayed = self.replay()
            question = self._execution_summary_question_payload_locked(replayed.events)
            if question is not None:
                return {"batch_id": batch_id, "item_ids": list(prepared["item_ids"]), "phase": "awaiting_execution_summary", "question": question}
            return self._commit_prepared_batch_checkpoint_locked(self.replay().events, prepared)

    def request_batch_retry(self, batch_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "batch retry approval")
            failure = self._latest_batch_failure(replayed.events, batch_id)
            return self._batch_retry_question_payload_locked(
                replayed.events,
                batch_id=batch_id,
                item_ids=list(failure["item_ids"]),
                failed_base_commit=failure["base_commit"],
            )

    def restore_batch_retry(self, batch_id: str, approval_nonce: str | None) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "retry-batch")
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            failure_event = self._latest_batch_failure_event(replayed.events, batch_id)
            failure = failure_event["payload"]
            item_ids = list(failure["item_ids"])
            question: dict[str, Any] | None = None
            answer: dict[str, Any] | None = None
            auto_approved = approval_nonce is None and (
                self._is_retryable_failure_event(failure_event)
                or not any(
                    event["type"] == "batch_retry_restored" and event.get("payload", {}).get("batch_id") == batch_id
                    for event in replayed.events
                )
            )
            if not auto_approved:
                if approval_nonce is None:
                    raise ContractError("batch retry approval is required after the first retry")
                for event in replayed.events:
                    payload = event.get("payload", {})
                    if (
                        event["type"] == "pending_question"
                        and payload.get("stage") == "execution_batch_retry"
                        and payload.get("nonce") == approval_nonce
                        and payload.get("batch_id") == batch_id
                        and payload.get("item_ids") == item_ids
                    ):
                        question = payload
                    if event["type"] == "answer_recorded" and payload.get("nonce") == approval_nonce:
                        answer = payload
                    if event["type"] == "batch_retry_restored" and payload.get("approval_nonce") == approval_nonce:
                        raise ContractError("batch retry approval nonce is stale or already used")
                if question is None:
                    raise ContractError("unknown batch retry approval nonce")
                if question.get("failed_base_commit") != failure["base_commit"]:
                    raise ContractError("batch retry approval is not bound to the failed attempt")
                if answer is None or answer.get("choice") != "approve":
                    raise ContractError("batch retry restore has not been approved")
            run_worktree = self._require_run_worktree(
                started,
                expected_head=failure["base_commit"],
                clean=False,
            )
            if Path(failure["run_worktree"]).resolve() != run_worktree.resolve():
                raise ContractError("failed batch attempt is not bound to the controller-owned worktree")
            git(run_worktree, "reset", "--hard", failure["base_commit"])
            git(run_worktree, "clean", "-fdx")
            payload = {
                "batch_id": batch_id,
                "item_ids": item_ids,
                "approval_nonce": approval_nonce,
                "auto_approved": auto_approved,
                "restored_to": failure["base_commit"],
                "run_worktree": str(run_worktree),
                "failure_event": failure_event["type"],
                "evidence": failure.get("evidence", ""),
            }
            for key in ("validator_nonce", "feedback_for_executor", "checked_items"):
                if key in failure:
                    payload[key] = failure[key]
            return self._append_event_locked("batch_retry_restored", payload)["payload"]

    def retry_batch(self, batch_id: str, approval_nonce: str | None) -> dict[str, Any]:
        self.restore_batch_retry(batch_id, approval_nonce)
        return self.assign_batch()

    def request_retry(self, item_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "retry approval")
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "request-retry")
            failure = self._latest_failure(replayed.events, item_id)
            return self._retry_question_payload_locked(
                replayed.events,
                item_id=item_id,
                failed_base_commit=failure["base_commit"],
            )

    def restore_retry(self, item_id: str, approval_nonce: str | None) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "retry-item")
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            self._reject_item_command_if_batch_member(replayed.events, item_id, "retry-item")
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            failure_event = self._latest_failure_event(replayed.events, item_id)
            failure = failure_event["payload"]
            question: dict[str, Any] | None = None
            answer: dict[str, Any] | None = None
            auto_approved = approval_nonce is None and (
                self._is_retryable_failure_event(failure_event)
                or not any(
                    event["type"] == "retry_restored" and event.get("payload", {}).get("item_id") == item_id
                    for event in replayed.events
                )
            )
            if not auto_approved:
                if approval_nonce is None:
                    raise ContractError("retry approval is required after the first retry")
                for event in replayed.events:
                    payload = event.get("payload", {})
                    if (
                        event["type"] == "pending_question"
                        and payload.get("stage") == "execution_retry"
                        and payload.get("nonce") == approval_nonce
                        and payload.get("item_id") == item_id
                    ):
                        question = payload
                    if event["type"] == "answer_recorded" and payload.get("nonce") == approval_nonce:
                        answer = payload
                    if event["type"] == "retry_restored" and payload.get("approval_nonce") == approval_nonce:
                        raise ContractError("retry approval nonce is stale or already used")
                if question is None:
                    raise ContractError("unknown retry approval nonce")
                if question.get("failed_base_commit") != failure["base_commit"]:
                    raise ContractError("retry approval is not bound to the failed attempt")
                if answer is None or answer.get("choice") != "approve":
                    raise ContractError("retry restore has not been approved")
            run_worktree = self._require_run_worktree(
                started,
                expected_head=failure["base_commit"],
                clean=False,
            )
            if Path(failure["run_worktree"]).resolve() != run_worktree.resolve():
                raise ContractError("failed attempt is not bound to the controller-owned worktree")
            git(run_worktree, "reset", "--hard", failure["base_commit"])
            git(run_worktree, "clean", "-fdx")
            payload = {
                "item_id": item_id,
                "approval_nonce": approval_nonce,
                "auto_approved": auto_approved,
                "restored_to": failure["base_commit"],
                "run_worktree": str(run_worktree),
                "failure_event": failure_event["type"],
                "evidence": failure.get("evidence", ""),
            }
            for key in ("validator_nonce", "feedback_for_executor", "checked_items"):
                if key in failure:
                    payload[key] = failure[key]
            return self._append_event_locked("retry_restored", payload)["payload"]

    def retry_item(self, item_id: str, approval_nonce: str | None) -> dict[str, Any]:
        self.restore_retry(item_id, approval_nonce)
        record = self._execution_manifest_record(self.replay().events)
        item = self._manifest_item(record["manifest"], item_id)
        if self._worker_config(record["manifest"], item).get("mode") == "host-multi-agent":
            return self.assign_item(item_id)
        return self.run_item(item_id)

    def final_audit(self) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            if lifecycle_status(replayed.events) == "awaiting_integration":
                for event in reversed(replayed.events):
                    if event["type"] == "final_audit_passed":
                        return event["payload"]
            self._require_lifecycle_locked(replayed.events, {"executing"}, "final audit")
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            missing = [item_id for item_id, status in statuses.items() if status != "verified"]
            if missing:
                raise ContractError(f"execution items are not verified: {', '.join(missing)}")
            allowed: list[str] = []
            for item in record["manifest"]["items"]:
                allowed.extend(self._item_allowed_paths(item))
            head = self._latest_checkpoint(replayed.events, started)
            try:
                self._require_protected_metadata_clean(started)
                run_worktree = self._require_run_worktree(started, expected_head=head, clean=True)
                audit = audit_git_delta(
                    run_worktree,
                    allowed_paths=allowed,
                    base_commit=started["source_base"],
                    head_commit=head,
                )
            except (ContractError, subprocess.CalledProcessError, OSError) as exc:
                payload = {
                    "stage": "final_audit",
                    "failure_event": "audit_failed",
                    "final_commit": head,
                    "evidence": bounded_evidence(f"final audit failed: {exc}"),
                    "retryable": False,
                }
                self._append_event_locked("audit_failed", payload)
                self._append_event_locked(
                    "awaiting_retry_decision",
                    {
                        "stage": "final_audit",
                        "failure_event": "audit_failed",
                        "final_commit": head,
                    },
                )
                raise
            active = self._matching_active_locked()
            payload = {"status": "passed", "final_commit": head, "changed_files": audit["changed_files"]}
            passed = self._append_event_locked("final_audit_passed", payload)["payload"]
            integration = self._auto_integrate_final_checkpoint_locked(record["manifest"], started, head, passed, active)
            return {**passed, "auto_integration": integration}

    def _manifest_destination_ref(self, manifest: dict[str, Any]) -> str:
        destination = None
        for key in ("integration_destination", "integration_destination_ref", "local_integration_destination_ref"):
            if key in manifest:
                destination = manifest[key]
                break
        if not isinstance(destination, str) or not destination.strip() or destination.startswith("-"):
            raise ContractError("execution manifest integration_destination is required")
        return destination

    def _destination_branch_ref(self, destination: str) -> str | None:
        if destination.startswith("refs/heads/"):
            branch = destination.removeprefix("refs/heads/")
        elif destination.startswith("refs/"):
            return None
        else:
            branch = destination
        if not branch or branch.startswith("-"):
            return None
        return f"refs/heads/{branch}"

    def _record_awaiting_integration_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "evidence" in payload:
            payload = {**payload, "evidence": bounded_evidence(str(payload["evidence"]))}
        return self._append_event_locked("awaiting_integration", payload)["payload"]

    def _auto_integration_awaiting_locked(
        self,
        *,
        final_checkpoint: str,
        destination_ref: str,
        destination_oid: str | None,
        stage: str,
        evidence: str,
    ) -> dict[str, Any]:
        return self._record_awaiting_integration_locked(
            {
                "stage": stage,
                "final_checkpoint": final_checkpoint,
                "destination_ref": destination_ref,
                "destination_oid": destination_oid,
                "evidence": evidence,
            }
        )

    def _auto_integrate_final_checkpoint_locked(
        self,
        manifest: dict[str, Any],
        started: dict[str, Any],
        final_checkpoint: str,
        final_audit: dict[str, Any],
        active: dict[str, Any],
    ) -> dict[str, Any]:
        destination = self._manifest_destination_ref(manifest)
        before_oid = git_maybe(self.repo, "rev-parse", "--verify", destination)
        branch_ref = self._destination_branch_ref(destination)
        if branch_ref is None:
            return self._auto_integration_awaiting_locked(
                final_checkpoint=final_checkpoint,
                destination_ref=destination,
                destination_oid=before_oid,
                stage="auto_integration_precondition",
                evidence="manifest integration destination is not a local branch",
            )
        checked_out_ref = git_maybe(self.repo, "symbolic-ref", "-q", "HEAD")
        if checked_out_ref != branch_ref:
            return self._auto_integration_awaiting_locked(
                final_checkpoint=final_checkpoint,
                destination_ref=destination,
                destination_oid=before_oid,
                stage="auto_integration_precondition",
                evidence=f"checked-out destination is {checked_out_ref or 'DETACHED'}, expected {branch_ref}",
            )
        try:
            require_clean_source(self.repo, ignored_paths=[self.artifact_dir])
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            return self._auto_integration_awaiting_locked(
                final_checkpoint=final_checkpoint,
                destination_ref=destination,
                destination_oid=before_oid,
                stage="auto_integration_precondition",
                evidence=f"source worktree is not clean: {exc}",
            )
        if before_oid is None:
            return self._auto_integration_awaiting_locked(
                final_checkpoint=final_checkpoint,
                destination_ref=destination,
                destination_oid=None,
                stage="auto_integration_precondition",
                evidence="manifest integration destination does not exist",
            )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", before_oid, final_checkpoint],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ancestor.returncode != 0:
            return self._auto_integration_awaiting_locked(
                final_checkpoint=final_checkpoint,
                destination_ref=destination,
                destination_oid=before_oid,
                stage="auto_integration_fast_forward",
                evidence="manifest integration destination is not fast-forwardable to the final checkpoint",
            )

        merge = subprocess.run(
            ["git", "merge", "--ff-only", final_checkpoint],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        after_oid = git_maybe(self.repo, "rev-parse", "--verify", destination)
        if merge.returncode != 0:
            evidence = ProcessResult(
                ["git", "merge", "--ff-only", final_checkpoint],
                merge.returncode,
                merge.stdout,
                merge.stderr,
            ).evidence("auto integration fast-forward", timeout_seconds=None)
            if after_oid == before_oid:
                return self._auto_integration_awaiting_locked(
                    final_checkpoint=final_checkpoint,
                    destination_ref=destination,
                    destination_oid=before_oid,
                    stage="auto_integration_fast_forward",
                    evidence=evidence,
                )
            self._append_event_locked(
                "integration_verification_failed",
                {
                    "stage": "auto_integration_fast_forward",
                    "final_checkpoint": final_checkpoint,
                    "destination_ref": destination,
                    "before_destination_oid": before_oid,
                    "after_destination_oid": after_oid,
                    "evidence": bounded_evidence(evidence),
                },
            )
            return {"stage": "auto_integration_fast_forward", "status": "failed_after_mutation"}

        if after_oid != final_checkpoint:
            evidence = f"fast-forward ended at {after_oid}, expected {final_checkpoint}"
            self._append_event_locked(
                "integration_verification_failed",
                {
                    "stage": "auto_integration_fast_forward",
                    "final_checkpoint": final_checkpoint,
                    "destination_ref": destination,
                    "before_destination_oid": before_oid,
                    "after_destination_oid": after_oid,
                    "evidence": bounded_evidence(evidence),
                },
            )
            return {"stage": "auto_integration_fast_forward", "status": "failed_after_mutation"}

        proof = self._validate_integrated_proof(manifest, started, final_checkpoint, destination)
        proof.update({"before_destination_oid": before_oid, "after_destination_oid": after_oid})
        try:
            integration_verification = self._run_full_integration_verification(
                final_checkpoint=final_checkpoint,
                expected_head=proof["object_id"],
                failure_context={
                    "destination_ref": destination,
                    "before_destination_oid": before_oid,
                    "after_destination_oid": after_oid,
                },
            )
        except ContractError:
            return {"stage": "integration_verification", "status": "failed_after_fast_forward", **proof}
        finished = self._append_event_locked(
            "run_finished",
            {
                "outcome": "integrated",
                "approval_nonce": started["approval_nonce"],
                "final_checkpoint": final_checkpoint,
                "run_branch": started["run_branch"],
                "run_worktree": started["run_worktree"],
                "auto_integrated": True,
                "final_audit": final_audit,
                **proof,
                "integration_verification": integration_verification,
            },
        )["payload"]
        self._release_active_locked(active, finished)
        return {"stage": "run_finished", "status": "integrated", **proof}

    def _final_checkpoint(self, events: list[dict[str, Any]], started: dict[str, Any]) -> str:
        for event in reversed(events):
            payload = event.get("payload", {})
            if event["type"] == "awaiting_integration" and isinstance(payload.get("final_checkpoint"), str):
                return payload["final_checkpoint"]
            if event["type"] == "final_audit_passed" and isinstance(payload.get("final_commit"), str):
                return payload["final_commit"]
        return self._latest_checkpoint(events, started)

    def _latest_failure_event_payload(self, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event["type"] in {
                "worker_failed",
                "batch_worker_failed",
                "validator_result_recorded",
                "batch_validator_result_recorded",
                "validator_protocol_rejected",
                "batch_validator_protocol_rejected",
                "validator_failed",
                "batch_validator_failed",
                "context_integrity_recovery",
                "batch_context_integrity_recovery",
                "verification_failed",
                "batch_verification_failed",
                "audit_failed",
                "batch_audit_failed",
                "execution_blocked",
                "batch_execution_blocked",
            }:
                payload = event.get("payload", {})
                if event["type"] in {"validator_result_recorded", "batch_validator_result_recorded"} and payload.get("status") != "fail":
                    continue
                failure = {
                    "failure_event_type": event["type"],
                    "failure_event_seq": event["seq"],
                    "failure_item_id": payload.get("item_id"),
                    "failure_batch_id": payload.get("batch_id"),
                    "failure_evidence": payload.get("evidence", ""),
                }
                if event["type"] in {"context_integrity_recovery", "batch_context_integrity_recovery"}:
                    projected = _context_integrity_projection(payload)
                    failure.update(
                        {
                            "failure_reason": projected["reason"],
                            "failure_status": projected["status"],
                            "plan_context_source_path": projected["source_path"],
                            "plan_context_source_hash": projected["source_hash"],
                            "plan_context_truncated": projected["truncated"],
                            "plan_context_truncation": projected["truncation"],
                        }
                    )
                return failure
        return None

    def _require_finish_approval(self, events: list[dict[str, Any]], approval_nonce: str, outcome: str) -> None:
        question: dict[str, Any] | None = None
        answer: dict[str, Any] | None = None
        for event in events:
            payload = event.get("payload", {})
            if event["type"] == "pending_question" and payload.get("stage") == "finish_run" and payload.get("nonce") == approval_nonce:
                question = payload
            if event["type"] == "answer_recorded" and payload.get("nonce") == approval_nonce:
                answer = payload
            if event["type"] == "run_finished" and payload.get("approval_nonce") == approval_nonce:
                raise ContractError("finish approval nonce is stale or already used")
        if question is None:
            raise ContractError("unknown finish approval nonce")
        choices = {option["id"] for option in question.get("options", [])}
        if outcome not in choices:
            raise ContractError(f"finish approval cannot record outcome {outcome!r}")
        if answer is None or answer.get("choice") != outcome:
            raise ContractError("finish outcome has not been approved")

    def _validate_integrated_proof(
        self,
        manifest: dict[str, Any],
        started: dict[str, Any],
        final_checkpoint: str,
        target_ref: str | None,
    ) -> dict[str, Any]:
        destination = self._manifest_destination_ref(manifest)
        if target_ref != destination:
            raise ContractError("integrated target ref must match manifest integration destination")
        if target_ref.startswith("-"):
            raise ContractError("integrated target ref is invalid")
        if target_ref == started["run_branch"] or target_ref == f"refs/heads/{started['run_branch']}":
            raise ContractError("integrated target ref cannot be the controller run branch")
        if target_ref.startswith("refs/optim-plans/") or target_ref.startswith("refs/proof/"):
            raise ContractError("integrated target ref cannot be a controller proof ref")
        try:
            object_id = git(self.repo, "rev-parse", "--verify", target_ref)
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", final_checkpoint, object_id],
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ContractError("integrated target ref does not contain the final checkpoint") from exc
        return {"destination_ref": target_ref, "object_id": object_id}

    def _full_integration_verification_argv(self) -> list[str]:
        code = (
            "import py_compile, subprocess, sys\n"
            "from pathlib import Path\n"
            "subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v'], check=True)\n"
            "for path in sorted(Path('scripts').glob('*.py')) + sorted(Path('hooks').glob('*.py')):\n"
            "    py_compile.compile(str(path), doraise=True)\n"
            "subprocess.run([sys.executable, 'scripts/validate_structure.py'], check=True)\n"
        )
        return [sys.executable, "-c", code]

    def _run_full_integration_verification(
        self,
        *,
        final_checkpoint: str,
        expected_head: str,
        failure_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_head = git(self.repo, "rev-parse", "--verify", "HEAD")
        if current_head != expected_head:
            evidence = bounded_evidence(
                "integration verification failed: "
                f"checked-out worktree HEAD {current_head} does not match integrated target {expected_head}"
            )
            self._append_event_locked(
                "integration_verification_failed",
                {
                    "stage": "integration_verification",
                    "final_checkpoint": final_checkpoint,
                    **(failure_context or {}),
                    "evidence": evidence,
                },
            )
            raise ContractError(evidence)
        verifier = run_process_group(
            self._full_integration_verification_argv(),
            cwd=self.repo,
            env=os.environ.copy(),
            timeout_seconds=FULL_INTEGRATION_VERIFICATION_TIMEOUT_SECONDS,
        )
        evidence = verifier.evidence(
            "integration verification",
            timeout_seconds=FULL_INTEGRATION_VERIFICATION_TIMEOUT_SECONDS,
        )
        payload = {
            "stage": "integration_verification",
            "final_checkpoint": final_checkpoint,
            **(failure_context or {}),
            "evidence": evidence,
        }
        if not verifier.ok():
            self._append_event_locked("integration_verification_failed", payload)
            raise ContractError(evidence)
        return payload

    def _validate_pr_proof(
        self,
        *,
        final_checkpoint: str,
        pr_url: str | None,
        remote: str | None,
        remote_ref: str | None,
    ) -> dict[str, Any]:
        if not isinstance(pr_url, str) or not pr_url.strip():
            raise ContractError("pr-opened outcome requires a PR URL")
        if not isinstance(remote, str) or not remote.strip() or remote.startswith("-"):
            raise ContractError("pr-opened outcome requires a remote")
        if not isinstance(remote_ref, str) or not remote_ref.strip() or remote_ref.startswith("-"):
            raise ContractError("pr-opened outcome requires a remote ref")
        fetch_ref = f"refs/optim-plans/proof/{self.run_id}"
        try:
            git(self.repo, "fetch", "--no-tags", remote, f"+{remote_ref}:{fetch_ref}")
            object_id = git(self.repo, "rev-parse", "--verify", fetch_ref)
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", final_checkpoint, object_id],
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ContractError("PR remote ref does not contain the final checkpoint") from exc
        return {"pr_url": pr_url, "remote": remote, "remote_ref": remote_ref, "proof_ref": fetch_ref, "object_id": object_id}

    def _discard_owned_worktree(self, started: dict[str, Any], *, expected_head: str) -> None:
        run_worktree = self._require_run_worktree(started, expected_head=expected_head, clean=False)
        git(self.repo, "worktree", "remove", "--force", str(run_worktree))
        git(self.repo, "branch", "-D", started["run_branch"])

    def _matching_active_locked(self) -> dict[str, Any]:
        if not self.active_file.exists():
            raise ContractError("active pointer was already released")
        active = parse_json_strict(self.active_file.read_text(encoding="utf-8"), source=str(self.active_file))
        if active.get("run_id") != self.run_id:
            raise ContractError("active pointer no longer names this run")
        return active

    def _summary_plan_items(self, manifest: dict[str, Any]) -> list["PlanItem"]:
        items = []
        for raw in manifest["items"]:
            summary = str(raw.get("summary") or raw.get("description") or raw["id"])
            verification = raw.get("verification")
            if verification is None:
                verification = manifest.get("verification_argv", "controller verification")
            if isinstance(verification, list):
                verification = " ".join(str(part) for part in verification)
            items.append(
                PlanItem(
                    raw["id"],
                    summary,
                    str(verification),
                    str(raw.get("evidence") or raw.get("acceptance") or "controller events"),
                    depends_on=list(raw.get("depends_on", [])),
                    acceptance=str(raw.get("acceptance") or ""),
                    allowed_paths=self._item_allowed_paths(raw),
                )
            )
        return items

    def _execution_summary_results(self, events: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        results = {
            item["id"]: {
                "status": "pending",
                "evidence": "",
                "attempts": 0,
                "changed_files": [],
                "commits": [],
                "retry_decisions": [],
                "limitations": "",
                "explanation": "",
                "context_integrity": {},
            }
            for item in manifest["items"]
        }
        for event in events:
            payload = event.get("payload", {})
            batch_ids = self._event_item_ids(payload) if event["type"].startswith("batch_") else []
            if batch_ids:
                event_type = event["type"]
                for current_id in batch_ids:
                    result = results.get(current_id)
                    if result is None:
                        continue
                    if event_type == "batch_started":
                        result["status"] = "in_progress"
                        result["attempts"] += 1
                    elif event_type == "batch_completed":
                        result["status"] = "worker_completed"
                        result["explanation"] = payload.get("evidence", "")
                    elif event_type == "batch_validator_result_recorded":
                        result["status"] = "validator_passed" if payload.get("status") == "pass" else "validator_failed"
                        result["evidence"] = payload.get("evidence", "")
                        if payload.get("feedback_for_executor"):
                            result["limitations"] = payload["feedback_for_executor"]
                    elif event_type in {
                        "batch_worker_failed",
                        "batch_validator_protocol_rejected",
                        "batch_validator_failed",
                        "batch_context_integrity_recovery",
                        "batch_verification_failed",
                        "batch_audit_failed",
                    }:
                        result["status"] = "failed"
                        result["limitations"] = payload.get("evidence", "")
                        if event_type == "batch_context_integrity_recovery":
                            result["status"] = "context_integrity_recovery"
                            result["limitations"] = _context_integrity_summary(payload)
                            result["context_integrity"] = _context_integrity_projection(payload)
                    elif event_type == "batch_execution_blocked":
                        result["status"] = "blocked"
                        result["limitations"] = payload.get("evidence", "")
                    elif event_type == "batch_retry_restored":
                        result["status"] = "pending"
                        result["retry_decisions"].append(f"restored batch {payload.get('batch_id', 'unknown')} to {payload.get('restored_to', 'unknown')}")
                    elif event_type == "batch_checkpoint_prepared":
                        result["status"] = "prepared"
                        result["changed_files"] = list(payload.get("changed_files", []))
                    elif event_type == "batch_checkpoint_created":
                        result["status"] = "checkpointed"
                        result["changed_files"] = list(payload.get("changed_files", []))
                        result["commits"].append(payload.get("commit", ""))
                continue
            if event["type"] == "awaiting_retry_decision" and isinstance(payload.get("item_ids"), list):
                for current_id in self._event_item_ids(payload):
                    result = results.get(current_id)
                    if result is not None:
                        result["retry_decisions"].append(f"awaiting batch retry after {payload.get('failure_event', 'failure')}")
                continue
            item_id = payload.get("item_id")
            result = results.get(item_id)
            if result is None:
                continue
            event_type = event["type"]
            if event_type == "item_started":
                result["status"] = "in_progress"
                result["attempts"] += 1
            elif event_type == "worker_completed":
                result["status"] = "worker_completed"
                result["explanation"] = payload.get("evidence", "")
            elif event_type == "validator_result_recorded":
                result["status"] = "validator_passed" if payload.get("status") == "pass" else "validator_failed"
                result["evidence"] = payload.get("evidence", "")
                if payload.get("feedback_for_executor"):
                    result["limitations"] = payload["feedback_for_executor"]
            elif event_type in {
                "worker_failed",
                "validator_protocol_rejected",
                "validator_failed",
                "context_integrity_recovery",
                "verification_failed",
                "audit_failed",
            }:
                result["status"] = "failed"
                result["limitations"] = payload.get("evidence", "")
                if event_type == "context_integrity_recovery":
                    result["status"] = "context_integrity_recovery"
                    result["limitations"] = _context_integrity_summary(payload)
                    result["context_integrity"] = _context_integrity_projection(payload)
            elif event_type == "execution_blocked":
                result["status"] = "blocked"
                result["limitations"] = payload.get("evidence", "")
            elif event_type == "awaiting_retry_decision":
                result["retry_decisions"].append(f"awaiting retry after {payload.get('failure_event', 'failure')}")
            elif event_type == "retry_restored":
                result["status"] = "pending"
                result["retry_decisions"].append(f"restored to {payload.get('restored_to', 'unknown')}")
            elif event_type == "item_verified":
                result["status"] = "verified"
                result["evidence"] = payload.get("evidence", "")
                result["changed_files"] = list(payload.get("changed_files", []))
                if payload.get("worker_evidence"):
                    result["explanation"] = payload["worker_evidence"]
            elif event_type == "checkpoint_prepared":
                result["status"] = "prepared"
                result["changed_files"] = list(payload.get("changed_files", []))
            elif event_type == "checkpoint_created":
                result["status"] = "checkpointed"
                result["changed_files"] = list(payload.get("changed_files", []))
                result["commits"].append(payload.get("commit", ""))
        return results

    def _execution_summary_final_audit(self, events: list[dict[str, Any]]) -> str:
        final_audit = "unknown"
        for event in events:
            payload = event.get("payload", {})
            if event["type"] == "final_audit_passed":
                final_audit = f"passed: {payload.get('final_commit', 'unknown')}"
            elif event["type"] == "awaiting_integration":
                final_audit = f"awaiting integration: {payload.get('evidence', payload.get('stage', 'unknown'))}"
            elif event["type"] == "integration_verification_failed":
                final_audit = f"integration verification failed: {payload.get('evidence', payload.get('stage', 'unknown'))}"
            elif event["type"] == "run_finished":
                final_audit = f"run_finished/{payload.get('outcome', 'unknown')}"
                verification = payload.get("integration_verification")
                if isinstance(verification, dict) and verification.get("evidence"):
                    final_audit = f"{final_audit}: {verification['evidence']}"
        return final_audit

    def _maybe_render_execution_summary_locked(self, events: list[dict[str, Any]] | None = None) -> Path | None:
        events = events or self.replay().events
        if self._execution_summary_decision(events) != "generate-summary":
            return None
        record = self._execution_manifest_record(events)
        started = self._execution_started_record(events)
        final_commit = self._latest_checkpoint(events, started)
        worker = record["manifest"].get("worker", {})
        if isinstance(worker, dict):
            agent_config = str(worker.get("mode") or worker.get("adapter") or worker.get("platform") or "unknown")
        else:
            agent_config = "unknown"
        text = render_execution_summary(
            self._summary_plan_items(record["manifest"]),
            self._execution_summary_results(events, record["manifest"]),
            base_commit=started["source_base"],
            final_commit=final_commit,
            agent_config=agent_config,
            final_audit=self._execution_summary_final_audit(events),
            language=self._controller_language(),
        )
        path = self.artifact_dir / EXECUTION_SUMMARY_FILE
        path.write_text(text, encoding="utf-8")
        return path

    def _release_active_locked(self, active: dict[str, Any], payload: dict[str, Any]) -> None:
        archive_dir = self.run_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        os.replace(self.active_file, archive_dir / "active.json")
        write_json_atomic(archive_dir / "terminal.json", payload)

    def finish_run(
        self,
        outcome: str,
        *,
        approval_nonce: str,
        target_ref: str | None = None,
        pr_url: str | None = None,
        remote: str | None = None,
        remote_ref: str | None = None,
        confirm_discard: bool = False,
        evidence: str = "",
    ) -> dict[str, Any]:
        if outcome not in FINISH_OUTCOMES:
            raise ContractError(f"unsupported finish outcome {outcome!r}")
        with self.controller_lock():
            replayed = self.replay()
            status = self._require_lifecycle_locked(
                replayed.events,
                {"context_integrity_recovery", "awaiting_integration", "awaiting_retry_decision", "blocked", "legacy_active"},
                "finish-run",
            )
            if any(event["type"] == "run_finished" for event in replayed.events):
                raise ContractError("run is already terminal")
            self._require_finish_approval(replayed.events, approval_nonce, outcome)
            active = self._matching_active_locked()
            if status == "legacy_active":
                if outcome not in {"kept", "failed", "aborted"}:
                    raise ContractError("legacy active runs can only be kept, failed, or aborted")
                payload = {
                    "outcome": outcome,
                    "approval_nonce": approval_nonce,
                    "legacy_active": True,
                }
                if evidence.strip():
                    payload["evidence"] = bounded_evidence(evidence)
                finished = self._append_event_locked("run_finished", payload)["payload"]
                self._release_active_locked(active, finished)
                return finished
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            final_checkpoint = self._final_checkpoint(replayed.events, started)
            if outcome in {"integrated", "pr-opened"} and status != "awaiting_integration":
                raise ContractError(f"{outcome} requires awaiting_integration")
            payload: dict[str, Any] = {
                "outcome": outcome,
                "approval_nonce": approval_nonce,
                "final_checkpoint": final_checkpoint,
                "run_branch": started["run_branch"],
                "run_worktree": started["run_worktree"],
            }
            if evidence.strip():
                payload["evidence"] = bounded_evidence(evidence)
            if outcome == "integrated":
                proof = self._validate_integrated_proof(record["manifest"], started, final_checkpoint, target_ref)
                payload.update(proof)
                payload["integration_verification"] = self._run_full_integration_verification(
                    final_checkpoint=final_checkpoint,
                    expected_head=proof["object_id"],
                )
            elif outcome == "pr-opened":
                payload.update(
                    self._validate_pr_proof(
                        final_checkpoint=final_checkpoint,
                        pr_url=pr_url,
                        remote=remote,
                        remote_ref=remote_ref,
                    )
                )
            elif outcome == "discarded":
                if not confirm_discard:
                    raise ContractError("discarded outcome requires explicit destructive confirmation")
                self._discard_owned_worktree(started, expected_head=final_checkpoint)
                payload["discarded"] = True
            elif outcome in {"failed", "aborted"}:
                failure = self._latest_failure_event_payload(replayed.events)
                if failure is not None:
                    payload.update(failure)
                elif not evidence.strip():
                    raise ContractError(f"{outcome} outcome requires failure or abort evidence")
                if confirm_discard:
                    self._discard_owned_worktree(started, expected_head=final_checkpoint)
                    payload["discarded"] = True
                else:
                    payload["preserved"] = True
            else:
                payload["preserved"] = True
            finished = self._append_event_locked("run_finished", payload)["payload"]
            self._release_active_locked(active, finished)
            return finished

    def replay(self) -> ReplayState:
        events: list[dict[str, Any]] = []
        if not self.events_file.exists():
            return ReplayState([], "initialized")
        for line_number, line in enumerate(self.events_file.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = parse_json_strict(line, source=f"events.jsonl:{line_number}")
            expected = len(events) + 1
            if event.get("schema") != SCHEMA_VERSION or event.get("seq") != expected:
                raise ContractError(f"event sequence gap or schema mismatch at line {line_number}")
            if not isinstance(event.get("type"), str):
                raise ContractError(f"event type missing at line {line_number}")
            events.append(event)
        return ReplayState(events, lifecycle_status(events), lifecycle_status_details(events))


@dataclass(frozen=True)
class PlanItem:
    id: str
    summary: str
    verification: str
    evidence: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    verifier_criterion_id: str = ""
    verifier_covered_item_ids: list[str] = field(default_factory=list)
    verifier_pass_condition: str = ""
    verifier_metric_threshold: str = ""
    verifier_non_quantification: str = ""


def render_plan(
    goal: str,
    items: list[PlanItem],
    *,
    version: int,
    repo_evidence: list[str] | None = None,
    resolved_decisions: list[str] | None = None,
    language: str | None = None,
) -> str:
    lines = [
        f"# PLAN_v{version}",
        "",
        f"{_text(language, 'Goal', '目标')}: {goal}",
        "",
        _text(
            language,
            "| ID | Depends on | Verification | Evidence | Acceptance | Allowed paths | Summary |",
            "| ID | 依赖 | 验证 | 证据 | 验收 | 允许路径 | 摘要 |",
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for item in items:
        depends = ", ".join(item.depends_on) if item.depends_on else _text(language, "none", "无")
        allowed_paths = ", ".join(item.allowed_paths) if item.allowed_paths else _text(language, "none", "无")
        acceptance = item.acceptance or _text(language, "not recorded", "未记录")
        lines.append(
            f"| {item.id} | {depends} | {item.verification} | {item.evidence} | "
            f"{acceptance} | {allowed_paths} | {item.summary} |"
        )
    lines.extend(["", _text(language, "## Verifier Checklist", "## 验证清单"), ""])
    for item in items:
        criterion_id = item.verifier_criterion_id or f"VC-{item.id}"
        covered_ids = ", ".join(item.verifier_covered_item_ids or [item.id])
        pass_condition = item.verifier_pass_condition or item.acceptance or _text(language, "not recorded", "未记录")
        metric = (
            f"{_text(language, 'Metric threshold', '指标阈值')}: {item.verifier_metric_threshold}"
            if item.verifier_metric_threshold
            else f"{_text(language, 'Non-quantification', '非量化说明')}: {item.verifier_non_quantification or _text(language, 'not recorded', '未记录')}"
        )
        lines.append(
            f"- [ ] {criterion_id} | {_text(language, 'Covered', '覆盖')}: {covered_ids} | "
            f"{_text(language, 'Pass', '通过条件')}: {pass_condition} | "
            f"{_text(language, 'Evidence', '证据')}: {item.evidence} | {metric}"
        )
    lines.extend(["", _text(language, "## Repo evidence", "## 仓库证据"), ""])
    lines.extend(f"- {evidence}" for evidence in (repo_evidence or [_text(language, "Not recorded.", "未记录。")]))
    lines.extend(["", _text(language, "## Resolved decisions", "## 已解决决策"), ""])
    lines.extend(f"- {decision}" for decision in (resolved_decisions or [_text(language, "Not recorded.", "未记录。")]))
    lines.extend(
        [
            "",
            _text(language, "## Revision ledger", "## 修订记录"),
            "",
            _text(language, "- Initial version; no prior findings.", "- 初始版本；没有既有发现。"),
        ]
    )
    return "\n".join(lines) + "\n"


def render_comments(mode: str, version: int, findings: list[dict[str, str]], *, language: str | None = None) -> str:
    title = f"PLAN_v{version}_{mode}_comments"
    lines = [f"# {title}", ""]
    for finding in findings:
        lines.append(f"- {finding.get('id', 'F-???')}: {finding.get('fix', _text(language, 'No fix recorded', '未记录修复'))}")
    return "\n".join(lines) + "\n"


def render_execution_summary(
    items: list[PlanItem],
    results: dict[str, dict[str, Any]],
    *,
    base_commit: str = "unknown",
    final_commit: str = "unknown",
    agent_config: str = "unknown",
    final_audit: str = "unknown",
    language: str | None = None,
) -> str:
    lines = [
        "# EXECUTION_SUMMARY",
        "",
        f"{_text(language, 'Base commit', '基线提交')}: {base_commit}",
        f"{_text(language, 'Final commit', '最终提交')}: {final_commit}",
        f"{_text(language, 'Agent config', '智能体配置')}: {agent_config}",
        f"{_text(language, 'Final audit', '最终审计')}: {final_audit}",
        "",
        _text(language, "Changed files and commits are recorded per item below.", "下方按条目记录变更文件和提交。"),
        "",
        _text(
            language,
            "| ID | Status | Evidence | Attempts | Changed files | Commits | Retry decisions | Limitations | Explanation |",
            "| ID | 状态 | 证据 | 尝试次数 | 变更文件 | 提交 | 重试决策 | 限制 | 说明 |",
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        result = results.get(item.id, {})
        status = result.get("status", _text(language, "missing", "缺失"))
        evidence = result.get("evidence") or item.evidence
        explanation = result.get("explanation", _text(language, "No validated result recorded", "未记录已验证结果"))
        attempts = result.get("attempts", 0)
        limitations = result.get("limitations", "")
        changed_files = ", ".join(result.get("changed_files", []))
        commits = ", ".join(result.get("commits", []))
        retry_decisions = ", ".join(result.get("retry_decisions", []))
        lines.append(
            f"| {item.id} | {status} | {evidence} | {attempts} | "
            f"{changed_files} | {commits} | {retry_decisions} | {limitations} | {explanation} |"
        )
    return "\n".join(lines) + "\n"


render_execution_results = render_execution_summary


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    reason: str


@dataclass(frozen=True)
class PlanLevel:
    name: str
    min_questions: int
    max_questions: int | None
    min_refinement_rounds: int
    max_refinement_rounds: int | None
    refinement_timeout_seconds: int | None
    max_refinement_comments_or_questions: int | None
    direct_execution_option: bool
    high_priority_only: bool
    websearch_required_in: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "min_questions": self.min_questions,
            "max_questions": self.max_questions,
            "min_refinement_rounds": self.min_refinement_rounds,
            "max_refinement_rounds": self.max_refinement_rounds,
            "refinement_timeout_seconds": self.refinement_timeout_seconds,
            "max_refinement_comments_or_questions": self.max_refinement_comments_or_questions,
            "direct_execution_option": self.direct_execution_option,
            "high_priority_only": self.high_priority_only,
            "websearch_required_in": list(self.websearch_required_in),
        }


PLAN_LEVELS = (
    PlanLevel("mini-plan", 1, 1, 0, 1, None, None, True, False),
    PlanLevel("small-plan", 1, 3, 1, 1, None, None, False, False),
    PlanLevel("plan", 1, 5, 0, 3, 600, 3, False, True),
    PlanLevel("big-plan", 5, 10, 0, 5, 1800, 5, False, True, ("brainstorming",)),
    PlanLevel("huge-plan", 10, None, 0, None, None, 5, False, True, ("brainstorming", "refinement"), ("huge plan",)),
)
_PLAN_LEVELS_BY_NAME = {name: level for level in PLAN_LEVELS for name in (level.name, *level.aliases)}


def plan_level(name: str) -> PlanLevel:
    key = name.strip().lower()
    if key not in _PLAN_LEVELS_BY_NAME:
        raise ContractError(f"unknown plan level {name!r}")
    return _PLAN_LEVELS_BY_NAME[key]


@dataclass(frozen=True)
class Question:
    nonce: str
    prompt: str
    options: list[Option]

    def to_json(self, *, expected_seq: int | None = None) -> dict[str, Any]:
        payload = {
            "nonce": self.nonce,
            "prompt": self.prompt,
            "options": [option.__dict__ for option in self.options],
            "recommended_option_id": self.options[0].id,
            "expected_seq": expected_seq,
        }
        if any(option.id == "other" for option in self.options):
            payload["free_form"] = {"option_id": "other", "required": False}
        return payload


class QuestionLedger:
    def __init__(self) -> None:
        self.pending: dict[str, Question] = {}
        self.answered: set[str] = set()

    def ask(
        self,
        prompt: str,
        *,
        recommended: tuple[str, str, str],
        alternatives: list[tuple[str, str, str]] | None = None,
        allow_auto_complete: bool = True,
        allow_other: bool = True,
        language: str | None = None,
    ) -> Question:
        options = [Option(*recommended)]
        options.extend(Option(*item) for item in (alternatives or []))
        if allow_other:
            options.append(Option("other", _text(language, "Other", "其他"), _text(language, "free-form answer", "自由回答")))
        if allow_auto_complete:
            options.append(
                Option(
                    "auto",
                    _text(language, "Auto-complete", "自动完成"),
                    _text(language, "use recommended answers until the next gate", "在下一个门禁前使用推荐答案"),
                )
            )
        question = Question(uuid.uuid4().hex, prompt, options)
        self.pending[question.nonce] = question
        return question

    def answer(self, nonce: str, choice: str) -> dict[str, str]:
        if nonce in self.answered or nonce not in self.pending:
            raise ContractError("stale or replayed question nonce")
        question = self.pending.pop(nonce)
        if choice not in {option.id for option in question.options}:
            raise ContractError(f"invalid answer choice {choice!r}")
        self.answered.add(nonce)
        return {"nonce": nonce, "choice": choice}


GENERIC_QUESTION_RESERVED_NAMES = {
    "agent-choice",
    "background-model",
    "default",
    "execution_launch",
    "execution_summary",
    "source_auto_commit",
    "finish_run",
    "finish-run",
    "approve",
    "stop",
    "integrated",
    "pr-opened",
    "kept",
    "discarded",
    "failed",
    "aborted",
    "generate-summary",
    "skip-summary",
    "always-skip-summary",
    LANGUAGE_SELECTION_STAGE,
    "other",
    "auto",
    "skip-refinement-execute",
}


def validate_generic_question(stage: str, decision_id: str, option_ids: list[str]) -> None:
    if not stage.strip():
        raise ContractError("generic question stage is required")
    if not decision_id.strip():
        raise ContractError("generic question decision_id is required")
    names = [stage, *option_ids]
    for name in names:
        if name != name.strip() or not name:
            raise ContractError("generic question names must be non-empty and trimmed")
        if name in GENERIC_QUESTION_RESERVED_NAMES or name.startswith("execution_") or name.startswith("execution-"):
            raise ContractError(f"generic question name {name!r} is reserved")
    if len(set(option_ids)) != len(option_ids):
        raise ContractError("generic question option ids must be unique")


def may_auto_answer(stage: str) -> bool:
    return stage in {
        "planning",
        "brainstorming",
        "refinement",
        "reviewer_selection",
        "criticizer_selection",
        "model_selection",
        "finalize_plan",
    }


class RefinementLedger:
    def __init__(self, *, max_items_per_round: int | None = 5) -> None:
        self.findings: dict[str, dict[str, Any]] = {}
        self.criticizer_questions = 0
        self.max_items_per_round = max_items_per_round

    def _check_round_limit(self, count: int) -> None:
        if self.max_items_per_round is not None and count >= self.max_items_per_round:
            raise ContractError(f"refinement may record at most {self.max_items_per_round} items per round")

    def add_finding(self, severity: str, affected_ids: list[str], evidence: str, fix: str) -> dict[str, Any]:
        self._check_round_limit(len(self.findings))
        finding_id = f"F-{len(self.findings) + 1:03d}"
        finding = {
            "id": finding_id,
            "severity": severity,
            "affected_ids": affected_ids,
            "evidence": evidence,
            "recommended_fix": fix,
            "disposition": "unresolved",
        }
        self.findings[finding_id] = finding
        return finding

    def disposition(self, finding_id: str, status: str, reason: str) -> None:
        if finding_id not in self.findings:
            raise ContractError(f"unknown finding {finding_id}")
        self.findings[finding_id]["disposition"] = status
        self.findings[finding_id]["reason"] = reason

    def converged(self) -> bool:
        return all(finding["disposition"] != "unresolved" for finding in self.findings.values())

    def add_criticizer_question(self, question: str) -> None:
        self._check_round_limit(self.criticizer_questions)
        self.criticizer_questions += 1


def require_clean_source(repo: Path, *, ignored_paths: list[Path] | None = None) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", ".", *_pathspec_exclusions(repo, ignored_paths)],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    if result.stdout:
        raise ContractError("source worktree must be clean before execution")


def resolve_path_scopes(repo: Path, scopes: list[str]) -> list[Path]:
    root = repo.resolve()
    resolved: list[Path] = []
    for scope in scopes:
        if scope.startswith("/") or ".." in Path(scope).parts:
            raise ContractError(f"path scope escapes repository: {scope}")
        raw_candidate = root / scope
        if raw_candidate.is_symlink():
            raise ContractError(f"path scope is a symlink: {scope}")
        candidate = raw_candidate.resolve()
        if not candidate.is_relative_to(root):
            raise ContractError(f"path scope escapes repository: {scope}")
        if candidate.is_symlink():
            raise ContractError(f"path scope is an escaping symlink or unsupported link: {scope}")
        if (candidate / ".git").exists():
            raise ContractError(f"path scope contains nested repository: {scope}")
        if candidate.exists():
            for child in candidate.rglob("*"):
                if child.is_symlink():
                    raise ContractError(f"path scope contains symlink: {child.relative_to(root)}")
                if child.name == ".git" or (child / ".git").exists():
                    raise ContractError(f"path scope contains nested repository: {child.relative_to(root)}")
        resolved.append(candidate)
    return resolved


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
