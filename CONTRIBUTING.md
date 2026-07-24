# Contributing

`optim-plans` is a small planning and execution controller for agentic coding.
Useful contributions make plans clearer, execution safer, or practitioner
workflows easier to verify.

## Ways to Contribute

- Improve planning prompts, reviewer output, criticizer questions, or execution
  evidence when a real workflow shows friction.
- Add focused tests for controller state, artifact contracts, hook decisions, or
  adapter command construction.
- Improve docs when setup, invocation, or safety boundaries are unclear.
- Report bugs with the exact command, repo state, expected result, actual
  result, and any generated artifact path.

## Setup

Use Python 3.11 or newer. Controller code should stay on Python standard
library APIs unless a dependency is already present and clearly earns its cost.

From the repo root:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/validate_structure.py
```

For local plugin checks, use the commands in `README.md` for Claude or Codex.

## Testing

Add or update tests before changing behavior. Keep coverage close to the risk:

- Controller state or lifecycle changes need focused unit tests.
- Hook changes need hook contract tests.
- Artifact format changes need artifact or skill contract tests.
- Pure documentation changes only need spelling, command, and link sanity
  checks unless they change documented behavior.

Run the narrowest relevant command first, then the full suite before opening a
pull request:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

## Planning Contracts

Generated public planning artifacts belong under `docs/optim-plans/`. Do not
write source files, skill docs, hooks, tests, README/config, or dependency
manifests before the planning flow reaches its explicit execution gate.

Preserve these invariants:

- First inspect the target repo, then ask unresolved planning questions.
- Keep user-facing planning and refinement questions as choice prompts with
  `Auto-complete` last, except execution approval questions.
- Do not offer `Auto-complete` for execution, retry, finish, merge, push,
  release, waiver, or destructive cleanup approval.
- Reviewer and criticizer work is read-only until the approved execution phase.
- Execution must start from an immutable manifest-bound approval.

## Safety Boundaries

Do not add write-capable shortcuts around human launch approval. Execution
changes must preserve:

- manifest-bound approval gates;
- adapter-only argv launch with `shell=False`;
- controller-run verification;
- path audits and protected Git metadata audits;
- explicit retry approval;
- evidence-backed `finish-run` terminal outcomes.

Do not describe hooks or agent sandboxes as confinement. The supported boundary
is repository-integrity detection and integration gating for a trusted local
repo/process. Hooks and sandboxes are defense in depth.

## Pull Requests

Keep PRs small and tied to a concrete practitioner value. A good PR explains:

- what problem it fixes;
- what behavior changed;
- what artifacts, state transitions, or commands prove it;
- which tests were run.

For non-trivial behavior changes, include or link the relevant
`docs/optim-plans/YYYY-MM-DD-topic/PLAN_vN.md` artifact.

## Changelog Upkeep

Update `CHANGELOG.md` for user-visible behavior, safety model, setup, or
workflow changes. Keep entries human-readable, newest-first, and grouped by
change type where practical.
