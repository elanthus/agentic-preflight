- `start --replace` in `reusable` mode and `gc` no longer delete commits on a validation
  branch when the validation checkout is detached at the reviewed head. Both read the
  branch tip as well as `HEAD` before releasing the checkout: `start` keeps the runner
  leased, and `gc` retains the run with reason `validation worktree holds unrecorded
  commits`, or `validation worktree could not be inspected` when the branch history
  cannot be read. `gc --force` still releases it.
