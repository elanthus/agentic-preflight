- Add an optional `--reason` to `respond --action dismissed|accepted`, from the closed
  list `false_positive`, `out_of_scope`, `duplicate`, `already_fixed`, `accepted_risk`,
  `deferred`, and `other`, and refuse it with `--action fixed`. The finding stores it as
  `response_reason`, and the `finding_resolved` event repeats it. It is telemetry only:
  stage evidence and attestations never contain it.
- Add an optional `--reason` to `abort`, from `user_cancelled`, `superseded`,
  `wrong_base`, `environment_failure`, `tool_error`, `no_longer_needed`, and `other`.
  The `aborted` event records it with `aborted_from_state`, the state that the abort left.
- Record `started_at` on each stage record when the stage begins: review at
  `BEGIN_REVIEW` and again at each return to review, docs at `BEGIN_DOCS`, lint at
  `RUN_LINT`, and test at `RUN_TEST`. Stage evidence, fingerprints, and attestations do
  not contain it.
- Run summaries add `findings.by_response_reason`, `terminal.abort_reason`,
  `terminal.aborted_from_state`, `stages.<name>.started_at` and `duration_seconds`, and
  `timing`: visits and wall-clock seconds per state from the `state_transition` events,
  with review and shell totals. An incomplete transition history is marked, and absent
  values stay `null`.
- `agentic-preflight report` adds "Finding responses" (reasons, and a perceived
  false-positive share with its denominator), "Abort reasons" (by reason and by state),
  the median and p90 duration of each stage, and the share of run time in the review and
  shell stages, for all runs and for each repository.
- The skill tells agents to always pass the closest `--reason` when they dismiss or
  accept a finding or abort a run, and to put the specifics in `--note`.
