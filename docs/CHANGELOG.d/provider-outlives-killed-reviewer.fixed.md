- On POSIX, the provider CLI that `agentic-preflight reviewer run` started is now killed when
  the reviewer itself is killed. Before, a `[stage] timeout_seconds` that expired during a
  command review, or a SIGTERM, SIGINT, or SIGHUP sent to the CLI during one, killed the
  reviewer and left `claude` or `codex` running in its own process group.
