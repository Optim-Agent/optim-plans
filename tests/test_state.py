from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import make_repo


class StateTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys(self) -> None:
        from scripts.optim_plans_core import ContractError, parse_json_strict

        with self.assertRaises(ContractError):
            parse_json_strict('{"a":1,"a":2}', source="dup")

    def test_run_state_lives_in_git_common_dir_and_replays_events(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="New Feature", plan_hash="abc123")
            self.assertTrue(str(state.run_dir).startswith(str(repo / ".git" / "optim-plans")))
            self.assertEqual(os.stat(state.root).st_mode & 0o077, 0)
            run = json.loads(state.run_file.read_text())
            self.assertEqual(run["topic"], "New Feature")
            self.assertEqual(run["plan_level"]["name"], "plan")
            state._require_plan_level("plan")
            with self.assertRaisesRegex(ContractError, "plan_level"):
                state._require_plan_level("deep-research-plan")

            first = state.append_event("pending_question", {"nonce": "n1"})
            second = state.append_event("answer_recorded", {"nonce": "n1"})
            replayed = state.replay()
            self.assertEqual([event["seq"] for event in replayed.events], [1, 2])
            self.assertEqual(replayed.status, "planning")

            with self.assertRaisesRegex(Exception, "active run"):
                OptimPlansState.initialize(repo, topic="Other", plan_hash="abc123")

    def test_load_alias_matches_load_active(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            with self.assertRaisesRegex(ContractError, "no active optim-plans run"):
                OptimPlansState.load(repo)
            with self.assertRaisesRegex(ContractError, "no active optim-plans run"):
                OptimPlansState.load_active(repo)

            state = OptimPlansState.initialize(repo, topic="Alias", plan_hash="abc123")
            loaded = OptimPlansState.load(repo)
            active = OptimPlansState.load_active(repo)
            self.assertEqual(loaded.run_id, state.run_id)
            self.assertEqual(loaded.run_id, active.run_id)
            self.assertEqual(loaded.artifact_dir, active.artifact_dir)

    def test_terminal_lifecycle_cannot_regress(self) -> None:
        from scripts.optim_plans_core import lifecycle_status

        events = [
            {"type": "run_finished", "payload": {"outcome": "failed"}},
            {"type": "pending_question", "payload": {"stage": "execution_launch"}},
        ]

        self.assertEqual(lifecycle_status(events), "failed")

    def test_append_event_is_locked_across_processes(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Concurrent", plan_hash="abc123")
            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "from scripts.optim_plans_core import OptimPlansState\n"
                "OptimPlansState.load_active(Path(sys.argv[1])).append_event('tick', {'worker': sys.argv[2]})\n"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), str(index)],
                    cwd=Path(__file__).resolve().parents[1],
                )
                for index in range(8)
            ]
            for worker in workers:
                self.assertEqual(worker.wait(timeout=10), 0)
            replayed = state.replay()
            self.assertEqual([event["seq"] for event in replayed.events], list(range(1, 9)))
            self.assertTrue(state.lock_file.is_file())

    def test_answer_nonce_consumption_is_atomic(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Answers", plan_hash="abc123")
            state.append_event("pending_question", {"nonce": "n1", "options": [{"id": "reviewer"}]})
            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "from scripts.optim_plans_core import OptimPlansState\n"
                "state = OptimPlansState.load_active(Path(sys.argv[1]))\n"
                "state.record_answer(sys.argv[2], 'reviewer')\n"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(repo), "n1"],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(2)
            ]
            codes = sorted(worker.wait(timeout=10) for worker in workers)
            self.assertEqual(codes, [0, 1])
            answers = [event for event in state.replay().events if event["type"] == "answer_recorded"]
            self.assertEqual(len(answers), 1)

    def test_replay_rejects_sequence_gaps(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState, append_json_line

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Gap", plan_hash="abc123")
            append_json_line(state.events_file, {"schema": 1, "seq": 2, "type": "bad", "time": "x"})
            with self.assertRaises(ContractError):
                state.replay()

    def _write_deep_ref_files(self, repo: Path, ref_id: str) -> tuple[str, str]:
        local = repo / "refs" / ref_id
        local.mkdir(parents=True)
        graph = local / "graph.json"
        graph.write_text('{"nodes":[]}\n', encoding="utf-8")
        return local.relative_to(repo).as_posix(), graph.relative_to(repo).as_posix()

    def _record_ready_ref(self, state, repo: Path, ref_id: str) -> None:
        local, graph = self._write_deep_ref_files(repo, ref_id)
        state.record_deep_ref(
            {
                "ref_id": ref_id,
                "name": ref_id,
                "url": f"https://example.invalid/{ref_id}",
                "commit": "abc123",
                "kind": "project",
                "local_path": local,
            }
        )
        state.record_deep_ref_graph({"ref_id": ref_id, "graph_json_path": graph, "coverage": "full", "backend": "graphify"})
        state.record_deep_ref_analysis({"ref_id": ref_id, "analysis_artifact": "README.md"})
        for index in range(3):
            question = state.request_deep_ref_adoption(
                {"ref_id": ref_id, "claim": f"{ref_id}-claim-{index}", "evidence_path": "README.md"}
            )
            state.record_answer(question["nonce"], "accept")

    def test_deep_research_projection_replays_ready_refs(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Deep", plan_hash="abc123", plan_level_name="deep-research-plan")

            for ref_id in ("r1", "r2", "r3"):
                self._record_ready_ref(state, repo, ref_id)

            projection = state.deep_research_projection()
            self.assertTrue(projection["required"])
            self.assertTrue(projection["ready"])
            self.assertEqual(projection["ref_count"], 3)
            self.assertEqual([ref["adoption_answer_count"] for ref in projection["refs"]], [3, 3, 3])
            registered = state.register_plan(repo / "README.md", 1)
            self.assertTrue(registered["deep_research_ready"])

    def test_deep_research_projection_reports_adversarial_blockers(self) -> None:
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Deep", plan_hash="abc123", plan_level_name="deep-research-plan")
            local, graph = self._write_deep_ref_files(repo, "r1")
            bad_graph = repo / "refs" / "r1" / "bad.json"
            bad_graph.write_text("{bad json", encoding="utf-8")

            state.append_event(
                "deep_ref_recorded",
                {
                    "ref_id": "r1",
                    "name": "r1",
                    "url": "https://example.invalid/r1",
                    "commit": "abc123",
                    "kind": "project",
                    "local_path": local,
                },
            )
            state.append_event("deep_ref_recorded", {"ref_id": "r1", "local_path": local})
            state.append_event("deep_ref_recorded", {"ref_id": "r2", "name": "r2", "url": "u", "commit": "c", "kind": "project", "local_path": "/tmp/outside"})
            state.append_event("deep_ref_graphified", {"ref_id": "r1", "graph_json_path": graph, "coverage": "full", "backend": "graphify", "commit": "wrong"})
            state.append_event("deep_ref_graphified", {"ref_id": "r1", "graph_json_path": bad_graph.relative_to(repo).as_posix(), "coverage": "full", "backend": "graphify"})
            state.append_event("deep_ref_analyzed", {"ref_id": "r1", "analysis_artifact": "missing.md"})
            state.append_event("deep_ref_waiver_recorded", {"ref_id": "r1", "waiver_type": "graphify-install-refused", "coverage": "coverage-sufficient", "answer_nonce": "missing"})
            for nonce in ("n1", "n2"):
                state.append_event(
                    "pending_question",
                    {
                        "nonce": nonce,
                        "stage": "deep-research-adoption",
                        "source_ref": "r1",
                        "claim": "duplicate",
                        "evidence_path": "README.md",
                        "options": [{"id": "accept"}],
                    },
                )
                state.append_event("answer_recorded", {"nonce": nonce, "choice": "accept"})

            blockers = "\n".join(state.deep_research_projection()["blockers"])
            for expected in (
                "duplicate ref_id",
                "must stay inside the repo",
                "graph commit mismatch",
                "bad.json",
                "analysis_artifact does not exist",
                "waiver answer nonce",
                "duplicate adoption claim",
                "at least 3 credible refs",
            ):
                self.assertIn(expected, blockers)


if __name__ == "__main__":
    unittest.main()
