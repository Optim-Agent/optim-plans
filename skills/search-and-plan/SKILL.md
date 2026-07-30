---
name: search-and-plan
description: Use when external research should inform a repo-change plan before implementation; exclude direct implementation-only, factual/explanation, trivial, or explicit no-plan requests.
---

# Search And Plan

Research first; planning and execution still follow `../optim-plans/SKILL.md`.

## Required Flow

1. Read and follow `../optim-plans/SKILL.md`, preserving explicit `mini-plan`, `small-plan`, `plan`, `big-plan`, and `huge-plan` routing.
2. Inspect the target Git repo read-only before asking product questions.
3. Initialize or resume the optim-plans controller before external research.
4. Perform read-only initial research. Prefer `agent-reach` when available; if missing, give one sentence of install guidance, do not install `agent-reach` or any other tool before execution approval, and continue with fallback search/repo evidence.
5. Ask the first evidence-informed question as a controller-backed optim-plans choice prompt, and submit/record that answer through the controller before writing refs.
6. Only after that recorded controller answer, write sources and `REF_ANALYSIS.md` under `docs/optim-plans/YYYY-MM-DD-topic/refs/search-and-plan/<topic>/`.
7. Ask any later product questions only after refs are persisted.
8. Continue into the selected optim-plans level: `PLAN_v1.md`, refinement, immutable execution approval, execution, validation, controller verification, and integration gates.

## Research Contract

- Keep a 3-7 high-signal source pack; if fewer than 3 credible sources exist, record the gap instead of padding with weak sources.
- Record attempted queries, attempted backends, backend failures with reasons, why sources were insufficient, and then continue from repository evidence.
- Backend failure fallback is valid only when the failure reason is recorded.
- Do not write pre-execution `./refs/` files; refs live only under the run artifact path above.
- Do not require a strict source manifest before planning.

## REF_ANALYSIS.md

Use these sections exactly:

- `## Sources`
- `## Findings`
- `## Adoptable ideas`
- `## Risks/not-applicable points`
- `## Evidence gaps`
- `## Candidate user decisions`

Every adoptable idea must be presented as an evidence-backed optim-plans choice prompt and recorded through the controller before it can be included in any plan.
