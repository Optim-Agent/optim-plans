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
        from scripts.optim_plans_core import OptimPlansState

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="New Feature", plan_hash="abc123")
            self.assertTrue(str(state.run_dir).startswith(str(repo / ".git" / "optim-plans")))
            self.assertEqual(os.stat(state.root).st_mode & 0o077, 0)
            self.assertEqual(json.loads(state.run_file.read_text())["topic"], "New Feature")

            first = state.append_event("pending_question", {"nonce": "n1"})
            second = state.append_event("answer_recorded", {"nonce": "n1"})
            replayed = state.replay()
            self.assertEqual([event["seq"] for event in replayed.events], [1, 2])
            self.assertEqual(replayed.status, "planning")

            with self.assertRaisesRegex(Exception, "active run"):
                OptimPlansState.initialize(repo, topic="Other", plan_hash="abc123")

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


if __name__ == "__main__":
    unittest.main()
