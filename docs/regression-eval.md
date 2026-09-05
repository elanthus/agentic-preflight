# Public regression eval

The public regression eval is a small, synthetic smoke corpus that checks whether reviewer
findings travel through the actual Agentic Preflight product path and whether the resulting
submissions are scored consistently. It is designed to catch plumbing regressions in command
review, grounding, persistence, and reporting.

It is not the private decision-quality evaluation. No private case, gold file, artifact, or
report is present here, and results from this corpus are not comparable to the private
evaluation.

## Corpus design

The corpus contains 12 plainly fictional toy projects: three each for correctness, security,
evaluation integrity, and documentation contract failures. Every case has three complete
trees. Method `public-smoke-v2` builds a separate repository for each reviewed snapshot: the
base tree on `main`, followed by only the selected tree on `review/change`. Commit subjects
are `Initial snapshot` and `Proposed change`; the repository directory uses a random opaque
identifier. The unselected snapshot is never written to refs, reflogs, or the object database.
Fixed snapshots are false-positive controls.

Each scorer-only `gold.json` names one mechanism, vulnerable path and line range, category,
severity range, and the expectation that the finding is absent after the fix. Gold is never
copied into a fixture repository. Before review, the runner also rejects any snapshot that
contains `gold.json`, asserts that the serialized context bundle contains neither that name
nor the serialized gold record or case ID, rejects top-level scorer labels, and asserts that
the delivered intent exactly equals the case intent. Regression tests capture actual provider
stdin through both worked wrappers for both snapshots, and inspect all Git objects for the
unselected snapshot. Real mode removes inherited `AP_EVAL_SCRIPT` from the subprocess
environment; only dry mode receives the scripted answer path.

Dry mode uses canned findings, but still creates real Git repositories and invokes the real
CLI in subprocesses for `init --no-hook`, `start --intent`, `context`, and `review run`.
Grounding-on and grounding-off runs use the same flow. Fixture commit identity and dates are
fixed, and reports omit timestamps, so identical inputs produce byte-identical JSON.

## Scoring

A vulnerable snapshot is a catch when at least one finding has the gold path and either its
line or its cited review-unit hunk overlaps the vulnerable gold range. A fixed snapshot is a
false positive under the same location rule. Failures before an accepted submission are
reported as unresolved; they are not silently converted to catches or misses.

Severity agreement checks whether a matched vulnerable finding falls within the gold severity
range. Category agreement searches the matched finding's title and detail with a fixed keyword
map. The category measure is intentionally heuristic: it can confirm vocabulary, not whether
the reviewer's reasoning is sound. Severity and category agreement are reported separately
and never gate execution.

`summary.json` records `method_version: public-smoke-v2` and contains per-case snapshot evidence and aggregate catch, fixed false-positive,
unresolved, severity-agreement, and category-agreement values for each grounding setting.
`summary.md` presents the same case outcomes and aggregates in one table.

## Running dry mode

Dry mode makes no model calls:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

Use `--grounding on` or `--grounding off` for one setting; the default is `both`.

## Running real mode

Real mode consumes the worked configurations and standard-library wrappers in
`docs/examples/`. It refuses to start unless `AP_EVAL_AUTHORIZED=1` is present. With 12 cases,
two reviewed snapshots, and two grounding settings, each command below makes exactly
`12 × 2 × 2 = 48` model calls for its selected executor:

```console
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor codex --out /tmp/ap-eval-codex
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor claude --out /tmp/ap-eval-claude
```

These commands are prepared for maintainer authorization; they are not run by CI. Selecting
one grounding setting halves the call count to 24.

## Honest limits

The corpus is synthetic and tiny. Its defects are deliberately legible and do not represent
the breadth, ambiguity, or base rates of production changes. Scripted dry mode proves the
product plumbing and scoring math, not reviewer judgment. Real mode adds reviewer behavior but
costs model calls and remains sensitive to model and tool versions. Neither mode measures the
private evaluation, and its rates must not be compared with private decision-quality results.

## Method versions and evidence

`public-smoke-v1` (reports without `method_version`) created base, vulnerable, and fixed
commits in one repository and exposed case labels in Git metadata. Those runs cannot be
claimed as provider-blinded evidence: the reviewed workspace could reveal the selected
condition and the other snapshot. Version 2 changes this input boundary without changing
the location-based scoring rule. Do not relabel old reports as version 2.

The separate [public evaluation implementation](https://github.com/elanthus/preflight-eval-results)
publishes the decision-quality library, synthetic paired replay, and versioned limitations
of the historical aggregate results. Its offline replay uses scripted adjudication and makes
no provider calls. It does not reproduce the private corpus or measure model quality.
