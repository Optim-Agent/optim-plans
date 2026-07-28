from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/optim-plans/SKILL.md"
ANALYZE_SKILL = ROOT / "skills/analyze-and-plan/SKILL.md"
RESUME_SKILL = ROOT / "skills/resume-previous-plan/SKILL.md"


def skill_description(path: Path) -> str:
    match = re.search(r"^description:\s*(.+)$", path.read_text(encoding="utf-8"), re.M)
    if not match:
        raise AssertionError(f"missing skill description in {path}")
    return match.group(1).strip()


class SkillContractTests(unittest.TestCase):
    def test_skill_entrypoint_is_concise_and_links_references(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertLessEqual(len(text.splitlines()), 180)
        self.assertIn("name: optim-plans", text)
        self.assertRegex(text, r"description: .*(?:MUST USE|Use) when")
        for reference in ("planning.md", "refinement.md", "execution.md", "artifacts.md"):
            self.assertIn(f"references/{reference}", text)
            self.assertTrue((ROOT / "skills/optim-plans/references" / reference).is_file())

    def test_skill_descriptions_target_planning_without_catchalls(self) -> None:
        descriptions = {
            "optim-plans": skill_description(SKILL),
            "analyze-and-plan": skill_description(ANALYZE_SKILL),
            **{
                level: skill_description(ROOT / "skills" / level / "SKILL.md")
                for level in ("mini-plan", "small-plan", "plan", "big-plan", "huge-plan")
            },
        }
        main = descriptions["optim-plans"]
        main_lower = main.lower()
        first_sentence = main_lower.split(".", 1)[0]
        for term in ("plan", "brainstorm", "design", "scope", "review"):
            self.assertIn(term, first_sentence)
        self.assertIn("must use when", first_sentence)
        self.assertIn("repo change", first_sentence)

        banned = ("any request", "every request", "all repo work")
        boundary = ("direct implementation-only", "factual/explanation", "trivial", "explicit no-plan")
        for name, description in descriptions.items():
            lower = description.lower()
            for phrase in boundary:
                self.assertIn(phrase, lower, name)
            for phrase in banned:
                self.assertNotIn(phrase, lower, name)

        wrapper_expectations = {
            "mini-plan": ("low-risk", "one alignment question"),
            "small-plan": ("small repo changes", "up to three"),
            "plan": ("normal repo changes", "bounded"),
            "big-plan": ("broad or risky", "websearch"),
            "huge-plan": ("open-ended", "high-risk"),
        }
        wrappers = {level: descriptions[level] for level in wrapper_expectations}
        self.assertEqual(len(set(wrappers.values())), len(wrappers))
        for level, terms in wrapper_expectations.items():
            description = wrappers[level]
            self.assertEqual(description.count("."), 1, level)
            self.assertLessEqual(len(description.rstrip(".").split()), 45, level)
            for term in terms:
                self.assertIn(term, description.lower(), level)

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
        self.assertIn("Every user-facing planning question must be a choice prompt", text)
        self.assertIn("The refinement mode question is exactly `Reviewer`, `Criticizer`, `Jump to executor`, `Auto-complete`", text)
        self.assertIn("Asking a planning or refinement question without `Auto-complete` as the last option", text)

    def test_refinement_choice_uses_mode_prompt_then_first_agent_choice(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        refinement = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        self.assertIn("recommend `Reviewer` first", text)
        self.assertIn("`Criticizer` second", refinement)
        self.assertIn("`Jump to executor` third", refinement)
        self.assertIn("then ask for the detected agent and effort", refinement)
        self.assertIn("recommend `Delegated foreground run` first", refinement)
        self.assertIn("same-platform delegated worker first", refinement)
        self.assertIn("offer only that platform's default and manual options", refinement)
        self.assertIn("Do not offer the opposite platform", refinement)
        self.assertIn("same worker role", refinement)
        self.assertIn("`refinement_worker.choice`", refinement)
        self.assertIn("`executor_worker.choice`", refinement)
        self.assertIn("`validator_worker.choice`", text)

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

    def test_plan_revision_copies_previous_plan_then_edits_new_version(self) -> None:
        text = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            re.compile(
                r"`cp PLAN_vN\.md PLAN_v\(N\+1\)\.md`[^\n]+"
                r"edit only `PLAN_v\(N\+1\)\.md`[^\n]+"
                r"reviewer/criticizer comments"
            ),
        )
        self.assertIn("`PLAN_vN_reviewer_comments.md`", text)
        self.assertIn("`PLAN_vN_criticizer_comments.md`", text)

    def test_plan_versions_require_verifier_checklist(self) -> None:
        planning = (ROOT / "skills/optim-plans/references/planning.md").read_text(encoding="utf-8")
        refinement = (ROOT / "skills/optim-plans/references/refinement.md").read_text(encoding="utf-8")
        for text in (planning, refinement):
            self.assertIn("`## Verifier Checklist`", text)
            self.assertIn("Markdown checkbox", text)
            self.assertIn("criterion ID", text)
            self.assertIn("covered item IDs", text)
            self.assertIn("pass condition", text)
            self.assertIn("evidence", text)
            self.assertRegex(text, re.compile("metric threshold.*non-quantification|not quantified", re.I | re.S))

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


    def test_language_policy_matches_request_language_scope(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("more than 60% of the user's planning request's natural-language body", text)
        self.assertIn("use that language for questioning, review summaries, criticizer questions, answer choices", text)
        self.assertIn("optim-plans Markdown under `docs/optim-plans/`", text)
        self.assertIn("`> 60%` threshold: ignore command prefixes, option IDs, file paths, and code spans", text)
        self.assertIn("localize visible option descriptions/reasons and agent-written choice prose", text)
        self.assertIn("controller-backed questions", text)
        self.assertIn("Always write commit messages in English", text)

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

    def test_resume_previous_plan_skill_is_read_only_and_active_first(self) -> None:
        self.assertTrue(RESUME_SKILL.is_file())
        text = RESUME_SKILL.read_text(encoding="utf-8")
        description = skill_description(RESUME_SKILL).lower()
        self.assertIn("resume", description)
        self.assertIn("interrupted", description)
        self.assertIn("read-only", description)
        for expected in (
            "status --repo",
            "previous-run --repo",
            "no active pointer",
            "Git-common fallback",
            "awaiting_retry_decision",
            "resume_command",
            "retry_command",
            "retry_item_id",
            "finish_approval_nonce",
            "Do not approve execution",
            "run `resume_command`",
            "restore an active pointer",
        ):
            self.assertIn(expected, text)

    def test_analyze_and_plan_problem_flow_contract(self) -> None:
        self.assertTrue(ANALYZE_SKILL.is_file())
        text = ANALYZE_SKILL.read_text(encoding="utf-8")
        description = skill_description(ANALYZE_SKILL).lower()
        for trigger in (
            "bug",
            "ci failure",
            "test failure",
            "regression",
            "incident",
            "broken behavior",
            "root cause",
            "rca",
            "debug",
        ):
            self.assertIn(trigger, description)
        for boundary in (
            "direct implementation-only",
            "factual/explanation",
            "trivial",
            "explicit no-plan",
            "ordinary feature ideas",
            "vague product planning",
        ):
            self.assertIn(boundary, description)
        for banned in ("any request", "every request", "all repo work"):
            self.assertNotIn(banned, description)
        for expected in (
            "in-message RCA summary",
            "PROBLEM_ANALYSIS.md",
            "Do not write `PROBLEM_ANALYSIS.md` on opt-out",
            "first controller-backed planning question",
            "artifact_dir",
            "`PLAN_v1.md`",
            "../{selected-level}/SKILL.md",
            "`mini-plan`",
            "`small-plan`",
            "`plan`",
            "`big-plan`",
            "`huge-plan`",
            "recommended first",
            "`Other` second-last",
            "`Auto-complete` last",
            "Auto-complete cannot approve execution",
        ):
            self.assertIn(expected, text)
        openai_yaml = (ROOT / "skills/analyze-and-plan/agents/openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Analyze and Plan"', openai_yaml)
        self.assertIn('short_description: "Analyze a problem before planning"', openai_yaml)
        self.assertIn("$analyze-and-plan", openai_yaml)

    def test_artifact_gate_precedes_target_repo_edits(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("only permitted writes are controller state and `docs/optim-plans/YYYY-MM-DD-topic/` artifacts", text)
        self.assertIn("before editing target files", text)
        self.assertIn("Treating any answer except `skip-refinement-execute` as execution approval", text)

    def test_execution_approval_forbids_auto_complete_option(self) -> None:
        text = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        self.assertIn("Do not offer `Auto-complete`", text)
        self.assertIn("execution approval", text)

    def test_execution_auto_integrates_after_success_and_manual_finish_is_recovery(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        execution = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        self.assertIn("manual recovery command", skill)
        self.assertIn("After every item is verified and the final audits pass", execution)
        self.assertIn("records `run_finished`", execution)
        self.assertIn("outcome `integrated`", execution)
        self.assertIn("awaiting_integration", execution)
        self.assertIn("finish approval", execution)
        self.assertIn("request_retry", execution)
        self.assertIn("request_finish_approval", execution)
        self.assertIn("integrated", execution)
        self.assertIn("pr-opened", execution)
        self.assertIn("kept", execution)
        self.assertIn("discarded", execution)
        self.assertIn("failed", execution)
        self.assertIn("aborted", execution)
        self.assertIn("full local proof", execution)
        self.assertIn("integration_verification_failed", execution)
        self.assertIn("checked-out destination", execution)
        self.assertIn("explicit confirmation", execution)
        self.assertIn("Auto-complete cannot approve finish-wrap", execution)

    def test_executor_choice_is_role_specific_before_manifest(self) -> None:
        execution = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        self.assertIn("ask `agent-choice --role executor`", execution)
        self.assertIn("default only from `executor_worker.choice`", execution)
        self.assertIn("continue to executor model/effort and `worker-config`", execution)
        self.assertIn("foreground executor execution is unsupported", execution)

    def test_execution_contract_matches_manifest_bound_controller_lifecycle(self) -> None:
        execution = (ROOT / "skills/optim-plans/references/execution.md").read_text(encoding="utf-8")
        for expected in (
            "immutable execution manifest",
            "manifest hash",
            "single-use human approval",
            "prepare-execution",
            "start-execution",
            "run-item",
            "assign-item",
            "assign-batch",
            "authorize-spawn",
            "authorize-batch-spawn",
            "register-agent",
            "register-batch-agent",
            "complete-item",
            "complete-batch",
            "advance-item",
            "advance-batch",
            "host-multi-agent",
            "`spawn_agent`",
            "`wait_agent`",
            "single-use launch nonce",
            "assignment nonce",
            "registered handle",
            "retry-item",
            "retry-batch",
            "all-or-nothing",
            "session resume",
            "finish-run",
            "adapter",
            "argv array",
            "`shell=False`",
            "`--agent`",
            "`optim-plans-executor`",
            "same-platform delegated worker",
            "exact cached smoke-tested worker block",
            "`smoke_tested_workers`",
            "`codex` host -> Codex sub-agent",
            "`claude` host -> Claude sub-agent",
            "foreground standalone agent",
            "not `--bg`",
            "not a hidden background subagent",
            "one controller-owned run worktree and run branch",
            "serial topological order",
            "verified checkpoint commit",
            "controller runs the verification argv",
            "path allowlists",
            "protected Git metadata",
            "explicit retry approval for later retries",
            "repository-integrity detection and integration gating",
            "not host confinement",
            "defense in depth",
        ):
            self.assertIn(expected, execution)
        self.assertNotIn("Workers launch only through a Claude or Codex adapter argv array", execution)
        self.assertNotIn("--worker-command", execution)
        self.assertNotIn("Run one fresh foreground worker", execution)
        self.assertNotIn("future layers", execution)

    def test_readme_removes_unsafe_direct_execution_examples(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Use the controller directly", readme)
        self.assertIn("prepare-execution", readme)
        self.assertIn("start-execution", readme)
        self.assertIn("assign-item", readme)
        self.assertIn("assign-batch", readme)
        self.assertIn("authorize-spawn", readme)
        self.assertIn("authorize-batch-spawn", readme)
        self.assertIn("register-agent", readme)
        self.assertIn("register-batch-agent", readme)
        self.assertIn("complete-item", readme)
        self.assertIn("complete-batch", readme)
        self.assertIn("advance-item", readme)
        self.assertIn("advance-batch", readme)
        self.assertIn("spawn_agent", readme)
        self.assertIn("wait_agent", readme)
        self.assertIn("run-item", readme)
        self.assertIn("retry-batch", readme)
        self.assertIn("all-or-nothing", readme)
        self.assertIn("session resume", readme)
        self.assertIn("finish-run", readme)
        self.assertIn("$optim-plans:resume-previous-plan", readme)
        self.assertIn("previous-run", readme)
        self.assertIn("resume_command", readme)
        self.assertIn("automatic checked-out fast-forward `run_finished` / `integrated`", readme)
        self.assertNotIn("--worker-command", readme)
        self.assertNotIn("python3 scripts/optim_plans.py run-worker", readme)
        self.assertIn("Repository-integrity detection and integration gating", readme)
        self.assertIn("does not provide host confinement", readme)

    def test_readme_documents_review_and_plan_method(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("$optim-plans:review-and-plan", readme)
        for expected in (
            "Documentation",
            "Efficiency/cost",
            "Feature completeness",
            "UX",
            "Safety/recoverability",
            "GitHub open-source project search",
            "repository URL",
            "supported claim",
            "one opportunity question at a time",
            "planning-only controller-backed choice",
            "Accepted opportunity IDs",
        ):
            self.assertIn(expected, readme)
        example = readme.split("<summary><strong>Detailed example: generic Python CLI project</strong></summary>", 1)[1].split("</details>", 1)[0]
        self.assertIn("<details>", readme)
        self.assertIn("generic Python CLI project", example)
        self.assertIn("OPP-003", example)
        self.assertIn("start/resume/terminate", example)

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
