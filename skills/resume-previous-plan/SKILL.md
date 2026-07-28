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
   `resume_command`, `retry_command`, `retry_item_id`, `retry_approval_nonce`, `finish_approval_nonce`, and `finish_choices`.

3. If `resume_command` is present, run it. This covers already approved execution launch, automatic first retry, and already approved later retries.

4. If `retry_approval_nonce` and `retry_command` are present but `resume_command` is absent, treat this explicit `resume-previous-plan` invocation as retry approval:

   ```bash
   python3 scripts/optim_plans.py answer --repo <repo> --nonce <retry_approval_nonce> --choice approve
   ```

   Then run `retry_command`.

5. If status fails with no active pointer, run the Git-common fallback:

   ```bash
   python3 scripts/optim_plans.py previous-run --repo <repo>
   ```

6. If `previous-run` returns a `candidate`, report its `run_id`, `status`, `artifact_dir`, `terminal_time`, `last_event_type`, and `next_action`. If it returns `candidate: null`, say no preserved run was found.

`awaiting_retry_decision` is covered: run `resume_command` for the automatic first retry. For later retries, approve `retry_approval_nonce` with `choice approve`, then run `retry_command`.

Do not approve execution launch, approve finish, restore an active pointer, delete evidence, or edit files from this skill. If only `finish_approval_nonce` is available, report `finish_choices` and stop because there is no unambiguous resume outcome.
