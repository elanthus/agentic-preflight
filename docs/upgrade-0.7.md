# Upgrade from 0.6.0 to 0.7.0

Upgrade the CLI, the copied agent skills, and the hosted verifier together.
Version 0.7.0 removes trusted CI test delegation and cross-run shell evidence reuse.
Applicable lint and tests finish locally before publication. Hosted CI still runs separately.
Review and documentation evidence can transfer when their inputs stay equivalent.

Python 3.11 through 3.13, Git 2.38 or newer, and the supported operating systems are unchanged.
Click 8.2 or newer is now required.
Installation does not rewrite repository configuration, scan saved runs, delete worktrees, or refresh copied skills.

## Rollout order

1. Inspect the active runs with `agentic-preflight status --all`.
   Preserve unmerged fixes and local skill customizations.
   Record the current CLI version and the hosted verifier pin.
   For versions before 0.6.0, first read the 0.6.0 breaking changes in [the changelog](../CHANGELOG.md#060---2026-09-13).
   Version 0.7.0 does not restore older schemas.
2. Prepare the trusted verifier on the protected base before you publish 0.7.0 evidence.
   For a package-based workflow, pin `agentic-preflight==0.7.0` only after it is on PyPI.
   For protected-base source installs, merge the reviewed source update first.
   While you prepare the release, test the producer and the verifier from the same immutable proposed commit.
   Do not execute PR code with policy credentials.
   For this repository's preparation PR, protected base `48b5b60` already implemented the 0.7.0 verification behavior, although its metadata said 0.6.0.
   That source build differs from published 0.6.0.
3. Remove retired configuration from `.agentic-preflight.toml` and from `~/.config/agentic-preflight/config.toml`.
   Use the table below.
   Commit repository configuration changes before you start a new run.
   Keep valid lint and test commands.
   Check that local execution has the dependencies and ignored inputs that it needs.
4. After 0.7.0 is published, upgrade the tool and its installed skill copies:

   ```bash
   uv tool install --force 'agentic-preflight==0.7.0'
   agentic-preflight --version
   agentic-preflight integrations status
   agentic-preflight integrations update
   agentic-preflight integrations status
   ```

   Expect version 0.7.0 and `current` for each managed installed integration.
   For a reviewed source checkout, run `./install.sh` again. On Windows, run `.\install.ps1`.
   Project copies need `integrations update --scope project`. Custom targets need `--target PATH`.
   Preserve modified or unmanaged bundles before an intentional `--force` replacement.
   Restart running agents so that they read the refreshed instructions.
5. In each source worktree, inspect `status` and follow its returned command.
   Start fresh local validation when required.
   Fetch the exact head, the notes, and the retained evidence refs before offline verification. See [CI setup](attestations-and-ci.md).
   Run an ordinary branch through review, docs, lint, tests, and hosted checks before you expand a team pilot.
   Confirm that missing evidence fails the required hosted check.
   Confirm that GitHub requires that check before merge.

## Configuration and automation changes

| 0.6.0 setting or interface | 0.7.0 action |
| --- | --- |
| `[ci]` test authority and delegation | Remove the whole table. File loading ignores it with a warning. It grants no authority. Install local test dependencies. The `ci` commands and pending-test publication are removed. |
| `[reuse]`, `[reuse.lint]`, `[reuse.test]` | Remove the whole section and its input declarations. Files warn and ignore it. Applicable lint and test stages always execute in new runs, including on identical trees. |
| `[hook] enabled` | Delete the key. It never controlled the hook. It now fails configuration-file loading. Keep `allow_force_push` only if existing policy needs it. |
| `[review] require_fix_commits`, `[worktree] ttl_hours` | Already removed in 0.6.0. They stay invalid in files. Known historical snapshot keys are tolerated. |
| `hosted-check` | Replace it with trusted-source Git fetches followed by offline `verify SHA` or `approval-check SHA --base BASE`. See the protected-base workflow. |
| Malformed CLI invocation | Expect exit 6 and a `usage_error` JSON envelope instead of exit 2 and Click usage text. `hook-check` keeps Click's usage behavior. A bare invocation prints help and exits 0. |
| Documentation inventory `exists`; command candidate `trust` | Use `path`, `size`, `touched_by_diff`, and candidate `source`. Workflow `run:` lines are no longer command candidates. Configured commands and supported manifests remain. |
| `stage.max_restarts`; `approval.mode = "owner_review"` and `approval.reviewer` | New settings. Published 0.6.0 rejects them. Upgrade protected consumers before you enable them. |

The new `[stage] max_restarts` setting defaults to 5.
It bounds the returns to review across the run, separately from each stage's `max_attempts`.
At the limit, only a person can decide to abort and start again. An upgrade does not reset that stop.

## Existing runs, notes, and worktrees

Run schema 2, attestation schema 7, and fingerprint version 2 stay in use.
Readers ignore known removed configuration keys in historical snapshots. They do not rewrite snapshots or digest bindings.
Unknown keys and invalid current values stay errors.
Fully verified 0.6.0 notes with original shell execution evidence stay readable.
Notes that contain refreshed lint or test evidence require a fresh local run, even when they describe identical trees.
Pending-test notes, delegated stages, and non-null delegation declarations are rejected.
Null retired attestation fields are discarded.
Schema identity does not imply compatibility with an older verifier.

Pending delegated runs become `ORPHANED`. Start a fresh local run.
Pushed and terminal records stay available for inspection and cleanup.
An interrupted nonterminal run with `pending-update.json` cannot resume validation.
The next `start` in its recorded source worktree orphans it, clears ownership, deletes only that journal, and starts fresh.
If the restart limit requires a person's decision, `start` does not do this.
Audit history, validation worktrees, and fix commits stay preserved.
Completed history and runs without a journal are unchanged by that retirement path.

An active pre-upgrade run with shell evidence can reopen review once.
The retired capture fields no longer match the current fingerprints.
Follow the returned sequence.
An interrupted merge-back whose source has not reached the verified tree requires fresh validation.
The old `mergeback_attempt.rebased_tree` field is accepted and ignored.
Unreadable or invalid saved records keep their ownership and resources, even with `gc --force`.
Inspect them. Do not delete pointers or edit evidence.

New isolated worktrees default to the user cache instead of the sibling `.agentic-preflight-worktrees/` directory.
Existing runs and explicitly configured roots keep their locations.
Ordinary `gc` can reclaim eligible unused runners in either location.
It preserves dirty, locked, leased, unreadable, or unmerged work.
Run directories and audit logs remain.
Inspect `data.legacy_worktrees` before you remove a remaining registration manually.
In-place mode stays the default.

## Approval and GitHub settings

Owner review is opt-in for installed projects.
On another author's PR, the configured human submits an Approve review.
On their own PR, they submit a Comment review whose trimmed body is exactly `approved`.
An ordinary conversation comment does not count.
Peer and owner approval can carry through a clean base merge or a clean rebase.
This happens only when the trusted check proves equivalent content and reports `approval_carried: true`.
Read `approved_head` for the counted review commit.
Content changes require fresh review. Missing original Git objects prevent carryover.
Preserve referenced evidence refs.

A configuration file and CODEOWNERS do not enable GitHub branch protection.
Administrators must configure required checks, required reviews, Environments, and bypass access.
See the [administrator checklist and settings snapshot](attestations-and-ci.md#administrator-setup-and-this-repositorys-settings).
This repository's contributor approval rules still apply when GitHub does not require the hosted human-approval check.

## Rollback limits

Before you produce new evidence, keep the previous reviewed configuration and skill copies in case rollback is needed.
After 0.7.0 writes snapshots with `max_restarts` or `approval.reviewer`, or uses owner review, the strict 0.6.0 readers can reject them.
Do not assume that a downgrade of the executable resumes those runs.
Preserve history and work. Restore compatible configuration and skills. Align the verifier.
Then start a fresh run with the selected version.
Never rewrite attestation JSON or digests to make them pass an older verifier.
After new runs or notes exist, prefer a forward fix.
