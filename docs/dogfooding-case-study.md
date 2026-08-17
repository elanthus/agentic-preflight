# Case study: two weeks of dogfooding across four repositories

From August 3 through August 17, 2026, Agentic Preflight was used while shipping work
across four public repositories: this project, a citation-grounded news pipeline, an
OSWorld evaluation and deployment project, and an agentic job-research application. The
period is useful as an operating sample because the repositories exercise different
failure surfaces: Git and release policy, untrusted content and model evaluation,
browser and deployment evidence, and long-running cached agent workflows.

This is an observational case study, not a controlled evaluation. It reports what the
public pull-request record supports and states the limits of that evidence explicitly.

## Snapshot

The observation window begins at `2026-08-03T00:00:00Z` and ends with the data collected
on August 17 at approximately 20:40 UTC. Pull requests are counted by creation time.

| Repository | PRs opened | PRs merged | Merged PRs that explicitly record preflight use | Merged PRs with a concrete finding record |
|---|---:|---:|---:|---:|
| [`agentic-preflight`](https://github.com/elanthus/agentic-preflight/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 34 | 26 | 25 | 7 |
| [`news-briefing`](https://github.com/elanthus/news-briefing/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 65 | 63 | 41 | 6 |
| [`OSWorldTasks`](https://github.com/elanthus/OSWorldTasks/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 35 | 34 | 30 | 1 |
| [`jobwright`](https://github.com/elanthus/jobwright/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 33 | 30 | 26 | 7 |
| **Total** | **167** | **153** | **122** | **21** |

"Explicitly record" is deliberately narrower than "used." A merged PR is counted only
when its description names Agentic Preflight or records a preflight review, gate,
attestation, risk verdict, or review record. A concrete finding record requires a
specific finding identifier or an explicit statement that preflight caught an issue.
PR descriptions that merely list tests or other reviewers do not count. This avoids
claiming that all 153 merged PRs were gated when the public record does not prove that.

The aggregate was produced from the `number`, `createdAt`, `mergedAt`, `closedAt`,
`body`, and `url` fields returned by the following command for each repository:

```bash
gh pr list --repo OWNER/REPOSITORY --state all --limit 100 \
  --json number,createdAt,mergedAt,closedAt,body,url
```

The point-in-time interval is inclusive from `2026-08-03T00:00:00Z` through
`2026-08-17T20:40:03Z`. A PR counts as merged only when `mergedAt` is non-null and no
later than the interval end. Recorded use is the case-insensitive regular expression
`preflight` applied to the body of a merged PR. A concrete finding record is the
case-insensitive expression `F00[1-9]|preflight caught` within that recorded-use cohort.
Each repository returned fewer than 100 PRs in the interval, so the requested result
limit did not truncate the sample.

The public descriptions also show clean runs at very different sizes. Examples include
9 delivered review units in
[`agentic-preflight` #57](https://github.com/elanthus/agentic-preflight/pull/57),
55 changed units in
[`OSWorldTasks` #8](https://github.com/elanthus/OSWorldTasks/pull/8), and 698 delivered
review units in [`jobwright` #63](https://github.com/elanthus/jobwright/pull/63). These
numbers are review-manifest units, not lines of code, and a clean receipt proves only
that every delivered unit was cited or marked examined clean.

## What the gate caught

The representative findings were semantic rather than syntax errors. Their PR records
describe regression coverage added with the repairs and final configured stages green,
with hosted CI providing a separate verification source when it was present. The
findings clustered at boundaries where an otherwise plausible implementation could
still preserve stale evidence, authorize the wrong actor, mutate supposedly immutable
input, or scan the wrong trust domain.

### Green evidence must bind every decision-making input

The same failure pattern appeared independently in three codebases:

- In [`agentic-preflight` #27](https://github.com/elanthus/agentic-preflight/pull/27), a
  high-severity finding showed that reused stage evidence was not originally bound to
  the user's intent. Follow-up review found that it could also outlive an effective
  configuration change. The repair added portable intent and configuration digests,
  fresh-base ancestry checks, and regression coverage.
- In [`jobwright` #37](https://github.com/elanthus/jobwright/pull/37), automatic model
  selection was missing from cache identity. A cached result could therefore survive a
  provider or model change. The repair fingerprints the resolved provider and model
  without recording credentials.
- In [`news-briefing` #46](https://github.com/elanthus/news-briefing/pull/46), evaluator
  checkpoints were not bound to the manifest and suite contents; a related semantic
  checkpoint could reuse an older first-topic-only result after the prompt expanded to
  all citing topics. The repairs added input hashes, advanced checkpoint identity, and
  stale-input tests.

The general lesson was stronger than "invalidate caches carefully": a green result is
valid only for the complete input manifest that produced it. Code, configuration,
intent, selected model, prompts, and evaluation corpus can all be correctness inputs.

### Human approval needs both identity and state checks

Two self-hosting changes exposed gaps that ordinary tests would not treat as publication
policy violations:

- [`agentic-preflight` #23](https://github.com/elanthus/agentic-preflight/pull/23)
  initially allowed an approval check without proving that the approving account was a
  repository owner, member, or collaborator. The same review found that repository Git
  configuration could influence notes-conflict handling. Both paths were made explicit
  and deterministic.
- [`agentic-preflight` #24](https://github.com/elanthus/agentic-preflight/pull/24) found
  that manual-merge policy did not reject a pull request whose GitHub auto-merge had
  already been enabled. Another high-severity finding corrected instructions that
  conflated push authorization with standing authorization to create a PR. The workflow
  now checks auto-merge state and keeps the two permissions distinct.

These repairs turned "a human is involved" into two testable claims: an eligible person
approved the exact head, and the hosted PR state still enforces the intended merge path.

### Trust boundaries can fail in both directions

The review record includes both missed protection and over-broad protection:

- [`agentic-preflight` #33](https://github.com/elanthus/agentic-preflight/pull/33)
  caught a secret-redaction gap: a dotenv loader could decode an escaped apostrophe in a
  double-quoted value while the redaction set retained the escaped spelling. The repair
  added the decoded representation and a regression test.
- [`news-briefing` #28](https://github.com/elanthus/news-briefing/pull/28) caught a
  high-severity ambiguity in configured source identifiers. An identifier containing
  the evaluator's error delimiter could bypass the intended corpus-health contract. The
  repair rejects ambiguous identifiers at configuration load.
- [`jobwright` #48](https://github.com/elanthus/jobwright/pull/48) caught the opposite
  error: a generated-output injection scan included the raw, untrusted research ledger.
  It would fail when a canary correctly remained confined to its source. The repair
  excludes raw source material and tests that boundary.

The third example mattered because a gate that reports expected containment as a
failure trains users to ignore it. False-positive boundaries are part of safety design,
not merely review polish.

### Resumability and immutability need adversarial review

Long-running and paid workflows produced another recurring class of findings:

- [`news-briefing` #38](https://github.com/elanthus/news-briefing/pull/38) checkpointed
  validated review batches but did not resume from them. A later malformed response
  could repeat already-paid calls. The fix binds resumable checkpoints to the suite
  hash, models, and batch size.
- [`jobwright` #53](https://github.com/elanthus/jobwright/pull/53) found that rescoring a
  newly added benchmark case could create artifact directories inside the source run,
  violating its immutability guarantee. The repaired path emits a missing-artifact
  result without constructing a mutating run context.
- [`OSWorldTasks` #2](https://github.com/elanthus/OSWorldTasks/pull/2) found that a cached
  extracted guest image could bypass archive-member validation. The repair verifies the
  cached member's size and CRC32 against the SHA-256-verified archive and adds regression
  tests.

All three defects were recovery-path defects. The happy path could pass while a resume,
rescore, or cache hit violated the stronger contract.

## How findings changed publication

The gate did more than write comments. Blocking findings stopped the state machine,
repair commits changed the reviewed snapshot, and the next review used a new manifest.
High-risk paths and high-severity findings also produced human-review verdicts rather
than allowing an automatic merge path. PR descriptions such as
[`agentic-preflight` #24](https://github.com/elanthus/agentic-preflight/pull/24),
[`news-briefing` #28](https://github.com/elanthus/news-briefing/pull/28), and
[`jobwright` #53](https://github.com/elanthus/jobwright/pull/53) preserve the finding,
repair commit, final disposition, and manual-merge requirement for a reviewer.

Across all 153 merged PRs, the median GitHub time from PR creation to merge was about
12.6 minutes; among the 122 PRs that explicitly recorded preflight use, it was about
13.1 minutes. Those numbers show the owner-operated workflow did not create a multi-day
publication queue, but they do **not** measure preflight overhead: most local review,
repair, lint, and test work happened before the PR was created. No causal speed claim is
made.

## What this evidence does not prove

- There was no randomized control group, so the record cannot estimate defect escape
  rate, reviewer recall, false-negative rate, or productivity improvement.
- PR bodies are author-maintained audit records, not independent telemetry. The
  representative findings above were checked against their linked public descriptions,
  but descriptions are mutable, the aggregate text classification can undercount
  undocumented use, and it cannot grade review quality.
- These four repositories have one owner. The sample demonstrates repeated use across
  heterogeneous codebases, not organization-wide adoption or peer-review effectiveness.
- Generated evaluation evidence and large refactors make line-count totals misleading,
  so this case study does not present changed lines as a quality or productivity metric.
- Clean review receipts prove reported coverage, not understanding. The same diff can
  produce different findings on another review, and CI and human review remain separate
  evidence sources.

## Conclusion

The two-week sample supports a bounded claim: Agentic Preflight repeatedly converted
agent review judgments into snapshot-bound repair loops and publication decisions across
four materially different repositories. Its highest-value catches were semantic
boundary failures—stale evidence, approval eligibility, secret normalization, trust
domain selection, resumability, and immutable inputs—that deterministic tests alone had
not made visible in the proposed changes.

The sample does not establish autonomous review quality. It does show why the project
records coverage, findings, stage results, and human-review requirements separately: a
useful gate must preserve both what it checked and what that check cannot prove.
