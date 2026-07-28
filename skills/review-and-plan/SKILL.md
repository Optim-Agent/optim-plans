---
name: review-and-plan
description: Use when the user asks Codex to review a repository for improvement opportunities, feature inspiration, roadmap ideas, or implementation candidates before planning changes; exclude direct implementation-only, factual/explanation, trivial, or explicit no-plan requests.
---

# Review And Plan

Discover opportunities first; implementation still goes through `../optim-plans/SKILL.md`.

## Required Flow

1. Inspect the target Git repo before asking product questions. Read the app shape, docs, tests, dependency files, entry points, recent change notes, and any obvious UX or safety-critical paths.
2. Search GitHub open-source projects for feature inspiration before ranking opportunities. Use `gh search repos` / `gh search code` when available; if `gh` is unavailable or unauthenticated, use the GitHub REST/search API or web search constrained to GitHub repository pages. Record sources either way.
3. Produce a ranked opportunity list grouped exactly by:
   - Documentation
   - Efficiency/cost
   - Feature completeness
   - UX
   - Safety/recoverability
4. For each opportunity, include stable fields:
   - `id`: stable ID such as `OPP-001`
   - `category`: one group above
   - `rank`: rank within category or overall list
   - `title`
   - `repo_evidence`: local file/behavior evidence
   - `repository_url`: GitHub source repository URL
   - `supported_claim`: concise claim supported by that source
   - `source_tool`: `gh`, `github-api`, or `web`
5. Ask one case-by-case opportunity question at a time before any accepted opportunity enters implementation scope. Use only planning-stage generic controller-backed choice questions.
6. Each opportunity question must recommend one disposition first: `accept`, `defer`, or `reject`, with evidence. Offer the other dispositions as alternatives, then `Other` second-last, then `Auto-complete` last.
7. Treat `Auto-complete` as acceptance of the recommended planning disposition only. It never approves execution.
8. Route accepted opportunity IDs into the normal optim-plans flow: `PLAN_v1.md`, refinement, immutable execution manifest approval, execution, validation, controller verification, and integration gates.

## Hard Boundaries

- Do not ask about preferences the repo or cited sources can answer.
- Do not implement from the opportunity list directly.
- Do not bypass execution approval, manifest binding, validator review, controller verification, path audits, or final integration checks.
- If a GitHub source only loosely inspires an idea, say that in `supported_claim`; do not overstate the source.
