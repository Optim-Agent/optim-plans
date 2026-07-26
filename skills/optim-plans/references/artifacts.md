# Artifact Contract

Public artifacts live in:

```text
docs/optim-plans/YYYY-MM-DD-topic/
```

Use `-2`, `-3`, and later suffixes for same-day topic collisions. Never overwrite an earlier run.

Before execution approval, write only controller state and files in this artifact directory. Source files, skill docs, README/config, hooks, scripts, tests, and dependency manifests are target repo changes and require the explicit execution gate.

Machine state lives under the Git common directory:

```text
.git/optim-plans/worktrees/<worktree-id>/active.json
.git/optim-plans/runs/<run-id>/run.json
.git/optim-plans/runs/<run-id>/events.jsonl
.git/optim-plans/runs/<run-id>/runtime.json
.git/optim-plans/runs/<run-id>/worker-states/
```

`run.json` is immutable. `events.jsonl` is authoritative for the write-once execution manifest record, approval nonce consumption, lifecycle state, retry decisions, checkpoint commits, final audits, and terminal finish outcome. `runtime.json` and `active.json` are rebuildable indexes; terminalization archives/releases only the matching active pointer.

Generate `EXECUTION_RESULTS.md` from validated controller state, not worker prose. Include one row per finalized plan ID with status, changed files, checkpoint commits, controller verification evidence, attempts, retry decisions, and limitations. Include the final checkpoint, final file/protected-metadata audit evidence, integration verification evidence when present, the automatic non-destructive `run_finished` / `kept` outcome, or the manual recovery `finish-run` outcome (`integrated`, `pr-opened`, `kept`, `discarded`, `failed`, or `aborted`).
