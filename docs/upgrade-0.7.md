# Upgrade from 0.6.0 to 0.7.0

Upgrade the CLI, copied agent skills, and hosted verifier together. Version 0.7.0
removes trusted CI test delegation and cross-run shell evidence reuse. Applicable
lint and tests finish locally before publication; hosted CI still runs separately.
Review and documentation evidence can transfer when their inputs remain equivalent.

Python 3.11–3.13, Git 2.38+, and the supported operating systems are unchanged.
Click 8.2 or newer is now required. Installation does not rewrite repository
configuration, scan saved runs, delete worktrees, or refresh copied skills.

## Rollout order

1. Inspect active runs with `agentic-preflight status --all` and preserve unmerged
   fixes and local skill customizations. Record the current CLI version and hosted
   verifier pin. For versions before 0.6.0, first read the 0.6.0 breaking changes in
   [the changelog](../CHANGELOG.md#060---2026-09-13); 0.7.0 does not restore older schemas.
2. Prepare the trusted verifier on the protected base before publishing 0.7.0 evidence.
   For a package-based workflow, pin `agentic-preflight==0.7.0` only after it is on PyPI.
   For protected-base source installs, merge the reviewed source update first.
   While preparing the release, test the producer and verifier from the same immutable
   proposed commit; do not execute PR code with policy credentials. This repository's
   preparation PR uses the current main source verifier, which already implements
   the 0.7.0 behavior. Its metadata still says 0.6.0; it is not the published 0.6.0 build.
3. Remove retired configuration from both `.agentic-preflight.toml` and
   `~/.config/agentic-preflight/config.toml`, using the table below. Commit repository
   configuration changes before starting a new run. Retain valid lint/test commands;
   check that local execution has the dependencies and ignored inputs it needs.
4. Once 0.7.0 is published, upgrade the tool and its installed skill copies:

   ```bash
   uv tool upgrade agentic-preflight
   agentic-preflight --version
   agentic-preflight integrations status
   agentic-preflight integrations update
   agentic-preflight integrations status
   ```

   Expect version 0.7.0 and `current` for each managed installed integration. For a
   reviewed source checkout, rerun `./install.sh` (Windows: `.\install.ps1`). Project
   copies need `integrations update --scope project`; custom targets need `--target PATH`.
   Preserve modified or unmanaged bundles before any intentional `--force` replacement.
   Restart running agents so they read the refreshed instructions.
5. In each source worktree, inspect `status`, follow its returned command, and start
   fresh local validation when required. Fetch the exact head, notes, and retained
   evidence refs before offline verification; see [CI setup](attestations-and-ci.md).
   Run an ordinary branch through review, docs, lint, tests, and hosted checks before
   expanding a team pilot. Confirm that missing evidence fails the required hosted
   check and that GitHub actually requires that check before merge.

## Configuration and automation changes

| 0.6.0 setting or interface | 0.7.0 action |
| --- | --- |
| `[ci]` test authority and delegation | Remove the whole table. Loading files ignores it with a warning; it grants no authority. Install local test dependencies. The `ci` commands and pending-test publication are removed. |
| `[reuse]`, `[reuse.lint]`, `[reuse.test]` | Remove the whole section and its input declarations. Files warn and ignore it. Applicable lint/test stages always execute in new runs, including identical trees. |
| `[hook] enabled` | Delete the key. It never controlled the hook and now fails configuration-file loading. Keep `allow_force_push` only if needed by existing policy. |
| `[review] require_fix_commits`, `[worktree] ttl_hours` | Already removed in 0.6.0. They remain invalid in files, but known historical snapshot keys are tolerated. |
| `hosted-check` | Replace it with trusted-source Git fetches followed by offline `verify SHA` or `approval-check SHA --base BASE`; see the protected-base workflow. |
| Malformed CLI invocation | Expect exit 6 and a `usage_error` JSON envelope instead of exit 2 and Click usage text. `hook-check` keeps Click's usage behavior. Bare invocation prints help and exits 0. |
| Documentation inventory `exists`; command candidate `trust` | Use `path`, `size`, `touched_by_diff`, and candidate `source`. Workflow `run:` lines are no longer command candidates; configured commands and supported manifests remain. |
| `stage.max_restarts`; `approval.mode = "owner_review"` and `approval.reviewer` | New settings; published 0.6.0 rejects them. Upgrade protected consumers before enabling them. |

New `[stage] max_restarts` defaults to 5 and bounds returns to review across the run,
separately from each stage's `max_attempts`. At the limit, only a person can decide to
abort and start again; an upgrade does not reset that stop.

## Existing runs, notes, and worktrees

Run schema 2, attestation schema 7, and fingerprint version 2 remain in use. Readers
ignore known removed configuration keys in historical snapshots without rewriting
snapshots or digest bindings. Unknown keys and invalid current values remain errors.
Fully verified 0.6.0 notes with original shell execution evidence remain readable.
Notes containing refreshed lint/test evidence require a fresh local run even when
they describe identical trees. Pending-test notes, delegated stages, and non-null
delegation declarations are rejected. Null retired attestation fields are discarded.
Schema identity does not imply compatibility with an older verifier.

Pending delegated runs become `ORPHANED`; start a fresh local run. Pushed and terminal
records remain available for inspection and cleanup. An interrupted nonterminal run
with `pending-update.json` cannot resume validation: the next `start` in its recorded
source worktree orphans it, clears ownership, deletes only that journal, and starts
fresh, unless the restart limit requires a person's decision. Audit history,
validation worktrees, and fix commits remain preserved. Completed history and runs
without a journal are unchanged by that retirement path.

An active pre-upgrade run with shell evidence may reopen review once because retired
capture fields no longer match current fingerprints. Follow the returned sequence.
An interrupted merge-back whose source has not reached the verified tree requires
fresh validation; the old `mergeback_attempt.rebased_tree` field is accepted and ignored.
Unreadable or invalid saved records retain their ownership and resources, even with
`gc --force`. Inspect them rather than deleting pointers or editing evidence.

New isolated worktrees default to the user cache instead of the sibling
`.agentic-preflight-worktrees/` directory. Existing runs and explicitly configured roots
keep their locations. Ordinary `gc` can reclaim eligible unused runners in either
location while preserving dirty, locked, leased, unreadable, or unmerged work.
Run directories and audit logs remain. Inspect `data.legacy_worktrees` before manually
removing any remaining registration. In-place mode remains the default.

## Approval and GitHub settings

Owner review is opt-in for installed projects. On another author's PR the configured
human submits an Approve review; on their own PR they submit a Comment review whose
trimmed body is exactly `approved`. An ordinary conversation comment does not count.
Peer and owner approval may carry through a clean base merge or clean rebase only when
the trusted check proves equivalent content and reports `approval_carried: true`.
Read `approved_head` for the counted review commit. Content changes require fresh review;
missing original Git objects prevent carryover. Preserve referenced evidence refs.

A configuration file and CODEOWNERS do not enable GitHub branch protection. Administrators
must configure required checks, required reviews, Environments, and bypass access.
See the [administrator checklist and settings snapshot](attestations-and-ci.md#administrator-setup-and-this-repositorys-settings).
This repository's contributor approval rules still apply even when GitHub does not
require the hosted human-approval check.

## Rollback limits

Before producing new evidence, retain the previous reviewed configuration and skill
copies if rollback is needed. After 0.7.0 writes snapshots with `max_restarts` or
`approval.reviewer`, or uses owner review, the strict 0.6.0 readers can reject them.
Do not assume downgrading the executable will resume those runs. Preserve history and
work, restore compatible configuration and skills, align the verifier, and start a
fresh run with the selected version. Never rewrite attestation JSON or digests to make
it pass an older verifier. Prefer a forward fix once new runs or notes have been written.
