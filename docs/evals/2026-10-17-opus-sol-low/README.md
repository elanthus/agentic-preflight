# Opus 5.5 and GPT-6.1 Sol at low effort — 2026-10-17

This public smoke run measured runtime, submitted findings, workflow states, and CLI token
usage for two independent command reviewers. GPT-6.1 Sol finished 8.2% faster. Opus 5.5
caught more seeded defects and also flagged more fixed controls. These observations describe
one run of small synthetic cases; they do not establish a general model ranking.

The run took place on October 17, 2026 in America/Los_Angeles (October 18 UTC), using
`public-smoke-v6` at source commit
[`b99ef109f1f012926d7ca508100957ca51406b72`](https://github.com/elanthus/agentic-preflight/tree/b99ef109f1f012926d7ca508100957ca51406b72).
Each reviewer ran 48 snapshots: 12 cases × vulnerable/fixed × grounding on/off. Claude ran
first, then Codex, on the same host. Both used explicit `low` effort and a 600-second
reviewer timeout. Grounding-on ran before grounding-off for each reviewer.

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

Findings count accepted submissions across all snapshots, without deduplicating across
grounding settings. A gold-location match uses the
[published scorer](https://github.com/elanthus/agentic-preflight/blob/b99ef109f1f012926d7ca508100957ca51406b72/evals/run.py),
not a separate human adjudication of each finding. In particular, a finding at the gold
location in a fixed snapshot can describe a different concern; the fixed-control rate is
the scorer's false-positive measure, not an adjudicated precision estimate.

Codex missed `changelog-phantom-fix`, `readme-removed-flag`, and `wrong-config-default` in
both grounding settings. These are the three documentation-contract cases. All other
vulnerable cases matched for both reviewers. The full findings, paths, lines, and scoring
outcomes are retained in the per-reviewer JSON reports.

### Grounding settings

| Reviewer | Grounding | Snapshot runtime sum | Findings | Vulnerable matches | Fixed-control matches |
|---|---|---:|---:|---:|---:|
| Opus 5.5 | On | 301.931 s | 32 | 12/12 | 5/12 |
| Opus 5.5 | Off | 284.945 s | 26 | 12/12 | 4/12 |
| GPT-6.1 Sol | On | 265.374 s | 11 | 9/12 | 0/12 |
| GPT-6.1 Sol | Off | 273.470 s | 12 | 9/12 | 0/12 |

These runtimes sum the snapshots in each setting. Full suite runtime also includes shared
case loading and temporary-workspace cleanup. Snapshot checkpoint writes add a small amount
of suite overhead outside the unmodified snapshot timers. Order and cache warming were not
controlled, so the setting differences cannot be attributed to grounding alone.

### Workflow states

Durations accumulate repeated visits and are measured in seconds. The eval ends after
review and automatic stage skips; it does not run a complete docs/lint/test/publish
lifecycle. `REVIEW_BLOCKED` records accepted blocking findings, not a failed or unresolved
eval. `DOCS_GREEN` can be reached by an automatic skip and does not imply a docs review was
executed.

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

State time starts at run creation, while full runtime starts before repository construction.
The approximately 33.856 s (Opus) and 32.375 s (Sol) outside recorded states also include
work before creation, work after observation, and suite overhead.

### Token usage

These are complete CLI-reported totals for the 48 invocations per reviewer. Both inputs
include cached tokens. Claude input is normalized as uncached input + cache reads + cache
creation. Codex input already includes cached input, and its output already includes
reasoning output; neither subset is added again.

| Token category | Opus 5.5 | GPT-6.1 Sol |
|---|---:|---:|
| Total input | 2,175,022 | 2,006,896 |
| Uncached input | 140 | 273,904 |
| Cache-read input | 1,231,921 | 1,732,992 |
| Cache-creation input | 942,961 | Unknown |
| Total output | 23,031 | 7,675 |
| Reasoning output, included in total output | Unknown | 1,221 |
| **Input + output** | **2,198,053** | **2,014,571** |

All 96 usage records have complete input/output totals, with no partial or unknown records.
Unsupported category breakdowns remain `null`. Provider requests and monetary cost remain
`null`; 48 wrapper launches do not establish either quantity. Different tokenizers and
CLI system/tool contexts also limit direct interpretation of the token comparison.

## Evidence and provenance

- [Claude summary JSON](claude.summary.json) and [Codex summary JSON](codex.summary.json)
  contain all 96 snapshot results, including findings, score matches, timing, ordered state
  visits, token evidence, and failure fields. All failure fields are `null` in this run.
- [Comparison JSON](comparison.json) contains aggregate metrics and all paired outcomes.
- [Snapshot CSV](snapshots.csv) and [state CSV](states.csv) provide tables for analysis.
  Empty CSV cells preserve unavailable values; use JSON to distinguish `null` explicitly.
- [Provenance](provenance.json) records the source commit and corpus tree, requested models, CLI
  versions, equivalent commands, original and published artifact hashes, and transformations.
- [SHA256SUMS](SHA256SUMS) checks the published files: run `shasum -a 256 -c SHA256SUMS`
  from this report directory.

The summary JSON was reformatted to keep each snapshot compact; parsed values are unchanged
from the saved run. CSV line endings were normalized from CRLF to LF without changing
parsed rows. The comparison's machine-local manifest
was replaced with a reference to portable provenance. No model calls were repeated for
publication. Original CLI event streams were temporary and are not included; the reports
retain normalized usage and accepted findings, not raw prompt/response transcripts.

The original launcher observed snapshots around the unchanged `run_evaluation` function,
writing checkpoints and collecting reported model identifiers. That local launcher is not
included here; its recorded hash is retained in provenance. Claude's captured events
reported `claude-opus-5-5`; Codex was requested with `--model gpt-6.1-sol`, but its captured
event stream did not independently report a resolved model identifier.

## Repeat the method

From a checkout of source commit `b99ef109f1f012926d7ca508100957ca51406b72`, install the
development environment with `uv sync --group dev`, authenticate both CLIs, and select
accounts that support the model IDs. The measured CLI versions are recorded in the results
table. Each command launches 48 reviewer wrappers and may consume provider quota; actual
requests and cost are not bounded by that count. These are equivalent uninstrumented
commands, not the original observer launcher:

```console
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=claude-opus-5-5 AP_REVIEWER_EFFORT=low AP_REVIEWER_TIMEOUT=600 \
  uv run python evals/run.py --mode real --executor claude --grounding both --out /tmp/ap-eval-claude-low
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=gpt-6.1-sol AP_REVIEWER_EFFORT=low AP_REVIEWER_TIMEOUT=600 \
  uv run python evals/run.py --mode real --executor codex --grounding both --out /tmp/ap-eval-codex-low
```

Runtime, provider caching, and model outputs vary. These commands reproduce the method,
not the observed findings or report bytes. The original host's complete provider account
and global CLI configuration were not archived.

## Limits

This is one sequential run per reviewer over 12 toy projects, each repeated across two
grounding settings. The observations are not independent samples from production changes,
and the single run does not establish statistical significance or a causal grounding effect.
The cases have legible defects, intents that name the intended property, and fixed controls
scored by location. See the [full smoke-method limitations](../../regression-eval.md#limits).
These results are not comparable to the private decision-quality evaluation and do not
measure a full repair, validation, or publication workflow.
