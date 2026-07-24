# Planning Contract

Inspect the target repo first and collect evidence before asking unresolved questions. Record that evidence in `PLAN_v1.md` when writing the plan.

Resolve unknowns by the evidence ladder: codebase first, cited web research second, user question last. When a recommended option rests on a web-verifiable claim, websearch it before presenting the question and record the source in `PLAN_v1.md`. Never present a recommendation backed only by an unchecked assumption.

One answered question is not a launch approval. When the decision tree is resolved, write `PLAN_v1.md` under `docs/optim-plans/YYYY-MM-DD-topic/`, then run refinement before any target repo file edit.

Every user-facing planning question is a choice prompt with options ordered:

1. recommended answer with a short reason;
2. alternatives;
3. `Other` for free-form text;
4. `Auto-complete`.

`PLAN_v1.md` must contain stable IDs for goals, non-goals, requirements, constraints, implementation items, acceptance criteria, verification class, expected evidence, dependencies, allowed paths, repo evidence, and resolved decisions. IDs are never recycled in later revisions.
