- In isolated worktree modes, `abort` from a lint or test state no longer deletes
  validation-branch commits that no `respond` or stage registered. It stops with
  `unmerged_work` (exit 5) and lists them in `data.fix_commits`; `--force` discards them
  and reports them in `data.discarded_fix_commits`. A recorded fix commit always counts,
  even when the validation checkout is detached or reset.
