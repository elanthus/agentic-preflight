- In isolated worktree modes, `abort` no longer deletes validation-branch commits that no
  `respond` or stage registered, from the end of base synchronization until merge-back.
  It stops with `unmerged_work` (exit 5) and lists them in `data.fix_commits`; `--force`
  discards them and reports them in `data.discarded_fix_commits` and the `aborted` event.
  A recorded fix commit always counts, and a checkout detached from the validation
  branch no longer hides the branch's commits. A history that cannot be read, such as a
  merge on the validation branch, also stops a plain `abort` when a non-empty commit
  follows the reviewed head, or when Git cannot open the validation checkout.
