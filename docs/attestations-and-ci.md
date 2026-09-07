# Portable attestations and CI enforcement

Successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. The version 4 note includes the run identity, commit and tree hashes, dedicated
SHA-256 bindings for user intent and effective configuration, finding status and severity
totals, and a complete stage set. Green lint and test stages include the exact command,
exit code, and SHA-256 of the redacted captured output. Explicitly skipped stages say why
and carry no invented process evidence. Version 5 adds per-stage fingerprints and
original execution provenance for refresh. Version 6 permits explicit pending test
delegation with complete local review/docs/lint evidence. Versions earlier than 4 are rejected.

`agentic-preflight push` first publishes the original-commit refs needed by v5/v6
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

The verifier must support the schema emitted by the producer. Pin it to the same
Agentic Preflight release, or to the same immutable source revision when validating
attestations produced by an unreleased source build. Do not use a v0.3.0 verifier for a
version 4 note: v0.3.0 accepts schema version 3, while current source accepts versions 4, 5, and 6.

For locally completed 0.5.3 attestations, these are example steps for a GitHub Actions
job with `contents: read` permissions. The checkout selects the attested head and its
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
  run: pipx install 'agentic-preflight==0.5.3'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

This minimal example assumes public fetch access and immediate note availability.
For protected policy enforcement, private repositories, and bounded availability
recovery, use the protected-base `hosted-check` flow below. For delegated tests,
use the separate [CI authority setup](#delegating-tests-to-trusted-ci); default
`verify` deliberately rejects pending test evidence.

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so it cannot brick a repository; the required remote check is
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
GitHub Environment gate; availability never grants either approval.

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
For v5 evidence, missing original commits are fetched from exact
`refs/agentic-preflight/evidence/<SHA>` refs in the same contributor remote and their
identities are checked before verification. Missing or mismatched provenance is a
permanent evidence/transport failure, not another missing-note retry.

| Reason or policy result | Recovery |
| --- | --- |
| `missing_notes_ref`, `missing_note` | Confirm that the exact head and its note were published to the selected remote; rerun the bounded check after publication. |
| `incompatible_schema` | Compare producer and protected verifier revisions. Install a compatible consumer first, or emit a supported format. Repeating local preflight alone does not repair compatibility. |
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

Both repository workflows now use `hosted-check` from their protected event-base
installation. Helper support landed first in PR #96; workflow adoption followed in a
separate PR so no caller depends on a command absent from its trusted base. Apply this
order in other repositories too: merge compatible helper support before requiring it
in hosted callers. Never execute proposed helper code with policy credentials to
shortcut the rollout. The attestation wire format is unchanged.

Same-head failure followed by a successful rerun does not establish replication delay
as the cause. Compare the recorded snapshots and verifier identities between attempts;
publication timing, concurrent notes updates, and visibility remain hypotheses until
those observations distinguish them.

## High-risk merge handling

High-risk merge handling is enforced by a separate `pull_request_target` workflow that
also installs its policy checker from the protected base and never executes proposed
branch content. It reruns when the head changes or a review is submitted or dismissed.

The default `manual_merge` mode reports success only while GitHub auto-merge is disabled
and instructs the agent never to merge or enable auto-merge. `environment` pauses a
dedicated job at the configured GitHub Environment, and `peer_review` retains the
exact-head approval rule for an eligible person other than the pull-request author.

Make **high-risk human approval** a required status check on `main`; keep **Require
review from Code Owners** enabled as the stricter ownership rule for sensitive paths.
Because the workflow and policy are loaded from the protected base, a pull request that
changes approval mode is judged by the old mode until that change is merged.

## Dependabot and other bot-authored pull requests

Dependabot creates commits on GitHub rather than through your local preflight workflow.
Those commits therefore have no `refs/notes/agentic-preflight` note, and a required
attestation check reports “has no agentic-preflight attestation.” If approval policy is
derived from the same attestation, its checks fail too; those are downstream failures,
not evidence that Dependabot found a vulnerability or that the update itself is bad.

Do not broadly exempt Dependabot from the required check. Group its version updates to
reduce review overhead, then attest each grouped pull request without rewriting its
commit:

1. Add a wildcard `groups` rule to each ecosystem in `.github/dependabot.yml`. This
   repository groups Python dependencies separately from GitHub Actions because Actions
   updates change trusted CI and release code.
2. Check out the Dependabot pull request locally with `gh pr checkout <number>`.
3. Run the normal Agentic Preflight workflow against that exact branch tip. Do not amend,
   squash, or rebase it after review without refreshing; any new SHA needs its own note.
4. When the workflow reaches its publication gate, run `agentic-preflight push`; it
   publishes required original-commit evidence refs first, then atomically pushes
   the unchanged branch and `refs/notes/agentic-preflight` together.
5. Re-run the failed GitHub Actions workflows from the pull request. A notes-only push
   does not create a new `pull_request` event, so the original failed runs will not
   automatically notice the new attestation.

Keep GitHub Actions updates under normal code-owner and manual-merge policy even when
they are grouped. For a frequently changing dependency pull request, finish review only
after Dependabot has stopped rebasing it; every rebase changes the attested SHA.

## Evidence reuse across rebases

After a rebase, run `start` with the original intent. With a compatible protected
base, it classifies each stage, preserves applicable evidence, and returns the
next command. `status` resumes after interruption. Shell stages rerun unless
their committed content contracts are satisfied. See the
[fingerprint contract](fingerprint-contract.md) for supported inputs and limits.

The new exact commit receives a v5 note. Reused stages retain original runs,
commits, execution times, commands/output digests, findings, and review manifests.
Refresh time is separate. The verifier recomputes available Git and policy
bindings and validates every review unit before accepting transferred coverage.
Unsigned provenance remains an audit record, not execution authentication.

The publisher retains original commits locally and publishes their evidence refs
before atomically publishing the branch and note. Gate summaries, manual push commands, and dry
runs include these refs. Keep them while published notes reference them; ordinary
run cleanup does not delete them. A fresh consumer fetches only the originals named
by the selected note. Upgrade the publisher and hook as well as the protected
consumer before relying on this transport. For an older note, republish from a
clone that retains its original commits; a note's hashes cannot recover lost data.

Upgrade trusted hosted consumers **before** enabling v5 production. In other
repositories, deploy the compatible verifier, then commit `[reuse]` with
`attestation_schema = 5` on the protected base. Explicit declarations take
precedence over the [legacy source-marker fallback](schema-compatibility.md#legacy-transition).
Until that consumer lands, its producer
runs all required local stages and emits v4; it never executes the PR's verifier
with policy credentials. Historical v4 notes without sufficient local
fingerprints are not silently upgraded into reusable evidence.

## Delegating tests to trusted CI

CI delegation is opt-in. Publication and merge have separate verification purposes:

| Command | Meaning |
| --- | --- |
| `agentic-preflight verify HEAD` | Complete local attestation; rejects pending delegation. It does not evaluate human merge approval. |
| `agentic-preflight verify HEAD --purpose publish` | Local publication requirements satisfied; delegated tests may still be pending. |
| `agentic-preflight ci status --repo OWNER/REPO --pr 86` | Current published local evidence, trusted integration tests, and configured human merge policy are satisfied only when `merge_requirements_satisfied` is true. |

Schema 6 has a `delegated` test stage with no command, exit code, output hash, or
execution timestamp. `green_at` is null; `publication_ready_at` records when the
local publication requirements were satisfied. Three local stage origins preserve
their actual execution provenance. The note includes the protected CI declaration
and original policy revision, not a fabricated remote pass. Earlier consumers
reject this schema. Default local snapshots retain the existing v4/v5 wire format.

Install consumers before producers:

1. Generate templates with `agentic-preflight ci templates --directory /tmp/preflight-ci`.
   Review and copy them into `.github/workflows/` on the protected default/base branch.
   Adapt the matrix and test commands in `preflight-tests.yml`, and the base branch
   in `preflight-ci.yml`. The templates assume this Python package's source is
   installed from that protected checkout. In another project, replace installation
   with an immutable release/revision supporting schema 6. Do this in both workflows.
2. Merge that consumer setup using complete local validation. Retrieve the numeric
   repository ID with `gh api repos/OWNER/REPO --jq .id` and the workflow ID with
   `gh api repos/OWNER/REPO/actions/workflows/preflight-tests.yml --jq .id`.
   Commit the [CI declaration](configuration.md#protected-ci-tests) on the protected
   base in a separate rollout; keep its PR on the local validation path until merged.
3. Register and install a dedicated GitHub App with Actions/Checks write and
   Contents/Pull requests read permissions for this repository. Set
   `PREFLIGHT_CI_APP_ID` as a repository variable and `check_app_id` in protected
   configuration. Store `PREFLIGHT_CI_PRIVATE_KEY` **only** as a secret in the
   `preflight-authority` environment, with deployment branches restricted to the
   exact protected base branch. Do not expose it as a repository-wide secret or to
   proposed workflows. The template uses the official
   [GitHub App token action](https://github.com/actions/create-github-app-token)
   pinned to an immutable revision and scopes the token to this repository.
4. Require **preflight merge readiness** specifically from that dedicated App.
   Do not select the generic GitHub Actions app: another workflow can imitate a
   job/check name from that source. The evaluator verifies the App identity of
   its check writes and refuses a mismatched token.
   Require branches to be up to date before merging, dismiss stale approvals, and
   retain CODEOWNERS protection for configuration, workflows, and verifier code.
   Replace a required legacy `verify HEAD` check when activating pending publication;
   leaving it required would reject every delegated note. Preserve other required
   checks and ownership/approval rules. Restrict check-writing credentials to the
   trusted evaluator. For environment mode, configure actual required reviewers on
   the named environment before enabling delegation.
5. After the protected policy is installed, synchronize feature branches and run the
   normal local sequence. `stage run test` records delegation without starting local
   tests. Publish through the usual authorized gate and open the PR. This produces
   the input CI needs without waiting for CI before the first push.

The dispatcher runs on protected PR events and base pushes. It dispatches the test
workflow on the protected base, passing the current head/base/integration identity.
The API must confirm that the named base branch is protected and still at that SHA;
retargeting to an unprotected branch cannot authorize an alternative policy.
The preparation job validates that identity using GitHub's PR and commit APIs. Tests
check out exactly that merge commit and run in isolated read-only jobs, including
for fork and Dependabot PRs. Do not add write credentials, secrets, shared caches,
or untrusted artifact execution to test jobs. The dispatcher/evaluator must never
check out PR code, install its dependencies, or execute its local actions.

The evaluator checks repository and workflow IDs, workflow path and protected
definition revision, candidate digest, exact integration parents/tree, and every
required job and execution step in the latest attempt of the latest matching run.
A PR workflow with the same job name cannot satisfy those bindings. Artifacts and
agent-provided result JSON are not test authority. The published Git note remains
unsigned audit evidence for local review; this feature does not authenticate model
judgment or implement the separate threat model in issue #25.

The combined verdict is attached to the exact integration SHA. A head check stays
pending: GitHub can fall back to head checks while a newly computed integration
commit has no checks, and that fallback must never reuse an old success. Each
reconciliation first marks both known subjects pending, then completes only the
validated integration subject. This also works when other required checks run on
the merge commit. See GitHub's
[head-versus-merge check rules](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

`ci status` retrieves the head repository's published note (including fork notes)
and live GitHub results even after local
`finish` or a restart. It fetches missing commit objects as inert Git data; it does
not overwrite local notes. No completion note or extra notes-only push is required.
Run it from a repository checkout with authenticated `gh` access. The hosted
reconciler updates the combined check on workflow events and every five minutes;
scheduled Actions may be delayed by GitHub. Strict up-to-date branch protection is
required because GitHub offers no atomic API-read-and-merge operation.

Pending, failed, unavailable, expired, and stale results exit 3 with a reason,
available run link, and next action. `ci status` is itself the recovery command for
this remote lifecycle; a new local `start` is needed only when reported local
evidence is invalid. Missing notes or API failures remain unknown. Rerun all jobs
for a transient failure without repeating local model review. A base update gets a
fresh integration run; dispatch explicitly with
`agentic-preflight ci dispatch --repo OWNER/REPO --pr 86` if needed. This command is
idempotent for an existing candidate; `--force` requests a fresh complete run.
Source repairs use the ordinary per-stage evidence invalidation flow from #85.
Manual-merge, environment, peer approval, and scoped cleanup requirements still apply.
