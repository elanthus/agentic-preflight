- Removing a strict validation worktree that `git worktree lock` protects no longer deletes
  its directory. `abort` and `finish` stop with an error that names the locked worktree, and
  `gc` retains it with the reason `validation worktree is locked`.
