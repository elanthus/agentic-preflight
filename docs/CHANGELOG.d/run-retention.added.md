- `gc` deletes the run directory of a reclaimed run whose record last changed more than
  `[retention] run_days` days ago (default 90; `0` keeps every record). The envelope lists
  them in `data.pruned`. `[retention]` is excluded from run snapshots and digests.
