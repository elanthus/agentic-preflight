# Portable attestations and CI enforcement

Successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. The version 4 note includes the run identity, commit and tree hashes, dedicated
SHA-256 bindings for user intent and effective configuration, finding status and severity
totals, and a complete stage set. Green lint and test stages include the exact command,
exit code, and SHA-256 of the redacted captured output. Explicitly skipped stages say why
and carry no invented process evidence. Version 5 adds per-stage fingerprints and
original execution provenance for refresh. Version 6 permits explicit pending test
delegation with complete local review/docs/lint evidence. Versions earlier than 4 are rejected.

`agentic-preflight push` atomically pushes the branch and
`refs/notes/agentic-preflight`, so the attestation is not stranded in one clone. Git
does not fetch notes in an ordinary checkout; fetch the dedicated ref before reading or
verifying it:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
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

A minimal GitHub Actions required check is:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
- name: Install the verifier that matches the attestation producer
  # Example for notes produced by the published v0.3.0 release. Replace this
  # with the exact matching release or immutable source revision.
  run: pipx install 'agentic-preflight==0.3.0'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so it cannot brick a repository; the required remote check is
what rejects a branch tip without an attestation.

This repository dogfoods that check by installing the verifier from the protected pull
request base commit, then fetching the proposed commit and its note from the
contributor's remote. The pull request cannot change the verifier that judges it.
Governance paths are also listed in `.github/CODEOWNERS`; enable **Require review from
Code Owners** in the branch ruleset because the file alone only requests reviewers.

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
   atomically pushes the unchanged branch and `refs/notes/agentic-preflight` together.
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

Upgrade trusted hosted consumers **before** enabling v5 production. In other
repositories, deploy the compatible verifier, then commit `[reuse]` with
`attestation_schema = 5` on the protected base. This repository recognizes the
consumer in its protected-base source. Until that consumer lands, its producer
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
