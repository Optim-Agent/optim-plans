from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from helpers import make_repo
except ModuleNotFoundError:
    from tests.helpers import make_repo


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

    def test_generic_question_reserved_names_are_rejected(self) -> None:
        from scripts.optim_plans_core import ContractError, validate_generic_question

        validate_generic_question("review-and-plan", "OPP-003", ["accept", "defer", "reject"])
        for stage in ("execution_launch", "execution_retry", "finish_run", "agent-choice", "background-model", "language-selection"):
            with self.subTest(stage=stage), self.assertRaises(ContractError):
                validate_generic_question(stage, "OPP-003", ["accept"])
        for option_id in ("skip-refinement-execute", "approve", "auto", "agent-choice"):
            with self.subTest(option_id=option_id), self.assertRaises(ContractError):
                validate_generic_question("review-and-plan", "OPP-003", [option_id])

    def test_language_selection_question_and_answer_contract(self) -> None:
        from scripts.optim_plans_core import OptimPlansState, read_config_language

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(
                repo,
                topic="Language",
                plan_hash="abc123",
                request_text="请帮我规划这个功能，需要中文问题和中文计划。",
            )
            state.append_event("initialized", {"topic": "Language"})
            question = state.ensure_language_selection()

            self.assertIsNotNone(question)
            assert question is not None
            self.assertEqual(question["stage"], "language-selection")
            self.assertEqual(question["recommended_option_id"], "zh-hans")
            self.assertEqual([option["id"] for option in question["options"]], ["zh-hans", "en", "zh-hant", "other", "auto"])
            self.assertEqual(question["options"][0]["language_value"], "zh-Hans")
            self.assertEqual(question["options"][1]["language_value"], "en")
            self.assertEqual(question["options"][2]["language_value"], "zh-Hant")
            self.assertEqual(state.ensure_language_selection()["nonce"], question["nonce"])

            state.record_answer(question["nonce"], "auto")

            self.assertEqual(read_config_language(repo), "zh-Hans")
            self.assertIsNone(state.ensure_language_selection())

    def test_language_other_keeps_gate_open(self) -> None:
        from scripts.optim_plans_core import OptimPlansState, read_config_language

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Language", plan_hash="abc123", request_text="Use English.")
            state.append_event("initialized", {"topic": "Language"})
            question = state.ensure_language_selection()
            assert question is not None

            self.assertEqual(state.record_answer(question["nonce"], "other")["choice"], "other")
            self.assertIsNone(read_config_language(repo))
            follow_up = state.ensure_language_selection(force=True)
            assert follow_up is not None
            self.assertEqual(follow_up["stage"], "language-selection")
            self.assertNotEqual(follow_up["nonce"], question["nonce"])

    def test_language_tag_normalization_boundaries(self) -> None:
        from scripts.optim_plans_core import language_renders_chinese, normalize_language_tag

        self.assertEqual(normalize_language_tag("zh-hans"), "zh-Hans")
        self.assertEqual(normalize_language_tag("en-us"), "en-US")
        self.assertEqual(normalize_language_tag("zh-hant-tw"), "zh-Hant-TW")
        self.assertEqual(normalize_language_tag("es-419"), "es-419")
        for value in ("-en", "en--US", "en-", "1", "", " en", "en_US", "en-a-b-c-d-e-f-g-h"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_language_tag(value))
        self.assertFalse(language_renders_chinese("zho"))
        self.assertFalse(language_renders_chinese("zhx"))

    def test_language_answer_write_failure_order_and_retry(self) -> None:
        from scripts.optim_plans_core import ContractError, OptimPlansState, optim_plans_config_path

        with tempfile.TemporaryDirectory() as raw:
            repo = make_repo(Path(raw))
            state = OptimPlansState.initialize(repo, topic="Language", plan_hash="abc123", request_text="Use English.")
            state.append_event("initialized", {"topic": "Language"})
            question = state.ensure_language_selection()
            assert question is not None

            with mock.patch("scripts.optim_plans_core.save_optim_plans_config_value", side_effect=OSError("config boom")):
                with self.assertRaises(OSError):
                    state.record_answer(question["nonce"], "en")

            self.assertFalse(
                any(
                    event["type"] == "answer_recorded" and event.get("payload", {}).get("nonce") == question["nonce"]
                    for event in state.replay().events
                )
            )

            with mock.patch("scripts.optim_plans_core.append_json_line", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    state.record_answer(question["nonce"], "en")

            self.assertEqual(json.loads(optim_plans_config_path(repo).read_text(encoding="utf-8"))["language"], "en")
            self.assertFalse(
                any(
                    event["type"] == "answer_recorded" and event.get("payload", {}).get("nonce") == question["nonce"]
                    for event in state.replay().events
                )
            )
            with self.assertRaisesRegex(ContractError, "conflicts"):
                state.record_answer(question["nonce"], "zh-hans")

            self.assertEqual(state.record_answer(question["nonce"], "en")["choice"], "en")

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
                ("deep-research-plan", 10, None, 0, None, None, 5, False, True, ("brainstorming", "refinement")),
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
        self.assertEqual(plan_level("deep research plan").name, "deep-research-plan")
        with self.assertRaises(ContractError):
            plan_level("mega-plan")


if __name__ == "__main__":
    unittest.main()
