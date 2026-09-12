# Project evidence and engineering notes

This page collects the evaluation, implementation, and differentiation material that a
portfolio or resume reviewer may want when assessing Agentic Preflight. Product users
should start with the [README](../README.md).

## Sustained dogfooding

Across two observation windows—August 3–17 and August 18–September 6, 2026—the public
record of four owner-operated repositories contains 408 merged pull requests. Of those,
323 descriptions explicitly record Agentic Preflight use and 64 contain a concrete
finding record, including documentation issues and deliberately retained observations.
The follow-up adds schema-adaptation, spending-cap, and retrieval-coverage findings,
alongside cases where later review and CI still found gaps.

These numbers show sustained dogfooding. They do not establish external adoption or a
count of distinct bugs fixed. Read the
[case study, representative findings, methodology, and limits](dogfooding-case-study.md)
before interpreting them.

## Evaluation evidence

The [public regression eval](regression-eval.md) is a synthetic smoke corpus that drives
the real command-review product path across vulnerable and fixed toy snapshots. Its dry
mode checks deterministic plumbing and scoring. It is not the private decision-quality
evaluation, and its results are not comparable to that evaluation.

The [independent-review guide](independent-review.md) explains how the project runs and
measures a second command-line reviewer over the same snapshot-bound review bundle.

The README animation is real CLI output recorded with
[VHS](https://github.com/charmbracelet/vhs). Run
the [release guide’s recording procedure](RELEASING.md#cutting-a-release) from a source
checkout to regenerate it with that checkout’s CLI in a throwaway repository containing
the demonstrated unguarded division defect. The coding agent supplies the judgment between commands; the package supplies the workflow
and record.

## Engineering choices

Agentic Preflight is a deterministic state machine with a JSON-over-stdout CLI. The core
package is daemonless and does not call a model. It delegates judgment to the coding agent
already active in the workspace or, when repository policy requires one, an independent
command reviewer.

The implementation emphasizes Git semantics and durable, inspectable evidence:

- State transitions make review, documentation, lint, test, merge-back, and approval
  load-bearing gates. Inapplicable stages advance through explicit skip transitions.
- Review submissions bind complete changed-hunk coverage to a snapshot manifest instead
  of accepting a findings-only payload.
- Successful runs produce schema-validated Git notes bound to the exact commit and tree.
- Deterministic path policy and recorded finding severity derive risk; the reviewer cannot
  lower the policy verdict.
- In-place, reusable, and fresh strict worktree modes support different isolation and
  cache tradeoffs, while run ownership remains scoped to each source worktree.
- Tests use real Git repositories rather than mocking Git behavior.

The principal design boundaries are recorded in
[ADR 0001](adr/0001-orchestration-boundaries.md) and
[ADR 0002](adr/0002-scope-run-ownership-to-worktrees.md).
[State-machine guarantees and recovery](state-machine.md) distinguishes graph ordering
from coordinator checks, local record commits, and reconciliation of Git effects. The
[fingerprint contract](fingerprint-contract.md) specifies when stage evidence may apply
to a rewritten commit, and the [context-grounding design](context-grounding.md) explains
how reviews receive bounded repository-owned context without a model or network call.

CI rejects overall test coverage below 85% and installs the built wheel as a `uv` tool
before invoking the CLI. See the [contributor guide](../CONTRIBUTING.md) for the local
development workflow.

## Prior art and differentiation

Agentic Preflight was inspired by [`no-mistakes`](https://github.com/kunchenguid/no-mistakes)
and its staged review, test, documentation, lint, push, pull-request, and CI workflow. As
of [`no-mistakes` v1.48.0](https://github.com/kunchenguid/no-mistakes/releases/tag/v1.48.0),
both projects bind publication to reviewed work and both emit structured evidence. They
make different tradeoffs about workflow ownership and what the durable record proves:

| Area | `no-mistakes` | Agentic Preflight |
|---|---|---|
| Agent execution | Launches a required, configurable pipeline agent with ordered fallbacks | Uses the active coding agent by default; an external command reviewer can be required by risk |
| Git integration | Routes an opted-in push through a local proxy remote | Uses an advisory pre-push hook; manual mode disables the CLI's own push path |
| Stage control | Fixes the stage order but permits per-run and approval-time skips | Makes every gate load-bearing; only explicit code/config-driven skips traverse it and record a reason |
| Review completeness | Reviews the diff and records the exact approved head | Inventories every included changed hunk and non-text change after `[diff] exclude`, then requires a snapshot-bound `examined: "all"` assertion and derives a cited/clean receipt |
| Durable evidence | Writes a data-only step-status snapshot into the PR body; it can become stale until the body is rewritten | Atomically pushes a schema-validated Git note bound to the exact commit and tree, with config/intent bindings, review coverage, executor evidence, and shell-output hashes |
| Risk and approval | The reviewer returns `risk_level` and rationale; findings pause for user action | Repository path policy and recorded findings deterministically derive risk; the model cannot lower the verdict |
| Publication approval | Automatically forwards the validated branch after the local pipeline | Shows the exact remote, branch, commits, and risk before a token-gated push, or refuses its own push in manual mode |
| Local architecture | Runs a daemon, proxy repository, SQLite store, TUI, and disposable worktrees | Runs as a daemonless JSON-over-stdout CLI with file-based state and an agent skill |
| Validation checkout | Always isolates the pipeline in a disposable worktree | Offers in-place, reusable isolated, and fresh strict worktree modes |
| Hosted lifecycle | Creates PRs across several forges, monitors CI, and can auto-fix failures | Uses the active agent and `gh` for PR creation, check monitoring, and opt-in cleanup; opt-in test authority adds protected dispatch and live CI verification in the CLI |
| Runtime and platforms | Ships as a Go application for macOS, Linux, and Windows | Ships as a Python package for supported macOS, Linux, and Windows combinations |

## Scope of the evidence

The case study is observational, the public regression corpus is synthetic, and clean
review receipts prove reported coverage rather than reviewer understanding. Together,
the artifacts show the project's design choices, reproducibility practices, and use on
owner-operated repositories. They do not demonstrate external adoption or replace an
independent product-quality assessment.
