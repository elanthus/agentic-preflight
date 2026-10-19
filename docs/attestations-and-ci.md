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
  run: pipx install 'agentic-preflight==0.7.0'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

This minimal example assumes public fetch access and immediate note availability.
For protected policy enforcement and private repositories, use the protected-base
fetch and verification flow below.

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so a missing tool never blocks every push; the required remote check is
what rejects a branch tip without an attestation.

This repository dogfoods that check by installing the verifier from the protected pull
request base commit, then fetching the proposed commit and its note from the
contributor's remote. The pull request cannot change the verifier that judges it.
Governance paths are also listed in `.github/CODEOWNERS`. That file requests reviewers;
GitHub enforces Code Owner approval only when an administrator enables
**Require review from Code Owners** in a branch ruleset.

## Administrator setup and this repository's settings

Local checks, hosted checks, and merge requirements are separate:

| Layer | What enables it | What it establishes |
| --- | --- | --- |
| Local advisory workflow | Install the CLI/skill and run `init` in each clone | Normal pushes check the exact tip; the hook remains bypassable and fail-open on missing tooling/configuration. |
| Hosted verification | Commit trusted workflows that install a pinned release or protected-base source | CI rejects missing or invalid evidence and evaluates configured approval policy. A workflow alone does not block merge. |
| GitHub merge enforcement | An administrator enables a branch ruleset and names required checks | GitHub blocks merge until those checks satisfy the rules, subject to configured bypass permissions. |

For a corporate pilot, require **trusted preflight attestation**, the team's CI and
security checks, and **high-risk human approval** if approval policy must block merge.
Enable Code Owner review for ownership enforcement and review-thread resolution if
required by the team. Configure required reviewers for any approval Environment.
Review ruleset bypass actors as part of that setup. Installing the package or committing
its configuration changes none of these GitHub settings.

Read-only GitHub API inspection on 2026-10-13 found this repository's active
[PR first ruleset](https://github.com/elanthus/agentic-preflight/rules/19934690):

- Pull requests and protection against branch deletion are required on `main`.
- Required checks are `test / test (ubuntu-latest, py3.13)`, `built wheel smoke test`,
  `trusted preflight attestation`, `CodeQL (Python)`, and `dependency vulnerability audit`.
- Required approving reviews are zero; Code Owner review, last-push approval,
  review-thread resolution, and strict up-to-date checks are disabled.
- `high-risk human approval` is reported by its workflow but is not a required check.
  Contributor instructions still require owner approval for substantive authorization,
  approval-enforcement, or bypass-rule changes.
- `elanthus` has a pull-request-only ruleset bypass. Repository auto-merge is disabled;
  merge, squash, and rebase merge methods are enabled. The Dependabot auto-merge
  workflow therefore needs an administrator to enable auto-merge before it can operate.
- The `pypi` Environment requires `elanthus` review and permits `v*` tags. Self-review is
  allowed and administrators can bypass its protection. See [Releasing](RELEASING.md).

These are a dated observation, not a guarantee for another repository or a later run.
No settings were changed during release preparation.

## Protected-base fetch and verification

Both repository workflows install their checker from the protected event-base commit
and fetch proposed content only as inert Git data. They select the contributor's remote
for fork pull requests, then fetch the exact event head, notes, and retained evidence:

```bash
git fetch --no-tags -- "$source_remote" "$HEAD_SHA" \
  '+refs/notes/agentic-preflight:refs/notes/agentic-preflight' \
  '+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
agentic-preflight verify "$HEAD_SHA"
```

For approval evaluation, call `approval-check "$HEAD_SHA" --base "$BASE_SHA"`
with `--reviews-file "$reviews_file" --author "$PR_AUTHOR"` and optional
`--report-only` or `--environment-approved`. These flags retain their policy meanings.
The caller still enforces auto-merge restrictions and actual GitHub Environment approval.
Never install or execute proposed code with policy credentials.

The head and notes are published atomically. Retained evidence refs are published first
and include original commits needed for reused stage evidence and approval after a clean
rebase. Fetch them from the same source remote as the note. The wildcard also works when
there are no retained evidence refs. Forced ref updates replace any previous runner
snapshot; verification always checks the exact event SHA, its tree and its evidence.
A fork's notes must never be borrowed from the protected base repository.

The trusted attestation job has a five-minute limit; the approval policy job has a
thirty-minute limit. These limits cover setup, transport, verification, and policy API
requests, so stalled operations cannot keep the checks pending for the default job
duration.

A failed fetch fails the job immediately. `verify SHA` and `approval-check` remain offline
and deterministic; they do not retry or repair missing evidence. Evidence failures retain
exit code 2, `attestation_failed`, a structured `data.reason`, and recovery instructions.
Missing notes require publication to the selected remote. Unsupported schemas require a
matching producer and verifier; invalid evidence or wrong commit/tree bindings require
valid evidence for the exact commit. Investigate transport failures as Git access or
permissions problems. Print the verification envelope and preserve its exit status
before reading approval outputs. Rerun the workflow after correcting the cause.

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
branch rules require automated CI and dependency audits. Human approval, Code Owner
review, and resolved review threads are not mandatory GitHub merge requirements;
contributors must still follow this repository's approval and review instructions.
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

This repository exempts **all verified Dependabot updates from human approval**,
including major versions and GitHub Actions. The protected-base human-approval
workflow verifies the actual `dependabot[bot]` Bot identity, same-repository head and
base, current event SHAs, and the complete paginated commit list against Git history.
Every non-merge contribution must be attributed to the bot, committed by GitHub's
`web-flow` identity, and have a verified signature whose verification reason is
`valid`. A human-signed commit with a forged bot author does not qualify.
Merge commits qualify only when their second parent belongs to the
protected base and their tree exactly matches Git's clean merge of the two parents.
Human-added commits, conflict resolutions, unrelated branch merges, missing records,
and snapshot mismatches do not receive this exemption; ordinary approval policy applies.
Only protected-base code runs, with read-only permissions; proposed code is inert Git
data. The hosted report records the exact head and base and the verification result.

This approval exemption does not skip attestation, tests, packaging checks, or audits,
and does not expand auto-merge eligibility. Outside the narrow exception below, the
normal attestation check must succeed before bot provenance can waive human review.
The local `approval-check` command still reports the configured path-based policy;
the repository's hosted workflow separately verifies and applies the bot exemption.

A narrower class of Dependabot updates also skips local attestation and is eligible
for the auto-merge workflow: same-repository PRs authored by the actual
`dependabot[bot]` Bot account, with exactly one verified Dependabot commit matching
the event and current head, whose metadata
identifies every dependency as a Python patch/minor update in the root directory and
whose modified files are limited to `pyproject.toml` and `uv.lock`. Unknown metadata,
renames, deletions, additional commits or files, major updates, and GitHub Actions updates do not
qualify for the attestation/auto-merge exception. Grouped updates qualify only when every member qualifies.

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

Major updates and GitHub Actions updates keep the original attestation workflow.
Check out those Dependabot PRs, run preflight, publish the head and its note, then
rerun CI if only notes changed (a notes-only push does not trigger a PR workflow).
After required CI and the protected-base provenance check pass for the exact head,
an agent may perform the user-authorized merge without human review. Other bots and
human-modified Dependabot PRs retain the ordinary approval workflow.

## Evidence reuse across rebases

After a rebase, run `start` with the original intent. With the current verifier on the
protected base, it classifies each stage, preserves applicable evidence, and returns the
next command. `status` resumes after interruption. Shell stages always rerun in new runs; their evidence never transfers into another run. See the
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
