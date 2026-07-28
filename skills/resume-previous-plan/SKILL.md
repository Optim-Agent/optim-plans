---
name: resume-previous-plan
description: Use when the user wants to resume or rediscover an interrupted optim-plans run; read-only discovery only, not approval or execution.
---

# Resume Previous Plan

Find the user's previous optim-plans run without approving or executing recovery.

1. First run active status for the current worktree:

   ```bash
   python3 scripts/optim_plans.py status --repo <repo>
   ```

2. If status succeeds, report the active `run_id`, `status`, `next_action`, and any controller-provided recovery fields:
   `resume_command`, `retry_command`, `retry_item_id`, `retry_approval_nonce`, `finish_approval_nonce`, and `finish_choices`.

3. If status fails with no active pointer, run the Git-common fallback:

   ```bash
   python3 scripts/optim_plans.py previous-run --repo <repo>
   ```

4. If `previous-run` returns a `candidate`, report its `run_id`, `status`, `artifact_dir`, `terminal_time`, `last_event_type`, and `next_action`. If it returns `candidate: null`, say no preserved run was found.

`awaiting_retry_decision` is covered: show the retry path and the finish path. The automatic first retry may appear as `resume_command`; later retries require `retry_approval_nonce` before `retry_command`.

Do not approve execution, retry, finish, run `resume_command`, restore an active pointer, delete evidence, or edit files from this skill. Ask before any state-changing recovery.
