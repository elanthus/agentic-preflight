- `status` on a run that an earlier release started now returns `agentic-preflight abort`
  as `next.command` and says that this release cannot merge the run back, gate it, or push
  it. It used to return the state's ordinary next command, and after a refused `mergeback`
  it pointed back to `mergeback`. `status` also no longer checks or advances such a run's
  evidence, so it does not reopen review on it. `events`, `logs`, and a `start` that resumes
  the run report the same next command. A run that an earlier release already pushed is
  unaffected and still finishes with `finish`.
- `context`, `review contracts`, `review run`, `submit-findings`, `respond`, `verify`, and
  `stage run` now refuse such a run with exit 4, `needs_human`, and `data.reason`
  `unknown_config_provenance`, as `mergeback`, `gate`, and `push` already did. No review or
  stage runs for a run that cannot publish.
- The advice to cherry-pick recorded fix commits before aborting such a run now appears
  only in isolated modes. In `in_place` mode the fixes are already on the branch.
