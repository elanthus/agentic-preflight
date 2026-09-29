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
trees. Each reviewed snapshot uses a separate repository: the base tree on `main`, followed
by only the selected tree on `review/change`. Commit subjects
are `Initial snapshot` and `Proposed change`; the repository directory uses a random opaque
identifier. The runner never copies or commits the unselected tree; blobs shared with the
base or selected tree can legitimately appear in the object database. Fixed snapshots are
false-positive controls.

Each scorer-only `gold.json` names one mechanism, vulnerable path and line range, category,
severity range, and the expectation that the finding is absent after the fix. Gold is never
copied into a fixture repository. Before review, the runner also rejects any snapshot that
contains `gold.json`, asserts that the serialized context bundle contains neither that name
nor the serialized gold record or case ID, and asserts that the delivered intent exactly
equals the case intent. A top-level scorer-label shape assertion guards against future runner
enrichment; the product CLI does not emit those fields today. Regression tests capture actual
reviewer-wrapper stdin through both worked wrappers for both snapshots. Across every corpus case and
both selected snapshots, they hash every file in all three trees and inspect all Git objects,
including unreachable objects, for blobs unique to the unselected tree. Shared content from
the base or selected tree is allowed. Real mode removes inherited `AP_EVAL_SCRIPT` from the
subprocess environment; only dry mode receives the scripted answer path.

Dry mode uses canned findings, but still creates real Git repositories and invokes the real
CLI in subprocesses for `init --no-hook`, `start --intent`, `context`, and `review run`.
Grounding-on and grounding-off runs use the same flow. Every fixture tree carries a
`CODEOWNERS` file and an `AGENTS.md`, and a grounding-on run fails unless `context` returns
at least one grounding entry for every reviewed snapshot. Fixture commit identity and dates are
fixed, and reports omit timestamps, so identical inputs produce byte-identical JSON.

## Scoring

A vulnerable snapshot is a catch when at least one finding has the gold path and a line
within two lines of the vulnerable gold range. A finding without a line matches when its cited
review-unit hunk overlaps the gold range; a finding with a line far from the gold range is
not rescued by the hunk. A fixed snapshot is a false positive under the same location rule.
Every fixed snapshot changes the gold file, so a finding on that file resolves to a review
unit and can count as a false positive.
Case loading fails unless every gold range intersects the lines changed from base to
vulnerable. Failures before an accepted submission are
reported as unresolved; they are not silently converted to catches or misses.

Severity agreement checks whether a matched vulnerable finding falls within the gold severity
range. Keyword hit rate is the share of matched findings whose title or detail contains a
substring from a fixed per-category keyword map. It confirms vocabulary, not whether the
reviewer's reasoning is sound, and the scripted details were written to hit the map, so the
dry-mode value is 1.0 by construction. Severity agreement and keyword hit rate are reported
separately and never gate execution.

`summary.json` records the current `method_version` and contains per-case snapshot evidence
and aggregate catch, fixed false-positive, unresolved, severity-agreement, and
keyword-hit-rate values for each grounding setting.
`summary.md` opens with the mode and executor, then presents the same case outcomes and
aggregates in one table: booleans as `yes` or `no`, missing values as `n/a`, and rates
with three decimals.

## Running dry mode

Dry mode launches the scripted reviewer wrapper for each snapshot, but makes no provider calls:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

Use `--grounding on` or `--grounding off` for one setting; the default is `both`.

CI runs dry mode and fails when its `summary.json` differs from the committed golden file
`evals/golden/dry-summary.json`. After an intentional corpus or scoring change, regenerate it:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
cp /tmp/agentic-preflight-evals/summary.json evals/golden/dry-summary.json
```

## Running real mode

Real mode consumes the worked configurations and standard-library wrappers in
`docs/examples/`. It refuses to start unless `AP_EVAL_AUTHORIZED=1` is present. With 12 cases,
two reviewed snapshots, and two grounding settings, each command below launches exactly
`12 × 2 × 2 = 48` reviewer-wrapper invocations for its selected executor:

```console
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor codex --out /tmp/ap-eval-codex
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor claude --out /tmp/ap-eval-claude
```

These commands are prepared for maintainer authorization; they are not run by CI. Selecting
one grounding setting halves the wrapper-invocation count to 24. A wrapper invocation is not
a provider request: a CLI can fail before calling a provider, retry, use tools, or make more
than one provider request. `summary.json` records `reviewer_invocations` and marks
`provider_requests`, `provider_tokens`, and `provider_cost_usd` as `null`; those measurements
are unavailable unless separately collected from provider telemetry. In dry mode all three are
known zero.
`AP_EVAL_AUTHORIZED=1` authorizes the disclosed wrapper launches only; it is not a
provider-request or spend cap.

## Limits

The corpus is synthetic and tiny. Its defects are deliberately legible and do not represent
the breadth, ambiguity, or base rates of production changes. Real-mode catch rates from this
corpus are inflated for three further reasons:

- Case intents name the property the defect violates, for example "without exposing the
  supplied credential in logs" or "keeping every read inside its configured storage root",
  so the reviewer is told where to look.
- About half the vulnerable diffs remove a protection that the base tree already has, such as
  a containment check or a `max` default. A reviewer can spot the deletion in the diff
  without reasoning about the defect.
- Two or three cases, such as the shell injection and the hardcoded token comparison, are
  caught by stock ruff rules that `evals/.ruff.toml` suppresses so the fixtures pass lint.

Scripted dry mode proves the
product plumbing and scoring math, not reviewer judgment. Real mode adds reviewer behavior but
launches external reviewer wrappers and remains sensitive to model and tool versions. Neither
mode measures the
private evaluation, and its rates must not be compared with private decision-quality results.

## Evaluation evidence

The separate [public evaluation implementation](https://github.com/elanthus/preflight-eval-results/tree/5f98146bd67f445aecb9d340e067b3609a97620d)
publishes the decision-quality library, synthetic paired replay, and versioned limitations
of the aggregate results. Its offline replay uses scripted adjudication and makes
no provider calls. It does not reproduce the private corpus or measure model quality.
