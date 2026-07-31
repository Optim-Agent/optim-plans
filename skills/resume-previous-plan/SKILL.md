---
name: resume-previous-plan
description: Use when the user wants to resume or rediscover an interrupted optim-plans run; automatically run the active recovery path when one is available.
---

# Resume Previous Plan

Find the user's previous optim-plans run and continue the active recovery path when one is available.

1. First run active status for the current worktree:

   ```bash
   python3 scripts/optim_plans.py status --repo <repo>
   ```

2. If status succeeds, report the active `run_id`, `status`, `next_action`, and any controller-provided recovery fields:
   `resume_command`, `retry_command`, `retry_item_id`, `finish_approval_nonce`, and `finish_choices`.

3. If `resume_command` is present, run it. This covers already approved execution launch and retryable execution recovery until the controller reports `blocked`.

4. If status fails with no active pointer, run the Git-common fallback:

   ```bash
   python3 scripts/optim_plans.py previous-run --repo <repo>
   ```

5. If `previous-run` returns a `candidate`, report its `run_id`, `status`, `artifact_dir`, `terminal_time`, `last_event_type`, and `next_action`. If it returns `candidate: null`, say no preserved run was found.

`awaiting_retry_decision` is covered for retryable execution failures: run `resume_command`. Manual recovery and blocked/finish cases still require explicit user approval.

Do not approve execution launch, approve finish, restore an active pointer, delete evidence, or edit files from this skill. If only `finish_approval_nonce` is available, report `finish_choices` and stop because there is no unambiguous resume outcome.
