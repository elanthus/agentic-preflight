- Run summaries record what the gate caught before the push:
  `findings.review_blocking_fixed`, `findings.docs_blocking_fixed`, and
  `stages.lint.red_then_green` and `stages.test.red_then_green`.
- The tool times each external command that it runs on its own (sync, setup, the review
  command, lint, test, baseline, and merge-back) with new `tool_command_started` and
  `tool_command_finished` events. Summaries add `timing.tool_seconds` and, for runs with
  a complete transition history, `timing.agent_seconds`.
- `report` adds `caught` (the share of `DONE` runs with any catch, and a breakdown by
  source) and `tool_time` (median and p90 tool seconds per `DONE` run and per synced pull
  request), with "Caught before push" and "Tool time" Markdown sections.
