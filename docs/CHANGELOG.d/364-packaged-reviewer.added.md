- `agentic-preflight reviewer run --provider claude|codex` runs the independent command
  reviewer from the package. Configure it as `[review] command` (and as
  `[review.contracts] planner_command`). It is the former example wrapper, moved into
  the package with the same prompts, provider CLI arguments, `AP_*` environment
  variables, defaults, events file, and output. Unlike the wrapper, it reads and writes
  its own standard streams as UTF-8 instead of the locale encoding. It shells out to the `claude` or `codex` CLI and adds
  no model SDK dependency. Like `hook-check`, it prints no JSON envelope, and a usage
  error exits 2 with Click's usage message. See ADR 0003 and
  `docs/independent-review.md`.
