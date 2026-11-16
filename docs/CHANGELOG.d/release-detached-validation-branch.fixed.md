- `start --replace` in `reusable` mode and `gc` no longer delete commits on a validation
  branch when the validation checkout is detached at the reviewed head. Both read the
  branch tip as well as `HEAD` before releasing the checkout. `start` puts the runner back
  on its branch and keeps it leased, so the fresh run exits 4 (`needs_human`) instead of
  leasing and resetting it. `gc` retains the run with reason `validation worktree holds
  unrecorded commits`, or `validation worktree could not be inspected` when the branch
  history cannot be read, and its instruction names the run's `ap/<run_id>` branch.
  `gc --force` still releases it.
