#!/usr/bin/env python3
"""Strict state, artifact, interaction, and Git primitives for optim-plans."""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_EVIDENCE_CHARS = 4096
TIMEOUT_KILL_GRACE_SECONDS = 1.0
ADAPTER_NAMES = {"claude", "codex"}
FINISH_OUTCOMES = {"integrated", "pr-opened", "kept", "discarded", "failed", "aborted"}
LEGACY_ACTIVE_EVENT_TYPES = {"item_completed", "execution_completed", "run_completed", "worker_result_recorded"}
LIFECYCLE_EVENT_TYPES = {
    "execution_manifest_created",
    "execution_started",
    "item_started",
    "worker_completed",
    "worker_failed",
    "verification_failed",
    "audit_failed",
    "awaiting_retry_decision",
    "retry_restored",
    "item_verified",
    "checkpoint_created",
    "final_audit_passed",
    "awaiting_integration",
    "run_finished",
}


class ContractError(RuntimeError):
    """A user-actionable optim-plans contract violation."""


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


def git_common_dir(repo: Path) -> Path:
    common = git(repo, "rev-parse", "--git-common-dir")
    path = Path(common)
    if not path.is_absolute():
        path = repo / path
    return path.absolute()


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


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        elif event_type == "awaiting_retry_decision":
            status = "awaiting_retry_decision"
        elif event_type in {"worker_failed", "verification_failed", "audit_failed"}:
            status = "awaiting_retry_decision"
        elif event_type == "worker_completed":
            status = "verifying"
        elif event_type == "pending_question" and payload.get("stage") == "execution_launch":
            status = "awaiting_approval"
        elif event_type == "execution_manifest_created":
            status = "awaiting_approval"
        elif event_type in {"execution_started", "item_started", "retry_restored", "item_verified", "checkpoint_created", "final_audit_passed"}:
            status = "executing"
    return status


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
    def initialize(cls, repo: Path | str, *, topic: str, plan_hash: str) -> "OptimPlansState":
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

    @contextmanager
    def controller_lock(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.controller_lock():
            return self._append_event_locked(event_type, payload)

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
        write_json_atomic(self.runtime_file, {"status": lifecycle_status([*replayed.events, event]), "last_seq": event["seq"]})
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
            event = self._append_event_locked("answer_recorded", {"nonce": nonce, "choice": choice})
            return event["payload"]

    def persist_execution_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"planning"}, "prepare-execution")
            if any(event["type"] == "execution_manifest_created" for event in replayed.events):
                raise ContractError("execution manifest is write-once")
            canonical = canonical_execution_manifest(manifest)
            payload = {"manifest": canonical, "manifest_hash": execution_manifest_hash(canonical)}
            return self._append_event_locked("execution_manifest_created", payload)["payload"]

    def prepare_execution(self, manifest_path: Path) -> dict[str, Any]:
        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(f"cannot read execution manifest {manifest_path}: {exc}") from exc
        manifest = parse_json_strict(raw, source=str(manifest_path))
        if not isinstance(manifest, dict):
            raise ContractError("execution manifest must be a JSON object")
        self.persist_execution_manifest(manifest)
        return self.request_execution_approval()

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
        canonical = canonical_execution_manifest(manifest)
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
        canonical = canonical_execution_manifest(manifest)
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
                    candidate["type"] == "retry_restored" and candidate["seq"] > answer_seq
                    for candidate in events
                ):
                    return payload
                break
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": "Approve finish outcome?",
            "options": [
                {"id": "integrated", "label": "Integrated", "reason": "local destination ref contains final checkpoint"},
                {"id": "pr-opened", "label": "PR opened", "reason": "remote ref and PR URL contain final checkpoint"},
                {"id": "kept", "label": "Keep", "reason": "preserve run worktree and branch"},
                {"id": "discarded", "label": "Discard", "reason": "remove validated controller-owned worktree and branch"},
                {"id": "failed", "label": "Failed", "reason": "preserve failure evidence"},
                {"id": "aborted", "label": "Aborted", "reason": "preserve abort evidence"},
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
        payload = {
            "nonce": uuid.uuid4().hex,
            "prompt": f"Approve retry restore for {item_id}?",
            "options": [
                {"id": "approve", "label": "Approve retry", "reason": "restore failed run worktree once"},
                {"id": "stop", "label": "Stop", "reason": "preserve failed attempt"},
                {"id": "other", "label": "Other", "reason": "free-form answer"},
            ],
            "recommended_option_id": "approve",
            "free_form": {"option_id": "other", "required": False},
            "stage": "execution_retry",
            "item_id": item_id,
            "failed_base_commit": failed_base_commit,
        }
        return self._append_event_locked("pending_question", payload)["payload"]

    def request_finish_approval(self) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(
                replayed.events,
                {"awaiting_retry_decision", "awaiting_integration", "legacy_active"},
                "finish approval",
            )
            return self._finish_question_payload_locked(replayed.events)

    def request_execution_approval(self) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            existing = [
                event.get("payload", {})
                for event in replayed.events
                if event["type"] == "pending_question"
                and event.get("payload", {}).get("stage") == "execution_launch"
            ]
            if len(existing) > 1:
                raise ContractError("multiple execution approval questions recorded")
            if existing:
                question = existing[0]
                if question.get("manifest") != record["manifest"] or question.get("manifest_hash") != record["manifest_hash"]:
                    raise ContractError("execution approval question is not bound to the manifest")
                return question

            payload = {
                "nonce": uuid.uuid4().hex,
                "prompt": "Approve execution?",
                "options": [
                    {"id": "approve", "label": "Approve execution", "reason": "launch exactly this manifest"},
                    {"id": "stop", "label": "Stop", "reason": "do not launch execution"},
                    {"id": "other", "label": "Other", "reason": "free-form answer"},
                ],
                "recommended_option_id": "approve",
                "free_form": {"option_id": "other", "required": False},
                "expected_seq": len(replayed.events) + 1,
                "stage": "execution_launch",
                "manifest": record["manifest"],
                "manifest_hash": record["manifest_hash"],
            }
            return self._append_event_locked("pending_question", payload)["payload"]

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
            if event["type"] == "checkpoint_created":
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
            if clean and _status_entries(run_worktree):
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
        adapter = raw.get("adapter", raw.get("name"))
        argv = raw.get("argv")
        env = raw.get("env", {})
        if adapter not in ADAPTER_NAMES:
            raise ContractError("worker adapter must be claude or codex")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv) or not argv[0]:
            raise ContractError("worker adapter argv must have a non-empty executable and string arguments")
        if Path(argv[0]).name != adapter:
            raise ContractError("worker adapter argv executable does not match adapter")
        if adapter == "codex" and "exec" not in argv[1:]:
            raise ContractError("codex worker argv must be an exec adapter command")
        if adapter == "codex" and "--output-schema" not in argv:
            raise ContractError("codex worker argv must include an output schema")
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
        legacy_timeout = raw.get("timeout_seconds", manifest.get("worker_timeout_seconds"))
        if legacy_timeout is not None and (not isinstance(legacy_timeout, (int, float)) or legacy_timeout <= 0):
            raise ContractError("worker timeout_seconds must be positive")
        return {
            "adapter": adapter,
            "argv": list(argv),
            "env": dict(env),
            "config_files": list(config_files),
            "timeout_seconds": None,
        }

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
            try:
                target.resolve().relative_to(self.run_dir.resolve())
            except ValueError as exc:
                raise ContractError("worker adapter config files must live under the run directory") from exc
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

    def _owned_launch_path(self, raw: str, *, flag: str) -> Path:
        target = Path(raw)
        if not target.is_absolute():
            raise ContractError(f"{flag} path must be absolute")
        try:
            target.resolve().relative_to(self.run_dir.resolve())
        except ValueError as exc:
            raise ContractError(f"{flag} path must live under the controller run directory") from exc
        return target

    def _ensure_adapter_launch_files(self, config: dict[str, Any], *, write: bool = True) -> None:
        argv = config["argv"]
        self._write_manifest_config_files(config["config_files"], write=write)
        for flag in ("--output-schema", "--settings"):
            if flag not in argv:
                continue
            index = argv.index(flag) + 1
            if index >= len(argv):
                raise ContractError(f"{flag} requires a path")
            target = self._owned_launch_path(argv[index], flag=flag)
            if not write:
                continue
            if flag == "--output-schema":
                write_json_atomic(
                    target,
                    {
                        "type": "object",
                        "required": ["nonce", "item_id", "status", "evidence"],
                        "properties": {
                            "nonce": {"type": "string"},
                            "item_id": {"type": "string"},
                            "status": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                )
            elif not target.exists():
                write_json_atomic(target, {})
        if "--plugin-dir" in argv:
            index = argv.index("--plugin-dir") + 1
            if index >= len(argv):
                raise ContractError("--plugin-dir requires a path")
            target = self._owned_launch_path(argv[index], flag="--plugin-dir")
            if write:
                target.mkdir(parents=True, exist_ok=True)

    def _record_attempt_failure_locked(
        self,
        event_type: str,
        item_id: str,
        *,
        evidence: str,
        start: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "item_id": item_id,
            "evidence": bounded_evidence(evidence),
            "base_commit": start["base_commit"],
            "run_worktree": start["run_worktree"],
        }
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
        self._retry_question_payload_locked(
            self.replay().events,
            item_id=item_id,
            failed_base_commit=start["base_commit"],
        )
        self._finish_question_payload_locked(self.replay().events)
        return payload

    def record_attempt_failure(self, event_type: str, item_id: str, *, evidence: str) -> dict[str, Any]:
        if event_type not in {"verification_failed", "audit_failed"}:
            raise ContractError(f"unsupported failure event {event_type!r}")
        if not evidence.strip():
            raise ContractError("failure evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] not in {"in_progress", "completed"}:
                raise ContractError(f"{item_id} has no active attempt to fail")
            start = self._latest_item_start(replayed.events, item_id)
            return self._record_attempt_failure_locked(event_type, item_id, evidence=evidence, start=start)

    def _worker_result_evidence(self, item_id: str, *, result_path: Path, worker_nonce: str) -> str:
        if not result_path.exists():
            raise ContractError("worker result file is missing")
        payload = parse_json_strict(result_path.read_text(encoding="utf-8"), source=str(result_path))
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

    def _assert_protected_metadata_before_verification(self, item_id: str) -> None:
        try:
            with self.controller_lock():
                replayed = self.replay()
                started = self._execution_started_record(replayed.events)
                self._require_protected_metadata_clean(started)
        except (ContractError, subprocess.CalledProcessError, OSError) as exc:
            self.record_attempt_failure("audit_failed", item_id, evidence=f"audit failed: {exc}")
            raise

    def run_item(self, item_id: str) -> dict[str, Any]:
        replayed = self.replay()
        self._require_lifecycle_locked(replayed.events, {"executing"}, "run-item")
        record = self._execution_manifest_record(replayed.events)
        item = self._manifest_item(record["manifest"], item_id)
        worker_config = self._worker_config(record["manifest"], item)
        verification_config = self._verification_config(record["manifest"], item)
        self._ensure_adapter_launch_files(worker_config, write=False)

        started = self.begin_item(item_id)
        self._ensure_adapter_launch_files(worker_config)
        run_worktree = Path(started["run_worktree"])
        worker_nonce = uuid.uuid4().hex
        result_path = self.run_dir / "worker-results" / f"{item_id}-{started['attempt']}.json"
        state_path = self.run_dir / "worker-states" / f"{item_id}-{started['attempt']}.json"
        if result_path.exists():
            self.record_worker_failure(item_id, evidence="worker result path already exists")
            raise ContractError("worker result path already exists")
        write_json_atomic(state_path, {"run_id": self.run_id, "worker_nonce": worker_nonce})
        result_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(worker_config["env"])
        env.update(
            {
                "OPTIM_PLANS_RUN_ID": self.run_id,
                "OPTIM_PLANS_WORKER_NONCE": worker_nonce,
                "OPTIM_PLANS_STATE_PATH": str(state_path),
                "OPTIM_PLANS_IDS": item_id,
                "OPTIM_PLANS_SCOPES": os.pathsep.join(started["allowed_paths"]),
                "OPTIM_PLANS_RESULT_PATH": str(result_path),
            }
        )
        worker = run_process_group(
            worker_config["argv"],
            cwd=run_worktree,
            env=env,
            timeout_seconds=worker_config["timeout_seconds"],
        )
        if not worker.ok():
            evidence = worker.evidence("worker", timeout_seconds=worker_config["timeout_seconds"])
            self.record_worker_failure(item_id, evidence=evidence)
            raise ContractError(evidence)
        try:
            worker_evidence = self._worker_result_evidence(item_id, result_path=result_path, worker_nonce=worker_nonce)
        except ContractError as exc:
            self.record_worker_failure(item_id, evidence=f"worker result rejected: {exc}")
            raise
        self.record_worker_completion(item_id, evidence=worker_evidence)

        self._assert_protected_metadata_before_verification(item_id)
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
            self.record_attempt_failure("verification_failed", item_id, evidence=verifier_evidence)
            raise ContractError(verifier_evidence)
        checkpoint = self.checkpoint_item(item_id, evidence=verifier_evidence)
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
            item_id = payload.get("item_id")
            if item_id not in statuses:
                continue
            if event["type"] == "item_started":
                statuses[item_id] = "in_progress"
            elif event["type"] == "worker_completed":
                statuses[item_id] = "completed"
            elif event["type"] in {"worker_failed", "verification_failed", "audit_failed"}:
                statuses[item_id] = "failed"
            elif event["type"] == "retry_restored":
                statuses[item_id] = "pending"
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

    def _latest_failure(self, events: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload", {})
            if payload.get("item_id") != item_id:
                continue
            if event["type"] in {"worker_failed", "verification_failed", "audit_failed"}:
                return payload
            if event["type"] in {"checkpoint_created", "retry_restored", "item_started"}:
                break
        raise ContractError(f"{item_id} has no failed attempt to retry")

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
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "pending":
                raise ContractError(f"{item_id} is not ready for execution")
            blocked = [
                current_id
                for current_id, status in statuses.items()
                if status in {"in_progress", "completed", "failed"}
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
            }
            return self._append_event_locked("item_started", payload)["payload"]

    def record_worker_completion(self, item_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("worker completion evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
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
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "in_progress":
                raise ContractError(f"{item_id} is not in progress")
            start = self._latest_item_start(replayed.events, item_id)
            return self._record_attempt_failure_locked("worker_failed", item_id, evidence=evidence, start=start)

    def checkpoint_item(self, item_id: str, *, evidence: str) -> dict[str, Any]:
        if not evidence.strip():
            raise ContractError("verification evidence is required")
        with self.controller_lock():
            replayed = self.replay()
            record = self._execution_manifest_record(replayed.events)
            started = self._execution_started_record(replayed.events)
            item = self._manifest_item(record["manifest"], item_id)
            statuses = self._item_statuses(replayed.events, record["manifest"])
            if statuses[item_id] != "completed":
                raise ContractError(f"{item_id} is not completed and ready for checkpoint")
            start = self._latest_item_start(replayed.events, item_id)
            try:
                self._require_protected_metadata_clean(started)
                run_worktree = self._require_run_worktree(
                    started,
                    expected_head=start["base_commit"],
                    clean=False,
                )
                audit = audit_git_delta(
                    run_worktree,
                    allowed_paths=self._item_allowed_paths(item),
                    base_commit=start["base_commit"],
                    head_commit=start["base_commit"],
                )
            except (ContractError, subprocess.CalledProcessError, OSError) as exc:
                self._record_attempt_failure_locked("audit_failed", item_id, evidence=f"audit failed: {exc}", start=start)
                raise
            try:
                for path in audit["changed_files"]:
                    git(run_worktree, "add", "-A", "--", path)
                env = os.environ.copy()
                env.update(
                    {
                        "GIT_AUTHOR_NAME": "Optim Plans",
                        "GIT_AUTHOR_EMAIL": "optim-plans@example.invalid",
                        "GIT_COMMITTER_NAME": "Optim Plans",
                        "GIT_COMMITTER_EMAIL": "optim-plans@example.invalid",
                        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
                    }
                )
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "commit",
                        "--allow-empty",
                        "-m",
                        f"optim-plans checkpoint {self.run_id} {item_id} attempt {start['attempt']}",
                    ],
                    cwd=run_worktree,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                    env=env,
                )
                commit = git(run_worktree, "rev-parse", "--verify", "HEAD")
                clean_audit = audit_git_delta(run_worktree, allowed_paths=self._item_allowed_paths(item))
                if clean_audit["changed_files"]:
                    raise ContractError("run worktree is not clean after checkpoint")
            except (ContractError, subprocess.CalledProcessError, OSError) as exc:
                self._record_attempt_failure_locked(
                    "audit_failed", item_id, evidence=f"checkpoint failed: {exc}", start=start
                )
                raise
            verified = {
                "item_id": item_id,
                "evidence": evidence,
                "worker_evidence": next(
                    event.get("payload", {}).get("evidence")
                    for event in reversed(replayed.events)
                    if event["type"] == "worker_completed" and event.get("payload", {}).get("item_id") == item_id
                ),
                "changed_files": audit["changed_files"],
            }
            self._append_event_locked("item_verified", verified)
            payload = {"item_id": item_id, "commit": commit, "changed_files": audit["changed_files"]}
            return self._append_event_locked("checkpoint_created", payload)["payload"]

    def request_retry(self, item_id: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "retry approval")
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            failure = self._latest_failure(replayed.events, item_id)
            return self._retry_question_payload_locked(
                replayed.events,
                item_id=item_id,
                failed_base_commit=failure["base_commit"],
            )

    def restore_retry(self, item_id: str, approval_nonce: str) -> dict[str, Any]:
        with self.controller_lock():
            replayed = self.replay()
            self._require_lifecycle_locked(replayed.events, {"awaiting_retry_decision"}, "retry-item")
            record = self._execution_manifest_record(replayed.events)
            self._manifest_item(record["manifest"], item_id)
            started = self._execution_started_record(replayed.events)
            self._require_protected_metadata_clean(started)
            failure = self._latest_failure(replayed.events, item_id)
            question: dict[str, Any] | None = None
            answer: dict[str, Any] | None = None
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
                "restored_to": failure["base_commit"],
                "run_worktree": str(run_worktree),
            }
            return self._append_event_locked("retry_restored", payload)["payload"]

    def retry_item(self, item_id: str, approval_nonce: str) -> dict[str, Any]:
        self.restore_retry(item_id, approval_nonce)
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
            payload = {"status": "passed", "final_commit": head, "changed_files": audit["changed_files"]}
            passed = self._append_event_locked("final_audit_passed", payload)["payload"]
            self._append_event_locked("awaiting_integration", {"final_checkpoint": head, "audit": passed})
            self._finish_question_payload_locked(self.replay().events)
            return passed

    def _manifest_destination_ref(self, manifest: dict[str, Any]) -> str:
        destination = None
        for key in ("integration_destination", "integration_destination_ref", "local_integration_destination_ref"):
            if key in manifest:
                destination = manifest[key]
                break
        if not isinstance(destination, str) or not destination.strip() or destination.startswith("-"):
            raise ContractError("execution manifest integration_destination is required")
        return destination

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
            if event["type"] in {"worker_failed", "verification_failed", "audit_failed"}:
                payload = event.get("payload", {})
                return {
                    "failure_event_type": event["type"],
                    "failure_event_seq": event["seq"],
                    "failure_item_id": payload.get("item_id"),
                    "failure_evidence": payload.get("evidence", ""),
                }
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
                {"awaiting_integration", "awaiting_retry_decision", "legacy_active"},
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
                payload.update(
                    self._validate_integrated_proof(record["manifest"], started, final_checkpoint, target_ref)
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
        return ReplayState(events, lifecycle_status(events))


@dataclass(frozen=True)
class PlanItem:
    id: str
    summary: str
    verification: str
    evidence: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: str = ""
    allowed_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkerAssignment:
    item_id: str
    nonce: str
    scopes: list[str]
    result_path: Path

    def env(self, *, run_id: str, state_path: Path) -> dict[str, str]:
        return {
            "OPTIM_PLANS_RUN_ID": run_id,
            "OPTIM_PLANS_WORKER_NONCE": self.nonce,
            "OPTIM_PLANS_STATE_PATH": str(state_path),
            "OPTIM_PLANS_IDS": self.item_id,
            "OPTIM_PLANS_SCOPES": os.pathsep.join(self.scopes),
            "OPTIM_PLANS_RESULT_PATH": str(self.result_path),
        }


def render_plan(
    goal: str,
    items: list[PlanItem],
    *,
    version: int,
    repo_evidence: list[str] | None = None,
    resolved_decisions: list[str] | None = None,
) -> str:
    lines = [
        f"# PLAN_v{version}",
        "",
        f"Goal: {goal}",
        "",
        "| ID | Depends on | Verification | Evidence | Acceptance | Allowed paths | Summary |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in items:
        depends = ", ".join(item.depends_on) if item.depends_on else "none"
        allowed_paths = ", ".join(item.allowed_paths) if item.allowed_paths else "none"
        acceptance = item.acceptance or "not recorded"
        lines.append(
            f"| {item.id} | {depends} | {item.verification} | {item.evidence} | "
            f"{acceptance} | {allowed_paths} | {item.summary} |"
        )
    lines.extend(["", "## Repo evidence", ""])
    lines.extend(f"- {evidence}" for evidence in (repo_evidence or ["Not recorded."]))
    lines.extend(["", "## Resolved decisions", ""])
    lines.extend(f"- {decision}" for decision in (resolved_decisions or ["Not recorded."]))
    lines.extend(["", "## Revision ledger", "", "- Initial version; no prior findings."])
    return "\n".join(lines) + "\n"


def render_comments(mode: str, version: int, findings: list[dict[str, str]]) -> str:
    title = f"PLAN_v{version}_{mode}_comments"
    lines = [f"# {title}", ""]
    for finding in findings:
        lines.append(f"- {finding.get('id', 'F-???')}: {finding.get('fix', 'No fix recorded')}")
    return "\n".join(lines) + "\n"


def render_execution_results(
    items: list[PlanItem],
    results: dict[str, dict[str, Any]],
    *,
    base_commit: str = "unknown",
    final_commit: str = "unknown",
    agent_config: str = "unknown",
    final_audit: str = "unknown",
) -> str:
    lines = [
        "# EXECUTION_RESULTS",
        "",
        f"Base commit: {base_commit}",
        f"Final commit: {final_commit}",
        f"Agent config: {agent_config}",
        f"Final audit: {final_audit}",
        "",
        "Changed files and commits are recorded per item below.",
        "",
        "| ID | Status | Evidence | Attempts | Changed files | Commits | Limitations | Explanation |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        result = results.get(item.id, {})
        status = result.get("status", "missing")
        explanation = result.get("explanation", "No validated result recorded")
        attempts = result.get("attempts", 0)
        limitations = result.get("limitations", "")
        changed_files = ", ".join(result.get("changed_files", []))
        commits = ", ".join(result.get("commits", []))
        lines.append(
            f"| {item.id} | {status} | {item.evidence} | {attempts} | "
            f"{changed_files} | {commits} | {limitations} | {explanation} |"
        )
    return "\n".join(lines) + "\n"


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
        return {
            "nonce": self.nonce,
            "prompt": self.prompt,
            "options": [option.__dict__ for option in self.options],
            "recommended_option_id": self.options[0].id,
            "free_form": {"option_id": "other", "required": False},
            "expected_seq": expected_seq,
        }


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
    ) -> Question:
        options = [Option(*recommended)]
        options.extend(Option(*item) for item in (alternatives or []))
        options.append(Option("other", "Other", "free-form answer"))
        if allow_auto_complete:
            options.append(Option("auto", "Auto-complete", "use recommended answers until the next gate"))
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


class ExecutionLedger:
    def __init__(self, items: list[PlanItem]) -> None:
        self.items = {item.id: item for item in items}
        self.status = {item.id: "pending" for item in items}
        self.attempts = {item.id: [] for item in items}

    def ready_ids(self) -> list[str]:
        ready: list[str] = []
        for item in self.items.values():
            if self.status[item.id] != "pending":
                continue
            if all(self.status[dep] == "verified" for dep in item.depends_on):
                ready.append(item.id)
        return ready

    def record_attempt(self, item_id: str, evidence: str) -> None:
        if item_id not in self.items:
            raise ContractError(f"unknown plan item {item_id}")
        if not evidence.strip():
            raise ContractError("attempt evidence is required")
        attempts = self.attempts[item_id]
        if evidence in attempts:
            raise ContractError("attempt evidence must be distinct")
        attempts.append(evidence)
        if len(attempts) >= 3:
            self.status[item_id] = "needs_confirmation"

    def confirm_not_achievable(self, item_id: str, evidence: str) -> None:
        if self.status.get(item_id) != "needs_confirmation":
            raise ContractError(f"{item_id} does not need not_achievable confirmation")
        if not evidence.strip():
            raise ContractError("confirmation evidence is required")
        self.attempts[item_id].append(f"confirmation: {evidence}")
        self.status[item_id] = "not_achievable"

    def prepare_worker(
        self,
        item_id: str,
        *,
        scopes: list[str],
        result_dir: Path | None = None,
        result_path: Path | None = None,
    ) -> WorkerAssignment:
        if item_id not in self.items:
            raise ContractError(f"unknown plan item {item_id}")
        if item_id not in self.ready_ids():
            raise ContractError(f"{item_id} is not ready for execution")
        if not scopes:
            raise ContractError("worker scopes are required")
        nonce = uuid.uuid4().hex
        if result_path is None:
            if result_dir is None:
                raise ContractError("worker result directory is required")
            result_path = Path(result_dir) / f"{item_id}-{nonce}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            raise ContractError("worker result path already exists")
        return WorkerAssignment(item_id, nonce, list(scopes), result_path)

    def complete_worker(
        self,
        assignment: WorkerAssignment,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        if assignment.item_id not in self.items:
            raise ContractError(f"unknown plan item {assignment.item_id}")
        if self.status[assignment.item_id] != "pending":
            raise ContractError(f"{assignment.item_id} is not pending")
        if timed_out:
            self.record_attempt(assignment.item_id, "worker timed out")
            raise ContractError("worker timed out")
        if exit_code != 0:
            self.record_attempt(assignment.item_id, f"worker exited with exit {exit_code}")
            raise ContractError(f"worker exited with exit {exit_code}")
        if not assignment.result_path.exists():
            raise ContractError("worker result file is missing")
        payload = parse_json_strict(assignment.result_path.read_text(encoding="utf-8"), source=str(assignment.result_path))
        if not isinstance(payload, dict):
            raise ContractError("worker result must be a JSON object")
        for key in ("nonce", "item_id", "status", "evidence"):
            if key not in payload:
                raise ContractError(f"worker result is missing {key!r}")
        if payload["nonce"] != assignment.nonce:
            raise ContractError("worker result nonce does not match assignment")
        if payload["item_id"] != assignment.item_id:
            raise ContractError("worker result item_id does not match assignment")
        if payload["status"] not in {"completed", "verified"}:
            raise ContractError(f"unsupported worker result status {payload['status']!r}")
        if not isinstance(payload["evidence"], str) or not payload["evidence"].strip():
            raise ContractError("worker result evidence is required")
        self.status[assignment.item_id] = "completed"
        return payload


def require_clean_source(repo: Path, *, ignored_paths: list[Path] | None = None) -> None:
    root = repo.resolve()
    exclusions: list[str] = []
    for path in ignored_paths or []:
        try:
            relative = Path(path).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        exclusions.append(f":(exclude){relative}")
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", ".", *exclusions],
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
