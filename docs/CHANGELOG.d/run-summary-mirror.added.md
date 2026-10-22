- Write a compact, versioned `summary.json` (summary schema 1) of code-derived facts
  for each run when it reaches `DONE`, `ABORTED`, or `ORPHANED`, and append the same
  summary as one line to a local, user-level `runs.jsonl` mirror under
  `$XDG_DATA_HOME/agentic-preflight/telemetry` or
  `~/.local/share/agentic-preflight/telemetry`. Summaries record states, counts, and
  digests only: never the intent text, diffs, finding text, logs, or command output.
  Nothing is sent over the network, and a telemetry failure never fails the command.

- Add the `[telemetry]` configuration section (`mirror`, `path`), intended for the user
  configuration file. It is excluded from run snapshots, so it never changes a
  configuration digest, stage evidence, or an attestation.

- Add `agentic-preflight telemetry rebuild`, which regenerates the summaries of every
  terminal run record in a clone (run schema 1 and 2) and rewrites that repository's
  mirror rows, keeping the newest row per run and leaving other repositories' rows
  unchanged. `finish`, `abort`, and `gc` report the summary outcome in `data.telemetry`.
