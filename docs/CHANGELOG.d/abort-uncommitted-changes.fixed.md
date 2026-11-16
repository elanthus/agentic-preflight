- In `strict` and `reusable` worktree modes, a plain `abort` no longer deletes
  uncommitted changes in the validation worktree. It exits 5 with `unmerged_work` and
  lists the paths in the new `data.uncommitted_paths`, alongside any unmerged fix
  commits; copied files and ignored files do not count, as in `gc`. `--force` still
  discards them. `in_place` runs are unchanged.
