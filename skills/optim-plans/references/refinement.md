# Refinement Contract

After each plan version, ask for `Reviewer` or `Criticizer`, then ask for the detected agent and effort. When asking the user to choose an agent for reviewing, questioning, or criticizing, use the `agent-choice` stage: recommend `Current foreground session` first so Auto-complete continues in that session, with `Delegated foreground run` as the alternative. If the user selects the `background` option id, use the `background-model` stage to ask for Codex or Claude model/effort, including manual model/effort options; despite the legacy option id, Claude delegation must be a standalone foreground sub-agent run with visible output. Every user-facing refinement question is a choice prompt with `Auto-complete` last.

Reviewer mode returns findings. The controller assigns finding IDs and records severity, affected plan IDs, evidence, impact, recommended fix, and disposition.

Evidence fields cite verifiable sources: repo paths for codebase claims, URLs for web claims. When a finding's correctness depends on an unsure technical fact, websearch it before recording a disposition and cite the source.

Criticizer mode asks zero to five adaptive questions. It is not reviewer mode: do not batch findings, do not self-apply fixes, and do not let the criticizer answer its own challenges. Each fresh turn receives the full validated transcript and unresolved ledger, then returns exactly one schema-validated user-facing question or `no_question`. Before presenting criticizer-question options, show the original criticism and summarize the main criticizing point in at most three sentences; highlight the most important point. That question may include evidence and a recommended answer, but it must be asked with the normal card ordering contract. No question means convergence only when the ledger has no unresolved material finding.

Do not write `PLAN_vN_criticizer_comments.md` or `PLAN_v(N+1).md` until every criticizer question has a recorded user answer or an Auto-complete answer recorded by the controller. The criticizer comments file records challenge, presented options, selected answer, answer source, and summarized fix; only then revise the plan.

Write only the comment artifact matching the selected mode: `PLAN_vN_reviewer_comments.md` or `PLAN_vN_criticizer_comments.md`. Then write `PLAN_v(N+1).md` with a revision ledger.
