# Portable attestations and CI enforcement

A successful merge-back writes a schema version 7 JSON attestation as a Git note on the exact commit.
Every note includes an explicit `outcome`. `verified` means that all local validation is complete.
A note includes the run identity and the commit and tree hashes.
It includes dedicated SHA-256 bindings for the user intent and for the complete effective configuration snapshot.
It also includes the finding totals, the stage fingerprints, and the original execution provenance.
A green shell stage includes its command, exit code, and redacted-output digest.
A skipped stage records why it was skipped.
Any other schema version is rejected.

`agentic-preflight push` first publishes the original-commit refs that the attestation evidence needs.
Then it pushes the branch and `refs/notes/agentic-preflight` atomically.
If the publication of an original fails, the branch push does not start.
An interrupted publication can leave retained evidence refs that are ready for a retry.
Git does not fetch notes in an ordinary checkout.
Fetch the dedicated ref before you read or verify it:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
git fetch origin 'refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
git notes --ref=refs/notes/agentic-preflight show HEAD
agentic-preflight verify HEAD
```

`verify <sha>` exits non-zero in these cases:

- The note is missing or malformed.
- The note names another commit or describes another tree.
- The note omits a stage.
- The note claims a green shell stage without its command, zero exit code, and output hash.

## Required GitHub check

The verifier requires schema version 7.
Pin it to the same Agentic Preflight release as the producer.
For attestations from an unreleased source build, pin it to the same immutable source revision.
Notes with any other schema are rejected.

These are example steps for a GitHub Actions job with `contents: read` permissions.
The checkout selects the attested head and its repository, including for forks.
It does not install or execute that source.
Install Python 3.11 through 3.13 and `pipx` on the runner first.
The pinned package must be published before this example can run.

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
  run: pipx install 'agentic-preflight==0.8.0'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

This minimal example assumes public fetch access and immediate note availability.
For protected policy enforcement and for private repositories, use the protected-base fetch and verification flow below.

Make that job a required status check in branch protection.
The local hook stays fail-open and bypassable, so a missing tool never blocks every push.
The required remote check is what rejects a branch tip without an attestation.

This repository uses that check on itself.
It installs the verifier from the protected pull-request base commit.
Then it fetches the proposed commit and its note from the contributor's remote.
The pull request cannot change the verifier that judges it.
Governance paths are also listed in `.github/CODEOWNERS`.
That file requests reviewers.
GitHub enforces Code Owner approval only when an administrator enables **Require review from Code Owners** in a branch ruleset.

## Administrator setup and this repository's settings

Local checks, hosted checks, and merge requirements are separate:

| Layer | What enables it | What it establishes |
| --- | --- | --- |
| Local advisory workflow | Install the CLI and the skill. Run `init` in each clone | Normal pushes check the exact tip. The hook stays bypassable and fail-open when the tool or the configuration is missing. |
| Hosted verification | Commit trusted workflows that install a pinned release or protected-base source | CI rejects missing or invalid evidence and evaluates the configured approval policy. A workflow alone does not block merge. |
| GitHub merge enforcement | An administrator enables a branch ruleset and names the required checks | GitHub blocks merge until those checks satisfy the rules, subject to the configured bypass permissions. |

For a corporate pilot, require **trusted preflight attestation** and the team's CI and security checks.
Require **high-risk human approval** if approval policy must block merge.
Enable Code Owner review for ownership enforcement. Enable review-thread resolution if the team requires it.
Configure required reviewers for any approval Environment.
Review the ruleset bypass actors as part of that setup.
Installation of the package or a commit of its configuration changes none of these GitHub settings.

A read-only GitHub API inspection on 2026-10-13 found this repository's active [PR first ruleset](https://github.com/elanthus/agentic-preflight/rules/19934690):

- Pull requests and protection against branch deletion are required on `main`.
- The required checks are `test / test (ubuntu-latest, py3.13)`, `built wheel smoke test`, `trusted preflight attestation`, `CodeQL (Python)`, and `dependency vulnerability audit`.
- Zero approving reviews are required. Code Owner review, last-push approval, review-thread resolution, and strict up-to-date checks are disabled.
- The workflow reports `high-risk human approval`, but it is not a required check.
  Contributor instructions still require owner approval for substantive authorization, approval-enforcement, or bypass-rule changes.
- `elanthus` has a pull-request-only ruleset bypass.
  Repository auto-merge is disabled. The merge, squash, and rebase merge methods are enabled.
  Thus, the Dependabot auto-merge workflow needs an administrator to enable auto-merge before it can operate.
- The `pypi` Environment requires `elanthus` review and permits `v*` tags.
  Self-review is permitted. Administrators can bypass its protection. See [Releasing](RELEASING.md).

These points are a dated observation. They are not a guarantee for another repository or for a later run.
No settings were changed during release preparation.

## Protected-base fetch and verification

Both repository workflows install their checker from the protected event-base commit.
They fetch the proposed content only as inert Git data.
They select the contributor's remote for fork pull requests.
Then they fetch the exact event head, the notes, and the retained evidence:

```bash
git fetch --no-tags -- "$source_remote" "$HEAD_SHA" \
  '+refs/notes/agentic-preflight:refs/notes/agentic-preflight' \
  '+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
agentic-preflight verify "$HEAD_SHA"
```

For approval evaluation, call `approval-check "$HEAD_SHA" --base "$BASE_SHA"` with `--reviews-file "$reviews_file" --author "$PR_AUTHOR"`.
Add `--report-only` or `--environment-approved` when applicable.
These flags keep their policy meanings.
The caller still enforces the auto-merge restrictions and the actual GitHub Environment approval.
Never install or execute proposed code with policy credentials.

The head and the notes are published atomically.
Retained evidence refs are published first.
They include the original commits that reused stage evidence and approval after a clean rebase need.
Fetch them from the same source remote as the note.
The wildcard also works when there are no retained evidence refs.
Forced ref updates replace any previous runner snapshot.
Verification always checks the exact event SHA, its tree, and its evidence.
Never borrow a fork's notes from the protected base repository.

The trusted attestation job has a five-minute limit. The approval policy job has a thirty-minute limit.
These limits cover setup, transport, verification, and policy API requests.
Thus, a stalled operation cannot keep the checks pending for the default job duration.

A failed fetch fails the job immediately.
`verify SHA` and `approval-check` stay offline and deterministic. They do not retry or repair missing evidence.
Evidence failures keep exit code 2, `attestation_failed`, a structured `data.reason`, and recovery instructions.
A missing note requires publication to the selected remote.
An unsupported schema requires a matching producer and verifier.
Invalid evidence or wrong commit or tree bindings require valid evidence for the exact commit.
Investigate transport failures as Git access or permission problems.
Print the verification envelope and preserve its exit status before you read the approval outputs.
Run the workflow again after you correct the cause.

## High-risk merge handling

A separate `pull_request_target` workflow enforces high-risk merge handling.
It also installs its policy checker from the protected base. It never executes proposed branch content.
It runs again when the head changes or when a review is submitted, edited, or dismissed.

The default `manual_merge` mode reports success only while GitHub auto-merge is disabled.
It instructs the agent never to merge or enable auto-merge.
`environment` pauses a dedicated job at the configured GitHub Environment.
`peer_review` requires approval by an eligible person other than the pull-request author.
`owner_review` requires the configured reviewer: an Approve review on another author's PR, or a Comment review that contains exactly `approved` on their own PR.
In both review modes, the approval must be on the current head or on an equivalent earlier head.
An earlier head is equivalent when the trusted checker proves from Git objects that the branch content is unchanged.
A clean merge from the base or a clean rebase keeps the approval.
Any content change needs a new review.
See [approval configuration](configuration.md#high-risk-merge-handling-approval).

This repository configures `owner_review` with `reviewer = "elanthus"`.
Thus, the approval check can report a review status for the current content.
The review is an Approve review on another author's PR, or a Comment review that contains only `approved` on your own PR.
Its current branch rules require automated CI and dependency audits.
Human approval, Code Owner review, and resolved review threads are not mandatory GitHub merge requirements.
Contributors must still follow this repository's approval and review instructions.
Changes only to `pyproject.toml` or `uv.lock` no longer trigger high-risk review or CODEOWNERS requests.
Dependabot automation settings stay protected policy.

A project that enforces human approval must make **high-risk human approval** a required status check.
It must also enable **Require review from Code Owners** for sensitive paths.
The workflow and the policy are loaded from the protected base.
Thus, a pull request that changes the approval mode is judged by the old mode until that change merges.
`approval-check SHA --base BASE` reads `[policy]` and `[approval]` from the `.agentic-preflight.toml` file committed at `BASE`.
It does not read the checked-out working tree or the user configuration.
A base commit with no such file uses the defaults.

## Dependabot and other bot-authored pull requests

This repository exempts **all verified Dependabot updates from human approval**, including major versions and GitHub Actions.
The protected-base human-approval workflow verifies the actual `dependabot[bot]` Bot identity and a same-repository head and base.
It also verifies the current event SHAs and the complete paginated commit list against Git history.
Every non-merge contribution must be attributed to the bot, committed by GitHub's `web-flow` identity, and have a verified signature whose verification reason is `valid`.
A human-signed commit with a forged bot author does not qualify.
A merge commit qualifies only under two conditions.
Its second parent belongs to the protected base, and its tree exactly matches Git's clean merge of the two parents.
Human-added commits, conflict resolutions, unrelated branch merges, missing records, and snapshot mismatches do not receive this exemption.
Ordinary approval policy applies to them.
Only protected-base code runs, with read-only permissions. Proposed code is inert Git data.
The hosted report records the exact head and base and the verification result.

This approval exemption does not skip attestation, tests, packaging checks, or audits.
It does not expand auto-merge eligibility.
Outside the narrow exception below, the normal attestation check must succeed before bot provenance can waive human review.
The local `approval-check` command still reports the configured path-based policy.
The repository's hosted workflow separately verifies and applies the bot exemption.

A narrower class of Dependabot updates also skips local attestation and is eligible for the auto-merge workflow.
These are same-repository PRs that the actual `dependabot[bot]` Bot account authored, with exactly one verified Dependabot commit that matches the event and the current head.
Their metadata must identify every dependency as a Python patch or minor update in the root directory.
Their modified files must be limited to `pyproject.toml` and `uv.lock`.
Unknown metadata, renames, deletions, additional commits or files, major updates, and GitHub Actions updates do not qualify for the attestation and auto-merge exception.
A grouped update qualifies only when every member qualifies.

The shared `dependabot-policy.yml` workflow checks the bot identity, the signed commit metadata, and every grouped dependency.
It also checks the complete paginated commit and file lists and the current head.
CI still runs tests, the built-wheel smoke check, and dependency audits.
This repository already makes owner approval optional for routine Python dependencies.
The exception prevents missing local notes from failing the hosted approval report.
The attestation job is skipped only for this verified class.
Its existing required check can stay configured, because GitHub treats an intentionally skipped job as successful.
See [GitHub's conditional job documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions).

The `dependabot-auto-merge.yml` workflow requests squash auto-merge only after the shared eligibility check succeeds and matches the verified event head.
Its write-token job never checks out or executes PR code.
GitHub still enforces the required checks before merge.
Repository administrators must separately enable **Allow auto-merge** and squash merging.
They must keep the required tests, packaging checks, and dependency audits.
This workflow change does not alter repository settings. It does not enable auto-merge on an existing PR.

Major updates and GitHub Actions updates keep the original attestation workflow.
Check out those Dependabot PRs, run preflight, and publish the head and its note.
Run CI again if only the notes changed. A notes-only push does not trigger a PR workflow.
After the required CI and the protected-base provenance check pass for the exact head, an agent can perform the user-authorized merge without human review.
Other bots and human-modified Dependabot PRs keep the ordinary approval workflow.

## Evidence reuse across rebases

After a rebase, run `start` with the original intent.
With the current verifier on the protected base, `start` classifies each stage, preserves applicable evidence, and returns the next command.
`status` resumes after an interruption.
Shell stages always run again in new runs. Their evidence never transfers into another run.
See the [fingerprint contract](fingerprint-contract.md) for the supported inputs and limits.

The new exact commit receives a version 7 note.
Reused stages keep the original runs, commits, execution times, commands and output digests, findings, and review manifests.
The refresh time is separate.
The verifier recomputes the available Git and policy bindings.
It validates every review unit before it accepts transferred coverage.
Unsigned provenance stays an audit record, not execution authentication.

The publisher keeps the original commits locally.
It publishes their evidence refs before it publishes the branch and the note atomically.
Gate summaries, manual push commands, and dry runs include these refs.
Keep them while published notes reference them. Ordinary run cleanup does not delete them.
The documented wildcard fetch retrieves every retained evidence ref from the source remote.
The verifier then uses only the originals that the selected note names.
Keep the publisher, the hook, and the protected verifier on the same release.
A note's hashes cannot recover lost data.

Every completed local run requires complete stage provenance before publication.
The tool never executes the PR's verifier with policy credentials.
