from __future__ import annotations

import unittest


class InteractionTests(unittest.TestCase):
    def test_question_order_and_nonce_validation(self) -> None:
        from scripts.optim_plans_core import ContractError, QuestionLedger

        ledger = QuestionLedger()
        question = ledger.ask(
            "Pick mode",
            recommended=("reviewer", "Reviewer", "fast convergence"),
            alternatives=[("criticizer", "Criticizer", "more challenge")],
        )
        self.assertEqual(question.options[0].id, "reviewer")
        self.assertEqual(question.options[-2].id, "other")
        self.assertEqual(question.options[-1].id, "auto")
        payload = question.to_json(expected_seq=4)
        self.assertEqual(payload["recommended_option_id"], "reviewer")
        self.assertEqual(payload["free_form"], {"option_id": "other", "required": False})
        self.assertEqual(payload["expected_seq"], 4)
        ledger.answer(question.nonce, "reviewer")
        with self.assertRaises(ContractError):
            ledger.answer(question.nonce, "reviewer")

    def test_auto_complete_stops_at_execution_gate(self) -> None:
        from scripts.optim_plans_core import may_auto_answer

        self.assertTrue(may_auto_answer("refinement"))
        self.assertFalse(may_auto_answer("execution_launch"))
        self.assertFalse(may_auto_answer("unknown_future_gate"))

    def test_execution_approval_question_omits_auto_complete(self) -> None:
        from scripts.optim_plans_core import QuestionLedger

        ledger = QuestionLedger()
        question = ledger.ask(
            "Approve execution?",
            recommended=("approve", "Approve execution", "launch exactly this command"),
            alternatives=[("stop", "Stop", "do not launch")],
            allow_auto_complete=False,
        )
        self.assertEqual([option.id for option in question.options], ["approve", "stop", "other"])

    def test_plan_levels_capture_question_bounds(self) -> None:
        from scripts.optim_plans_core import ContractError, PLAN_LEVELS, plan_level

        self.assertEqual(
            [
                (
                    level.name,
                    level.min_questions,
                    level.max_questions,
                    level.min_refinement_rounds,
                    level.max_refinement_rounds,
                    level.refinement_timeout_seconds,
                    level.max_refinement_comments_or_questions,
                    level.direct_execution_option,
                    level.high_priority_only,
                    level.websearch_required_in,
                )
                for level in PLAN_LEVELS
            ],
            [
                ("mini-plan", 1, 1, 0, 1, None, None, True, False, ()),
                ("small-plan", 1, 3, 1, 1, None, None, False, False, ()),
                ("plan", 1, 5, 0, 3, 600, 3, False, True, ()),
                ("big-plan", 5, 10, 0, 5, 1800, 5, False, True, ("brainstorming",)),
                ("huge-plan", 10, None, 0, None, None, 5, False, True, ("brainstorming", "refinement")),
            ],
        )
        self.assertEqual(
            plan_level("plan").to_json(),
            {
                "name": "plan",
                "min_questions": 1,
                "max_questions": 5,
                "min_refinement_rounds": 0,
                "max_refinement_rounds": 3,
                "refinement_timeout_seconds": 600,
                "max_refinement_comments_or_questions": 3,
                "direct_execution_option": False,
                "high_priority_only": True,
                "websearch_required_in": [],
            },
        )
        self.assertEqual(plan_level("huge plan").name, "huge-plan")
        with self.assertRaises(ContractError):
            plan_level("mega-plan")


if __name__ == "__main__":
    unittest.main()
