from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import git, make_repo


class GitIsolationTests(unittest.TestCase):
    def _start_state(self, repo: Path, items: list[dict[str, object]]):
        from scripts.optim_plans_core import OptimPlansState

        state = OptimPlansState.initialize(repo, topic="Isolation", plan_hash="abc123")
        state.persist_execution_manifest(
            {
                "plan_hash": "abc123",
                "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                "integration_destination": "main",
                "items": items,
            }
        )
        question = state.request_execution_approval()
        state.record_answer(question["nonce"], "approve")
        started = state.start_execution(question["nonce"])
        return state, Path(started["run_worktree"])

    def test_dirty_staged_and_untracked_source_repos_are_rejected(self) -> None:
        from scripts.optim_plans_core import ContractError, require_clean_source

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "untracked.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ContractError):
                require_clean_source(repo)
            (repo / "untracked.txt").unlink()
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                require_clean_source(repo)
            git(repo, "add", "README.md")
            with self.assertRaises(ContractError):
                require_clean_source(repo)

    def test_path_scopes_reject_escaping_symlink(self) -> None:
        from scripts.optim_plans_core import ContractError, resolve_path_scopes

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            outside = Path(raw) / "outside"
            outside.mkdir()
            (repo / "safe").mkdir()
            (repo / "safe/link").symlink_to(outside)
            with self.assertRaises(ContractError):
                resolve_path_scopes(repo, ["safe/link"])

    def test_path_scopes_reject_top_level_in_repo_symlink(self) -> None:
        from scripts.optim_plans_core import ContractError, resolve_path_scopes

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "real").mkdir()
            (repo / "link").symlink_to(repo / "real")
            with self.assertRaises(ContractError):
                resolve_path_scopes(repo, ["link"])

    def test_path_scopes_reject_bad_descendants(self) -> None:
        from scripts.optim_plans_core import ContractError, resolve_path_scopes

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            outside = Path(raw) / "outside"
            outside.mkdir()
            (repo / "safe").mkdir()
            (repo / "safe/link").symlink_to(outside)
            with self.assertRaises(ContractError):
                resolve_path_scopes(repo, ["safe"])
            (repo / "safe/link").unlink()
            (repo / "safe/nested").mkdir()
            (repo / "safe/nested/.git").mkdir()
            with self.assertRaises(ContractError):
                resolve_path_scopes(repo, ["safe"])

    def test_checkpoint_audit_covers_git_status_classes_and_special_paths(self) -> None:
        from scripts.optim_plans_core import ContractError

        def prepare_repo(raw: str) -> tuple[object, Path, Path]:
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            (repo / "src/ok.txt").write_text("ok\n", encoding="utf-8")
            (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
            git(repo, "add", "src/ok.txt", ".gitignore")
            git(repo, "commit", "-m", "fixture")
            state, run_worktree = self._start_state(repo, [{"id": "TASK-001", "allowed_paths": ["src"]}])
            state.begin_item("TASK-001")
            return state, repo, run_worktree

        cases = {
            "tracked-out-of-scope": lambda run, repo: (run / "README.md").write_text("hijack\n", encoding="utf-8"),
            "staged-out-of-scope": lambda run, repo: (
                (run / "oops.txt").write_text("oops\n", encoding="utf-8"),
                git(run, "add", "oops.txt"),
            ),
            "untracked-out-of-scope": lambda run, repo: (run / "oops.txt").write_text("oops\n", encoding="utf-8"),
            "ignored": lambda run, repo: (run / "src/debug.log").write_text("debug\n", encoding="utf-8"),
            "symlink": lambda run, repo: (run / "src/link").symlink_to("ok.txt"),
            "nested-repo": lambda run, repo: ((run / "src/nested").mkdir(), (run / "src/nested/.git").mkdir()),
            "gitlink": lambda run, repo: git(
                run,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{git(repo, 'rev-parse', '--verify', 'HEAD')},src/submodule",
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as raw:
                    state, repo, run_worktree = prepare_repo(raw)
                    mutate(run_worktree, repo)
                    state.record_worker_completion("TASK-001", evidence="worker finished")
                    with self.assertRaises(ContractError):
                        state.checkpoint_item("TASK-001", evidence="unit ok")

    def test_checkpoint_audit_checks_both_rename_paths_and_deleted_special_entries(self) -> None:
        from scripts.optim_plans_core import ContractError, audit_git_delta

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
            git(repo, "add", "outside.txt")
            git(repo, "commit", "-m", "rename fixture")
            state, run_worktree = self._start_state(repo, [{"id": "TASK-001", "allowed_paths": ["src"]}])
            state.begin_item("TASK-001")
            (run_worktree / "src").mkdir()
            git(run_worktree, "mv", "outside.txt", "src/inside.txt")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            with self.assertRaises(ContractError):
                state.checkpoint_item("TASK-001", evidence="unit ok")

        for kind in ("symlink", "gitlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                repo = make_repo(Path(raw))
                (repo / "src").mkdir()
                if kind == "symlink":
                    (repo / "src/link").symlink_to("target")
                    git(repo, "add", "src/link")
                else:
                    git(
                        repo,
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"160000,{git(repo, 'rev-parse', '--verify', 'HEAD')},src/submodule",
                    )
                git(repo, "commit", "-m", f"{kind} fixture")
                if kind == "gitlink":
                    base = git(repo, "rev-parse", "--verify", "HEAD")
                    git(repo, "update-index", "--force-remove", "src/submodule")
                    with self.assertRaises(ContractError):
                        audit_git_delta(repo, allowed_paths=["src"], base_commit=base, head_commit=base)
                    continue
                state, run_worktree = self._start_state(
                    repo, [{"id": "TASK-001", "allowed_paths": ["src"]}]
                )
                state.begin_item("TASK-001")
                git(run_worktree, "rm", "src/link" if kind == "symlink" else "src/submodule")
                state.record_worker_completion("TASK-001", evidence="worker finished")
                with self.assertRaises(ContractError):
                    state.checkpoint_item("TASK-001", evidence="unit ok")

    def test_final_audit_rejects_committed_out_of_scope_delta(self) -> None:
        from scripts.optim_plans_core import ContractError

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            (repo / "src/ok.txt").write_text("ok\n", encoding="utf-8")
            git(repo, "add", "src/ok.txt")
            git(repo, "commit", "-m", "fixture")
            state, run_worktree = self._start_state(repo, [{"id": "TASK-001", "allowed_paths": ["src"]}])

            state.begin_item("TASK-001")
            (run_worktree / "src/ok.txt").write_text("changed\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            state.checkpoint_item("TASK-001", evidence="unit ok")

            (run_worktree / "README.md").write_text("hijack\n", encoding="utf-8")
            git(run_worktree, "add", "README.md")
            git(run_worktree, "commit", "-m", "manual out of scope")
            with self.assertRaises(ContractError):
                state.final_audit()

    def test_interrupted_setup_exact_adopts_or_fails_without_deleting_unknown_data(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Adopt", plan_hash="abc123")
            source_base = git(repo, "rev-parse", "--verify", "HEAD")
            run_branch = f"optim-plans/run/{state.run_id}"
            run_worktree = state.root / "run-worktrees" / state.run_id
            git(repo, "branch", run_branch, source_base)
            git(repo, "worktree", "add", str(run_worktree), run_branch)
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": source_base,
                    "integration_destination": "main",
                    "items": [{"id": "TASK-001", "allowed_paths": ["README.md"]}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            self.assertTrue(state.start_execution(question["nonce"])["run_worktree_adopted"])

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="No Adopt", plan_hash="abc123")
            unknown = state.root / "run-worktrees" / state.run_id
            unknown.mkdir(parents=True)
            sentinel = unknown / "keep.txt"
            sentinel.write_text("do not delete\n", encoding="utf-8")
            state.persist_execution_manifest(
                {
                    "plan_hash": "abc123",
                    "source_base": git(repo, "rev-parse", "--verify", "HEAD"),
                    "integration_destination": "main",
                    "items": [{"id": "TASK-001", "allowed_paths": ["README.md"]}],
                }
            )
            question = state.request_execution_approval()
            state.record_answer(question["nonce"], "approve")
            with self.assertRaises(ContractError):
                state.start_execution(question["nonce"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not delete\n")

    def test_protected_metadata_drift_blocks_checkpoint_without_restoring(self) -> None:
        from scripts.optim_plans_core import ContractError, git_common_dir

        def prepared(raw: str):
            repo = make_repo(Path(raw))
            (repo / "src").mkdir()
            (repo / "src/ok.txt").write_text("ok\n", encoding="utf-8")
            git(repo, "add", "src/ok.txt")
            git(repo, "commit", "-m", "fixture")
            state, run_worktree = self._start_state(repo, [{"id": "TASK-001", "allowed_paths": ["src/ok.txt"]}])
            state.begin_item("TASK-001")
            (run_worktree / "src/ok.txt").write_text("changed\n", encoding="utf-8")
            state.record_worker_completion("TASK-001", evidence="worker finished")
            return state, repo, run_worktree

        cases = {
            "config": lambda state, repo, run: git(repo, "config", "optim-plans.test", "drift"),
            "hook": lambda state, repo, run: (
                (git_common_dir(repo) / "hooks").mkdir(exist_ok=True),
                (git_common_dir(repo) / "hooks/pre-commit").write_text("#!/bin/sh\n", encoding="utf-8"),
            ),
            "worktree": lambda state, repo, run: git(
                repo, "worktree", "add", "--detach", str(Path(run).parent / "other"), "HEAD"
            ),
            "source-ref": lambda state, repo, run: (
                (repo / "README.md").write_text("moved\n", encoding="utf-8"),
                git(repo, "add", "README.md"),
                git(repo, "commit", "-m", "move source"),
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as raw:
                    state, repo, run_worktree = prepared(raw)
                    mutate(state, repo, run_worktree)
                    with self.assertRaises(ContractError):
                        state.checkpoint_item("TASK-001", evidence="unit ok")
                    if name == "config":
                        self.assertEqual(git(repo, "config", "optim-plans.test"), "drift")
                    if name == "hook":
                        self.assertTrue((git_common_dir(repo) / "hooks/pre-commit").exists())


if __name__ == "__main__":
    unittest.main()
