# Prior art and differentiation

Agentic Preflight was inspired by [`no-mistakes`](https://github.com/kunchenguid/no-mistakes)
and its staged review, test, documentation, lint, push, pull-request, and CI workflow. As
of [`no-mistakes` v1.48.0](https://github.com/kunchenguid/no-mistakes/releases/tag/v1.48.0),
both projects bind publication to reviewed work and both emit structured evidence. They
make different tradeoffs about workflow ownership and what the stored record proves:

| Area | `no-mistakes` | Agentic Preflight |
|---|---|---|
| Agent execution | Launches a required, configurable pipeline agent with ordered fallbacks | Uses the active coding agent by default; an external command reviewer can be required by risk |
| Git integration | Routes an opted-in push through a local proxy remote | Uses an advisory pre-push hook; manual mode disables the CLI's own push path |
| Stage control | Fixes the stage order but permits per-run and approval-time skips | Requires every stage; only explicit code- or config-driven skips bypass one, and each records a reason |
| Review completeness | Reviews the diff and records the exact approved head | Inventories every included changed hunk and non-text change after `[diff] exclude`, then requires a snapshot-bound `examined: "all"` assertion and derives a cited or clean result for each unit |
| Stored evidence | Writes a data-only step-status snapshot into the PR body; it can become stale until the body is rewritten | Atomically pushes a schema-validated Git note bound to the exact commit and tree, with config/intent bindings, review coverage, executor evidence, and shell-output hashes |
| Risk and approval | The reviewer returns `risk_level` and rationale; findings pause for user action | Repository path policy and recorded findings deterministically derive risk; the model cannot lower the verdict |
| Publication approval | Automatically forwards the validated branch after the local pipeline | Shows the exact remote, branch, commits, and risk before a token-gated push, or refuses its own push in manual mode |
| Local architecture | Runs a daemon, proxy repository, SQLite store, TUI, and disposable worktrees | Runs as a daemonless JSON-over-stdout CLI with file-based state and an agent skill |
| Validation worktree | Always isolates the pipeline in a disposable worktree | Offers in-place, reusable isolated, and fresh strict worktree modes |
| Hosted lifecycle | Creates PRs across several forges, monitors CI, and can auto-fix failures | Uses the active agent and `gh` for PR creation, check monitoring, and opt-in cleanup; opt-in test authority adds protected dispatch and live CI verification in the CLI |
| Runtime and platforms | Ships as a Go application for macOS, Linux, and Windows | Ships as a Python package for supported macOS, Linux, and Windows combinations |
