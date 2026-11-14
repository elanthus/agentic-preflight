- `gc` and the `[retention] run_days` prune now reclaim a run that `abort --force` ended.
  That abort already discarded the run's fix commits, so `gc` no longer retains the run as
  "unmerged fix commits" forever.
