---
name: optim-plans
description: Use when turning a rough idea, feature request, repo change, or project goal into a plan before implementation in Claude or Codex — especially when requirements are vague, assumptions unexamined, or execution touches shared state.
---

# Optim Plans

Plan before execution. The bundled controller keeps durable state under the Git common directory and public Markdown under `docs/optim-plans/YYYY-MM-DD-topic/`.

This flow fixes two failure modes in order: grilling the user fixes building the *wrong thing*; adversarial reviewer/criticizer passes fix a plan that *sounds right but breaks*.

<HARD-GATE>
Do NOT write code, scaffold files, edit repo docs/config, or change target files until the plan has passed refinement and the human has explicitly approved the immutable execution manifest through the controller. Before execution approval, the only permitted writes are controller state and `docs/optim-plans/YYYY-MM-DD-topic/` artifacts. Auto-complete can answer planning questions; Auto-complete never approves execution, and execution approval questions must not offer `Auto-complete`. For mini-plan only, `skip-refinement-execute` is explicit execution approval when presented with the manifest-bound launch details and must not offer `Auto-complete`.
</HARD-GATE>

## First Turn Contract

Treat the user's prompt as a planning target, not write authorization. After any read-only context check, the first visible response must be one optim-plans choice question, not a completed analysis or file edit. It must include recommended first, `Other` second-last, and `Auto-complete` last. References inform the recommended option; they never replace the user interview. One human-choice answer is necessary but not sufficient: after the answer, continue through `PLAN_v1.md`, refinement, and explicit execution approval before editing target files.

## Plan Request Levels

Users may choose the planning depth in the prompt or via direct controller flags:

- `mini-plan`: 1 planning question; zero or one refinement round. The refinement choice includes recommended `skip-refinement-execute` before `reviewer` and `criticizer`; that choice is explicit execution approval for the manifest-bound launch.
- `small-plan`: 1 to 3 planning questions; exactly one refinement round.
- `plan`: 1 to 5 planning questions; at most three refinement rounds, 600 seconds per reviewer/criticizer pass, and at most three high-priority comments or questions per round.
- `big-plan`: 5 to 10 planning questions; websearch is required for brainstorming, with at most five refinement rounds, 1800 seconds per reviewer/criticizer pass, and at most five high-priority comments or questions per round.
- `huge-plan` / `huge plan`: at least 10 planning questions, no maximum; websearch is required for brainstorming and refinement, with no refinement round or timeout limit and at most five high-priority comments or questions per round.

For `plan`, `big-plan`, and `huge-plan`, only high-priority comments or criticisms continue refinement; if a round produces none, terminate that round.

If no level is named, auto-select the smallest level that fits the user's prompt and repo evidence before the first planning question. Do not ask the user to choose the level as that question.

## Anti-Pattern: "Too Small To Plan"

Every request goes through this flow — a config change, a one-function utility, all of them. Small tasks are where unexamined assumptions waste the most work. The plan may be short; skipping the flow may not.

## Process Flow

```dot
digraph optim_plans {
    "Init controller, inspect repo" [shape=box];
    "Grill: one question at a time" [shape=box];
    "PLAN_v1.md" [shape=box];
    "Reviewer / Criticizer pass" [shape=box];
    "Unresolved findings?" [shape=diamond];
    "PLAN_v(N+1).md" [shape=box];
    "Human approves execution?" [shape=diamond];
    "Manifest-gated execution + controller verification" [shape=box];
    "Awaiting integration" [shape=box];
    "Finish wrap" [shape=doublecircle];

    "Init controller, inspect repo" -> "Grill: one question at a time";
    "Grill: one question at a time" -> "PLAN_v1.md";
    "PLAN_v1.md" -> "Reviewer / Criticizer pass";
    "Reviewer / Criticizer pass" -> "Unresolved findings?";
    "Unresolved findings?" -> "PLAN_v(N+1).md" [label="yes"];
    "PLAN_v(N+1).md" -> "Reviewer / Criticizer pass";
    "Unresolved findings?" -> "Human approves execution?" [label="no"];
    "Human approves execution?" -> "Manifest-gated execution + controller verification" [label="yes"];
    "Manifest-gated execution + controller verification" -> "Awaiting integration";
    "Awaiting integration" -> "Finish wrap";
}
```

## Checklist

Create a task for each item and complete them in order:

1. Inspect the target Git repository before asking any product question.
2. Start or resume the controller:

   ```bash
   python3 <plugin-root>/scripts/optim_plans.py init --repo <repo> --topic "<topic>"
   python3 <plugin-root>/scripts/optim_plans.py status --repo <repo>
   ```

3. Grill the user: ask unresolved questions one at a time until none remain.
4. Write `PLAN_v1.md` under `docs/optim-plans/YYYY-MM-DD-topic/`, including the repo evidence and resolved decisions.
5. Run reviewer or criticizer refinement; produce `PLAN_v(N+1).md` until converged.
6. Obtain explicit human approval for the immutable execution manifest, execute serial items through the controller, then complete the finish wrap.

## Grilling the User

Direct, evidence-based, relentless until answers are real:

- If a question can be answered by exploring the codebase, explore the codebase instead of asking. Never spend a user question on something the repo already answers.
- If the repo can't answer but the web might (library behavior, version constraints, external API semantics, domain facts), websearch first and cite sources. The evidence ladder is: codebase → cited web research → user question.
- Walk each branch of the decision tree, resolving dependent decisions in order. One question per message; if a topic needs more exploration, split it into multiple questions.
- Every user-facing planning or refinement question must be a choice prompt: recommended option first with a short reason, alternatives, `Other` second-last, `Auto-complete` last, except the mini-plan `skip-refinement-execute` combined launch question.
- When asking the user to choose an agent for reviewing, questioning, or criticizing, recommend the delegated foreground run first so Auto-complete uses a standalone visible run.
- Challenge vague or hand-waving answers. If an answer contradicts repo evidence, say so and re-ask — never silently accept it.
- Never answer product questions on the user's behalf unless they pick `Auto-complete`. Auto-complete may accept recommended planning and refinement answers; it never approves execution, waivers, merge, push, release, or destructive cleanup.

## Refinement Stance

- You are the final arbiter of every finding: incorporate critiques worth acting on, reject bad ones with a recorded reason. Caving to everything defeats the review; ignoring it defeats the point.
- Justify dispositions with evidence, not vibes. When a finding or plan decision rests on an unsure technical claim, websearch it and cite the source in the finding record or revision ledger before dispositioning.
- Criticizer mode is not reviewer mode: if the criticizer raises a challenge, ask the user that refinement question before writing criticizer comments or the next plan.
- Never fake convergence. A finding stays `unresolved` until genuinely dispositioned — a flagged disagreement beats a false approval.

## Load References

- Read `references/planning.md` when brainstorming or writing `PLAN_v1.md`.
- Read `references/refinement.md` when choosing reviewer vs criticizer, recording comments, or producing the next plan version.
- Read `references/execution.md` before any write-capable launch or verification loop.
- Read `references/artifacts.md` when creating or validating public Markdown and state files.

## Question Bridge

When native cards are available, render the controller's pending question as cards. Otherwise print numbered Markdown. Submit only the selected option ID and nonce back to the controller. Stale or replayed nonces must be rejected by the controller, not hand-waved by the agent.

## Invariants

- One active run per Git worktree.
- `run.json` is immutable; `events.jsonl` is authoritative.
- Reviewer and criticizer sessions are read-only and fresh.
- Execution requires a clean committed Git base and explicit manifest-bound human launch approval.
- Workers run only through adapter argv with `shell=False` in one controller-owned run worktree and branch.
- Controller verification, path audits, and protected Git metadata audits are authoritative; worker prose is evidence only.
- Verified items become checkpoint commits in serial DAG order. Failed attempts require explicit retry approval before restoration.
- All verified items plus final audits enter `awaiting_integration`; `finish-run` records the evidence-backed terminal outcome.
- The trust boundary is repository-integrity detection and integration gating, not host confinement.
- Hooks only inject context and deny unsafe tool calls; they are defense in depth and the controller owns continuation.

## Red Flags — STOP and Return to the Flow

- Implementing anything before the execution gate
- Batching multiple questions into one message
- Asking a planning or refinement question without `Auto-complete` as the last option, except the mini-plan `skip-refinement-execute` combined launch question
- Treating one answered planning question or ordinary refinement question as execution approval
- Launching a worker outside the manifest-bound `prepare-execution` / `start-execution` / `run-item` flow
- Treating hooks, shell parsing, worker self-attestation, or a verifier agent as the authoritative safety boundary
- Editing target files before `PLAN_v1.md` and refinement artifacts exist
- Accepting a vague answer to keep momentum
- Asking the user (or guessing) something a websearch could have answered with sources
- Dispositioning a finding on an unsure technical claim without cited evidence
- Revising from criticizer comments before every criticizer question has a recorded answer
- Answering a product question yourself without `Auto-complete`
- Offering `Auto-complete` on an execution approval question
- Letting Auto-complete touch execution, waivers, merge, push, release, or cleanup
- Hand-waving a stale or replayed nonce instead of letting the controller reject it
