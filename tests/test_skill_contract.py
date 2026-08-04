from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/optim-plans/SKILL.md"
ANALYZE_SKILL = ROOT / "skills/analyze-and-plan/SKILL.md"
RESUME_SKILL = ROOT / "skills/resume-previous-plan/SKILL.md"
RESEARCH_SKILL = ROOT / "skills/research-and-plan/SKILL.md"
SEARCH_SKILL = ROOT / "skills/search-and-plan/SKILL.md"
DEEP_RESEARCH_SKILL = ROOT / "skills/deep-research-plan/SKILL.md"


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
                for level in ("mini-plan", "small-plan", "plan", "big-plan", "huge-plan", "deep-research-plan")
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
            "deep-research-plan": ("downloaded projects", "graphify json"),
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
        self.assertIn("`.git/optim-plans/config.json` top-level `language` field", text)
        self.assertIn("Run controller `init` with the original request in `--request-text`", text)
        self.assertIn("the controller's `language-selection` question", text)
        self.assertIn("`zh-hans`, `en`, `zh-hant`, `other`, `auto`", text)
        self.assertIn("`language_value` metadata (`zh-Hans`, `en`, `zh-Hant`)", text)
        self.assertIn("more than 60% of the user's planning request's natural-language body", text)
        self.assertIn("Use the selected language for questioning, review summaries, criticizer questions, answer choices", text)
        self.assertIn("optim-plans Markdown under `docs/optim-plans/`", text)
        self.assertIn("`> 60%` threshold: ignore command prefixes, option IDs, file paths, and code spans", text)
        self.assertIn("localize visible prompt text, option labels, option descriptions/reasons", text)
        self.assertIn("controller-backed questions", text)
        self.assertIn("Valid unsupported BCP47-style tags fall back to English renderers", text)
        self.assertIn("primary subtag is exactly `zh`", text)
        self.assertIn("Always write commit messages in English", text)

    def test_plan_request_levels_are_documented(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for expected in (
            "`mini-plan`: 1 planning question; zero or one refinement round",
            "`small-plan`: 1 to 3 planning questions; exactly one refinement round",
            "`plan`: 1 to 5 planning questions; at most three refinement rounds",
            "`big-plan`: 5 to 10 planning questions; websearch is required for brainstorming",
            "`huge-plan` / `huge plan`: at least 10 planning questions, no maximum; websearch is required for brainstorming and refinement",
            "`deep-research-plan` / `deep research plan`: `huge-plan` depth plus",
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
        for level in ("mini-plan", "small-plan", "plan", "big-plan", "huge-plan", "deep-research-plan"):
            path = ROOT / "skills" / level / "SKILL.md"
            self.assertTrue(path.is_file(), level)
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"name: {level}", text)
            self.assertIn("../optim-plans/SKILL.md", text)
            self.assertIn(f"`{level}`", text)
            self.assertNotIn("depth preset", text)

    def test_resume_previous_plan_skill_resumes_active_recovery_first(self) -> None:
        self.assertTrue(RESUME_SKILL.is_file())
        text = RESUME_SKILL.read_text(encoding="utf-8")
        description = skill_description(RESUME_SKILL).lower()
        self.assertIn("resume", description)
        self.assertIn("interrupted", description)
        self.assertIn("automatically run", description)
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
            "retryable execution recovery until the controller reports `blocked`",
            "Do not approve execution launch",
            "run `resume_command`",
            "restore an active pointer",
            "there is no unambiguous resume outcome",
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
        self.assertIn("build a foreground executor worker block", execution)
        self.assertIn("returns the manifest-bound assignment and launch block", execution)

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
            "authorize-validator-spawn",
            "register-validator",
            "complete-validator",
            "authorize-batch-validator-spawn",
            "register-batch-validator",
            "complete-batch-validator",
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
            "current Claude executors",
            "`run-item` starts the `optim-plans-executor` with `--agent` as a foreground standalone subagent",
            "waits synchronously for the adapter process",
            "stdout JSON envelope",
            "current Claude executor manifests",
            "`run-item` synchronous wait/stdout",
            "The legacy `background` choice does not mean current Claude host/background execution",
            "foreground standalone agent",
            "not Codex-style `wait_agent`",
            "not `--bg`",
            "not a hidden background subagent",
            "not host/background mode",
            "not background notification/outputFile wait",
            "one controller-owned run worktree and run branch",
            "serial topological order",
            "verified checkpoint commit",
            "controller runs the verification argv",
            "path allowlists",
            "protected Git metadata",
            "ignored_audit_noise",
            "`.xsw/`",
            "`*.pyc`",
            "auto-restore and continue until success or `blocked`",
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
        self.assertIn("Codex host-multi-agent executor/validator path", readme)
        self.assertIn("run-item", readme)
        self.assertIn("current Claude executor path: foreground standalone --agent", readme)
        self.assertIn("synchronous run-item wait/stdout", readme)
        self.assertIn("no host wait_agent, --bg, hidden background subagent, host/background mode, or notification/outputFile wait", readme)
        self.assertIn("Current Claude executors use the CLI adapter path", readme)
        self.assertIn("waits synchronously, and reads stdout JSON", readme)
        self.assertIn("They do not use host `spawn_agent` / `wait_agent`, `--bg`, hidden background subagents, host/background mode, or notification/outputFile waits", readme)
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

    def test_skill_distinguishes_codex_host_and_claude_adapter_execution(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for expected in (
            "Current Codex executor/validator delegation uses host-multi-agent mode",
            "host `spawn_agent`",
            "host `wait_agent`",
            "Current Claude executor delegation uses the CLI adapter path",
            "`run-item` launches the `optim-plans-executor` with `--agent` as a foreground standalone subagent",
            "waits synchronously, and reads stdout JSON",
            "not Codex-style `wait_agent`",
            "host/background mode",
            "notification/outputFile wait",
        ):
            self.assertIn(expected, text)

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

    def test_search_and_plan_contract(self) -> None:
        self.assertTrue(SEARCH_SKILL.is_file())
        text = SEARCH_SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: search-and-plan", text)
        self.assertIn("../optim-plans/SKILL.md", text)
        for expected in (
            "Inspect the target Git repo read-only before asking product questions",
            "Initialize or resume the optim-plans controller before external research",
            "Perform read-only initial research",
            "Ask the first evidence-informed question as a controller-backed optim-plans choice prompt",
            "submit/record that answer through the controller before writing refs",
            "docs/optim-plans/YYYY-MM-DD-topic/refs/search-and-plan/<topic>/",
            "Ask any later product questions only after refs are persisted",
            "`PLAN_v1.md`, refinement, immutable execution approval, execution, validation, controller verification, and integration gates",
            "`mini-plan`, `small-plan`, `plan`, `big-plan`, and `huge-plan` routing",
            "Prefer `agent-reach` when available",
            "if missing, give one sentence of install guidance",
            "do not install `agent-reach` or any other tool before execution approval",
            "continue with fallback search/repo evidence",
            "Record attempted queries, attempted backends, backend failures with reasons, why sources were insufficient",
            "continue from repository evidence",
            "Backend failure fallback is valid only when the failure reason is recorded",
            "Do not write pre-execution `./refs/` files",
            "Do not require a strict source manifest before planning",
            "3-7 high-signal source pack",
            "Every adoptable idea must be presented as an evidence-backed optim-plans choice prompt",
            "recorded through the controller before it can be included in any plan",
        ):
            self.assertIn(expected, text)
        for section in (
            "## Sources",
            "## Findings",
            "## Adoptable ideas",
            "## Risks/not-applicable points",
            "## Evidence gaps",
            "## Candidate user decisions",
        ):
            self.assertIn(f"`{section}`", text)

    def test_deep_research_plan_contract(self) -> None:
        self.assertTrue(DEEP_RESEARCH_SKILL.is_file())
        text = DEEP_RESEARCH_SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        for expected in (
            "name: deep-research-plan",
            "../optim-plans/SKILL.md",
            "treating the request as if it named `deep-research-plan`",
            "root `.gitignore` ignores `refs/`",
            "Prefer `agent-reach` when available",
            "If `agent-reach` is missing, ask exactly one yes/no sentence",
            "Download at least 3 relevant refs into `./refs/<topic>/`",
            "`deep-record-ref`",
            "`deep-record-graph`",
            "`deep-record-analysis`",
            "Do not satisfy this with `curl` alone",
            "README-only snapshots",
            "abstract-only paper notes",
            "generate graphify JSON beside the ref",
            "If `graphify` is missing, ask exactly one yes/no sentence",
            "nonce-bound graphify waiver",
            "`deep-waiver-question`",
            "`deep-record-waiver`",
            "at least 3 ref-specific controller-backed choice questions",
            "`deep-adoption-question`",
            "`register-plan --path docs/optim-plans/.../PLAN_v1.md --version 1`",
            "deep-research readiness blocker",
            "recommended option first",
            "`Other` second-last",
            "`Auto-complete` last",
            "at least 10 planning questions",
            "Block rather than pad when fewer than 3 credible refs exist",
        ):
            self.assertIn(expected, text)

    def test_research_and_plan_alias_points_to_search_and_plan(self) -> None:
        self.assertTrue(RESEARCH_SKILL.is_file())
        text = RESEARCH_SKILL.read_text(encoding="utf-8")
        self.assertIn("name: research-and-plan", text)
        self.assertIn("Alias for search-and-plan", text)
        self.assertIn("../search-and-plan/SKILL.md", text)

    def test_readme_documents_search_and_plan_method(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (
            "$optim-plans:search-and-plan",
            "first evidence-informed controller-backed choice",
            "REF_ANALYSIS.md",
            "3-7 source pack",
            "docs/optim-plans/YYYY-MM-DD-topic/refs/search-and-plan/<topic>/",
            "prefers `agent-reach`",
            "does not install tools before execution approval",
            "records backend failures/evidence gaps",
            "Adoptable ideas must be accepted through evidence-backed optim-plans choice prompts",
            "skills/search-and-plan/SKILL.md",
        ):
            self.assertIn(expected, readme)

    def test_readme_documents_deep_research_plan_method(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (
            "$optim-plans:deep-research-plan",
            "stronger-than-huge",
            "downloads at least 3 refs into ignored `./refs/`",
            "`agent-reach`",
            "`graphify` JSON",
            "`deep-record-ref`",
            "nonce-bound `deep-adoption-question`",
            "nonce-bound waiver",
            "`register-plan --version 1`",
            "`prepare-execution` rechecks",
            "at least 3 ref-specific controller-backed questions",
            "not allowed to rely only on `curl`, README files, or abstracts",
            "skills/deep-research-plan/SKILL.md",
        ):
            self.assertIn(expected, readme)

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
