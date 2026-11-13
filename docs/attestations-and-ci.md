# Portable attestations and CI enforcement

A successful merge-back writes a schema version 7 JSON attestation as a Git note on the exact commit.
Every note includes an explicit `outcome`. `verified` means that all local validation is complete.
Any other schema version is rejected.
`push` publishes the note, so anyone who can read the remote can read it.

A note contains these fields:

- The kind, schema version, outcome, and the time that local validation went green.
- The run ID, the branch, the base branch, and the commit, tree, and merge-base hashes.
- A SHA-256 digest of the intent. The intent text itself is not included.
- The published configuration snapshot and its SHA-256 digest.
- For each stage, its status, a reason when it was skipped, and its process evidence.
  A command review and a green lint or test stage carry the command, the exit code, and a SHA-256 digest of the redacted output.
  The output itself is not included.
  Review also records its executor and its coverage: the manifest and grounding digests, the reviewed head, the unit count, the cited and clean unit IDs, the excluded file paths, and a contract-plan digest when contract checks are enabled.
- The totals of findings by status and severity.
- For each stage, its evidence: a version, a digest of the original execution, the current fingerprint, and, for evidence reused from an earlier run, the refresh time and the derivation reason.
- For each stage, the original execution:
  - The original run ID, stage, commits, branch, base branch, and finish time.
  - The source worktree ID, an unsalted SHA-256 digest of the local path of the worktree's Git directory.
  - The published configuration snapshot and its digest, and the stage result as above.
  - The original fingerprint.
  - Every finding from that stage in full: ID, stage, whether code owns it, path, line, unit, severity, action, title, detail, suggestion, status, fix commit, and response note.
    The `respond --reason` label is not included.
- A fingerprint holds a version, tree hashes, and SHA-256 digests.
  A review fingerprint also holds the excluded file paths, the executor, and whether the configuration was supported.
  A docs fingerprint also holds whether the configuration was supported.
  A shell fingerprint also holds why its inputs were unavailable, when they were.

The published configuration snapshot leaves out every section that came from the user configuration file, `~/.config/agentic-preflight/config.toml`.
Those sections apply to the run, but they describe one machine.
A run that an earlier release started did not record which sections came from that file, so `mergeback`, `gate`, and `push` refuse it. Abort it and start a fresh run before it can publish.
`[diff]` is the exception. Verification rebuilds the review diff from its exclusions, so it is published from either file.
A section that the repository file sets is published as written.

The review command depends on `[review]`, and the lint and test commands depend on `[commands]`, including a `stage run --command` override.
When that section came from the user file, the note records `[redacted user-level command]` in place of the command.
The shell-command digest in the fingerprint covers that marker, not the command, so the note cannot confirm a guessed command.
Otherwise the command is published as written, so a verifier can compare it with the protected policy.

Evidence reused from an earlier run is held to the same rule.
Reuse refuses evidence whose configuration snapshot holds a section that the current run withholds.
It also refuses evidence from a run record without user-level sections, such as one that an earlier release wrote, unless that snapshot equals the one the current run publishes.
Reuse also refuses evidence with finding text that the current copied-file values would redact.

Finding text and response notes are redacted before they are saved, in the same way as stage logs.
A value from a `[worktree] copy_files` file that appears verbatim becomes `[redacted]`.
This covers findings from a review command, from `submit-findings`, and from `review compare`, and the `respond --note` text.
See [the limits of exact-value redaction](worktree-modes.md#secrets-in-worktrees) and [what a published note reveals](limits.md#what-a-published-note-reveals).

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
- The note's merge base is not the only merge base of the head and the trusted base.
- The evidence does not satisfy the policy committed at the trusted base.

The trusted base is the `--base` revision, or else the note's base branch.
Verification reads policy from the `.agentic-preflight.toml` committed there, not from the note.
See [what verification trusts](limits.md#attestations-are-not-signatures).

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

`pipx install` pins the verifier release but resolves its dependencies at install time.
To pin those as well, install the verifier from its `uv.lock`, as the protected-base flow below does.
Take that lock file and source from a separate checkout of a trusted revision, such as the protected base commit or a release tag.
Never install from the attested-head checkout above: that is the code under verification.

Pass `--base` with the pull request's base SHA when the job runs for a pull request.
Without it, the note names its own base branch.

This minimal example assumes public fetch access and immediate note availability.
For protected policy enforcement and for private repositories, use the protected-base fetch and verification flow below.

Make that job a required status check in branch protection.
The local hook stays fail-open and bypassable, so a missing tool never blocks every push.
The required remote check is what rejects a branch tip without an attestation.

This repository uses that check on itself.
It runs in `.github/workflows/attestation.yml` on `pull_request_target`, so GitHub takes the workflow definition from the base branch, not from the pull request.
The job checks out the base commit and installs the verifier's runtime dependencies from that commit's `uv.lock`, not from a fresh resolution at run time.
It runs the verifier from the same checkout.
Then it fetches the proposed commit and its note from the contributor's remote as data, without checking them out or running anything from them.
A pull request can still change the workflow, the verifier, or its scripts, but those changes take effect only after merge; they never judge the pull request that proposes them.
The workflow token is read-only.
Every checkout in a `pull_request_target` workflow sets `persist-credentials: false`, so the token is not left in the checkout's Git configuration for later steps.
This repository is public, so the fetches that follow work without credentials.
A private repository must authenticate those fetches; pass the read-only token to the fetch command alone rather than persisting it for the whole job.
A `pull_request` trigger would not give this guarantee: it runs the workflow file from the pull request's merge ref, so the pull request could rewrite the job itself.
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

A read-only GitHub API inspection on 2026-11-01 found this repository's active [PR first ruleset](https://github.com/elanthus/agentic-preflight/rules/19934690):

- Pull requests and protection against branch deletion are required on `main`.
- The required checks are `test / test (ubuntu-latest, py3.13)`, `built wheel smoke test`, `trusted preflight attestation`, `CodeQL (Python)`, `dependency vulnerability audit`, and `high-risk human approval`.
- Zero approving reviews are required. Code Owner review, last-push approval, review-thread resolution, and strict up-to-date checks are disabled.
- `high-risk human approval` became a required check on 2026-11-01. Until then the workflow reported it without blocking merge.
  The job fails until an eligible review covers the current head. A review from an owner, member, or collaborator, or a dismissal by a user with write access, re-runs the head's policy runs, which replaces each failed result in place; see [how a review reaches the check](#how-a-review-reaches-the-check).
  Contributor instructions still require owner approval for substantive authorization, approval-enforcement, or bypass-rule changes.
- `elanthus` has a pull-request-only ruleset bypass.
  Repository auto-merge is disabled. The merge, squash, and rebase merge methods are enabled.
  Thus, the Dependabot auto-merge workflow needs an administrator to enable auto-merge before it can operate.
- The `pypi` Environment requires `elanthus` review and permits `v*` tags.
  Self-review is permitted. Administrators can bypass its protection. See [Releasing](RELEASING.md).

These points are a dated observation. They are not a guarantee for another repository or for a later run.
No settings were changed during release preparation.

## Protected-base fetch and verification

Both repository workflows run their checker from the protected event-base checkout.
They fetch the proposed content only as inert Git data.
They select the contributor's remote for fork pull requests.
Then they fetch the exact event head, the notes, and the retained evidence:

```bash
git fetch --no-tags -- "$source_remote" "$HEAD_SHA" \
  '+refs/notes/agentic-preflight:refs/notes/agentic-preflight' \
  '+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*'
uv run --locked --no-dev --no-sync --python 3.11 \
  python -m agentic_preflight verify "$HEAD_SHA" --base "$BASE_SHA"
```

`--base` makes the protected pull-request base, not the note, decide the merge base and the policy.

Before that step, each job installs only the runtime dependencies pinned in the base commit's `uv.lock`:

```bash
uv sync --locked --no-dev --no-install-project --python 3.11
```

`--no-install-project` skips building and installing the checker itself.
No build backend or build dependency therefore runs in the job, and nothing is resolved at run time.
The checker runs from the base checkout as `python -m agentic_preflight`, and `--no-sync` keeps `uv run` from installing anything.
The `verify` and `approval-check` commands do not need the package's installed metadata.
The Dependabot verification script imports the package from the same checkout through `PYTHONPATH`.
Each job also pins the uv release in its `astral-sh/setup-uv` step.
Maintainers bump that `version` by hand; Dependabot updates the action, not the uv release it installs.
`tests/test_workflow_evidence.py` rejects any other install form in these two workflows.

For approval evaluation, call `approval-check "$HEAD_SHA" --base "$BASE_SHA"` through the same `uv run --locked --no-dev --no-sync` and `python -m agentic_preflight` prefix with `--reviews-file "$reviews_file" --author "$PR_AUTHOR"`.
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
A `base_mismatch` means the protected base cannot judge the head. Check that `--base` names the branch the change merges into; otherwise synchronize the branch with its protected base and start a fresh run.
Investigate transport failures as Git access or permission problems.
Print the verification envelope and preserve its exit status before you read the approval outputs.
Run the workflow again after you correct the cause.

## High-risk merge handling

A separate `pull_request_target` workflow enforces high-risk merge handling.
It also runs its policy checker from the protected base with the dependencies pinned in that commit's `uv.lock`. It never executes proposed branch content.
It runs again when the head changes.
In the review modes, a submitted, edited, or dismissed review re-runs it when it comes from an owner, member, or collaborator, or from a user with write access; see [how a review reaches the check](#how-a-review-reaches-the-check).

The default `manual_merge` mode reports success only while GitHub auto-merge is disabled.
It instructs the agent never to merge or enable auto-merge.
`environment` pauses a dedicated job at the configured GitHub Environment.
`peer_review` requires approval by an eligible person other than the pull-request author.
`owner_review` requires the configured reviewer: an Approve review on another author's PR, or a Comment review that contains exactly `approved` on their own PR.
An agent that holds the reviewer's credentials can post that Comment review itself. See [Approval checks cannot tell a person from an agent](limits.md#approval-checks-cannot-tell-a-person-from-an-agent).
In both review modes, the approval must be on the current head or on an equivalent earlier head.
An earlier head is equivalent when the trusted checker proves from Git objects that the branch content is unchanged.
A clean merge from the base or a clean rebase keeps the approval.
Any content change needs a new review.
See [approval configuration](configuration.md#high-risk-merge-handling-approval).

### How a review reaches the check

A finished check does not change when a review arrives.
Three workflows cooperate so that it does, and only one of them is trusted:

- `human-approval.yml` runs only on `pull_request_target`, from the protected base. Its `high-risk human approval` job is the required check.
- `human-approval-review.yml` runs on `pull_request_review`. A review event executes the workflow file from the pull request's merge ref, with a read-only token for forks, so this workflow has no permissions and does nothing but record the reviewed pull request number in an artifact.
- `human-approval-rerun.yml` runs on `workflow_run`, always from the default branch, with `actions: write` and no checkout. It reads the number, requires the pull request to be open with the reviewed head and repository, and checks who started the relay. Any GitHub user can submit a Comment review on a public pull request, so it continues only for a user with an owner, member, or collaborator review on the pull request, which are the reviews that `approval-check` can count, or for a user with write access, who may have dismissed a review. A GitHub App or other bot account has no review that counts and no repository permission to look up. Neither the `workflow_run` payload nor the Actions API names the review action that started the relay, and the relay artifact is untrusted. So a bot qualifies once the pull request's issue events show that it dismissed an owner, member, or collaborator review, no later than a minute after GitHub created the relay run, to allow for clock skew between the two records. The dismissed review must still be dismissed and must come from a person. A later review or comment by the same bot then also re-runs the check. That is deliberate. Only authorized runs enter the `rerun` job's concurrency group, which is keyed by the pull request and its head, and every authorized run re-runs all of that pull request's policy runs for the current head against the current reviews. When a later authorized run replaces a pending one, no evaluation is lost. Over-authorizing costs only redundant re-runs, and only for a bot that has dismissed an eligible review on that pull request. Under-authorizing would leave a stale success, which is the failure this check exists to prevent. If the issue events cannot be read, the job fails closed: it logs a warning, skips the re-run, and succeeds, so the policy run can still be re-run by hand. GitHub supplies the user, and the reviews come from the repository API, not from the pull request. This check runs in an `authorize` job with no concurrency group. The `rerun` job runs only when `authorize` allows it, and only then joins the concurrency group for the pull request and head, so an ineligible review cannot replace an eligible re-run that is waiting there. The `rerun` job checks again that the pull request still has the reviewed head, because the head can move while it waits. Then it selects every `pull_request_target` policy run for that head. For each run, oldest first, it reads the evaluation that run recorded in its `human-approval-policy` artifact once its policy job has written it. When that record shows `peer_review` or `owner_review` mode and a required human approval, it waits for the run to finish and re-runs it.

Each re-run replaces the earlier `high-risk human approval` result inside the same run.
That matters because GitHub counts every run of a required check name on a head: a new run that passed beside an old run that failed would still block merge.
One head can have several policy runs, for example after a draft is marked ready for review, so the companion re-runs all of them rather than only the newest.
A run that lists no pull requests, such as one from a fork, counts when it comes from the reviewed head repository; a run that lists another pull request does not.
All waits share one twenty-minute budget, so the job stays inside its timeout however many runs a head has.
The check is deliberately not a commit status.
Any workflow with the repository's Actions token can write a commit status, and a ruleset cannot tell one workflow from another, so a branch workflow could mark a high-risk pull request approved.
A job of the protected-base run is harder to imitate, but it is not proof against imitation.
The pull request cannot change that job, because GitHub runs it from the base.
A required check matches on the check name, though, and every workflow in the repository reports through the same GitHub Actions app.
A `pull_request` workflow that the pull request adds could therefore report a passing job with the same name.
The real job still reports its own result for that name on the same head.
The behavior described above suggests that a failing real result keeps blocking the merge.
This project has not tested a same-name job from another workflow, so it does not rely on that.
Such a pull request changes `.github/workflows/**`, so the policy requires approval, but the imitation targets that very check.
To enforce the check by its source rather than by its name, an administrator should:

- Select **GitHub Actions** as the expected source of each required check in the branch ruleset. A commit status or another app then cannot satisfy the name, although another Actions workflow still can.
- Where the plan offers it, use a ruleset that requires the workflow itself to pass, pinned to a protected ref, instead of a required check name. GitHub then runs the workflow file from that ref.

`manual_merge` and `environment` modes do not read reviews, and a re-run in `environment` mode would discard a granted Environment approval, so the companion leaves those runs alone.
The recorded mode, not the current base's configuration, decides, because a re-run replays the original event and base; a run from before the artifact existed is not re-run automatically.
GitHub permits a re-run for 30 days after the original run; after that, push the branch again to start a new one.
A pull request with merge conflicts has no merge ref, so a review of it runs no relay and re-runs nothing; the check stays failed until the conflicts are resolved and pushed, which starts a new policy run.

This repository configures `owner_review` with `reviewer = "elanthus"`.
Thus, the approval check can report a review status for the current content.
The review is an Approve review on another author's PR, or a Comment review that contains only `approved` on your own PR.
Its current branch rules require automated CI, dependency audits, and the `high-risk human approval` check.
Code Owner review and resolved review threads are not mandatory GitHub merge requirements.
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
A merge commit passes this commit check only under two conditions.
Its second parent belongs to the protected base, and its tree exactly matches Git's clean merge of the two parents.
The head-actor check below still applies to the current head, so a clean merge that a person pushed as the head does not qualify.
The verifier does not establish who pushed an earlier clean merge below a Dependabot-set head; it verifies only that the merge adds no content beyond its parents.

Signed bot metadata shows only that GitHub created a commit, so the workflow also binds the exemption to the account that set the head.
The run's event must be a `pull_request_target` `opened` or `synchronize` event for the exact head and base.
Its sender must be the `dependabot[bot]` Bot account with GitHub's account id for it, `49699333`.
A `synchronize` event must also report that head as the result of its push.
The pull request's base branch must be the repository's default branch, both as named in the same payload.
The result is judged against the base at event time, and retargeting a pull request (an `edited` event) does not re-run the workflow.
`edited` is deliberately not a trigger: it cannot establish who set the head, so every edit, including Dependabot's own description updates, would fail the exemption.
A pull request retargeted after its run therefore keeps that run's result; the default-branch rule prevents a run against another base from carrying over to the default branch.
The workflow passes the event name and the event payload file to the verifier, which fails closed on any other shape.
A head that a person pushed, including a clean merge from **Update branch**, does not qualify.
Nor does a run from a `reopened`, `ready_for_review`, or auto-merge event, because its sender is whoever performed that action and not necessarily whoever set the head.
Such a pull request follows the ordinary policy: it needs a preflight attestation for its head, which the attestation workflow verifies, and owner approval where the approval policy requires it.
Alternatively, ask Dependabot to rebase or recreate the pull request, which restores the exemption with a new Dependabot-set head.
A re-run replays the original event payload, so a review-triggered re-run keeps the sender of the event that set the head.

Human-added commits, human-pushed heads (including a clean base merge that a person pushed as the head), pull requests into other branches, conflict resolutions, unrelated branch merges, missing records, and snapshot mismatches do not receive this exemption.
Ordinary approval policy applies to them.
The exemption therefore means that GitHub reported Dependabot set the head, and that every commit since the base is GitHub-signed bot work or a clean protected-base merge.
It is not a review of the content. See [Limits](limits.md#the-dependabot-exemption-trusts-githubs-event-sender).
Only protected-base code runs, with read-only permissions. Proposed code is inert Git data.
The hosted report records the exact head and base and the verification result.

This approval exemption does not skip attestation, tests, packaging checks, or audits.
It does not expand auto-merge eligibility.
Outside the narrow exception below, the normal attestation check must succeed before bot provenance can waive human review.
The local `approval-check` command still reports the configured path-based policy.
The repository's hosted workflow separately verifies and applies the bot exemption.

A narrower class of Dependabot updates also skips local attestation and is eligible for the auto-merge workflow.
These are same-repository PRs that the actual `dependabot[bot]` Bot account authored, with exactly one verified Dependabot commit that matches the event and the current head.
The head must have been set by the same kind of Dependabot-sent `opened` or `synchronize` event into the default branch that the approval exemption requires.
Their metadata must identify every dependency as a Python patch or minor update in the root directory.
Their modified files must be limited to `pyproject.toml` and `uv.lock`.
Unknown metadata, renames, deletions, additional commits or files, major updates, and GitHub Actions updates do not qualify for the attestation and auto-merge exception.
A grouped update qualifies only when every member qualifies.

The shared `dependabot-policy.yml` workflow checks the bot identity, the event sender, the signed commit metadata, and every grouped dependency.
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
