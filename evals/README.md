# Public regression eval

This directory is a synthetic smoke corpus for the real Agentic Preflight product path.
Method `public-smoke-v2` gives each reviewed snapshot an isolated two-commit Git repository
with neutral metadata. Each tiny, plainly fictional project has base, vulnerable, and fixed
snapshots. The runner
creates real Git repositories and drives `init`, `start`, `context`, and command review
through `python -m agentic_preflight`.

It is not the private decision-quality evaluation, does not contain private cases or gold
artifacts, and must not be used as a substitute or comparison point for that evaluation.
The scripted dry run proves orchestration and scoring, not reviewer judgment.

Run the deterministic, model-free mode:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

Real mode uses the worked Codex or Claude reviewer wrapper. It is deliberately authorization
gated because the default `--grounding both` run makes 48 model calls per executor:

```console
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor codex --out /tmp/ap-eval-codex
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor claude --out /tmp/ap-eval-claude
```

Pass `--grounding on` or `--grounding off` to run one setting (24 model calls in real mode).
The output directory receives deterministic `summary.json` and a one-table `summary.md`.
See [the full method and limits](../docs/regression-eval.md).
