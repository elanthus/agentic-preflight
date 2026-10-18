- Record full evaluation and snapshot runtimes, accepted findings, workflow state visits
  and durations, and Codex/Claude CLI token usage in public regression reports. Preserve
  failed-run diagnostics, unknown counts, and partial observations separately from exact
  totals; normalize provider cache accounting without double counting.
- Append state-transition audit events for run creation and committed transitions,
  including intermediate states within a transaction, without changing run schema 2.
