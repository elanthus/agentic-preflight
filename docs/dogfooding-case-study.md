# Case study: sustained dogfooding across four repositories

Two observation windows, August 3–17 and August 18–September 6, 2026, contain 408
merged PRs across the same four owner-operated repositories. Of those, 323 descriptions
explicitly record Preflight use and 64 contain a concrete finding record. These are
combined snapshot counts, not external adoption or distinct bugs fixed. The original
snapshot is preserved below, followed by the newer evidence and its limits.

From August 3 through August 17, 2026, Agentic Preflight was used while shipping work
across four public repositories: this project, a citation-grounded news pipeline, an
OSWorld evaluation and deployment project, and an agentic job-research application. The
period is useful as an operating sample because the repositories exercise different
failure surfaces: Git and release policy, untrusted content and model evaluation,
browser and deployment evidence, and long-running cached agent workflows.

This is an observational case study, not a controlled evaluation. It reports what the
public pull-request record supports and states the limits of that evidence explicitly.

## Original snapshot: August 3–17

The observation window begins at `2026-08-03T00:00:00Z` and ends with the data collected
on August 17 at approximately 20:40 UTC. Pull requests are counted by creation time.

| Repository | PRs opened | PRs merged | Merged PRs that explicitly record preflight use | Merged PRs with a concrete finding record |
|---|---:|---:|---:|---:|
| [`agentic-preflight`](https://github.com/elanthus/agentic-preflight/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 34 | 26 | 25 | 7 |
| [`news-briefing`](https://github.com/elanthus/news-briefing/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 65 | 63 | 42 | 9 |
| [`OSWorldTasks`](https://github.com/elanthus/OSWorldTasks/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 35 | 34 | 29 | 1 |
| [`jobwright`](https://github.com/elanthus/jobwright/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 33 | 30 | 26 | 7 |
| **Total** | **167** | **153** | **122** | **24** |

The repository links in the table are live, day-level browsing links. They can include
PRs created later on August 17 than the exact cutoff and are not the aggregate snapshots.
The fixed counts and the excerpts used to classify them are preserved in the
[`dogfooding-case-study-evidence.json`](dogfooding-case-study-evidence.json) ledger.

"Explicitly record" is deliberately narrower than "used." A merged PR is counted only
when its description contains positive workflow-status evidence for Preflight or a
detailed `F`-identifier-and-severity finding record. Package paths such as
`agentic_preflight` do not qualify by themselves. A statement such as "no findings"
records use but not a concrete finding, and a hypothetical "preflight caught no issue"
does not qualify as a finding. This avoids claiming that all 153 merged PRs were gated
when the public record does not prove that.

The aggregate was produced from the `number`, `createdAt`, `mergedAt`, `closedAt`,
`body`, and `url` fields returned by the following command for each repository:

```bash
gh pr list --repo OWNER/REPOSITORY --state all --limit 100 \
  --json number,createdAt,mergedAt,closedAt,body,url
```

The point-in-time interval is inclusive from `2026-08-03T00:00:00Z` through
`2026-08-17T20:40:03Z`. A PR counts as merged only when `mergedAt` is non-null and no
later than the interval end. Recorded-use evidence is either a line containing a
word-boundary `F` followed by at least three digits plus a severity, a Markdown heading
containing the standalone word `preflight`, an attestation or audit reference, an
explicit outcome such as `passed`, `green`, `examined`, or a risk verdict, or a
validation-list item that names a Preflight check. Feature, configuration, and review-
scope mentions without an outcome do not qualify. Concrete-finding evidence is either
the detailed `F` record or an affirmative statement that Preflight caught, identified,
found, or flagged an issue. Immediate `no`/`none`/`zero` negations are excluded. The
ledger stores the selected excerpt and classification reason for every counted PR, plus
the rejected borderline examples. Each repository returned fewer than 100 PRs in the
interval, so the requested result limit did not truncate the sample.

The public descriptions also show clean runs at very different sizes. Examples include
9 delivered review units in
[`agentic-preflight` #57](https://github.com/elanthus/agentic-preflight/pull/57),
55 changed units in
[`OSWorldTasks` #8](https://github.com/elanthus/OSWorldTasks/pull/8), and 698 delivered
review units in [`jobwright` #63](https://github.com/elanthus/jobwright/pull/63). These
numbers are review-manifest units, not lines of code, and a clean receipt proves only
that every delivered unit was cited or marked examined clean.

## Follow-up: August 18–September 6

The follow-up uses PR creation times from `2026-08-18T00:00:00Z` through
`2026-09-06T23:59:59Z`, inclusive. A PR counts as merged only if its `mergedAt` is
within that cutoff. The descriptions were collected on September 7, so they may include
edits made after September 6; this is a retrospective reading of the public record,
not a preserved September 6 description snapshot.

| Repository | PRs opened | PRs merged | Merged PRs recording use | Merged PRs with a concrete finding record |
|---|---:|---:|---:|---:|
| `agentic-preflight` | 32 | 32 | 25 | 12 |
| `news-briefing` | 91 | 91 | 67 | 7 |
| `OSWorldTasks` | 59 | 59 | 46 | 6 |
| `jobwright` | 75 | 73 | 63 | 15 |
| **Follow-up total** | **257** | **255** | **201** | **40** |
| **Original + follow-up** | **424** | **408** | **323** | **64** |

The [follow-up evidence ledger](dogfooding-follow-up-2026-09-06-evidence.json) preserves
all 257 PR identities and timestamps, the selected exact excerpts, exclusion reasons,
and SHA-256 hashes of the retrieved descriptions. Each repository returned fewer than
the requested 1,000 records. Collection used:

```bash
gh pr list --repo OWNER/REPOSITORY --state all \
  --search 'created:2026-08-18..2026-09-06' --limit 1000 \
  --json number,title,createdAt,mergedAt,closedAt,body,url
```

The substantive positive-evidence criteria from the original snapshot are retained.
For the follow-up, manual adjudication also recognizes explicit zero-exit stage results
and `TEST_GREEN`, preserving adjacent command/result lines together when Markdown
formatting separates them. A successful stage records use; it does not prove that the
entire workflow completed. Configuration descriptions and evidence explicitly attributed
only to another PR do not qualify. For example,
[`news-briefing` #160](https://github.com/elanthus/news-briefing/pull/160) describes the
workflow and earlier findings, while
[`OSWorldTasks` #127](https://github.com/elanthus/OSWorldTasks/pull/127) attributes its
Preflight record to source PRs. Neither is counted as recorded use in this follow-up.

Combined totals add the two disjoint snapshots without reclassifying the original
ledger or updating its merge states. They exclude the unsampled remainder of August 17
after 20:40:03 UTC and do not backfill later merges of PRs from the original window.
[`jobwright` #179](https://github.com/elanthus/jobwright/pull/179), for example, was
opened on September 6 but merged on September 7, so it contributes only to the
follow-up's opened count. Counts include PRs merged into branches other than `main`;
they are not counts of releases or independent deployments. The windows differ in
length and work mix, so their raw totals are not a productivity comparison.

### Three additional findings

- **Preserve literal property names while adapting schemas.**
  [`news-briefing` #146](https://github.com/elanthus/news-briefing/pull/146) records a
  medium-severity finding: removing an unsupported schema keyword could also remove a
  legitimate property named `uniqueItems`. Commit `e795a5f` distinguished schema-keyword
  dictionaries from literal property maps. The description records a clean seven-unit
  review after the repair.
- **Make a spending cap cover the entire policy lifecycle.**
  [`OSWorldTasks` #86](https://github.com/elanthus/OSWorldTasks/pull/86) records a
  high-severity design finding: the proposed paid-call cap omitted calls during reset
  or close. Commit `284c45d` prohibited provider calls outside `act` and counted retries
  toward the per-action request limit. This was a correction to a design contract;
  the PR did not run paid calls or demonstrate realized cost savings.
- **Keep targeted evidence from being crowded out.**
  [`jobwright` #164](https://github.com/elanthus/jobwright/pull/164) records a
  high-severity finding: targeted topics could lose their slots within a retrieval
  category. Commit `010b444` interleaved topic groups. The PR describes hermetic
  regression coverage; it did not establish live model-quality gains.

### Where the workflow still needed help

A clean local review did not end the repair process. In
[`agentic-preflight` #61](https://github.com/elanthus/agentic-preflight/pull/61), the
record separates Preflight's initial redaction findings from CodeRabbit's follow-up
findings and later repairs, including transient secret-file rewrites and incomplete
SHA-256 repository support. In
[`OSWorldTasks` #86](https://github.com/elanthus/OSWorldTasks/pull/86), the description
records a clean revised diff before several further hosted-review rounds found gaps
in recovery, provider-attempt accounting, and evidence persistence. Those subsequent
findings are not presented here as Preflight's initial catches, and the changed
snapshots do not support a numerical miss rate.

Hosted CI also exposed an evidence-manifest problem in
[`OSWorldTasks` #54](https://github.com/elanthus/OSWorldTasks/pull/54): editing
`deploy/README.md` disturbed frozen evidence. The repair preserved the tracked file and
required a fresh Preflight run. This is an example of CI supplying information beyond
the local review record.

The 40 follow-up finding records include documentation corrections, low-severity
observations, and intentional `no_op` dispositions. Multiple PRs can also discuss the
same issue. Neither 40 nor the combined 64 measures distinct defects fixed, prevented
incidents, or reviewer recall. The newer record supports sustained use and documented
repair loops while continuing to show why hosted checks and additional review matter.

## What the original sample's gate caught

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

## How findings changed publication in the original sample

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
  but descriptions are mutable, the preserved rule-based classification can still miss
  undocumented use or misclassify prose, and it cannot grade review quality.
- These four repositories have one owner. The sample demonstrates repeated use across
  heterogeneous codebases, not organization-wide adoption or peer-review effectiveness.
- Generated evaluation evidence and large refactors make line-count totals misleading,
  so this case study does not present changed lines as a quality or productivity metric.
- Clean review receipts prove reported coverage, not understanding. The same diff can
  produce different findings on another review, and CI and human review remain separate
  evidence sources.

## Conclusion

The original sample and follow-up support a bounded claim: Agentic Preflight repeatedly
converted agent review judgments into snapshot-bound repair loops and publication decisions across
four materially different repositories. Its highest-value catches were semantic
boundary failures—stale evidence, approval eligibility, secret normalization, trust
domain selection, resumability, and immutable inputs—that deterministic tests alone had
not made visible in the proposed changes.

The sample does not establish autonomous review quality. It does show why the project
records coverage, findings, stage results, and human-review requirements separately: a
useful gate must preserve both what it checked and what that check cannot prove.
