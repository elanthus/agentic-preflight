- In `strict` and `reusable` worktree modes, a plain `abort` no longer deletes
  uncommitted changes in the validation worktree. It exits 5 with `unmerged_work` and
  lists up to 200 paths in the new `data.uncommitted_paths`, with the total in
  `data.uncommitted_path_count`, alongside any unmerged fix commits; copied files and
  ignored files do not count, as in `gc`. A worktree that cannot be inspected also
  stops it, with `data.inspection_error` set and the path fields `null`. `--force`
  still discards them. `in_place` runs are unchanged.
