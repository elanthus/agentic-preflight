# Case study: sustained dogfooding across four repositories

Two observation windows cover August 3–17 and August 18–September 6, 2026.
They contain 408 merged PRs across the same four repositories, all operated by one owner.
Of these PRs, 323 descriptions explicitly record Preflight use.
Of the merged PR descriptions, 64 contain a concrete finding record.
These combined snapshot counts do not measure external adoption or distinct defects fixed.
The original snapshot follows, with the later evidence and its limits.

From August 3 through August 17, 2026, the owner used Agentic Preflight to publish work across four public repositories:

- Agentic Preflight
- A news pipeline that grounds its output in citations
- An OSWorld evaluation and deployment project
- An application that uses agents for job research

These repositories exercise different failure conditions.
They cover Git and release policy, untrusted content, model evaluation, browser and deployment evidence, and long-running workflows with cached results.
This observational case study describes evidence from public pull requests and its limits.
It is not a controlled evaluation.

## Original snapshot: August 3–17

The observation window starts at `2026-08-03T00:00:00Z`.
It ends with data collected on August 17 at approximately 20:40 UTC.
Pull requests count by creation time.

| Repository | PRs opened | PRs merged | Merged PRs that explicitly record preflight use | Merged PRs with a concrete finding record |
|---|---:|---:|---:|---:|
| [`agentic-preflight`](https://github.com/elanthus/agentic-preflight/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 34 | 26 | 25 | 7 |
| [`news-briefing`](https://github.com/elanthus/news-briefing/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 65 | 63 | 42 | 9 |
| [`OSWorldTasks`](https://github.com/elanthus/OSWorldTasks/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 35 | 34 | 29 | 1 |
| [`jobwright`](https://github.com/elanthus/jobwright/pulls?q=is%3Apr+created%3A2026-08-03..2026-08-17) | 33 | 30 | 26 | 7 |
| **Total** | **167** | **153** | **122** | **24** |

The table's repository links are live browsing links with day-level date filters.
They can include PRs created after the exact August 17 cutoff.
They are not the aggregate snapshots.
The [`dogfooding-case-study-evidence.json`](https://github.com/elanthus/preflight-eval-results/blob/f784772cbbc17618ad05636c9ca37065affc194f/evidence/dogfooding/dogfooding-case-study-evidence.json)
ledger preserves fixed counts and classification excerpts.

Evidence ledgers are collection records. They are never edited by hand after collection.
A correction requires a new collection with a new `collected_at` value.

A PR can use Preflight without explicitly recording that use.
The count includes a merged PR only if its description contains positive workflow-status evidence or a detailed `F`-identifier-and-severity finding record.
A package path such as `agentic_preflight` alone does not qualify.
A statement such as "no findings" records use but no concrete finding.
A hypothetical "preflight caught no issue" does not qualify as a finding.
The public record therefore does not prove that all 153 merged PRs had a run.

The aggregate uses the `number`, `createdAt`, `mergedAt`, `closedAt`, `body`, and `url` fields.
The following command returned those fields for each repository:

```bash
gh pr list --repo OWNER/REPOSITORY --state all --limit 100 \
  --json number,createdAt,mergedAt,closedAt,body,url
```

The interval includes both endpoints: `2026-08-03T00:00:00Z` through `2026-08-17T20:40:03Z`.
A PR counts as merged only if `mergedAt` is non-null and no later than the interval end.
Recorded-use evidence can be any of the following:

- A line with a word-boundary `F`, at least three following digits, and a severity
- A Markdown heading with the standalone word `preflight`
- An attestation or audit reference
- An explicit outcome such as `passed`, `green`, `examined`, or a risk verdict
- A validation-list item that names a Preflight check

Feature, configuration, and review-scope mentions without an outcome do not qualify.
Concrete-finding evidence requires a detailed `F` record or an affirmative statement that Preflight caught, identified, found, or flagged an issue.
Immediate `no`/`none`/`zero` negations are excluded.
The ledger stores the selected excerpt and classification reason for each counted PR.
It also stores rejected borderline examples.
Each repository returned fewer than 100 PRs in the interval, so the result limit did not truncate the sample.

Public descriptions show clean runs with different review sizes:

- [`agentic-preflight` #57](https://github.com/elanthus/agentic-preflight/pull/57): 9 delivered review units
- [`OSWorldTasks` #8](https://github.com/elanthus/OSWorldTasks/pull/8): 55 changed units
- [`jobwright` #63](https://github.com/elanthus/jobwright/pull/63): 698 delivered review units

These are review-manifest units, not lines of code.
A clean receipt proves only that each delivered unit was cited or marked examined clean.

## Follow-up: August 18–September 6

The follow-up includes PR creation times from `2026-08-18T00:00:00Z` through `2026-09-06T23:59:59Z`, including both endpoints.
A PR counts as merged only if its `mergedAt` is within that cutoff.
The descriptions were collected on September 7 and can include later edits.
This is a retrospective reading of the public record. It is not a preserved September 6 description snapshot.

| Repository | PRs opened | PRs merged | Merged PRs recording use | Merged PRs with a concrete finding record |
|---|---:|---:|---:|---:|
| `agentic-preflight` | 32 | 32 | 25 | 12 |
| `news-briefing` | 91 | 91 | 67 | 7 |
| `OSWorldTasks` | 59 | 59 | 46 | 6 |
| `jobwright` | 75 | 73 | 63 | 15 |
| **Follow-up total** | **257** | **255** | **201** | **40** |
| **Original + follow-up** | **424** | **408** | **323** | **64** |

The [follow-up evidence ledger](https://github.com/elanthus/preflight-eval-results/blob/f784772cbbc17618ad05636c9ca37065affc194f/evidence/dogfooding/dogfooding-follow-up-2026-09-06-evidence.json)
preserves all 257 PR identities and timestamps.
It includes selected exact excerpts, exclusion reasons, and SHA-256 hashes of retrieved descriptions.
Each repository returned fewer than the requested 1,000 records.
Collection used this command:

```bash
gh pr list --repo OWNER/REPOSITORY --state all \
  --search 'created:2026-08-18..2026-09-06' --limit 1000 \
  --json number,title,createdAt,mergedAt,closedAt,body,url
```

The follow-up keeps the original criteria for positive evidence.
Manual classification also recognizes explicit zero-exit stage results and `TEST_GREEN`.
It keeps adjacent command/result lines together when Markdown formatting separates them.
A successful stage records use. It does not prove that the complete workflow finished.
Configuration descriptions and evidence attributed only to another PR do not qualify.

For example, [`news-briefing` #160](https://github.com/elanthus/news-briefing/pull/160) describes the workflow and earlier findings.
[`OSWorldTasks` #127](https://github.com/elanthus/OSWorldTasks/pull/127) attributes its Preflight record to source PRs.
Neither counts as recorded use in this follow-up.

Combined totals add the two separate snapshots.
They do not reclassify the original ledger or update its merge states.
They exclude the unsampled part of August 17 after 20:40:03 UTC.
They also exclude later merges of PRs from the original window.

For example, [`jobwright` #179](https://github.com/elanthus/jobwright/pull/179) opened on September 6 and merged on September 7.
It contributes only to the follow-up's opened count.
Counts include PRs merged into branches other than `main`.
They do not count releases or independent deployments.
The windows differ in length and work mix. Their raw totals do not compare productivity.

### Three additional findings

- **Preserve literal property names during schema changes.**
  [`news-briefing` #146](https://github.com/elanthus/news-briefing/pull/146) records a medium-severity finding.
  Removal of an unsupported schema keyword could also remove a valid property named `uniqueItems`.
  Commit `e795a5f` distinguished schema-keyword dictionaries from literal property maps.
  The description records a clean seven-unit review after repair.
- **Apply a spending cap to the complete policy lifecycle.**
  [`OSWorldTasks` #86](https://github.com/elanthus/OSWorldTasks/pull/86) records a high-severity design finding.
  The proposed paid-call cap omitted calls during reset or close.
  Commit `284c45d` prohibited provider calls outside `act` and counted retries toward the per-action request limit.
  This corrected a design contract. The PR did not run paid calls or demonstrate actual cost savings.
- **Keep space for targeted evidence.**
  [`jobwright` #164](https://github.com/elanthus/jobwright/pull/164) records a high-severity finding.
  Targeted topics could lose their slots within a retrieval category.
  Commit `010b444` interleaved topic groups.
  The PR describes regression tests in an isolated environment. It did not establish live model-quality gains.

### Where the workflow still needed help

A clean local review did not end repairs.
In [`agentic-preflight` #61](https://github.com/elanthus/agentic-preflight/pull/61), the record separates initial Preflight redaction findings from later CodeRabbit findings and repairs.
Later repairs included temporary secret-file rewrites and incomplete SHA-256 repository support.

In [`OSWorldTasks` #86](https://github.com/elanthus/OSWorldTasks/pull/86), the description records a clean revised diff before further hosted reviews found gaps.
Those gaps concerned recovery, provider-attempt accounting, and evidence persistence.
This case study does not attribute those later findings to Preflight's initial review.
The changed snapshots do not support a numerical miss rate.

Hosted CI also found an evidence-manifest problem in
[`OSWorldTasks` #54](https://github.com/elanthus/OSWorldTasks/pull/54).
An edit to `deploy/README.md` changed frozen evidence.
The repair preserved the tracked file and required a new Preflight run.
CI supplied information beyond the local review record.

The 40 follow-up finding records include documentation corrections, low-severity observations, and intentional `no_op` dispositions.
Multiple PRs can discuss the same issue.
Neither 40 nor the combined 64 measures distinct defects fixed, prevented incidents, or reviewer recall.
The later record shows continued use and documented repair cycles.
It also shows why hosted checks and additional review remain necessary.

## What the original sample's runs caught

The representative findings concerned meaning and behavior rather than syntax.
Their PR records describe regression tests added with repairs and final configured stages that passed.
Where available, hosted CI supplied separate verification.
These findings concerned implementations that could keep invalid evidence, authorize the wrong actor, change immutable input, or scan the wrong trust domain.

### Green evidence must bind every decision-making input

The same failure pattern occurred independently in three codebases:

- [`agentic-preflight` #27](https://github.com/elanthus/agentic-preflight/pull/27) records a high-severity finding.
  Reused stage evidence was not bound to the user's intent.
  Further review found that evidence could also remain valid after an effective configuration change.
  The repair added portable intent and configuration digests, current-base ancestry checks, and regression tests.
- [`jobwright` #37](https://github.com/elanthus/jobwright/pull/37) omitted automatic model selection from cache identity.
  A cached result could remain valid after a provider or model change.
  The repair fingerprints the resolved provider and model without recording credentials.
- [`news-briefing` #46](https://github.com/elanthus/news-briefing/pull/46) had evaluator checkpoints that were not bound to the manifest and suite contents.
  A related semantic checkpoint could reuse an older result for only the first topic after the prompt expanded to all citing topics.
  Repairs added input hashes, changed checkpoint identity, and tests for changed inputs.

A successful result is valid only for the complete input manifest that produced it.
Code, configuration, intent, selected model, prompts, and evaluation corpus can all affect correctness.

### Human approval needs both identity and state checks

Two changes to Agentic Preflight exposed publication policy gaps that ordinary tests would not identify:

- [`agentic-preflight` #23](https://github.com/elanthus/agentic-preflight/pull/23) initially permitted an approval check without proof of the approving account's eligibility.
  The account had to be a repository owner, member, or collaborator.
  The review also found that repository Git configuration could affect notes-conflict handling.
  Repairs made both paths explicit and deterministic.
- [`agentic-preflight` #24](https://github.com/elanthus/agentic-preflight/pull/24) found that manual-merge policy did not reject a PR with GitHub auto-merge enabled.
  Another high-severity finding concerned instructions that confused push authorization with standing permission to create a PR.
  The repair added an auto-merge state check and kept the permissions separate.

The repairs established two testable claims.
An eligible person must approve the exact head.
The hosted PR state must still enforce the intended merge path.

### Trust boundaries can fail in both directions

The record includes insufficient protection and protection applied too broadly:

- [`agentic-preflight` #33](https://github.com/elanthus/agentic-preflight/pull/33) found a secret-redaction gap.
  A dotenv loader could decode an escaped apostrophe in a double-quoted value.
  The redaction set kept the escaped spelling.
  The repair added the decoded representation and a regression test.
- [`news-briefing` #28](https://github.com/elanthus/news-briefing/pull/28) found a high-severity ambiguity in configured source identifiers.
  An identifier with the evaluator's error delimiter could bypass the intended corpus-health contract.
  The repair rejects ambiguous identifiers when configuration loads.
- [`jobwright` #48](https://github.com/elanthus/jobwright/pull/48) found the opposite error.
  An injection scan for generated output also scanned the raw, untrusted research ledger.
  It would fail when a canary correctly stayed within its source.
  The repair excludes raw source material and tests that boundary.

A check that reports correct containment as failure can teach users to ignore it.
False-positive boundaries are part of safety design.

### Resumability and immutability need adversarial review

Long-running and paid workflows produced another repeated group of findings:

- [`news-briefing` #38](https://github.com/elanthus/news-briefing/pull/38) saved checkpoints for validated review batches but did not resume from them.
  A later malformed response could repeat calls that had already incurred cost.
  The fix binds resumable checkpoints to the suite hash, models, and batch size.
- [`jobwright` #53](https://github.com/elanthus/jobwright/pull/53) found that rescoring a new benchmark case could create artifact directories inside the source run.
  This violated the guarantee that the source run would not change.
  The repair returns a missing-artifact result without a run context that changes the source.
- [`OSWorldTasks` #2](https://github.com/elanthus/OSWorldTasks/pull/2) found that a cached extracted guest image could bypass archive-member validation.
  The repair checks the cached member's size and CRC32 against the SHA-256-verified archive.
  It also adds regression tests.

All three defects occurred during recovery.
The normal path could pass while a resume, rescore, or cache hit violated the stronger contract.

## How findings changed publication in the original sample

Blocking findings stopped the state machine.
Repair commits changed the reviewed snapshot. The next review used a new manifest.
High-risk paths and high-severity findings also required human review instead of an automatic merge path.
The following PR descriptions preserve findings, repair commits, final dispositions, and manual-merge requirements:

- [`agentic-preflight` #24](https://github.com/elanthus/agentic-preflight/pull/24)
- [`news-briefing` #28](https://github.com/elanthus/news-briefing/pull/28)
- [`jobwright` #53](https://github.com/elanthus/jobwright/pull/53)

Across all 153 merged PRs, the median GitHub time from PR creation to merge was about 12.6 minutes.
For the 122 PRs that explicitly recorded Preflight use, it was about 13.1 minutes.
These values show that the owner-operated workflow did not create a publication queue lasting several days.
They do **not** measure additional time caused by Preflight.
Most local review, repair, lint, and test work occurred before PR creation.
The evidence does not show that Preflight caused a speed change.

## What this evidence does not prove

- There was no randomized control group.
  The record cannot estimate defect escape rate, reviewer recall, false-negative rate, or productivity improvement.
- Authors maintain PR bodies as audit records. These bodies are not independent measurements.
  The representative findings were checked against linked public descriptions, but descriptions can change.
  The preserved classification rules can miss undocumented use or misclassify prose. They cannot grade review quality.
- One owner operates all four repositories.
  The sample shows repeated use across different codebases. It does not establish organization-wide adoption or peer-review effectiveness.
- Generated evaluation evidence and large refactors make line counts misleading.
  This study does not use changed lines as a quality or productivity measure.
- Clean review receipts prove reported coverage. They do not prove understanding.
  Another review of the same diff can produce different findings.
  CI and human review remain separate evidence sources.

## Conclusion

The original sample and follow-up support a limited claim.
Agentic Preflight repeatedly converted agent review judgments into repairs and publication decisions bound to specific snapshots across four repositories.
Its most valuable findings concerned behavior at boundaries between components or policies.
These included invalid evidence, approval eligibility, secret normalization, trust domain selection, resumability, and immutable inputs.
Deterministic tests alone had not exposed those failures in the proposed changes.

The sample does not establish autonomous review quality.
It shows why the project records coverage, findings, stage results, and human-review requirements separately.
A useful check must preserve both what it checked and the limits of that evidence.
