- A repository's `.agentic-preflight.toml` can no longer set `[telemetry] path`. Before,
  a repository's `[telemetry]` section replaced the user's, so a cloned repository could
  make every `git push` append `hook-decisions.jsonl`, and every finished run append
  `runs.jsonl`, to a directory it chose, including a Windows UNC path that opens an
  outbound SMB connection. The repository's `path` is now ignored with a warning, and a
  repository section can only turn `mirror` and `hook_log` off, never on.
- `[telemetry] mirror` and `hook_log` accept only `true` and `false`. Before, values
  such as `1` or `"yes"` were read as `true`, so a repository could use them to turn on a
  file that the user turned off.
