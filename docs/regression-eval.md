# Public regression eval

The public regression eval uses a small synthetic corpus for smoke tests.
It checks that reviewer findings pass through the actual Agentic Preflight workflow and receive consistent scores.
It checks for regressions in command review, grounding, persistence, and reporting.

This is separate from the private decision-quality evaluation.
This directory contains no private cases, gold files, artifacts, or reports.
Do not compare results from this corpus with the private evaluation.

## Corpus design

The corpus contains 12 small fictional projects.
It has three cases for each category: correctness, security, evaluation integrity, and documentation contract failures.
Each case has three complete trees: base, vulnerable, and fixed.
Fixed snapshots are false-positive controls.

Each reviewed snapshot uses a separate repository.
The base tree is on `main`. Only the selected tree follows it on `review/change`.
The commit subjects are `Initial snapshot` and `Proposed change`.
The repository directory uses a random opaque identifier.

The runner never copies or commits the unselected tree.
The object database can contain blobs shared with the base or selected tree.

Each `gold.json` file supplies expected results to the scorer only.
It names one mechanism, vulnerable path and line range, category, and severity range.
It also specifies that the finding must be absent after the fix.
The runner never copies gold data into a fixture repository.
Before review, the runner rejects snapshots that contain `gold.json`.

The runner checks that the serialized context contains none of the following:

- The name `gold.json`
- The serialized gold record
- The case ID

It also checks that the delivered intent exactly equals the case intent.
An assertion rejects top-level scorer-label structures to prevent future additions that could expose scoring information.
The product CLI does not emit those fields today.
Regression tests capture actual reviewer-wrapper stdin through both example wrappers for both snapshots.

For one representative case per selected snapshot, tests hash every file in all three trees.
They inspect all Git objects, including unreachable objects, for blobs unique to the unselected tree.
They permit shared content from the base or selected tree.
Real mode removes inherited `AP_EVAL_SCRIPT` from the subprocess environment.
Only dry mode receives the scripted answer path.

Dry mode uses prepared findings but creates real Git repositories.
It invokes the real CLI in subprocesses for `init --no-hook`, `start --intent`, `context`, and `review run`.
Runs with grounding on and off use the same procedure.
Every fixture tree contains `CODEOWNERS` and `AGENTS.md`.
With grounding on, each reviewed snapshot must have at least one grounding entry in `context`, or the run fails.

Fixture commit identities and dates are fixed. Scoring is deterministic.
The runner measures runtime and state timing separately.
These measurements cause complete reports to vary between runs.

## Scoring

A vulnerable snapshot is a catch if a finding matches the gold path and permitted line range.
The permitted range extends two lines before and after the vulnerable gold range.
A finding without a line matches if its cited review-unit hunk overlaps the gold range.
A finding with a line outside the permitted range does not match, even if its hunk overlaps.

A fixed snapshot is a false positive under the same location rule.
Each fixed snapshot changes the file named by the gold record.
Thus, a finding on that file resolves to a review unit and can count as a false positive.
Case loading fails unless each gold range intersects lines changed from base to vulnerable.

A failure before an accepted submission produces an unresolved snapshot.
It does not count as a catch or a miss.
A failed `init`, `start`, `context`, or `review run` marks only that case and snapshot unresolved.
The run continues and writes `summary.json` and `summary.md`.

Repository setup errors, leakage guards, malformed CLI envelopes, and scoring errors also produce unresolved snapshots.
Each unresolved snapshot records the failed operation and diagnostic.
Leakage guards still prevent review from starting.
If the operation returned a complete failure envelope, the runner keeps it.

Severity agreement checks whether a matched vulnerable finding is within the gold severity range.

Keyword hit rate uses a fixed keyword map for each category.
It is the share of matched findings with a mapped substring in the title or detail.
It confirms vocabulary. It does not establish that the reviewer's reasoning is correct.

The scripted details deliberately match the map, so dry mode has a keyword hit rate of 1.0.
Severity agreement and keyword hit rate are separate measurements. Neither blocks execution.

`summary.json` records `method_version` and snapshot evidence for each case.
For each grounding setting, it also records aggregate catches, fixed false positives, unresolved snapshots, severity agreement, and keyword hit rate.

`summary.md` starts with the mode and executor.
It shows the same case outcomes and aggregates in one table.
Boolean values appear as `yes` or `no`. Missing values appear as `n/a`. Rates have three decimal places.
Additional tables show token categories and workflow states.

## Runtime, findings, states, and tokens

Method `public-smoke-v6` adds measurements to both reports. The scoring rules are unchanged.

### Runtime

`started_at` and `finished_at` are UTC timestamps.
`runtime_seconds` uses a monotonic clock.
Each snapshot runtime includes repository construction, CLI execution, review, and scoring.
The suite runtime includes case loading, all snapshots, and cleanup of temporary workspaces.
It ends before report serialization.

Each grounding setting records the sum of snapshot runtimes.
Failed snapshots retain their runtime.

### Findings

`finding_count` and `findings` retain accepted submissions for each snapshot.
A failed run without an accepted submission has unknown findings. Its count is not zero.
Aggregate `finding_count` is `null` if any snapshot is unmeasured.
`observed_finding_count`, `finding_snapshots_measured`, and `observed_findings_by_severity` describe the measured subset.

Counts include submitted findings, including repetitions across independent snapshots or grounding settings.
Gold-location matches remain separate scoring evidence.

### Workflow states

`workflow` contains ordered state visits, the final state, and visit counts and seconds for each state.
State-transition audit events retain intermediate states within one committed transaction.
Counts and durations include repeated visits.
The last state's duration ends when snapshot observation stops.
Missing, inconsistent, or truncated history sets `complete` to false.

An unknown final duration stays `null`.
`observed_duration_seconds` retains measured intervals.
The runner compares monotonic times only within this evaluation on the same host.

### Token usage

Each snapshot's `usage` has a source, status, and observed token categories.
The status is `complete`, `partial`, or `unknown`.
Aggregate `usage.totals` contains an exact count only if every snapshot has complete evidence for that category.

`usage.observed` retains known counts from partial records. `usage.measurements` reports coverage by status.
`provider_tokens` is the exact aggregate total or `null`.
Do not use a partial observed count as a total.

Codex uses `codex exec --json` completion events.
Its input includes cached input. Its output includes reasoning output.
These subsets are not added again.

Claude uses `--output-format stream-json --verbose`.
The runner prefers the final `modelUsage` aggregate, which includes subagents.
It adds Claude's uncached input, cache-read input, and cache-creation input to calculate total input.
Claude's `result.usage` covers only the main loop.
Without a per-model aggregate for the complete call, the runner reports `result.usage` as partial.

Assistant-message usage is counted once per message ID.
Its output count is a placeholder and remains unknown.
Zero counters from a crash result do not erase earlier observations.
Error results, missing fields, malformed streams, and interrupted CLI processes retain available evidence with partial or unknown status.

The harness captures CLI stdout directly to a temporary file outside the reviewer tree.
It uses the example wrappers' `AP_REVIEWER_EVENTS_FILE` interface.
This keeps flushed events after CLI failure, wrapper timeout, or an outer review-command timeout.
A separate completion marker distinguishes a complete invocation from an earlier completion event in a killed process.
Findings submission stdout is unchanged.

The harness removes temporary raw events after it collects normalized metrics and diagnostics.
An abrupt termination of the evaluation runner itself can prevent report writing.

The CLI reports these usage measurements.
They do not independently verify billing or count provider requests.
Unsupported categories stay `null`.
Known zero applies only to dry mode or a wrapper known not to have launched.
The smoke evaluation ends after review.
Its full runtime does not cover a lint/test/merge-back workflow.

Usage semantics follow the providers' documentation:
[Codex JSONL events](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable)
and [Claude token accounting](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

## Running dry mode

Dry mode launches the scripted reviewer wrapper for each snapshot. It makes no provider calls.
To run dry mode, use this command:

```console
uv run python evals/run.py --mode dry --out /tmp/agentic-preflight-evals
```

Use `--grounding on` or `--grounding off` for one setting. The default is `both`.

CI uses pytest to check scripted catches, misses, fixed-snapshot false positives, and unresolved outcomes.
A separate test runs identical inputs twice.
It compares scoring and excludes measured timing.
Tests do not compare reports with committed fixture commit hashes.
Intentional corpus or configuration changes do not require a new reference summary.
Tests still validate every case fixture.

## Running real mode

Real mode uses the example configurations and standard-library wrappers in `docs/examples/`.
It refuses to start unless `AP_EVAL_AUTHORIZED=1` is present.
Each command uses 12 cases, two reviewed snapshots, and two grounding settings.
This launches exactly `12 × 2 × 2 = 48` reviewer-wrapper invocations for the selected executor.

With maintainer authorization, run these commands:

```console
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor codex --out /tmp/ap-eval-codex
AP_EVAL_AUTHORIZED=1 uv run python evals/run.py --mode real --executor claude --out /tmp/ap-eval-claude
```

CI does not run these commands.
One grounding setting reduces the wrapper-invocation count to 24.
A wrapper invocation is not a provider request.
A CLI can fail before a provider call, retry, use tools, or make multiple provider requests.

`summary.json` records `reviewer_invocations`.
It sets `provider_requests` and `provider_cost_usd` to `null` because wrapper launches do not establish either quantity.
The runner measures token usage from CLI output as described in [Token usage](#token-usage).
In dry mode, requests, tokens, and cost are known zero.
`AP_EVAL_AUTHORIZED=1` authorizes only the disclosed wrapper launches.
It does not set a provider-request or spending limit.

## Limits

The corpus is small and synthetic. Its defects are deliberately easy to identify.
It does not represent the range, ambiguity, or defect frequency of production changes.
Three additional factors increase real-mode catch rates:

- Case intents name the property that the defect violates.
  Examples include "without exposing the supplied credential in logs" and "keeping every read inside its configured storage root".
  These statements tell the reviewer where to look.
- About half the vulnerable diffs remove an existing protection, such as a containment check or a `max` default.
  A reviewer can identify the deletion without reasoning about the defect.
- Standard ruff rules catch two or three cases, including shell injection and a hardcoded token comparison.
  `evals/.ruff.toml` suppresses those rules so the fixtures pass lint.

Scripted dry mode verifies the product workflow and scoring calculations. It does not measure reviewer judgment.
Real mode adds reviewer behavior through external reviewer wrappers.
Its results depend on model and tool versions.
Neither mode measures the private evaluation.
Do not compare these rates with private decision-quality results.

## Evaluation evidence

The [2026-10-17 Opus 5.5 / GPT-6.1 Sol comparison](evals/2026-10-17-opus-sol-low/README.md)
publishes one real `public-smoke-v6` run at low effort.
It includes runtime, accepted findings, workflow state timing, CLI token accounting, and snapshot evidence.
Its observations apply only to this public smoke corpus.

The separate [public evaluation implementation](https://github.com/elanthus/preflight-eval-results/tree/5f98146bd67f445aecb9d340e067b3609a97620d)
publishes the decision-quality library, synthetic paired replay, and versioned limits of the aggregate results.
Its offline replay uses scripted adjudication and makes no provider calls.
It does not reproduce the private corpus or measure model quality.
