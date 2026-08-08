# Planning Contract

Inspect the target repo first and collect evidence before asking unresolved questions. Record that evidence in `PLAN_v1.md` when writing the plan.

During brainstorming QA, probe applicable edge-case dimensions before `PLAN_v1.md`: boundaries, empty or invalid inputs, failure and retry paths, state transitions, concurrency, and compatibility.

Resolve unknowns by the evidence ladder: codebase first, cited web research second, user question last. When a recommended option rests on a web-verifiable claim, websearch it before presenting the question and record the source in `PLAN_v1.md`. Never present a recommendation backed only by an unchecked assumption.

One answered question is not a launch approval. When the decision tree is resolved, ask a final pre-`PLAN_v1.md` scope confirmation: whether the user has more modification requirements. Recommend `No more requirements` first when the scope is settled, offer `Add more requirements`, then `Other`, then `Auto-complete`. Write `PLAN_v1.md` under `docs/optim-plans/YYYY-MM-DD-topic/` only after the user selects no more requirements or Auto-complete accepts that recommendation, then run refinement before any target repo file edit. If the user adds requirements, resolve only the necessary follow-up questions, then repeat the final scope confirmation before writing `PLAN_v1.md`.

Every user-facing planning question is a choice prompt with options ordered:

1. recommended answer with a short reason;
2. alternatives;
3. `Other` for free-form text;
4. `Auto-complete`.

`PLAN_v1.md` must contain stable IDs for goals, non-goals, requirements, constraints, implementation items, acceptance criteria, verification class, expected evidence, dependencies, allowed paths, repo evidence, and resolved decisions. IDs are never recycled in later revisions.

Every future `PLAN_vN.md` artifact must include a dedicated `## Verifier Checklist` section. Each verifier criterion is a Markdown checkbox and includes a criterion ID, covered item IDs, pass condition, evidence, and either a metric threshold or a justification for why the criterion is not quantified.
