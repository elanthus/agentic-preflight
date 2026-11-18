- On POSIX, `agentic-preflight reviewer run` now starts a watcher process that kills the
  provider CLI when the reviewer itself is killed. Before, a `[stage] timeout_seconds` that
  expired during a command review, or a SIGTERM, SIGINT, or SIGHUP sent to the CLI during
  one, killed the reviewer and left `claude` or `codex` running in its own process group.
  The provider CLI still runs on in two cases: the watcher process could not be started, or
  the reviewer was killed in the instant between starting the provider CLI and naming it to
  the watcher.
