from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/optim-plans/SKILL.md"


class SkillContractTests(unittest.TestCase):
    def test_skill_entrypoint_is_concise_and_links_references(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertLessEqual(len(text.splitlines()), 180)
        self.assertIn("name: optim-plans", text)
        self.assertRegex(text, r"description: .*Use when")
        for reference in ("planning.md", "refinement.md", "execution.md", "artifacts.md"):
            self.assertIn(f"references/{reference}", text)
            self.assertTrue((ROOT / "skills/optim-plans/references" / reference).is_file())

    def test_choice_ordering_and_execution_gate_are_explicit(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertRegex(text, re.compile("recommended.*first", re.I | re.S))
        self.assertRegex(text, re.compile("Other.*second-last", re.I | re.S))
        self.assertRegex(text, re.compile("Auto-complete.*last", re.I | re.S))
        self.assertIn("Auto-complete never approves execution", text)
        self.assertIn("`skip-refinement-execute`", text)
        self.assertIn("explicit execution approval", text)
        self.assertIn("must not offer `Auto-complete`", text)

    def test_every_user_facing_question_requires_auto_complete(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Every user-facing planning or refinement question must be a choice prompt", text)
        self.assertIn("except the mini-plan `skip-refinement-execute` combined launch question", text)
        self.assertIn("Asking a planning or refinement question without `Auto-complete` as the last option", text)

    def test_refinement_agent_choice_recommends_delegated_foreground_run(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        refinement = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        self.assertIn("delegated foreground run first", text)
        self.assertIn("recommend `Delegated foreground run` first", refinement)
        self.assertIn("Auto-complete uses a standalone visible run", refinement)

    def test_criticizer_questions_gate_plan_revision(self) -> None:
        text = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        self.assertIn("Do not write `PLAN_vN_criticizer_comments.md` or `PLAN_v(N+1).md`", text)
        self.assertIn("until every criticizer question has a recorded user answer", text)

    def test_criticizer_questions_show_criticism_context_first(self) -> None:
        text = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        self.assertIn("Before presenting criticizer-question options", text)
        self.assertIn("original criticism", text)
        self.assertIn("at most three sentences", text)
        self.assertIn("highlight the most important point", text)

    def test_first_turn_forbids_reference_only_direct_edits(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertRegex(text, re.compile("first visible response.*question", re.I | re.S))
        self.assertIn("planning target, not write authorization", text)
        self.assertIn("References inform the recommended option; they never replace the user interview", text)
        self.assertIn("One human-choice answer is necessary but not sufficient", text)

    def test_plan_v1_is_only_pre_refinement_planning_artifact(self) -> None:
        paths = [
            SKILL,
            ROOT / "skills/optim-plans/references/planning.md",
            ROOT / "README.md",
            ROOT / "evals/pressure_cases.json",
        ]
        for path in paths:
            self.assertNotIn("BRAINSTORM.md", path.read_text(encoding="utf-8"), str(path))
        planning = (ROOT / "skills/optim-plans/references/planning.md").read_text(encoding="utf-8")
        self.assertIn("collect evidence before asking unresolved questions", planning)
        self.assertIn("Record that evidence in `PLAN_v1.md`", planning)

    def test_plan_request_levels_are_documented(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for expected in (
            "`mini-plan`: 1 planning question; zero or one refinement round",
            "`small-plan`: 1 to 3 planning questions; exactly one refinement round",
            "`plan`: 1 to 5 planning questions; at most three refinement rounds",
            "`big-plan`: 5 to 10 planning questions; websearch is required for brainstorming",
            "`huge-plan` / `huge plan`: at least 10 planning questions, no maximum; websearch is required for brainstorming and refinement",
            "high-priority",
            "600 seconds",
            "1800 seconds",
        ):
            self.assertIn(expected, text)

    def test_normal_calls_auto_select_plan_level(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("If no level is named, auto-select", text)
        self.assertIn("smallest level that fits", text)
        self.assertNotIn("If no level is named, use `plan`.", text)

    def test_plan_level_wrapper_skills_exist_for_codex_completion(self) -> None:
        for level in ("mini-plan", "small-plan", "plan", "big-plan", "huge-plan"):
            path = ROOT / "skills" / level / "SKILL.md"
            self.assertTrue(path.is_file(), level)
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"name: {level}", text)
            self.assertIn("../optim-plans/SKILL.md", text)
            self.assertIn(f"`{level}`", text)
            self.assertNotIn("depth preset", text)

    def test_artifact_gate_precedes_target_repo_edits(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("only permitted writes are controller state and `docs/optim-plans/YYYY-MM-DD-topic/` artifacts", text)
        self.assertIn("before editing target files", text)
        self.assertIn("Treating one answered planning question or ordinary refinement question as execution approval", text)

    def test_execution_approval_forbids_auto_complete_option(self) -> None:
        text = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        self.assertIn("Do not offer `Auto-complete`", text)
        self.assertIn("execution approval", text)

    def test_execution_finish_wrap_is_required_after_execution(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        execution = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        self.assertIn("finish wrap", skill)
        self.assertIn("After every item is verified and the final audits pass", execution)
        self.assertIn("awaiting_integration", execution)
        self.assertIn("integrated", execution)
        self.assertIn("pr-opened", execution)
        self.assertIn("kept", execution)
        self.assertIn("discarded", execution)
        self.assertIn("failed", execution)
        self.assertIn("aborted", execution)
        self.assertIn("explicit confirmation", execution)
        self.assertIn("Auto-complete cannot approve finish-wrap", execution)

    def test_execution_contract_matches_manifest_bound_controller_lifecycle(self) -> None:
        execution = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        for expected in (
            "immutable execution manifest",
            "manifest hash",
            "single-use human approval",
            "prepare-execution",
            "start-execution",
            "run-item",
            "retry-item",
            "finish-run",
            "adapter",
            "argv array",
            "`shell=False`",
            "`--agent`",
            "`optim-plans-executor`",
            "foreground standalone agent",
            "not `--bg`",
            "not a hidden background subagent",
            "one controller-owned run worktree and run branch",
            "serial topological order",
            "verified checkpoint commit",
            "controller runs the verification argv",
            "path allowlists",
            "protected Git metadata",
            "explicit retry approval",
            "repository-integrity detection and integration gating",
            "not host confinement",
            "defense in depth",
        ):
            self.assertIn(expected, execution)
        self.assertNotIn("--worker-command", execution)
        self.assertNotIn("Run one fresh foreground worker", execution)
        self.assertNotIn("future layers", execution)

    def test_readme_removes_unsafe_direct_execution_examples(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Use the controller directly", readme)
        self.assertIn("prepare-execution", readme)
        self.assertIn("start-execution", readme)
        self.assertIn("run-item", readme)
        self.assertIn("finish-run", readme)
        self.assertIn("awaiting_integration", readme)
        self.assertNotIn("--worker-command", readme)
        self.assertNotIn("python3 scripts/optim_plans.py run-worker", readme)
        self.assertIn("Repository-integrity detection and integration gating", readme)
        self.assertIn("does not provide host confinement", readme)

    def test_eval_pressure_cases_exist(self) -> None:
        cases = ROOT / "evals/pressure_cases.json"
        self.assertTrue(cases.is_file())
        text = cases.read_text(encoding="utf-8")
        self.assertIn("execution_gate", text)
        self.assertIn("reference_only_direct_edit", text)
        self.assertIn("single_question_direct_execution", text)
        self.assertIn("missing_auto_complete_option", text)


if __name__ == "__main__":
    unittest.main()
