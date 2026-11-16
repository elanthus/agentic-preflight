- A low- or medium-severity `auto_fix` finding left open at a green review or docs stage
  no longer lets the run continue to lint, test, and `mergeback`. There, `respond` was no
  longer legal and `mergeback` failed with an internal error, leaving the run in
  `MERGEBACK_PENDING` with a retry hint that could never succeed. `context --section docs`
  and `stage run lint` now refuse with exit 3 and `open_findings`, naming the findings and
  the `respond` command, and `status` gives the same `next`. `verify` no longer skips a
  disabled docs stage past such a finding. `mergeback` refuses such a run from an earlier
  release the same way, before it records an attempt or touches the source branch, and
  points to `abort`; for a run that release's error left mid-merge, it says the branch may
  already hold the fix commits. A run whose validation branch changed after its tests
  returns to review instead, where `respond` is legal again.
- In isolated modes, `abort` from a lint or test state now counts commits on the
  validation branch that no `respond` or stage registered, and stops with `unmerged_work`
  instead of deleting them with the branch. The `open_findings` refusal and `status` list
  them in `data.fix_commits`.
