from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import make_repo


class ArtifactTests(unittest.TestCase):
    def test_artifact_directories_do_not_overwrite_same_day_topics(self) -> None:
        from scripts.optim_plans_core import create_artifact_dir

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            first = create_artifact_dir(repo, "My Plan", date="2026-07-23")
            second = create_artifact_dir(repo, "My Plan", date="2026-07-23")
            self.assertEqual(first.name, "2026-07-23-my-plan")
            self.assertEqual(second.name, "2026-07-23-my-plan-2")

    def test_plan_comments_and_execution_report_cover_ids(self) -> None:
        from scripts.optim_plans_core import (
            PlanItem,
            render_comments,
            render_execution_results,
            render_plan,
        )

        items = [
            PlanItem(
                "REQ-001",
                "Support cards",
                "deterministic command",
                "test_cards",
                acceptance="Cards are ordered",
                allowed_paths=["skills/"],
                verifier_criterion_id="VC-001",
                verifier_covered_item_ids=["REQ-001"],
                verifier_pass_condition="Cards are ordered",
                verifier_metric_threshold="100% of rendered card options preserve order",
            ),
            PlanItem(
                "TASK-001",
                "Implement cards",
                "deterministic command",
                "python -m unittest",
                acceptance="Tests pass",
                allowed_paths=["scripts/"],
                verifier_criterion_id="VC-002",
                verifier_covered_item_ids=["TASK-001"],
                verifier_pass_condition="Tests pass",
                verifier_non_quantification="Binary unit-test result is sufficient",
            ),
        ]
        plan = render_plan(
            "Goal",
            items,
            version=1,
            repo_evidence=["tests/test_artifacts.py"],
            resolved_decisions=["Use PLAN_v1.md only"],
        )
        self.assertIn("REQ-001", plan)
        self.assertIn("Acceptance", plan)
        self.assertIn("Allowed paths", plan)
        self.assertIn("Repo evidence", plan)
        self.assertIn("tests/test_artifacts.py", plan)
        self.assertIn("Resolved decisions", plan)
        self.assertIn("Use PLAN_v1.md only", plan)
        self.assertIn("Revision ledger", plan)
        self.assertIn("## Verifier Checklist", plan)
        self.assertIn("- [ ] VC-001 | Covered: REQ-001 | Pass: Cards are ordered | Evidence: test_cards | Metric threshold: 100% of rendered card options preserve order", plan)
        self.assertIn("- [ ] VC-002 | Covered: TASK-001 | Pass: Tests pass | Evidence: python -m unittest | Non-quantification: Binary unit-test result is sufficient", plan)
        comments = render_comments("reviewer", 1, [{"id": "F-001", "fix": "Add nonce check"}])
        self.assertIn("PLAN_v1_reviewer_comments", comments)
        report = render_execution_results(
            items,
            {"REQ-001": {"status": "verified", "attempts": 1, "limitations": "none"}},
            base_commit="base",
            final_commit="final",
            agent_config="codex/default",
            final_audit="passed",
        )
        self.assertIn("TASK-001", report)
        self.assertIn("missing", report)
        self.assertIn("Base commit: base", report)
        self.assertIn("Final audit: passed", report)
        self.assertIn("Changed files", report)
        self.assertIn("Commits", report)


if __name__ == "__main__":
    unittest.main()
