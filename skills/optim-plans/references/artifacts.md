# Artifact Contract

Public artifacts live in:

```text
docs/optim-plans/YYYY-MM-DD-topic/
```

Use `-2`, `-3`, and later suffixes for same-day topic collisions. Never overwrite an earlier run.

Before execution approval, write only controller state and files in this artifact directory. Source files, skill docs, README/config, hooks, scripts, tests, and dependency manifests are target repo changes and require the explicit execution gate.

Machine state lives under the Git common directory:

```text
.git/optim-plans/config.json
.git/optim-plans/worktrees/<worktree-id>/active.json
.git/optim-plans/runs/<run-id>/run.json
.git/optim-plans/runs/<run-id>/events.jsonl
.git/optim-plans/runs/<run-id>/runtime.json
.git/optim-plans/runs/<run-id>/controller.lock
.git/optim-plans/runs/<run-id>/worker-states/
.git/optim-plans/runs/<run-id>/validator-states/
.git/optim-plans/runs/<run-id>/archive/active.json
.git/optim-plans/runs/<run-id>/archive/terminal.json
```

`config.json` stores repo-scoped language, worker preferences, smoke-tested worker cache, and `execution_summary.mode`; the persistent skip value is `{"mode": "always-skip"}`. `run.json` is immutable. `events.jsonl` is authoritative for the write-once execution manifest record, approval nonce consumption, lifecycle state, validator decisions, retry decisions, checkpoint commits, final audits, and terminal finish outcome. `runtime.json` and `active.json` are rebuildable indexes; terminalization archives/releases only the matching active pointer into `archive/`.

At the first checkpoint, the controller asks the run-level `execution_summary` question unless a decision already exists. Choices are `generate-summary`, `skip-summary`, and `always-skip-summary`; only `generate-summary` writes `EXECUTION_SUMMARY.md`, and `always-skip-summary` also persists `execution_summary.mode` for future runs. Generate the summary from validated controller state, not worker prose. Include one row per finalized plan ID with status, changed files, checkpoint commits, validator result evidence/feedback, any validator feedback injected into amelioration retries, controller verification evidence, attempts, retry decisions, and limitations. Include the final checkpoint, final file/protected-metadata audit evidence, integration verification evidence when present, the automatic checked-out fast-forward `run_finished` / `integrated` outcome, any `awaiting_integration` or `integration_verification_failed` recovery evidence, or the manual recovery `finish-run` outcome (`integrated`, `pr-opened`, `kept`, `discarded`, `failed`, or `aborted`).
