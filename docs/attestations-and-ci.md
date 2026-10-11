# Portable attestations and CI enforcement

Successful merge-back writes a schema version 7 JSON attestation as a Git note on the
exact commit. Every note includes an explicit `outcome`: `verified` means all local
validation is complete. Notes include the run
identity, commit and tree hashes, dedicated SHA-256 bindings for user intent and the
complete effective configuration snapshot, finding totals, stage fingerprints, and
original execution provenance. Green shell stages include their command, exit code, and
redacted-output digest; skipped stages say why. Any other schema version is rejected.

`agentic-preflight push` first publishes the original-commit refs needed by attestation
evidence, then atomically pushes the branch and `refs/notes/agentic-preflight`.
If publishing an original fails, the branch push does not start. An interrupted
publication can leave retained evidence refs ready for a retry. Git
does not fetch notes in an ordinary checkout; fetch the dedicated ref before reading or
verifying it:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
git fetch origin 'refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
git notes --ref=refs/notes/agentic-preflight show HEAD
agentic-preflight verify HEAD
```

`verify <sha>` exits non-zero when the note is missing or malformed, names another
commit, describes another tree, omits a stage, or claims a green shell stage without its
command, zero exit code, and output hash.

## Required GitHub check

The verifier requires schema version 7. Pin it to the same Agentic Preflight release as
the producer, or to the same immutable source revision when validating attestations
produced by an unreleased source build. Notes with any other schema are rejected.

These are example steps for a GitHub Actions job with `contents: read` permissions.
The checkout selects the attested head and its
repository, including for forks; it does not install or execute that source. Install
Python 3.11–3.13 and `pipx` on the runner first. The pinned package must be published
before this example can run.

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    repository: ${{ github.event.pull_request.head.repo.full_name || github.repository }}
    ref: ${{ github.event.pull_request.head.sha || github.sha }}
    fetch-depth: 0
    persist-credentials: false
- run: git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
- run: git fetch origin 'refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
- name: Install the matching released verifier
  run: pipx install 'agentic-preflight==0.6.0'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

This minimal example assumes public fetch access and immediate note availability.
For protected policy enforcement, private repositories, and bounded availability
recovery, use the protected-base `hosted-check` flow below.

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so a missing tool never blocks every push; the required remote check is
what rejects a branch tip without an attestation.

This repository dogfoods that check by installing the verifier from the protected pull
request base commit, then fetching the proposed commit and its note from the
contributor's remote. The pull request cannot change the verifier that judges it.
Governance paths are also listed in `.github/CODEOWNERS`; enable **Require review from
Code Owners** in the branch ruleset because the file alone only requests reviewers.

## Bounded hosted note availability

`verify SHA` remains offline and deterministic. `verify` and `approval-check` preserve
exit code 2 and `attestation_failed` for evidence failures, with a structured
`data.reason` and recovery instruction. They do not infer a producer version from an
unknown field or invoke local review/tests to repair missing remote evidence.

The explicit `hosted-check` command provides shared availability handling for trusted
CI callers. Run the installed protected-base tool from the original event-base checkout:

```bash
agentic-preflight hosted-check "$HEAD_SHA" --base "$BASE_SHA" \
  --source-remote "$source_remote" --head-ref "refs/heads/$HEAD_REF"
```

All SHAs and the branch name come from the original event. Resolve `source_remote`
to the contributor's configured remote for a fork, as with the existing workflows.
For policy evaluation, add `--mode approval --reviews-file "$reviews_file" --author
"$PR_AUTHOR"`. The existing `--report-only` and `--environment-approved` options retain
their meanings. The caller must still enforce auto-merge restrictions and the actual
GitHub Environment approval; availability never grants either approval.

The helper makes at most four attempts, waiting 2, 4, and 8 seconds between them.
Each availability Git command has a 30-second timeout. Only a missing advertised
notes ref or a missing expected-head note is retried. A fetch failure is not proof of
absence and stops immediately, even if a previous local note exists. No sleep follows
the final attempt. The library accepts injected delays, clock, and sleeper for tests.

Each attempt checks the advertised and fetched PR head against the fixed event SHA.
It fetches notes into a temporary ref, records the actual notes commit, and reads its
immutable tree. Strict verification and approval evaluation consume that same decoded
note. A final head lookup must still match before success. Temporary refs are removed;
the normal local notes ref is neither merged nor overwritten.
For attestation evidence, missing original commits are fetched from exact
`refs/agentic-preflight/evidence/<SHA>` refs in the same contributor remote and their
identities are checked before verification. Missing or mismatched provenance is a
permanent evidence/transport failure, not another missing-note retry.

| Reason or policy result | Recovery |
| --- | --- |
| `missing_notes_ref`, `missing_note` | Confirm that the exact head and its note were published to the selected remote; rerun the bounded check after publication. |
| `incompatible_schema` | Use the current producer and protected verifier so the note uses schema version 7. Repeating local preflight with a different producer does not repair the present note. |
| `malformed_payload`, `invalid_evidence`, `commit_mismatch`, `tree_mismatch` | Inspect and restore valid evidence for the exact commit. The present note is not retried. |
| `stale_candidate` | Run the new event's check. The old event never borrows the new head or its note. |
| `git_failure`, `git_timeout`, `io_failure` | Investigate access, authentication, transport, or local I/O. Raw Git stderr is omitted because it can contain credentials; the operation and exit/timeout remain visible. |
| `verifier_mismatch` | Use the original protected event-base checkout and its installed tool. |
| Unmet human approval / manual merge | Follow the existing approval policy. Availability does not retry or override that policy. |

Save the workflow run ID and attempt, event head/base, source remote/repository,
`availability.verifier_revision` (the protected checkout revision), verifier package
version, and each attempt's advertised/fetched notes commit, observed/fetched head,
note presence/object ID, reason, and elapsed time. Diagnostics omit note bodies and
URL credentials. CLI stdout contains one JSON envelope, including failure diagnostics.
The hosted shell caller must print that envelope and return the original command exit
status before reading approval outputs.

Both repository workflows use `hosted-check` from their protected event-base
installation. Never execute proposed helper code with policy credentials.

Same-head failure followed by a successful rerun does not establish replication delay
as the cause. Compare the recorded snapshots and verifier identities between attempts;
publication timing, concurrent notes updates, and visibility remain hypotheses until
those observations distinguish them.

## High-risk merge handling

High-risk merge handling is enforced by a separate `pull_request_target` workflow that
also installs its policy checker from the protected base and never executes proposed
branch content. It reruns when the head changes or a review is submitted, edited, or dismissed.

The default `manual_merge` mode reports success only while GitHub auto-merge is disabled
and instructs the agent never to merge or enable auto-merge. `environment` pauses a
dedicated job at the configured GitHub Environment, and `peer_review` requires approval
by an eligible person other than the pull-request author. `owner_review` requires the
configured reviewer: an Approve review on another author's PR or a Comment review
containing exactly `approved` on their own PR. In both review modes the approval must be
on the current head, or on an earlier head whose branch content the trusted checker proves
unchanged from Git objects. A clean merge from the base or a clean rebase keeps approval;
any content change needs a new review. See
[approval configuration](configuration.md#high-risk-merge-handling-approval).

This repository configures `owner_review` with `reviewer = "elanthus"`, so the approval
check can report a review status for the current content: an Approve review on another
author's PR, or a Comment review containing only `approved` on your own PR. Its current
branch rules require automated CI and dependency audits, with review feedback resolved
before merge; human approval and Code Owner review are not mandatory merge requirements.
Changes only to `pyproject.toml` or `uv.lock` no longer trigger high-risk review or
CODEOWNERS requests.
Dependabot automation settings remain protected policy.

Projects choosing to enforce human approval should make **high-risk human approval** a
required status check and enable **Require review from Code Owners** for sensitive paths.
Because the workflow and policy are loaded from the protected base, a pull request that
changes approval mode is judged by the old mode until that change is merged.
`approval-check SHA --base BASE` reads `[policy]` and `[approval]` from the
`.agentic-preflight.toml` committed at `BASE`, not from the checked-out working tree or
user configuration. A base commit with no such file uses the defaults.

## Dependabot and other bot-authored pull requests

This repository exempts a narrow class of Dependabot updates from local attestation
and the hosted human-approval policy: same-repository PRs authored by the actual
`dependabot[bot]` Bot account, with exactly one verified Dependabot commit matching
the event and current head, whose metadata
identifies every dependency as a Python patch/minor update in the root directory and
whose modified files are limited to `pyproject.toml` and `uv.lock`. Unknown metadata,
renames, deletions, additional commits or files, major updates, and GitHub Actions updates do not
qualify. Grouped updates qualify only when every member qualifies.

The shared `dependabot-policy.yml` workflow checks the bot identity, signed commit
metadata, every grouped dependency, the complete paginated commit and file lists, and the current
head. CI still runs tests, the built-wheel smoke check, and dependency audits. This
repository already makes owner approval optional for routine Python dependencies;
the exception prevents missing local notes from failing the hosted approval report.
The attestation job is skipped only for this verified class. Its existing required
check may remain configured because GitHub treats an intentionally skipped job as
successful. See [GitHub's conditional job documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions).

The `dependabot-auto-merge.yml` workflow requests squash auto-merge only after the
shared eligibility check succeeds, matching the verified event head. Its write-token
job never checks out or executes PR code. GitHub still enforces required checks before
merging. Repository administrators must separately enable **Allow auto-merge** and
squash merging, and retain required tests, packaging checks, and dependency audits;
this workflow change does not alter repository settings or enable auto-merge on an
existing PR.

Major updates, GitHub Actions updates, and all other bot or human PRs keep the original
attestation and approval workflow. Check out those Dependabot PRs, run preflight against
the exact unchanged tip, publish its note, then rerun CI (a notes-only push does not
trigger a new PR workflow). Review and merge major and Actions updates manually.

## Evidence reuse across rebases

After a rebase, run `start` with the original intent. With the current verifier on the
protected base, it classifies each stage, preserves applicable evidence, and returns the
next command. `status` resumes after interruption. Shell stages rerun unless
their committed content contracts are satisfied. See the
[fingerprint contract](fingerprint-contract.md) for supported inputs and limits.

The new exact commit receives a version 7 note. Reused stages retain original runs,
commits, execution times, commands/output digests, findings, and review manifests.
Refresh time is separate. The verifier recomputes available Git and policy
bindings and validates every review unit before accepting transferred coverage.
Unsigned provenance remains an audit record, not execution authentication.

The publisher retains original commits locally and publishes their evidence refs
before atomically publishing the branch and note. Gate summaries, manual push commands, and dry
runs include these refs. Keep them while published notes reference them; ordinary
run cleanup does not delete them. A fresh consumer fetches only the originals named
by the selected note. Keep the publisher, hook, and protected verifier on the same
release. A note's hashes cannot recover lost data.

Every completed local run requires complete stage provenance before publication.
The tool never executes the PR's verifier with policy credentials.
