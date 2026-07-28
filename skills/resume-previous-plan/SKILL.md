---
name: resume-previous-plan
description: Use when the user wants to resume or rediscover an interrupted optim-plans run; read-only discovery only, not approval or execution.
---

# Resume Previous Plan

Find the user's previous optim-plans run without mutating controller state.

1. First run active status for the current worktree:

   ```bash
   python3 scripts/optim_plans.py status --repo <repo>
   ```

2. If status succeeds, report the active `run_id`, `status`, and any controller-provided next command such as `resume_command`, `retry_approval_nonce`, or `finish_approval_nonce`.

3. If status fails with no active pointer, run the Git-common fallback:

   ```bash
   python3 scripts/optim_plans.py previous-run --repo <repo>
   ```

4. If `previous-run` returns a `candidate`, report its `run_id`, `status`, `artifact_dir`, `terminal_time`, `last_event_type`, and `next_action`. If it returns `candidate: null`, say no preserved run was found.

Do not approve execution, retry, finish, restore an active pointer, delete evidence, or edit files from this skill. Ask before any state-changing recovery.
