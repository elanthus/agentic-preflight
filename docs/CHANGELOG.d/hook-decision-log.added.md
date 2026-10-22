- Log each pre-push hook decision to `hook-decisions.jsonl` in the local telemetry
  directory: the time, the repository identity, the remote name, whether the push was
  allowed, a machine-readable reason (`attested`, `already_on_remote`, `deletion`,
  `exempt`, `no_updates`, `config_unavailable`, `force_push_refused`, or
  `missing_attestation`), and each ref update with a seven-character SHA and whether it
  rewrites remote history. The write is best effort, bounded in time, and never changes
  the hook's exit code or message. Nothing is sent over the network.

- Add `[telemetry] hook_log` (default `true`) to turn the hook decision log off.

- `agentic-preflight report` adds `data.hook_decisions` and a "Hook decisions" Markdown
  section: allowed and blocked counts, blocked decisions by reason, and per-repository
  counts, with the report's scope and its `--repo`, `--since`, and `--until` filters.
  Malformed log lines are counted. The limits now state that `git push --no-verify`
  bypasses and pushes allowed because the tool was missing from `PATH` are not logged.
