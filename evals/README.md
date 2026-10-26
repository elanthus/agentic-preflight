# Public regression eval

This directory contains synthetic cases for smoke tests of the Agentic Preflight product workflow.
Method `public-smoke-v6` gives each reviewed snapshot a separate Git repository with two commits and neutral metadata.
Each small fictional project has base, vulnerable, and fixed snapshots.
The runner creates real Git repositories.
It runs `init`, `start`, `context`, and command review through `python -m agentic_preflight`.
The repositories and a data directory are in one temporary workspace, which the runner deletes at the end.
Each CLI subprocess gets `XDG_DATA_HOME` and `XDG_CACHE_HOME` in that workspace.
Thus, an eval run does not write to your default telemetry directory or to your worktree cache.
An explicit `[telemetry] path` in your user file still applies.
See [Repositories without a network remote](../docs/local-telemetry.md#repositories-without-a-network-remote).

This corpus does not contain private cases or gold artifacts from the private decision-quality evaluation.
Do not use it as a replacement or comparison for that evaluation.
The scripted dry run verifies orchestration and scoring. It does not measure reviewer judgment.

To run without a model, use this command:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

The pytest suite checks repeated scripted scoring outcomes separately from measured timing.
There is no committed reference summary or separate dry-eval CI job.
Tests validate all corpus cases. Git-history isolation tests use one case per selected snapshot.

Real mode uses the example Codex or Claude reviewer wrapper.
It requires explicit authorization.
The default `--grounding both` run launches 48 reviewer-wrapper invocations per executor.
That count does not measure provider requests, tokens, or cost.

To run real mode with authorization, use these commands:

```console
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor codex --out /tmp/ap-eval-codex
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor claude --out /tmp/ap-eval-claude
```

Pass `--grounding on` or `--grounding off` to run one setting.
One setting uses 24 wrapper invocations in real mode.
The output directory receives `summary.json` and `summary.md`.
These reports include scores, full evaluation and snapshot runtimes, findings, workflow state visits and timing, and CLI token usage.
Timestamps and measured durations cause report bytes to vary between runs.

`reviewer_invocations` records wrapper launches.
Token accounting separates exact totals from partial observations. Missing evidence stays `null`.
Dry-mode provider requests, tokens, and cost are zero.
Real-mode request counts and cost remain `null`.
See [the full method and limits](../docs/regression-eval.md).

For published real-run evidence, see
[Opus 5.5 and GPT-6.1 Sol at low effort, 2026-10-17](../docs/evals/2026-10-17-opus-sol-low/README.md).
