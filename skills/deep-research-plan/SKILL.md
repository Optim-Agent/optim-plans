---
name: deep-research-plan
description: Use deep-research-plan for repo-change plans needing downloaded projects, articles, or papers, per-reference analysis, graphify JSON, and stronger-than-huge planning; exclude direct implementation-only, factual/explanation, trivial, or explicit no-plan requests.
---

# Deep Research Plan

Research first; planning and execution still follow `../optim-plans/SKILL.md`.

## Required Flow

1. Read and follow `../optim-plans/SKILL.md`, treating the request as if it named `deep-research-plan`.
2. Inspect the target Git repo read-only and initialize or resume the optim-plans controller before external research.
3. Ensure the root `.gitignore` ignores `refs/`; add that ignore entry only during execution approval if it is missing.
4. Search proactively for related projects, articles, and papers. Prefer `agent-reach` when available. If `agent-reach` is missing, ask exactly one yes/no sentence about installing it locally; on yes, install it first and use it, and on no, use websearch fallback and record the fallback reason.
5. Download at least 3 relevant refs into `./refs/<topic>/` before writing `PLAN_v1.md`. Do not satisfy this with `curl` alone, README-only snapshots, or abstract-only paper notes; keep full local project, article, paper, docs, or artifact content sufficient for deep analysis.
6. For every downloaded ref, generate graphify JSON beside the ref before user adoption questions. If `graphify` is missing, ask exactly one yes/no sentence about installing it locally; on yes, install it first and generate the JSON, and on no, use read tools to deeply inspect the downloaded files and record the graphify waiver.
7. For every ref after analysis, ask at least 3 ref-specific controller-backed choice questions about whether or how to use that ref. Base each question on the downloaded content, put the recommended option first, `Other` second-last, and `Auto-complete` last, then record the answer before including the idea in any plan.
8. Continue as stronger-than-huge planning: at least 10 planning questions, required websearch in brainstorming and refinement, no refinement round or timeout limit, and at most five high-priority comments or questions per refinement round.

## Evidence Rules

- Keep attempted queries, selected refs, download commands/tools, analysis coverage, graphify output paths, backend failures, install refusals, and evidence gaps in the run artifacts.
- Block rather than pad when fewer than 3 credible refs exist, unless the user explicitly narrows or waives the minimum.
- Do not rely only on README files, landing pages, abstracts, summaries, or package metadata when deeper source material is available.
