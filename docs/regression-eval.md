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
reviewer-wrapper stdin through both worked wrappers for both snapshots. For one representative case per
selected snapshot, they hash every file in all three trees and inspect all Git objects,
including unreachable objects, for blobs unique to the unselected tree. Shared content from
the base or selected tree is allowed. Real mode removes inherited `AP_EVAL_SCRIPT` from the
subprocess environment; only dry mode receives the scripted answer path.

Dry mode uses canned findings, but still creates real Git repositories and invokes the real
CLI in subprocesses for `init --no-hook`, `start --intent`, `context`, and `review run`.
Grounding-on and grounding-off runs use the same flow. Every fixture tree carries a
`CODEOWNERS` file and an `AGENTS.md`, and a grounding-on run fails unless `context` returns
at least one grounding entry for every reviewed snapshot. Fixture commit identity and dates are
fixed. Scoring is deterministic; runtime and state timing are measured independently and
make complete reports vary across runs.

## Scoring

A vulnerable snapshot is a catch when at least one finding has the gold path and a line
within two lines of the vulnerable gold range. A finding without a line matches when its cited
review-unit hunk overlaps the gold range; a finding with a line far from the gold range is
not rescued by the hunk. A fixed snapshot is a false positive under the same location rule.
Every fixed snapshot changes the gold file, so a finding on that file resolves to a review
unit and can count as a false positive.
Case loading fails unless every gold range intersects the lines changed from base to
vulnerable. Failures before an accepted submission are
reported as unresolved; they are not silently converted to catches or misses. A failed
`init`, `start`, `context`, or `review run` marks only that case and snapshot unresolved,
and the run continues to write `summary.json` and `summary.md`. Repository setup errors,
leakage guards, malformed CLI envelopes, and scoring errors also produce an unresolved
snapshot with its failed operation and diagnostic. Leakage guards still prevent the review
from starting. The complete failure envelope is retained when one was returned.

Severity agreement checks whether a matched vulnerable finding falls within the gold severity
range. Keyword hit rate is the share of matched findings whose title or detail contains a
substring from a fixed per-category keyword map. It confirms vocabulary, not whether the
reviewer's reasoning is sound, and the scripted details were written to hit the map, so the
dry-mode value is 1.0 by construction. Severity agreement and keyword hit rate are reported
separately and never block execution.

`summary.json` records the current `method_version` and contains per-case snapshot evidence
and aggregate catch, fixed false-positive, unresolved, severity-agreement, and
keyword-hit-rate values for each grounding setting.
`summary.md` opens with the mode and executor, then presents the same case outcomes and
aggregates in one table: booleans as `yes` or `no`, missing values as `n/a`, and rates
with three decimals. Additional tables present token categories and workflow states.

## Runtime, findings, states, and tokens

Method `public-smoke-v6` adds telemetry to both reports without changing the scoring rules:

- `started_at` and `finished_at` are UTC timestamps. `runtime_seconds` uses a monotonic
  clock: each snapshot includes repository construction, CLI execution, review, and scoring;
  the suite includes case loading, all snapshots, and temporary-workspace cleanup, ending
  before report serialization. Each grounding setting also records the sum of snapshot
  runtimes. Failed snapshots retain their runtime.
- `finding_count` and `findings` retain accepted submissions per snapshot. A failed run
  without an accepted submission has unknown findings, not zero. Aggregate `finding_count`
  is `null` if any snapshot is unmeasured; `observed_finding_count`,
  `finding_snapshots_measured`, and `observed_findings_by_severity` describe the measured
  subset. Counts represent submitted findings, without deduplicating across independent
  snapshots or grounding settings. Gold-location matches remain separate scoring evidence.
- `workflow` contains ordered state visits, the final state, and visit counts and seconds
  per state. State-transition audit events retain intermediate states within one committed
  transaction. Repeated visits accumulate. The last state's duration ends when the snapshot
  observation stops. Missing, inconsistent, or truncated history sets `complete` to false;
  an unknown final duration stays `null`, while `observed_duration_seconds` retains measured
  intervals. Monotonic times are compared only within this evaluation on the same host.
- Each snapshot's `usage` has a source, a `complete`, `partial`, or `unknown` status, and
  observed token categories. Aggregate `usage.totals` contains exact counts only when every
  snapshot has complete evidence for that category. `usage.observed` retains known counts
  from partial records; `usage.measurements` reports coverage by status. `provider_tokens`
  is the exact aggregate total or `null`. Do not read a partial observed count as a total.

Codex uses `codex exec --json` completion events. Input includes cached input; output
includes reasoning output, so those subsets are not added again. Claude uses
`--output-format stream-json --verbose` and prefers the final `modelUsage` aggregate,
which includes subagents. Claude's uncached input, cache-read input, and cache-creation
input are added to normalize total input. Its `result.usage` covers only the main loop
and is reported as partial when no whole-call per-model aggregate is available.
Assistant-message usage is deduplicated by message ID; its output count is a placeholder
and remains unknown. A crash result's zeroed counters do not erase preceding observations.
Error results, missing fields, malformed streams, and interrupted CLI processes retain
available evidence with partial or unknown status.

The harness captures CLI stdout directly to a temporary file outside the reviewer tree,
through the worked wrappers' `AP_REVIEWER_EVENTS_FILE` interface. This retains flushed
events on CLI failure, wrapper timeout, or an outer review-command timeout. A separate
completion marker prevents a killed process's earlier completion event being mistaken
for a complete invocation. Findings submission stdout stays unchanged. Temporary raw
events are removed after normalized metrics and diagnostics have been collected.
An abrupt termination of the eval runner itself can prevent report writing.

These are CLI-reported usage measurements, not independent billing verification or a
count of provider requests. Unsupported categories stay `null`. Known zero is reserved
for dry mode or an explicitly unlaunched wrapper. The smoke eval still ends after review;
its full runtime is the full evaluation, not a lint/test/merge-back lifecycle.

Usage semantics follow the providers' documentation:
[Codex JSONL events](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable)
and [Claude token accounting](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

## Running dry mode

Dry mode launches the scripted reviewer wrapper for each snapshot, but makes no provider calls:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

Use `--grounding on` or `--grounding off` for one setting; the default is `both`.

CI checks scripted catches, misses, fixed-snapshot false positives, and unresolved outcomes
through the pytest suite. A separate test runs identical inputs twice and compares scoring
with measured timing excluded. Reports are not compared with committed fixture commit hashes;
intentional corpus or configuration changes do not require regenerating a summary golden.
All cases retain fixture-validation coverage.

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
`provider_requests` and `provider_cost_usd` as `null`; wrapper launches do not establish
either quantity. Token usage is measured from CLI output as described above. In dry mode
requests, tokens, and cost are known zero.
`AP_EVAL_AUTHORIZED=1` authorizes the disclosed wrapper launches only; it is not a
provider-request or spend cap.

## Limits

The corpus is synthetic and tiny. Its defects are intentionally legible and do not represent
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
