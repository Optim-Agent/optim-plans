<h1 align="center">optim-plans</h1>

<p align="center">
  Your planning loop and execution loop for agentic coding.
</p>

<p align="center">
  <a href="https://github.com/Optim-Agent/optim-plans/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Optim-Agent/optim-plans/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg?style=square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-blue?style=square" />
  <img alt="Status" src="https://img.shields.io/badge/status-active-brightgreen?style=square" />
  <a href="https://code.claude.com/docs/en/skills"><img alt="Claude Skill" src="https://img.shields.io/badge/Claude-Skill-D97757?logo=claude&logoColor=white&style=square"></a>
  <a href="https://developers.openai.com/codex/skills"><img alt="Codex Skill" src="https://img.shields.io/badge/Codex-Skill-blue?logo=openai&logoColor=white&style=square"></a>
  <img alt="Repo views" src="https://hits.sh/github.com/Optim-Agent/optim-plans.svg?label=repo%20views" />
</p>

<details>
<summary><strong>It may be your best planner ever.</strong></summary>

```text
all you need is a planning loop and an execution loop
both are elegantly featured in optim-plans
plans stay reviewable
execution stays gated
agents stay pointed at the work that matters
```

</details>

`optim-plans` turns "build me something" into a reviewed Markdown plan, records the decisions that shaped it, and keeps execution behind explicit approval. It gives Claude and Codex the two loops agentic coding actually needs: a planning-refining loop that keeps asking until the plan is real, and an execution loop that builds, verifies, and records what happened.

## The Two Rings

```text
Planning-refining ring

  question -> answer
       ^        |
       |        v
  smaller Q&A loop inside each planning decision

  PLAN_v1.md -> reviewer / criticizer -> PLAN_v2.md
       ^                                  |
       |__________________________________|

Execution ring

  prepare manifest -> approve -> start run worktree
        -> serial item checkpoint -> awaiting integration -> finish outcome
```

The first ring fights the wrong-plan problem. It turns vague intent into a plan, then refines that plan with review or criticism. Each decision can open its own smaller question-answering ring, so uncertainty gets resolved before code changes.

The second ring fights the wrong-execution problem. It starts only from an immutable manifest-bound human approval, runs serial checkpointed work in one controller-owned worktree and branch, verifies with controller-run commands and Git audits, then waits at `awaiting_integration` until a proven finish outcome records what happened.

## See it in action

```text
You: $optim-plans Design a tiny browser game where a wizard redirects falling stars into constellations.

AI:  I inspected the repo. One design choice changes the whole game loop.
     What should the player optimize for?

     1. Elegant constellation shapes — best fit for a calm puzzle game
     2. Survival under pressure — better for arcade pacing
     3. High-score combos — stronger replay loop
     4. Other
     5. Auto-complete

You: 1

AI:  Recorded the answer.
     Draft plan: star physics, constellation scoring, level pacing,
     keyboard controls, visual polish, and verification screenshots.
     Pick who should refine the plan:

     1. Reviewer — fresh read-only reviewer session
     2. Criticizer — fresh read-only criticizer session
     3. Jump to executor — skip refinement and launch from this plan
     4. Auto-complete

You: 2

AI:  Choose refinement agent:

     1. Delegated foreground run — choose a standalone sub-agent with visible output
     2. Current foreground session — continue reviewing, questioning, or criticizing in this session
     3. Other
     4. Auto-complete

You: 1

AI:  Choose model and effort:

     1. Codex detected defaults — use Codex model default with effort default
     2. Codex manual model/effort — choose explicit --model and reasoning effort for Codex
     3. Other
     4. Auto-complete

You: 1

AI:  Criticizer asks: the calm puzzle goal conflicts with falling-star pressure.
     Should failed catches be harmless, score-limiting, or level-ending?

     1. Score-limiting — preserves calm play while rewarding skill
     2. Harmless — pure sandbox
     3. Level-ending — closer to arcade survival
     4. Other
     5. Auto-complete

You: 1

AI:  Wrote docs/optim-plans/2026-07-24-wizard-stars-game/PLAN_v2.md.
     Plan hash: 8b2...
     Execution manifest hash: 41f...
     Run worktree: .git/optim-plans/run-worktrees/7c9...
     Run branch: optim-plans/run/7c9...
     Approve manifest-bound execution?

     1. Execute the scoped game implementation
     2. Stop
     3. Other
```

<details>
<summary><strong>What do the artifacts look like?</strong></summary>

Public artifacts are plain Markdown:

```text
docs/optim-plans/YYYY-MM-DD-topic/
  PLAN_v1.md
  PLAN_v1_reviewer_comments.md
  PLAN_v2.md
  EXECUTION_RESULTS.md
```

Machine state is separate and replayable:

```text
.git/optim-plans/
  worktrees/<worktree-id>/active.json
  runs/<run-id>/
    run.json
    events.jsonl
    runtime.json
    controller.lock
```

</details>

## Quick Start

### Install for local Claude Code development

From this repository root:

```bash
claude plugin validate --strict .
claude plugin marketplace add ./ --scope user
claude plugin install optim-plans@optim-plans-dev --scope user
```

For one-off development without installing, load this checkout for a single Claude session:

```bash
claude --plugin-dir . '$optim-plans Turn this idea into a reviewed plan before implementation.'
```

### Install for local Codex development

From this repository root:

```bash
codex plugin marketplace add .
codex plugin add optim-plans@optim-plans-dev
```

Then invoke the skill:

```text
$optim-plans Turn this idea into a reviewed plan before implementation.
```

Normal `$optim-plans` and `/optim-plans` calls auto-select the smallest planning depth that fits the prompt and repo evidence. Name a level only when you want to override that choice:

| Level | Questions | Best for | What optim-plans does |
|---|---:|---|---|
| `mini-plan` | 1 | Tiny, low-risk repo edits where one alignment check is enough. | Asks one focused question, writes a compact plan, then offers the same `Reviewer` / `Criticizer` / `Jump to executor` choice. |
| `small-plan` | 1-3 | Small docs, policy, or workflow changes that still need intent checked. | Resolves a few choices, writes `PLAN_v1.md`, then requires exactly one reviewer or criticizer pass before execution approval. |
| `plan` | 1-5 | Normal multi-decision project or repository changes. | Runs bounded planning and up to three high-priority refinement rounds with a 600 second reviewer/criticizer timeout. |
| `big-plan` | 5-10 | Broad features with research needs, shared-state risk, or unclear architecture. | Requires websearch during brainstorming and allows up to five high-priority refinement rounds with an 1800 second timeout. |
| `huge-plan` / `huge plan` | 10+ | New plugins, large systems, or open-ended efforts where the shape is not known yet. | Uses websearch during brainstorming and refinement, then keeps looping until no high-priority issue remains. |

<details>
<summary><strong>Illustrative examples for each planning level</strong></summary>

### `mini-plan`: one alignment check, then likely execute

```text
You: $optim-plans:mini-plan I am planning to add a `CHANGELOG.md` for this repo, recording each revision made for this project. You could look for the commit history as a source for creating this file.

AI:  I inspected the commit history. One choice controls the output:
     should the changelog group entries by commit, by release-style section,
     or by feature area?

     1. Release-style sections — readable now and easy to maintain later
     2. Commit-by-commit log — closest to the raw history
     3. Feature areas — best if the history is noisy
     4. Other
     5. Auto-complete
```

### `small-plan`: a small repo improvement with one required review

```text
You: $optim-plans:small-plan I am planning to add a `CONTRIBUTING.md` for this repo so practitioners can continuously improve this plan optimization tool and provide practical value. You could look for highly contributed GitHub projects as references for `CONTRIBUTING.md`.

AI:  I will inspect this repo first, then use a small amount of web research
     for contributor-guide patterns. What should the guide optimize for?

     1. Practical contributor workflow — setup, tests, PR rules, and review expectations
     2. Community tone — welcoming first, process second
     3. Maintainer policy — issue triage, releases, and governance
     4. Other
     5. Auto-complete
```

### `plan`: a normal multi-decision project change

```text
You: $optim-plans:plan I am planning to enrich the README.md of this repo so it seems more attractive and persuasive. Give concrete examples for "See it in action", for example, design a game, and show at least one possible brainstorming QA in this example.

AI:  I inspected the current README. The main decision is the pitch style:

     1. Bold but credible — stronger slogans, concrete examples, no overclaiming
     2. Technical and restrained — emphasize state, gates, and tests
     3. Sales-forward — optimize for excitement over detail
     4. Other
     5. Auto-complete
```

### `big-plan`: a broad new feature

```text
You: $optim-plans:big-plan I am planning to build a brand new feature for "optim-plans". The core ideas of this new feature are summarized as below, but you are allowed to help me refine and strengthen this initial idea: (1) remember recurring planning preferences, (2) recommend the smallest safe plan level, (3) generate better verification commands from repo evidence.

AI:  This touches product behavior, controller state, docs, and tests, so I
     will inspect the repo and use web research during brainstorming. First:
     where should preference memory live?

     1. Repo-local state — repeatable for the project and easier to audit
     2. User-level profile — portable across repos, but more privacy-sensitive
     3. No persistence yet — infer from current repo only
     4. Other
     5. Auto-complete
```

### `huge-plan`: a new plugin or system

```text
You: $optim-plans:huge-plan I am planning to build a brand new plugin for Claude and Codex. This skill is named "optim-plans" at the current directory. The core ideas of optim-plans are summarized as below, but you are allowed to help me refine and strengthen this initial idea: (1) turn vague ideas into reviewed plans, (2) run reviewer and criticizer loops before edits, (3) execute only after a human approval gate.

AI:  This is open-ended plugin design. I will inspect the directory, research
     plugin conventions during brainstorming and refinement, then keep asking
     until the architecture, safety model, artifacts, and verification story
     are all pinned down.

     First question: what is the primary failure optim-plans must prevent?

     1. Building the wrong thing — prioritize grilling and reviewed plans
     2. Unsafe execution — prioritize approval gates and scoped workers
     3. Lost context — prioritize durable artifacts and replayable state
     4. Other
     5. Auto-complete
```

</details>

### Use the controller directly

```bash
python3 scripts/optim_plans.py init --repo <repo> --topic "<topic>"
python3 scripts/optim_plans.py ask --repo <repo> --prompt "Choose reviewer" --plan-level small-plan
python3 scripts/optim_plans.py answer --repo <repo> --nonce <nonce> --choice <option-id>

# after PLAN_vN is final, write a manifest JSON that binds the plan hash,
# source base, adapter argv/config, item DAG, allowed paths, verification argv,
# run worktree/branch, integration destination, verification timeout, retry limits, and policy.
python3 scripts/optim_plans.py prepare-execution --repo <repo> --manifest <manifest.json>
python3 scripts/optim_plans.py answer --repo <repo> --nonce <approval-nonce> --choice approve
python3 scripts/optim_plans.py start-execution --repo <repo> --approval-nonce <approval-nonce>
python3 scripts/optim_plans.py run-item --repo <repo> --item-id TASK-001
python3 scripts/optim_plans.py status --repo <repo>
python3 scripts/optim_plans.py answer --repo <repo> --nonce <finish-nonce> --choice kept
python3 scripts/optim_plans.py finish-run --repo <repo> --outcome kept --approval-nonce <finish-nonce>
```

## Why optim-plans?

Agentic coding works best when the plan is durable, reviewable, and tied to verification. Chat-only planning breaks down when the context window moves, when an agent resumes later, or when a write-capable worker needs exact boundaries.

- **Human-in-the-loop by default** — questions are explicit, ordered, and nonce-bound.
- **Auto-complete with a hard stop** — recommended planning choices can proceed automatically, but only an explicit `Jump to executor` answer can approve execution.
- **Markdown for people, events for machines** — artifacts explain decisions; `events.jsonl` drives recovery.
- **Reviewer or criticizer loop** — reviewer mode audits the plan; criticizer mode challenges it with bounded follow-up questions.
- **Hooks are defense in depth** — they inject context and deny unsafe tool calls, but controller audits decide verification and integration.
- **Built for brownfield repos** — state stays under the Git common directory, outside monitored worktrees.

## Safety Model

`run.json` is immutable after initialization. `events.jsonl` is append-only, strictly sequenced, and replayed to derive state. `runtime.json` and `active.json` are rebuildable indexes.

Repository-integrity detection and integration gating are the implemented trust boundary. The controller assumes the target repository, local agent process, Git executable, and host OS are trusted; it does not provide host confinement or prevent a trusted process from writing outside the run worktree before detection. Hooks and agent sandboxes are defense in depth, not the authoritative boundary.

The implemented guardrails include:

- advisory lock around event append;
- strict JSON parsing with duplicate-key rejection;
- collision-safe artifact directories;
- immutable manifest-bound execution approval with a single-use nonce recorded in `events.jsonl`;
- atomic question nonce consumption;
- fail-closed Auto-complete allowlist;
- one controller-owned run worktree and run branch for the cumulative run;
- serial item execution with checkpoint commits in stable DAG order;
- adapter-only argv launch with `shell=False`;
- controller verification and Git audits for path allowlists and protected Git metadata;
- `awaiting_retry_decision` with explicit retry approval before restoration;
- `awaiting_integration` after every item and final audit pass;
- evidence-backed `finish-run` outcomes: `integrated`, `pr-opened`, `kept`, `discarded`, `failed`, and `aborted`;
- path-scope rejection and delta audits for symlinks, gitlinks, nested repos, ignored files, untracked files, staged files, and tracked changes;
- three distinct evidenced attempts before `not_achievable` can even request confirmation;
- Codex/Claude command builders with conservative role-specific flags;
- `SessionStart` and `PreToolUse` hooks with no Stop handler.

## Project Layout

```text
.codex-plugin/plugin.json          Codex plugin manifest
.claude-plugin/plugin.json         Claude plugin manifest
.agents/plugins/marketplace.json   Local Codex marketplace entry
skills/optim-plans/                Shared skill and references
scripts/optim_plans.py             Controller CLI
scripts/optim_plans_core.py        State, artifacts, approvals, Git checks
scripts/agent_adapters.py          Claude/Codex detection and command builders
hooks/                             Hook configs and dispatcher
tests/                             Standard-library unittest suite
evals/                             Skill pressure cases
```

## Docs

Start with the skill:

-> [`skills/optim-plans/SKILL.md`](skills/optim-plans/SKILL.md): when and how agents should invoke optim-plans<br>
-> [`skills/optim-plans/references/planning.md`](skills/optim-plans/references/planning.md): brainstorming and `PLAN_v1.md` contract<br>
-> [`skills/optim-plans/references/refinement.md`](skills/optim-plans/references/refinement.md): reviewer/criticizer loop contract<br>
-> [`skills/optim-plans/references/execution.md`](skills/optim-plans/references/execution.md): execution gate and verification contract<br>
-> [`skills/optim-plans/references/artifacts.md`](skills/optim-plans/references/artifacts.md): public Markdown and machine-state layout

## Verification

Run the local proof:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/*.py hooks/*.py
python3 scripts/validate_structure.py
```

Optional local authoring checks:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/optim-plans
```

CI runs the unittest suite, Python compilation, and structure validation on Linux and macOS.

## Current Scope

Implemented:

- plugin and marketplace metadata;
- shared skill and reference docs;
- durable state and artifact primitives;
- question contract and nonce validation;
- immutable manifest-bound execution approval;
- approval, execution-ledger, and refinement-ledger primitives;
- agent discovery and role-based command builders;
- one controller-owned run worktree and run branch;
- serial item execution with checkpoint commits;
- adapter-only argv launch with `shell=False`, using worker stdout for the result JSON envelope;
- controller verification and Git audits for path allowlists and protected Git metadata;
- explicit retry approval;
- `awaiting_integration`;
- evidence-backed `finish-run` outcomes;
- hook guard dispatcher and configs;
- CI and tests.

Not implemented yet:

- complete repo-inspecting `PLAN_v1.md` synthesis;
- live reviewer/criticizer subprocess orchestration;
- automatic `PLAN_vN` revision loop;
- detached background controller;
- parallel item execution;
- hosted merge or PR creation automation.

## Contributing

Small fixes can go straight to PR. For larger behavior changes, add or update a `docs/optim-plans/` plan artifact first so the intent is reviewable before code changes.

Development rules:

- keep controller code standard-library only;
- write or update tests before changing behavior;
- do not put runtime state in monitored worktrees;
- do not add hook-owned continuation;
- do not broaden Auto-complete across write-capable gates;
- do not add direct execution paths that bypass manifest-bound approval, controller verification, Git audits, or finish proof;
- do not make reviewer/criticizer sessions write-capable.

## Development

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/*.py hooks/*.py
python3 scripts/validate_structure.py
```

The repository intentionally has no Python dependency installation step.

## Acknowledgements

Thanks to [chaseai-yt/grill-me-codex](https://github.com/chaseai-yt/grill-me-codex) for motivating this project, [mattpocock/skills](https://github.com/mattpocock/skills) for initiating this idea, and [leo-lilinxiao/codex-autoresearch](https://github.com/leo-lilinxiao/codex-autoresearch) as a reference for the execution loop.

## License

MIT. See [`LICENSE`](LICENSE).
