- `status` on a run whose independent review command was interrupted now points to
  `agentic-preflight review run`, the command that records the interruption and retries.
  It used to point back to `status`, which does not recover the run.
