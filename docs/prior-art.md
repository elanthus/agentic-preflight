# Prior art and differentiation

[`no-mistakes`](https://github.com/kunchenguid/no-mistakes) inspired Agentic Preflight.
It has a staged workflow: review, test, documentation, lint, push, pull request, and CI.
This comparison uses [`no-mistakes` v1.48.0](https://github.com/kunchenguid/no-mistakes/releases/tag/v1.48.0).
Both projects bind publication to reviewed work. Both projects emit structured evidence.
They make different tradeoffs about workflow ownership and about what the stored record proves:

| Area | `no-mistakes` | Agentic Preflight |
|---|---|---|
| Agent execution | Launches a required, configurable pipeline agent with ordered fallbacks | Uses the active coding agent by default. Repository policy can require an external command reviewer by risk level |
| Git integration | Routes an opted-in push through a local proxy remote | Uses an advisory pre-push hook. Manual mode disables the CLI's own push path |
| Stage control | Fixes the stage order but permits per-run and approval-time skips | Requires every stage. Only explicit code-driven or configuration-driven skips bypass a stage, and each skip records a reason |
| Review completeness | Reviews the diff and records the exact approved head | Lists every included changed hunk and non-text change after `[diff] exclude`. Requires a snapshot-bound `examined: "all"` assertion. Derives a cited or clean result for each unit |
| Stored evidence | Writes a data-only step-status snapshot into the PR body. The snapshot can become stale until the body is rewritten | Atomically pushes a schema-validated Git note bound to the exact commit and tree. The note includes configuration and intent bindings, review coverage, executor evidence, and shell-output hashes |
| Risk and approval | The reviewer returns `risk_level` and a rationale. Findings pause for user action | Repository path policy and recorded findings derive the risk deterministically. The model cannot lower the verdict |
| Publication approval | Automatically forwards the validated branch after the local pipeline | Shows the exact remote, branch, commits, and risk before a token-gated push. In manual mode, refuses its own push |
| Local architecture | Runs a daemon, a proxy repository, a SQLite store, a TUI, and disposable worktrees | Runs as a daemonless CLI that prints JSON to stdout, with file-based state and an agent skill |
| Validation worktree | Always isolates the pipeline in a disposable worktree | Offers in-place, reusable isolated, and fresh strict worktree modes |
| Hosted lifecycle | Creates PRs on several forges, monitors CI, and can auto-fix failures | Uses the active agent and `gh` for PR creation, check monitoring, and opt-in cleanup |
| Runtime and platforms | Ships as a Go application for macOS, Linux, and Windows | Ships as a Python package for supported macOS, Linux, and Windows combinations |
