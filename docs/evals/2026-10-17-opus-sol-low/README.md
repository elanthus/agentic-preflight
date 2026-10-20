# Opus 5.5 and GPT-6.1 Sol at low effort — 2026-10-17

This public smoke run measured runtime, submitted findings, workflow states, and CLI token usage for two independent command reviewers.
GPT-6.1 Sol finished 8.2% faster.
Opus 5.5 found more seeded defects and flagged more fixed controls.
These observations cover one run of small synthetic cases. They do not establish a general model ranking.

The run occurred on October 17, 2026, in America/Los_Angeles (October 18 UTC).
It used `public-smoke-v6` at source commit
[`b99ef109f1f012926d7ca508100957ca51406b72`](https://github.com/elanthus/agentic-preflight/tree/b99ef109f1f012926d7ca508100957ca51406b72).

Each reviewer ran 48 snapshots: 12 cases × vulnerable/fixed × grounding on/off.
Claude ran first. Codex ran next on the same host.
Both used explicit `low` effort and a 600-second reviewer timeout.
Each reviewer ran with grounding on before grounding off.

## Results

| Measurement | Claude Opus 5.5 | GPT-6.1 Sol |
|---|---:|---:|
| Requested model ID | `claude-opus-5-5` | `gpt-6.1-sol` |
| CLI version | Claude Code 2.1.292 | codex-cli 0.163.0 |
| Full suite runtime | 587.431 s (9m 47s) | 539.373 s (8m 59s) |
| Median snapshot runtime | 11.279 s | 11.217 s |
| Submitted findings | 58 | 23 |
| Findings on vulnerable snapshots | 41 | 23 |
| Findings on fixed snapshots | 17 | 0 |
| Vulnerable trials with a gold-location match | 24/24 (100%) | 18/24 (75%) |
| Fixed controls with a gold-location match | 9/24 (37.5%) | 0/24 (0%) |
| Unresolved snapshots | 0/48 | 0/48 |
| Complete usage records | 48/48 | 48/48 |
| Complete workflow histories | 48/48 | 48/48 |
| Distinct workflow states / total visits | 9 / 397 | 9 / 414 |

The finding counts include accepted submissions from all snapshots.
They include repeated findings across grounding settings.
The [published scorer](https://github.com/elanthus/agentic-preflight/blob/b99ef109f1f012926d7ca508100957ca51406b72/evals/run.py)
checks whether a finding matches the expected defect location, called the gold location.
No separate human review classified each finding.

A finding at the gold location in a fixed snapshot can describe a different concern.
The fixed-control rate is the scorer's false-positive measure.
It is not a precision estimate based on human decisions about each finding.

Codex missed `changelog-phantom-fix`, `readme-removed-flag`, and `wrong-config-default` in both grounding settings.
These three cases test documentation contracts.
Both reviewers matched all other vulnerable cases.
The JSON reports for each reviewer retain complete findings, paths, lines, and scoring outcomes.

### Grounding settings

| Reviewer | Grounding | Snapshot runtime sum | Findings | Vulnerable matches | Fixed-control matches |
|---|---|---:|---:|---:|---:|
| Opus 5.5 | On | 301.931 s | 32 | 12/12 | 5/12 |
| Opus 5.5 | Off | 284.945 s | 26 | 12/12 | 4/12 |
| GPT-6.1 Sol | On | 265.374 s | 11 | 9/12 | 0/12 |
| GPT-6.1 Sol | Off | 273.470 s | 12 | 9/12 | 0/12 |

These runtimes are the sum of snapshot runtimes for each setting.
The full suite runtime also includes shared case loading and cleanup of temporary workspaces.
Snapshot checkpoint writes add a small amount of time outside the unchanged snapshot timers.
The run did not control order or cache warming.
Thus, grounding alone cannot explain the differences between settings.

### Workflow states

Durations include repeated visits and use seconds.
The evaluation ends after review and automatic stage skips.
It does not run the complete documentation, lint, test, and publication workflow.

`REVIEW_BLOCKED` records accepted blocking findings. It does not mean that the evaluation failed or remains unresolved.
An automatic skip can produce `DOCS_GREEN`. This state does not prove that a documentation review ran.

| State | Opus seconds | Opus visits | Sol seconds | Sol visits |
|---|---:|---:|---:|---:|
| `CREATED` | 0.773240 | 48 | 0.749344 | 48 |
| `WORKTREE_READY` | 0.001165 | 48 | 0.001181 | 48 |
| `SYNC_RUNNING` | 6.643193 | 48 | 6.564051 | 48 |
| `SYNC_GREEN` | 0.000973 | 48 | 0.000967 | 48 |
| `REVIEW_AWAITING_FINDINGS` | 39.234806 | 96 | 39.120522 | 96 |
| `REVIEW_COMMAND_RUNNING` | 503.347997 | 48 | 454.346013 | 48 |
| `REVIEW_BLOCKED` | 0.825646 | 28 | 0.420416 | 14 |
| `REVIEW_GREEN` | 0.988173 | 20 | 1.903882 | 34 |
| `DOCS_GREEN` | 1.759899 | 13 | 3.891604 | 30 |
| **Total recorded state time / visits** | **553.575092** | **397** | **506.997979** | **414** |

State time starts at run creation. Full runtime starts before repository construction.
Time outside recorded states is approximately 33.856 s for Opus and 32.375 s for Sol.
This time includes work before creation, work after observation, and additional suite work.

### Token usage

The CLI reported complete token totals for all 48 invocations per reviewer.
Both input totals include cached tokens.
Claude input is the sum of uncached input, cache reads, and cache creation.
Codex input already includes cached input. Codex output already includes reasoning output.
These Codex subsets are not added again.

| Token category | Opus 5.5 | GPT-6.1 Sol |
|---|---:|---:|
| Total input | 2,175,022 | 2,006,896 |
| Uncached input | 140 | 273,904 |
| Cache-read input | 1,231,921 | 1,732,992 |
| Cache-creation input | 942,961 | Unknown |
| Total output | 23,031 | 7,675 |
| Reasoning output, included in total output | Unknown | 1,221 |
| **Input + output** | **2,198,053** | **2,014,571** |

All 96 usage records have complete input/output totals. No record has partial or unknown totals.
Unsupported token categories remain `null`.

Provider requests and monetary cost also remain `null`.
The 48 wrapper launches do not establish either quantity.
The providers use different tokenizers and CLI system/tool contexts. These differences limit direct comparisons of token counts.

## Evidence and provenance

- [Claude summary JSON](claude.summary.json) and [Codex summary JSON](codex.summary.json) contain all 96 snapshot results.
  They include findings, score matches, timing, ordered state visits, token evidence, and failure fields.
  All failure fields are `null` in this run.
- [Comparison JSON](comparison.json) contains aggregate metrics and all paired outcomes.
- [Snapshot CSV](snapshots.csv) and [state CSV](states.csv) contain tables for analysis.
  Empty CSV cells represent unavailable values. Use JSON to identify `null` values explicitly.
- [Provenance](provenance.json) records the source commit, corpus tree, requested models, CLI versions, and equivalent commands.
  It also records original and published artifact hashes and transformations.
- [SHA256SUMS](SHA256SUMS) contains checksums for the published files.
  To check the files, run `shasum -a 256 -c SHA256SUMS` from this report directory.

The published summary JSON uses compact snapshot formatting.
Its parsed values are unchanged from the saved run.
The CSV files use LF line endings instead of CRLF. Their parsed rows are unchanged.
A reference to portable provenance replaces the comparison's local manifest.
Publication did not repeat any model calls.

The original CLI event streams were temporary and are not included.
The reports retain normalized usage and accepted findings. They do not contain raw prompt/response transcripts.

The original local launcher observed snapshots around the unchanged `run_evaluation` function.
It wrote checkpoints and collected reported model identifiers.
The launcher is not included. Provenance retains its recorded hash.
Claude's captured events reported `claude-opus-5-5`.
Codex received `--model gpt-6.1-sol`, but its captured events did not independently report a resolved model identifier.

## Repeat the method

Use a checkout of source commit `b99ef109f1f012926d7ca508100957ca51406b72`.
The results table records the measured CLI versions.

1. Install the development environment with `uv sync --group dev`.
2. Authenticate both CLIs.
3. Select accounts that support the model IDs.
4. Run the following commands.

Each command launches 48 reviewer wrappers and can consume provider quota.
This count does not limit actual requests or cost.
These equivalent commands do not include the original observer launcher.

```console
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=claude-opus-5-5 AP_REVIEWER_EFFORT=low AP_REVIEWER_TIMEOUT=600 \
  uv run python evals/run.py --mode real --executor claude --grounding both --out /tmp/ap-eval-claude-low
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=gpt-6.1-sol AP_REVIEWER_EFFORT=low AP_REVIEWER_TIMEOUT=600 \
  uv run python evals/run.py --mode real --executor codex --grounding both --out /tmp/ap-eval-codex-low
```

Runtime, provider caching, and model outputs vary.
These commands reproduce the method. They do not reproduce the observed findings or exact report bytes.
The archive does not include the original host's complete provider account and global CLI configuration.

## Limits

This report covers one sequential run per reviewer over 12 toy projects.
Each project repeats across two grounding settings.
The observations are not independent samples from production changes.
The single run does not establish statistical significance or show that grounding caused a change.

The cases have defects that are easy to identify. Their intents name the intended property.
The scorer uses location to score fixed controls.
See the [full smoke-method limitations](../../regression-eval.md#limits).
Do not compare these results with the private decision-quality evaluation.
These results do not measure a complete repair, validation, or publication workflow.
