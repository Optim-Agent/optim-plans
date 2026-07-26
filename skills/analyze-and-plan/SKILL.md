---
name: analyze-and-plan
description: MUST USE when investigating a bug, CI failure, test failure, regression, incident, broken behavior, root cause, RCA, or debug-why problem before deciding whether to create an optim-plans solution plan; do not use for ordinary feature ideas, vague product planning, direct implementation-only requests, factual/explanation questions, trivial operations, or explicit no-plan requests.
---

# Analyze and Plan

Use this skill for problem/failure inputs that need diagnosis before planning. Do not use it for ordinary feature planning, direct implementation-only requests, factual/explanation questions, trivial operations, or explicit no-plan requests.

## Workflow

1. Inspect available evidence first: repo files, logs, tests, command output, or user-provided symptoms.
2. Produce an in-message RCA summary before asking whether to plan. Use at most 5 Whys. Stop with `unknown` when evidence is insufficient; do not invent a cause.
3. Ask one choice prompt that includes the RCA summary. The choices are: recommended first to call `optim-plans`, second to review the summary only without a plan, `Other` second-last, and `Auto-complete` last.
4. On opt-out, stop after the summary. Do not write `PROBLEM_ANALYSIS.md` on opt-out.
5. On opt-in or Auto-complete choosing the recommendation, select the smallest fitting planning level, read and follow `../{selected-level}/SKILL.md`, and preserve that skill's first-turn contract.
6. Pass the original problem, RCA summary, evidence, and `PROBLEM_ANALYSIS.md` artifact intent as the planning request. The selected planning skill must initialize controller state and present and record its first controller-backed planning question before `PROBLEM_ANALYSIS.md` is written.
7. After opt-in, selected planning skill first controller-backed planning question recording, and selected run `artifact_dir` availability, write `PROBLEM_ANALYSIS.md` in that `artifact_dir` before `PLAN_v1.md`.

## Level Selection

- `mini-plan`: single-file, low-risk, clear root cause, obvious fix.
- `small-plan`: a few local files or behavior checks, limited risk.
- `plan`: normal multi-file fix with several decisions.
- `big-plan`: architecture boundary, external API/dependency, compatibility, or websearch risk.
- `huge-plan`: open-ended or cross-system issue where the RCA points to redesign.

## PROBLEM_ANALYSIS.md

Include:

- Original problem
- Evidence inspected
- RCA summary
- 5 Whys, ending early with `unknown` when evidence stops
- Suspected root cause
- Planning level selected and why

Auto-complete may opt into planning, but Auto-complete cannot approve execution.
