# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, 3.13, and 3.14 on macOS 15 or newer, on Linux, and on Windows 10 or newer.
Git 2.38 or newer is required.
Repositories that use the SHA-256 object format are not supported.
Run records and attestation notes hold only SHA-1 object IDs, so a run in such a repository fails before it is green, and no commit there can have a valid attestation.
The pre-push hook blocks a branch push in such a repository with a message that says so.

## Python versions

The package declares `requires-python = ">=3.11,<3.15"`.
The upper bound is the first Python minor version that CI does not test.
The tool runs inside your pre-push hook, so a crash on an untested interpreter would block your push.
The cap keeps installers off such an interpreter.
`pip` and `pipx` refuse to install the package on a Python version outside the range.
`uv tool install` picks an interpreter inside the range unless you name one with `--python`.
A version that you name outside the range is not supported, even when the installation succeeds.

A new Python minor version becomes supported in one change.
That change adds it to the CI matrix, fixes what fails, adds its classifier, and raises the cap to the next minor version.
`tests/test_python_support.py` keeps the classifiers, the cap, this policy, and the CI matrix in step.

Windows support is native.
It does not use WSL. It does not require a POSIX shell for ordinary use.
Know these three Windows-specific points before you adopt it:

- **A stage command that contains shell grammar needs Git Bash.**
  Commands execute directly as a program and its arguments where possible.
  Thus, `pytest`, `ruff check .`, and `npm run test` need no shell.
  A command that uses pipes, `&&`, redirection, or globs falls back to a shell.
  On Windows, that shell is the one that Git for Windows installs.
  The tool finds it through the Git installation, not through `PATH`.
  `bash.exe` on `PATH` is normally the WSL launcher, which would run the command against a different filesystem.
- **Symlink-related behavior requires Developer Mode.**
  Symlink creation is a privileged operation on Windows by default.
  This affects repositories that contain symlinks. Nothing else in the tool creates one.
- **State directories inherit their parent's access list.**
  On macOS and Linux the run store and the telemetry directory are owner-only (`0700`).
  An existing directory that another account owns keeps its permissions.
  On Windows the tool does not change their access list, so they get whatever the folder above them allows.
  A repository under your user profile (`C:\Users\<you>`) is private that way.
  A repository under a folder such as `C:\dev` usually inherits read access for every signed-in user, and so do run records, stage logs, and telemetry rows.
  Keep such a repository under your profile, or restrict the folder yourself.
  Files that `copy_files` copies are owner-only on every platform.

## Stage commands and your shell profile

This behavior is not Windows-specific.
It is the one behavioral difference that you must understand on every platform.

A stage command executes directly as a program and its arguments when it has no shell grammar.
A directly executed command does not source your login shell profile.
This matters when a version manager puts its shims on `PATH` from that profile and not from your environment.
Examples are `nvm`, `pyenv`, `rbenv`, `mise`, and `asdf`.
A stage can then run the system build of a program instead of the build that the version manager would select.
Nothing reports the difference.

A command whose program cannot be found without the profile is not affected.
Resolution fails, the command falls back to a shell, and the shell sources the profile as before.
The gap exists only for a program that exists in both places.

If a stage depends on one interpreter, select it in the command:

```toml
[commands]
test = "uv run pytest"
```

This is good practice in all cases.
A hosted CI job does not source your profile either.
A command that relies on the profile already behaves differently in CI.

Program resolution uses `PATH` only.
A bare command name is never resolved in a working directory.
Thus, a repository cannot supply the program that validates it.

## Windows cancellation

In 0.7.0, Ctrl+C during a stage or setup command cleans up the Windows process tree and propagates the interrupt.
The CLI creates a separate process group, so the child does not receive the console Ctrl+C directly.
Cleanup uses the existing bounded `taskkill` helper.
POSIX signal handling is unchanged.

## Repository shapes

[Repository shapes](docs/repository-shapes.md) gives the details and the tests behind each statement here.

These shapes are supported:

- Full clones, linked worktrees, and linked worktrees of a bare repository. Commands such as `status`, `init`, and `start` do not run in a bare directory itself.
- Shallow clones whose history contains the merge base of the branch and the base.
- Blobless partial clones (`--filter=blob:none`) whose promisor remote is reachable during a run.
- Submodules, reviewed as pointer changes. Submodule contents are not reviewed.
- Git LFS files, reviewed as pointer files. The LFS pre-push hook must be combined with `agentic-preflight hook-check`, which runs first.

Sparse checkouts are supported for review and documentation. The docs surface uses all matching committed blobs. Lint and test still see only the files in the sparse set.

Repositories that use the SHA-256 object format are not supported.
`start` and `init` refuse them with exit 3 and `unsupported_object_format`, before
creating a run or writing configuration and hooks.
Run records and attestation notes hold only SHA-1 object IDs, so no commit in such a repository can have a valid attestation.
See [issue #394](https://github.com/elanthus/agentic-preflight/issues/394).

## Evaluation report compatibility

The eval report method is now `public-smoke-v6`.
It adds measured runtime, findings, state-history timing, and CLI token accounting.
Full reports are no longer byte-identical across runs.
Consumers must preserve `null` exact totals.
They must distinguish partial observations from complete usage.
Run schema 2 is unchanged.
New `state_transition` audit events do not add fields to `run.json`.
Older run histories stay readable without backfilled timing.

## Opt-in contract-check evidence

`[review.contracts]` adds an optional phase requiring a locally available, trusted,
digest-pinned Python 3 image on a Linux Docker engine (including Docker Desktop).
Ordinary review does not require Docker. The default disabled configuration and
coverage wire shape remain unchanged. The worked wrappers support both planning
and final review; update the shared helper together with either wrapper.

Plans and receipts use schema `"2"` in private run artifacts. Checked units require a
behavioral inventory with input-class probe references or explicit gaps. Checks require
structured oracle provenance. Update the worked wrappers and their shared helper
together with the product. No release wrote a schema-1 receipt, so none is read: a
receipt in that shape is reported as unreadable and left in place.

Final context adds inventory, oracle provenance, and probe code within its summary
budget. Check details are ordered failed first, and long output streams are shortened
while the receipt keeps them whole. Omitted inventory and check details have separate counts; `handoff_complete`
reports whether both are present. Read the immutable full receipt for omitted data.
These are declared coverage claims, not proof that every input class was explored.

Accepted review coverage
includes optional `contracts_sha256`. Current readers accept older coverage without
that field. Older readers may refuse enabled configuration or new coverage fields;
preserve their records and use a compatible tool instead of deleting run history.
Execution evidence does not imply that the final reviewer or its oracle was correct.

The planning context's `contract_checks` object adds `execution`: the pinned `image`, the
execution limits, and `installed_packages`, a code-owned listing of the distributions
installed in the image or an explicit `unavailable` entry with a reason. The listing is a
private run artifact under `contracts/images/`, keyed by image digest. The listing adds
no `[review.contracts]` configuration key, no receipt field, and no change to the
configuration digest that existing receipts bind. It states availability only; it does
not assert which consumer contracts the repository supports.

## Validation tiers

The supported combinations receive different validation frequencies, so pull-request feedback stays fast:

- Pull requests and pushes to `main` run on `ubuntu-latest` and `macos-latest` with Python 3.11, 3.13, and 3.14. Python 3.11 and Python 3.14 are the oldest and newest supported versions. Python 3.13 stays because the `main` ruleset requires its test check; dropping it needs a matching ruleset change. This conserves GitHub Actions minutes while catching version-boundary and macOS regressions before merge.
- A scheduled regression run covers the oldest supported boundary, macOS 15 with Python 3.11, every Monday and Thursday.
- Manual CI runs and release tags cover Python 3.11, 3.12, 3.13, and 3.14 on `ubuntu-latest`, `macos-latest`, and `windows-latest`.

A platform is supported even when it is not in the pull-request job.
A failure that is specific to a supported combination is a release blocker.
Fix it with the same priority as a pull-request CI failure.

Other POSIX systems can work.
They are best effort until they are added to the supported matrix.
Python versions outside the declared range are not supported; see [Python versions](#python-versions).

## Upgrading from 0.6.0 to 0.7.0

Follow the [upgrade guide](docs/upgrade-0.7.md) for the rollout order, configuration cleanup, saved-run inspection, verifier alignment, and rollback limits.
Run schema 2, attestation schema 7, and fingerprint version 2 are retained.
The schema number alone does not guarantee that an older verifier accepts new policy fields or evidence.

## Attestation note content without user-level configuration

Attestation schema 7 and fingerprint version 2 are retained, and current verifiers read notes from earlier releases unchanged.
A note's `config_snapshot` now leaves out the sections that came from the user configuration file, except `[diff]`.
A command that depends on such a section is recorded as `[redacted user-level command]`.
The configuration digest and the review, docs, and shell fingerprint digests cover that published form, so a verifier that recomputes them from the note still agrees.
Evidence reuse refuses a candidate whose snapshot holds a section the current run withholds, or whose findings the current copied-file values would redact.
It also refuses evidence from a record without `user_config_sections`, such as one an earlier release wrote, unless its snapshot equals the current published snapshot.
Expect one fresh review and docs stage after upgrading if you have user-level sections.
A run with no user-level sections publishes the same snapshot and digests as before.
A consumer that read a user-level setting from a note must read it locally instead. A section missing from the snapshot was set in the user file.

Finding titles, details, suggestions, and response notes are redacted for copied-file values before they are saved, so `findings.json` and the note carry the redacted text.

Every run record that this release starts carries `user_config_sections`, an empty list when no section came from the user file.
A record without the field was started by an earlier release, and its user-level sections cannot be identified.
`mergeback`, `gate`, and `push` refuse such a run with exit 4, `needs_human`, and `data.reason` `unknown_config_provenance`.
`mergeback` refuses before it changes the source branch or writes a note.
`context`, `review contracts`, `review run`, `submit-findings`, `respond`, `verify`, and `stage run` refuse such a run the same way, so no review or stage runs for a run that cannot publish.
`status` reports the run without checking or advancing its evidence and returns `agentic-preflight abort` as `next.command`. `status`, `events`, `logs`, `abort`, and `gc` keep working on it.
At the validation restart limit, neither `status` nor a refused command advertises `abort`: the decision to abort such a run stays with the user.
A run that an earlier release already pushed publishes nothing more, so `finish` still completes it.
A run that an earlier release already merged back has a note that may hold user-level configuration; `gate` and `push` refuse it, and the error names the `git notes` command that removes that note.
Below that limit, abort the run and start a fresh one after upgrading. In isolated modes, cherry-pick any recorded fix commits (`data.fix_commits`) first, because `abort --force` discards them.
The field makes every new run record unreadable to earlier releases, which forbid unknown keys.
Before downgrading, finish or abort the runs that this release started. A record that an earlier release wrote stays without the field and stays readable to that release.

## Compatibility changes

`mergeback` has a new error code, `fix_commits_unmerged`, with exit 4.
A conflict retry returns it when the source branch moved but lacks a recorded fix commit, instead of resetting the validation branch and clearing `fix_commits`.
Its `data` holds `missing_fix_commits`, `fix_commits`, `post_sha`, and `worktree_path`, and the run returns to `MERGEBACK_CONFLICT`.
The run record and attestation schemas are unchanged.

Agentic Preflight 0.7.0 requires Click 8.2 or newer.
Documentation inventory entries no longer contain `exists`.
Detected command candidates no longer contain `trust`.
Consumers must use `path`, `size`, `touched_by_diff`, and candidate `source`.
Command candidates come from supported project manifests. Workflow run lines are no longer detected.
Attestation fingerprints are unchanged.

`approval-check` results add `approved_head` and `approval_carried`.
`approved_head` is the commit that the counted review was submitted on, or `null`.
`approval_carried` is a boolean.
`human_approvers` is unchanged.
In `peer_review` and `owner_review` modes, an approval of an earlier head with unchanged branch content now satisfies the policy.
A consumer that compared review commits with `head_sha` itself must read `approved_head` instead.
The attestation schema is unchanged.

The command-line interface, the configuration file, and the attestation schema follow Semantic Versioning.
The Python modules are internal. They have no compatibility promise.
While the version is 0.x, a minor release can include breaking changes. `CHANGELOG.md` identifies each one.

Exit code 6 is new in 0.7.0. It means a command-line usage error.
Earlier releases exited 2 for usage errors, the same code as a failed stage, and printed nothing on stdout.
A wrapper that treated exit 2 from a malformed invocation as a stage failure must now expect 6 and a `usage_error` envelope.
`hook-check` still exits 2 on a usage error.

Agentic Preflight 0.6.0 does not read run records or attestation notes that earlier releases wrote.
Unsupported saved data is reported as an error.
It is left unchanged, together with the work and ownership information that it refers to.

Run reads and attestation verification ignore configuration keys that a later release removed, such as `[hook] enabled`, when they appear in a saved `config_snapshot`.
Thus, a newer verifier accepts notes that an older release wrote.
A configuration file that sets such a key is still rejected, except by `telemetry rebuild`, `telemetry sync-hosted`, and `report`, which ignore it with a warning because they only read derived data.

## Owner review approval

`[approval] mode = "owner_review"` and its required `reviewer` GitHub login are new opt-in configuration.
The existing `manual_merge`, `environment`, and `peer_review` behavior and attestation schemas are unchanged.
Older versions cannot load the new mode.
Upgrade the trusted protected-base consumer before you enable it.
Policy changes are evaluated under the old protected-base mode until they merge.

The new `reviewer` field defaults to an empty string in configuration snapshots.
Old snapshots stay readable by this version.
Snapshots that this version writes include that new field. Older strict configuration readers can reject them.

Isolated worktrees now default to the user cache instead of the sibling `.agentic-preflight-worktrees` directory.
The cache is `$XDG_CACHE_HOME` when it is absolute, and otherwise `~/.cache`.
Existing configured roots stay supported.
New runs use the new default.
`gc` can reclaim eligible unused reusable runners at either location.
It preserves dirty, locked, leased, or retained work and reports the remaining legacy registrations.

## Saved merge-back attempts

Saved schema-v2 runs from 0.6.0 can contain the former `mergeback_attempt.rebased_tree` field.
Versions 0.7.0 through 0.8.1 accept and ignore it on load, and omit it on the next write.
Version 0.9.0 rejects it like any other unknown attempt field; see [Run records from before 0.7.0 are no longer read](#run-records-from-before-070-are-no-longer-read).
A pending source rebase that has not reached the recorded verified tree now becomes stale.
Restart preflight to validate the current source. Do not resume from that intermediate tree.

## Grounding and command candidates

Context grounding no longer emits generated `codeowners` entries.
Other grounding sources and GitHub ownership policy are unchanged.
Command detection no longer scrapes CI workflow `run:` lines.
Configured commands and manifest candidates stay supported.

## Removal of trusted CI test authority

The retired `[ci]` table is ignored when repository or user configuration loads.
A warning says that tests now run locally.
Historical attested configuration snapshots also ignore that table. They keep their original digest bindings.

Attestation schema 7 is retained.
Earlier `verified` notes can include null `publication_ready_at` and `test_delegation` fields. Readers discard those null fields.
Non-null declarations, `tests_pending` outcomes, and `delegated` attested stages are rejected for local and publication verification.
New notes contain only the `verified` outcome and complete local evidence.

In 0.7.0 through 0.8.1, historical run records discard the retired delegation field.
Pending delegated runs become `ORPHANED` and require a new local validation run.
Pushed and terminal records stay readable for inspection and cleanup.
Run history is preserved.
Version 0.9.0 rejects a run record that carries the delegation field or a delegated state; see [Run records from before 0.7.0 are no longer read](#run-records-from-before-070-are-no-longer-read).

## Saved pending run updates

Agentic Preflight 0.7.0 removes the local run and findings write-ahead journal.
An interrupted nonterminal run with `pending-update.json` cannot resume validation.
This section describes 0.7.0 through 0.8.1. Version 0.9.0 refuses such a run instead; see [Run records from before 0.7.0 are no longer read](#run-records-from-before-070-are-no-longer-read).
`status` directs you to `start`. Other validation commands refuse the run.

On the next `start` in that run's source worktree, the tool orphans the interrupted run.
It clears the run's ownership pointers, deletes only its pending journal, and creates a fresh run.
The exception is a run that has already reached the validation restart limit.
At that limit, the existing human stop remains. Only the user can decide to abort and restart.
This happens when validation starts, not when the package is installed.
The old audit record, the validation worktree, and any fix commits stay available for normal safe cleanup with `gc`.
The upgrade deletes no user work.
Completed history and nonterminal runs without a pending journal are unchanged.
The existing saved-record policy still protects unreadable run records.

Individual record replacements stay atomic.
Findings and run state are no longer recovered together after an interruption.

## Dependabot hosted exception

The repository workflows now share a narrowly scoped Dependabot eligibility check.
Eligible Python patch and minor updates skip local-attestation verification and satisfy the hosted approval gate.
The existing required check names stay available.
Other PRs keep their prior gates.
See `docs/attestations-and-ci.md`.

The Dependabot approval exemption and the auto-merge and attestation exception now also require that the `dependabot[bot]` account (login, Bot type, and account id `49699333`) set the current head through a `pull_request_target` `opened` or `synchronize` event on a pull request into the default branch.
A head that a person pushed, including a clean **Update branch** merge, and runs from `reopened`, `ready_for_review`, or auto-merge events no longer qualify.
Such a pull request follows the ordinary policy: it needs a preflight attestation for its head, which the attestation workflow verifies, and owner approval where the approval policy requires it.
Alternatively, `@dependabot rebase` or `@dependabot recreate` restores eligibility with a new Dependabot-set head.
`.github/scripts/verify_dependabot.py` requires the new `--event-name` and `--event-file` arguments.

Run read diagnostics now report `unreadable` for encoding or I/O failures and `invalid` for JSON, schema, or configuration failures.
They do not list field locations.
Unsupported records stay preserved.
Removed configuration keys are ignored on reads without rewritten snapshots.
Unknown keys and invalid current settings are still rejected.
The full snapshot content keys the bounded validation cache, so an edit cannot reuse the validation of a different snapshot.

## Hosted verification command removal

The `hosted-check` command was removed.
Hosted callers fetch the exact head, the notes, and the evidence refs from the contributor remote.
Then they call the offline `verify` or `approval-check` command.
Per-attempt availability diagnostics are no longer emitted.

## Retired shell input reuse declarations

`[reuse]`, including the lint and test input contracts, is retired. It is ignored with a configuration warning.
Applicable lint and test stages run again in every new run, even with identical trees.
The protected verifier rejects refreshed shell evidence.
It preserves the original execution and run identity and the bound base and head trees.
Same-run tree-equivalent merge-back publication is still supported.
Review and documentation refresh stay available.

Original same-commit notes and historical raw snapshots stay readable. Their digests are not rewritten.
Fingerprint version 2 and attestation schema 7 keep their existing wire fields and shell policy digest encoding.
The typed fingerprint models and their remaining safety bindings stay supported.

An active pre-upgrade run with completed shell evidence can reopen review once.
The retired capture fields no longer match fresh execution fingerprints.
Follow the returned stage sequence. The existing restart limit still applies.
Frozen policy stays authoritative for historical runs.
Committed protected policies can contain retired sections without blocking approval checks.

## Upgrading from 0.7.0 to 0.8.0

Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
Notes that 0.7.0 wrote stay verifiable, and run records that 0.7.0 wrote stay readable.
0.8.0 removes no configuration keys. The compatibility requirements for mixed versions are below.

0.7.0 rejects the new configuration keys (`[telemetry]`, including `hook_log`, and `[review.contracts]`) and the new run record keys (`producer`, `started_at`, `response_reason`).
Upgrade every machine that shares a clone before you enable the new configuration, and use 0.8.0 or later to inspect or clean up runs that 0.8.0 touched.
Pin the CI verifier to 0.8.0 as well; see [Attestations and CI](docs/attestations-and-ci.md).

The sections from here to [Upgrading from 0.8.1 to 0.9.0](#upgrading-from-081-to-090) describe the 0.8.0 changes, with two exceptions.
[Run record retention](#run-record-retention) is new in 0.9.0, and [Repositories without a network remote and telemetry](#repositories-without-a-network-remote-and-telemetry) describes a 0.8.1 change.
[Evaluation report compatibility](#evaluation-report-compatibility) and [Opt-in contract-check evidence](#opt-in-contract-check-evidence) above are also new in 0.8.0.

## Producer identity on run records

New schema-v2 run records carry an optional `producer` object.
It records the tool version, the Python version, the platform, the coding-agent client, and the model that the agent reported.
Records that earlier versions wrote load with `producer` absent, and a rewrite of such a record stores `producer` as `null`.
Run schema 2, attestation schema 7, fingerprints, and configuration digests are unchanged. None of them contain the producer.
Since [attestation schema 8](#attestation-schema-8), the note publishes the producer as `producer`. Fingerprints, evidence digests, and configuration digests still do not contain it.

Run models forbid unknown keys.
Thus, 0.7.0 reports a record that carries `producer`, including a rewritten older record, as unreadable.
This is the normal downgrade position. Do not delete the run history to recover. Use 0.8.0 or later to inspect or clean up those runs.

## Run summaries and the local telemetry mirror

Finished runs now write `summary.json` in the run directory and append one line to the user-level `runs.jsonl` mirror.
The summary is a new stored schema: `kind = "agentic-preflight-run-summary"`, `schema_version = 1`.
A reader must check both values and reject other versions.
Absent values are `null`. A reader must not count them as zero.
See [Local run telemetry](docs/local-telemetry.md) for the fields.

`summary.json` is derived data. It never binds evidence.
No fingerprint, evidence digest, configuration digest, or attestation contains a summary field.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

`[telemetry]` is a new configuration section.
Older tool versions reject a configuration file that contains it, as an unknown section.
Put it only in the user file of a machine that runs this version or a later one.
Run snapshots never contain the section, so it changes no attestation and no configuration digest.
Whether an older version reads a run record that this version writes is decided by the run-record sections above, not by this section.

`agentic-preflight telemetry rebuild` reads run schema 1 and 2 records without changing them.
It writes only `summary.json` files and the mirror.

The run event log can contain the new `summary_written` and `summary_failed` events.
The `aborted` event is now written together with the terminal transition. It carries the transition's `seq`.
`finish`, `abort`, and `gc` envelopes add `data.telemetry`.

## Run record retention

`[retention]` is a new configuration section with one key, `run_days`.
Older tool versions reject a configuration file that contains it, as an unknown section.
Run snapshots never contain the section, so it changes no attestation and no configuration digest.
`gc` now deletes reclaimed run directories older than `run_days` (default 90).
A reader that expects every finished run's directory to remain, such as `telemetry rebuild`, sees only the runs that `gc` kept.
The `gc` envelope adds `data.pruned`.

## Reasons and timing on run records and summaries

A stored finding can carry the new optional `response_reason`. A finding without a reason has no such key.
A stage record carries `started_at`, which is `null` for a stage that never began and in a rewritten older record.
Run models forbid unknown keys.
Thus, an earlier tool version cannot read a findings file that carries `response_reason`, or a run record written by this version, and it reports such a run record as unreadable.
Do not delete the run history to recover. Use this version or a later one to inspect or clean up those runs.

Stage evidence and attestations never contain `response_reason` or `started_at`.
This version removes the reason from each finding before it archives stage evidence, and it rejects evidence that contains one.
Thus, earlier verifiers, including a protected-base CI verifier, still read the notes that this version writes.
Evidence digests of earlier findings are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

`respond` and `abort` accept the new optional `--reason`. Existing invocations are unchanged.
The `finding_resolved` event adds `reason`. The `aborted` event adds `reason` and `aborted_from_state`.

Run summaries add `findings.by_response_reason`, `terminal.abort_reason`, `terminal.aborted_from_state`,
`stages.<name>.started_at`, `stages.<name>.duration_seconds`, and the optional top-level `timing`.
The summary keeps `schema_version = 1`. 0.8.0 is the first release that writes summaries, and `telemetry rebuild` regenerates the summary and mirror row of every readable terminal run.
It skips runs that are not terminal and leaves the existing summary and mirror row of an unreadable record in place.
A summary row without the new keys is still valid. The report counts the missing values as unknown.

## Hosted outcome records

`agentic-preflight telemetry sync-hosted` writes `hosted.json` in the run directory of each pushed run that it reads.
The file holds `fetched_at`, `sha`, `pr`, `checks`, `missing`, and `source`.
It has no schema version. A reader must ignore keys that it does not know.
The file is derived from GitHub. It never binds evidence.
No fingerprint, evidence digest, configuration digest, or attestation contains it.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

The run summary stays at `schema_version = 1`.
Its `hosted` object gets the keys `pr_number`, `pr_state`, `merged`, `checks_conclusion`, and `fetched_at`.
They are `null` until a sync joins the run.
Summaries from earlier versions do not have these keys. A reader must count a missing key as `null`.
`telemetry rebuild` adds the keys to every summary of the clone.
Earlier tool versions read the new summaries and ignore the new keys.
They do not read `hosted.json`, so a rebuild with an earlier version writes summaries without the keys.

`report` adds `hosted.synced` to the statistics of each group.

## Hook decision log

`hook-check` now appends each pre-push decision to `hook-decisions.jsonl` in the telemetry directory, beside `runs.jsonl`.
The file is a new stored format: one JSON object on each line, with the keys `at`, `repo`, `remote`, `allowed`, `reason`, and `updates`.
The lines have no schema version. A later version that changes the shape adds a `schema_version` key.
This version counts a line with an unknown key as malformed. It never treats that line as absent.
The reason codes are a closed set. See [Hook decisions](docs/local-telemetry.md#hook-decisions).
The log never binds evidence. No fingerprint, evidence digest, configuration digest, or attestation contains a log field.
Run schema 2, attestation schema 7, and the summary schema 1 are unchanged.

`[telemetry] hook_log` is a new key, `true` by default.
Older tool versions reject a configuration file that contains it, as an unknown key.
Put it only in the user file of a machine that runs this version or a later one.

The exit codes and the stderr messages of `hook-check` are unchanged.
The installed hook script now sets `AGENTIC_PREFLIGHT_PUSH_REMOTE` to the remote name before it runs `hook-check`.
An older `hook-check` ignores the variable, so a hook from this version works with an older tool.
`init` replaces a hook that is exactly the script that an earlier version installed. It does not change any other hook.
A hook from an earlier version still works. Its log lines have `remote = null`.

In `agentic-preflight report`, `data.hook_decisions` is a new key, and the Markdown has a new "Hook decisions" section before the limits.
`data.limits` has two more items.

## Temporary repositories and telemetry

Without an explicit `[telemetry] path`, runs and hook decisions of temporary repositories no longer reach the telemetry directory.
A repository is temporary when both are true: its worktree is under a system temporary directory, and it has no network remote.
The system temporary directories are `tempfile.gettempdir()`, `$TMPDIR` when it is set, and `/tmp` except on Windows, compared as resolved real paths.
A network remote is an `origin` whose normalized form starts with a host name. A missing `origin`, a local path such as a bare repository, and a loopback host are not network remotes.
Thus, a scratch clone under the temporary directory that pushes to GitHub keeps its telemetry.
A run of a temporary repository still writes `summary.json`. It appends no line to `runs.jsonl`, and `hook-check` appends no line to `hook-decisions.jsonl`.
See [Temporary repositories](docs/local-telemetry.md#repositories-without-a-network-remote).

An explicit `[telemetry] path` is an opt-in, and keeps the earlier behavior for every repository.
`[telemetry] mirror` and `hook_log` are unchanged.
Rows that the mirror already has are not removed.

The `summary_written` event and `data.telemetry` add `reason`: `temporary_repository`, or `null`.
The `mirror` object of `telemetry rebuild` and of `telemetry sync-hosted` adds `reason` and `temporary_runs`.
In `agentic-preflight report`, `data.source.clone` adds `temporary_runs`, and `unmirrored_terminal_runs` no longer counts those runs.
The summary schema 1, the hook decision lines, run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
No fingerprint, evidence digest, configuration digest, or attestation reads these keys.

## Repositories without a network remote and telemetry

The exclusion from the default telemetry location no longer depends on where a repository lives.
Without an explicit `[telemetry] path`, runs and hook decisions of every repository without a network remote stay out of the telemetry directory, under the system temporary directory or anywhere else.
A network remote is an `origin` whose normalized form starts with a host name. A missing `origin`, a local path such as a bare repository, and a loopback host are not network remotes.
This remote-based rule reduces fixture noise; it does not identify whether a repository contains real work or can push. Legitimate local-only repositories and repositories that push to local or loopback remotes are also excluded by default. Fixture repositories that a harness creates outside the temporary directory, for example under the home directory, no longer reach `runs.jsonl` or `hook-decisions.jsonl`, and no longer appear in a report that reads the default mirror.
`agentic-preflight report` inside such a clone still summarizes the clone's own run records in memory, and counts them in `data.source.clone.excluded_runs`.
A scratch clone under the temporary directory that pushes to GitHub keeps its telemetry, as before.
See [Repositories without a network remote](docs/local-telemetry.md#repositories-without-a-network-remote).

An explicit `[telemetry] path` is an opt-in, and keeps the earlier behavior for every repository.
`[telemetry] mirror` and `hook_log` are unchanged.
Rows that the mirror already has are not removed. Delete the rows of fixtures yourself.

The reason code `temporary_repository` is replaced by `no_network_remote` in the `summary_written` event, in `data.telemetry` of `finish`, `abort`, and `gc`, and in the `mirror` object of `telemetry rebuild` and `telemetry sync-hosted`.
`mirror.temporary_runs` is renamed `mirror.excluded_runs`, and `data.source.clone.temporary_runs` in `agentic-preflight report` is renamed `data.source.clone.excluded_runs`.
A reader of these envelopes that matched the old names must use the new ones. No other key changes.
The normalized form of a relative local remote, for example an `origin` of `fixtures/bare.git`, is now `./fixtures/bare` instead of `fixtures/bare`, in `repo.remote` of summaries and hook decision lines written from now on. Git reads such a value as a path, and the prefix keeps its first segment from passing for a host.
The summary schema 1, the hook decision lines, run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
No fingerprint, evidence digest, configuration digest, or attestation reads these keys.

## Upgrading from 0.8.1 to 0.9.0

Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
Run records that 0.8.1 wrote stay readable.
Run records that 0.6.0 wrote, and that no later release rewrote, do not; see [Run records from before 0.7.0 are no longer read](#run-records-from-before-070-are-no-longer-read).
0.9.0 removes no configuration keys that 0.8.1 accepts in a configuration file for a workflow command.
`telemetry rebuild`, `telemetry sync-hosted`, `report`, and the `[retention]` read in `gc` skipped `[review] require_fix_commits` and `[worktree] ttl_hours` with a warning in 0.8.1; in 0.9.0 they fail on either key with a configuration error. Delete the key from the file.
It ignores `path` in a repository's `[telemetry]` section, and it accepts only `true` and `false` for `mirror` and `hook_log`; see [Repository telemetry settings can only turn telemetry off](#repository-telemetry-settings-can-only-turn-telemetry-off).

Upgrade the protected-base CI verifier before, or together with, the local CLI and the pre-push hook.
The review digest now hashes patch text that does not depend on Git configuration or Git attributes, so a 0.8.1 verifier rejects a note that 0.9.0 writes with "review fingerprint does not match the Git diff".
See [Review patches no longer depend on Git configuration](#review-patches-no-longer-depend-on-git-configuration) and [Review patches no longer depend on Git attributes](#review-patches-no-longer-depend-on-git-attributes).

A note that 0.8.1 wrote still verifies under 0.9.0 in the usual case. These are the exceptions:

- The note's patch text cannot be rebuilt, for example because its producer changed a diff setting or the verifier's clone abbreviates object IDs to a different length.
- The note relied on an attribute that showed a text file as binary.
- The note does not satisfy the policy committed at the protected base; see [Attestation verification reads the protected base](#attestation-verification-reads-the-protected-base).

Validate the commit again to replace such a note.

Push or abort every open run before you upgrade.
0.9.0 cannot merge back, gate, or push a run that 0.8.1 started, because the record does not say which configuration came from the user file.
`status` returns `agentic-preflight abort` as `next.command` for such a run. Every command that would validate or publish it exits 4 with `needs_human` and `data.reason` `unknown_config_provenance`.
Abort such a run and start a fresh one, unless it has reached the validation restart limit, where no command is advertised and the user decides; see [Attestation note content without user-level configuration](#attestation-note-content-without-user-level-configuration).
Review evidence that 0.8.1 recorded does not carry to a new head.
0.8.1 cannot read a run record that 0.9.0 starts, so finish or abort those runs before a downgrade.

0.8.1 rejects these new configuration keys as unknown: `[general] network_timeout_seconds`, `[review] trusted_paths`, `[retention] run_days`, and `[telemetry.hosted] local_equivalent_checks`.
Upgrade every machine that shares a clone, and the protected verifier, before you set one of them in a repository file.

Check wrappers, scripts, and copied skills against these changes:

- `respond --action dismissed|accepted` requires `--reason`; see [Require response reasons in the CLI](#require-response-reasons-in-the-cli). Run `agentic-preflight integrations update` so that installed skills pass it.
- `logs --stage` returns a bounded window. Pass `--all` for the whole log; see [`logs` returns a bounded window](#logs-returns-a-bounded-window).
- In `strict` and `reusable` modes, a plain `abort` exits 5 with `unmerged_work` when the validation worktree holds uncommitted changes or unregistered commits; see [Abort protects uncommitted changes in the validation worktree](#abort-protects-uncommitted-changes-in-the-validation-worktree) and [Abort protects commits that nothing registered](#abort-protects-commits-that-nothing-registered).
- Fetches and pushes cannot prompt, and they time out; see [Fetch and push cannot prompt, and time out](#fetch-and-push-cannot-prompt-and-time-out).
- `push` publishes only the pushed commits' notes, and `data.refspec`, `data.push_commands`, and `data.would_push` no longer hold runnable notes refspecs; see [Push publishes only the pushed commits' notes](#push-publishes-only-the-pushed-commits-notes).
- New error codes: `unsupported_base_ref`, `unsupported_object_format`, `missing_merge_base`, and `unrelated_histories` from `start`; `findings_unresolved` from `context --section docs`, `stage run lint`, and `mergeback`; `remote_timeout` from `start`, `gate`, and `push`; and `review_diff_attribute` from `context` and review.
- Run summaries and `agentic-preflight report` no longer hold the recorded hosted outcome: the `hosted.last_ci_outcome` and `hosted.pr_url_recorded` summary fields, the "Hosted outcomes recorded" report section, and the `outcome_recorded`, `outcomes`, `pr_url_recorded`, `pr_url_excluded_runs`, and `note` keys of `data.overall.hosted`.

A project that copied the hosted workflows must copy the new versions and add `human-approval-review.yml` and `human-approval-rerun.yml`; see [Reviews re-run the hosted approval check](#reviews-re-run-the-hosted-approval-check) and [Hosted workflow hardening](#hosted-workflow-hardening).
A pull request that edits a configured reviewer script now needs the configured human approval; see [The reviewer's own files need human review](#the-reviewers-own-files-need-human-review).
The example reviewer wrappers in `docs/examples/reviewers/` are deprecated in favour of `agentic-preflight reviewer run` and are planned for removal in 0.10.0; see [Packaged command reviewer](#packaged-command-reviewer).

The package now declares `requires-python = ">=3.11,<3.15"`; see [Python versions](#python-versions).

The sections from here to the end of this file describe the 0.9.0 changes.
[Python versions](#python-versions), [Repository shapes](#repository-shapes), [Attestation note content without user-level configuration](#attestation-note-content-without-user-level-configuration), and [Run record retention](#run-record-retention) above are also new in 0.9.0.

## Run records from before 0.7.0 are no longer read

0.9.0 reads the run records that 0.8 wrote.
It no longer reads four things that 0.6.0 and earlier releases wrote, which 0.7.0 through 0.8.1 still accepted:

- A run record with the `test_delegation` key, or in the `TEST_DELEGATED` or `PUBLICATION_READY` state, is invalid.
  0.6.0 wrote the key in each run record, with `null` when the run delegated no tests.
  0.7.0 through 0.8.1 dropped the key on load and orphaned a run that still waited for delegated tests.
- A run record whose `mergeback_attempt` holds `rebased_tree` is invalid.
  0.7.0 through 0.8.1 ignored the field.
- A nonterminal run with a `pending-update.json` journal beside its record is invalid.
  0.7.0 through 0.8.1 orphaned such a run at the next `start` and let `abort` end it.
  A terminal run with a journal stays readable, and its evidence is still never reused.
- `[review] require_fix_commits` and `[worktree] ttl_hours`, which 0.6.0 removed, are unknown keys everywhere.
  An attested configuration snapshot from 0.5 that holds one fails validation.
  `telemetry rebuild`, `telemetry sync-hosted`, `report`, and the `[retention]` read in `gc` no longer skip them in a configuration file with a warning.

Null `publication_ready_at` and `test_delegation` fields in an attestation note are still discarded, and the configuration that 0.7.0 removed is still tolerated as before.

0.9.0 handles an invalid record like any other record that it cannot read.
It leaves the record, its ownership pointers, and its worktree unchanged.
A command that loads the record exits 3 with `run_record_unreadable` and `data.reason` `invalid`.
`status` reports `data.readable: false`, and `status --all` and `gc` list the record.
`gc` keeps it, keeps a reusable runner that it names, and does not offer `gc --force`.
`telemetry rebuild` and `report` read raw records, so they still summarize a finished one.

A release from 0.7.0 through 0.8.1 removed the two keys only when it rewrote a record, and it never rewrote a finished record.
A clone that ran 0.6.0 can therefore hold finished records that 0.9.0 reports as unreadable.
0.9.0 has no command that converts or deletes them.

- Before you upgrade, use 0.8.1 to retire the unfinished ones: run `status --all`, then `start` or `abort` in each listed source worktree, then `gc`.
- A finished record (`PUSHED`, `DONE`, `ABORTED`, or `ORPHANED`) becomes readable again when `test_delegation` and `mergeback_attempt.rebased_tree` are deleted from its `run.json`.
  That is the change 0.7.0 through 0.8.1 made when they rewrote a record.
  The 0.9.0 release has a one-off script that makes exactly this change in a clone and keeps a copy of each original: [`migrate-finished-records.py`](https://github.com/elanthus/agentic-preflight/releases/download/v0.9.0/migrate-finished-records.py).
  Run it once in the clone, first without options to see what it would change, then with `--apply`.
  The script is not part of the package and is not maintained after the release.
- For an interrupted run that 0.9.0 refuses, install 0.8.1 again, `abort` the run or `start` over it, and upgrade.

## Attestation verification reads the protected base

`verify SHA`, `approval-check`, `push`, and the pre-push hook no longer take policy or the merge base from the note.
They recompute the merge base against the trusted base, which is `--base` when given and otherwise the note's `base_ref` resolved to `refs/remotes/origin/<branch>` or `refs/heads/<branch>`.
Only `--base` makes that base independent of the note. `push` and the pre-push hook pass none, so a forged note there can name a branch whose policy is weaker.
The note's `merge_base_sha` must be the only merge base of that base and the head. A mismatch fails with the new `data.reason` `base_mismatch`.
Policy comes from the `.agentic-preflight.toml` committed at the trusted base. The user configuration and the branch's own edits no longer relax it.
A note that skips docs the base enables, or whose lint, test, or command-review command differs from one the base configures, is now rejected.
So is a note that excludes review files the base does not exclude.
A branch that changes those settings fails verification until a human merges it.
A docs stage that is green while the base disables docs now passes. Earlier verifiers rejected it.

`verify` gains the optional `--base REV`. The hosted workflows pass the pull request's base SHA.
`start` now refuses, with exit 3 and the new error code `unsupported_base_ref`, a `--base-ref` or `[general] base_ref` that does not name an existing `refs/remotes/origin/<branch>` or `refs/heads/<branch>`, such as a tag, another remote, a commit SHA, or revision syntax. Earlier releases started such a run. `push` and the pre-push hook now could not verify its note, so `start` stops it before any stage runs.
Attestation schema 7, fingerprint version 2, and the evidence and configuration digests are unchanged.
Notes that an earlier release wrote stay readable, and they verify when they satisfy the protected base policy.

## Fetch and push cannot prompt, and time out

The fetches in `start` and in a manual-mode `gate`, and the pushes, notes fetch, and remote query in `push`, now run with `GIT_TERMINAL_PROMPT=0` and an empty stdin.
On Linux and macOS they also run without a controlling terminal.
When `GIT_SSH_COMMAND`, `GIT_SSH`, and `core.sshCommand` are all unset, they run `ssh` as `ssh -o BatchMode=yes`. A configured SSH command is used unchanged.
A remote that needs a username, a password, a new host key, or a key passphrase typed at the terminal now fails the command with Git's error instead of waiting.
Store the credential in a credential helper or an SSH agent, and accept the host key once outside the tool.
The pre-push hook that Git runs inside `push` inherits that environment, so a hook that asks a question now fails.

The new key `[general] network_timeout_seconds` (default 600, at least 1) stops each of those commands, with the processes it started, when it runs longer.
For a push, the limit includes the pre-push hook.
The key is in the run snapshot only when it differs from the default, so default configurations keep their snapshots and configuration digests. No review or docs fingerprint includes it.
An earlier release rejects a configuration that sets the key, as an unknown key.

`start`, `gate`, and `push` have a new error code, `remote_timeout`, with exit 4. Its `data` holds `remote`, `operation`, and `timeout_seconds`, and for `start` also `base_ref` and `worktree_path`.
`start` records a new `remote_timeout` event and leaves the run in `SYNC_CONFLICT`, as for `sync_failed`. `push` leaves the run in `AWAITING_PUSH_CONFIRM`.
Run schema 2 and attestation schema 7 are unchanged.

## Reviews re-run the hosted approval check

`human-approval.yml` no longer listens to `pull_request_review`.
Two new workflow files handle reviews: `human-approval-review.yml` records the reviewed pull request number, and `human-approval-rerun.yml` re-runs each protected-base policy run for the head.
A project that copied the hosted workflow must add both files, or a review will not update the `high-risk human approval` check until someone re-runs the failed run by hand.
The required check keeps its name. No ruleset change is needed.
The re-run companion needs `actions: write`; it runs from the default branch and never checks out pull request code.
The policy run now uploads its evaluation as the `human-approval-policy` artifact, kept for 30 days; the companion reads the approval mode from it and leaves a run without one alone.
It re-runs only runs whose record shows a review mode and a required human approval.
GitHub allows a re-run for 30 days after the original run. An older pull request needs a new push before approval can land.
The `approval-check` command, its outputs, and the attestation schema are unchanged.

## Documentation inventory lists committed files

The documentation inventory now lists only files committed at the head commit.
Files not committed at the head commit that match a documentation pattern, such as a Sphinx `docs/_build/` tree or a nested worktree under `.claude/worktrees/`, no longer appear in `doc_surface`, in the docs allowlist, or in the docs fingerprint.
A docs finding against such a file is now rejected as outside the allowlist.
In a repository with such files, `doc_surface_sha256` changes, so docs evidence recorded before the upgrade does not carry to a new head and the docs stage runs again.
In a repository without them, the inventory and its digest are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Repository telemetry settings can only turn telemetry off

A `[telemetry]` section in `.agentic-preflight.toml` no longer replaces the user's section.
Its `path` is ignored, and every command that loads the configuration prints a warning that names the file. The pre-push hook stays silent.
Its `mirror = true` and `hook_log = true` keep the user's value. Its `mirror = false` and `hook_log = false` still apply.
A repository that set `path` to collect its contributors' telemetry no longer does so. Each contributor who wants it must set `path` in their own user file.
A test harness that turns telemetry off with a repository section, as [Isolate a test harness](docs/local-telemetry.md#isolate-a-test-harness) describes, is unaffected.
Invalid `mirror` and `hook_log` values and unknown keys in the repository section are still errors. Its `path` is ignored whatever its value.
`mirror` and `hook_log` now accept only `true` and `false`, in either file. Earlier releases also accepted values such as `1`, `"yes"`, and `"on"`. Such a value is now an error that names the file.
Attestation verification reads the protected base's `[telemetry]` section by the same rule, so a `path` there no longer fails verification.
No configuration digest, snapshot, fingerprint, or attestation changes, because none of them contains `[telemetry]`.

## Event logs skip torn lines

An event appended after an interrupted append now starts on a new line instead of joining the torn line.
A run's event log line that is not a JSON object, such as a torn line, is now skipped wherever it appears.
A line that is JSON but not an object, such as `7`, used to make `events` fail with `internal_error`.
Earlier releases skipped only a torn final line and reported a malformed earlier line as `run_record_unreadable` with reason `invalid`, from `events`, from `status` in `MERGEBACK_CONFLICT`, and from `telemetry rebuild`.
The `events` envelope adds `data.skipped_lines`, the number of lines skipped.
Run schema 2, attestation schema 7, and the event format are unchanged.

## Push publishes only the pushed commits' notes

`push` no longer pushes the local `refs/notes/agentic-preflight` ref.
It pushes a notes commit built from the remote notes tip plus the notes of the commits it pushes.
The remote notes history still only moves forward, so a reader that fetches the notes ref is unaffected.
`gate`, `push`, and `push --dry-run` envelopes add `data.published_notes`, a list of commit SHAs.
From `push` it lists only the notes added or changed on the remote. From dry runs, and from `gate` outside manual mode, neither of which contacts the remote, it lists every pushed commit that has a local note. In manual gate mode, `gate` fetches the remote notes and the list is exact.
The branch and notes push now carries `--force-with-lease` on `refs/notes/agentic-preflight`, so a remote notes ref that changed after the fetch, including by deletion or rewind, rejects the push. `push` then rebuilds, and the manual command must be regenerated with `gate`.
After a successful push, the published notes commit is merged into the local notes ref, so that ref gains merge commits.
`gc` and publication delete leftover `refs/notes/agentic-preflight-publish-*` and `refs/agentic-preflight/fetched-notes-*` refs.
Outside manual gate mode, `data.refspec`, `data.push_commands`, and `data.would_push` show the notes source as `<notes-commit>` instead of `refs/notes/agentic-preflight`.
A wrapper that ran those strings as commands must run `push` instead.
In manual gate mode, `gate` now fetches the remote notes, and `data.manual_command` names the notes commit's SHA.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Run and findings records are validated before they are written

The run store parses each run and findings record back through its loader before writing it, and writes neither file of a transaction when either is invalid.
Such a refusal is reported as `internal_error`; the earlier files stay unchanged.
`respond --note` longer than 4,000 characters now exits 6 with `usage_error`, and the Python `respond` API raises `invalid_response`. Earlier releases exited 0 and wrote a findings record that could not be read.
A `run_record_unreadable` error envelope from a mutating command adds `data.pointer_paths`, a list of ownership pointer file paths, when the record names the `in_place` or `strict` worktree mode. When the mode is `reusable` or the record names the reusable runner, the key is absent and `next.instruction` says to keep the runner and stop. When the mode cannot be determined, the key is absent and `next.instruction` says to keep the record and its pointers and stop. When no pointer names the run, the plain recovery applies.
Single-run `status`, `status --all`, and `gc` output are unchanged.
Run schema 2, attestation schema 7, fingerprint version 2, and the findings format are unchanged.

## Open `auto_fix` findings keep the run in review

`context --section docs`, `stage run lint`, and `mergeback` have a new error code, `findings_unresolved`, with exit 3.
They return it while a review or docs finding is still open and either blocking or `auto_fix`. `error.message` and `blocking` name the findings.
Earlier releases let such a run continue, and `mergeback` then failed with `internal_error` and left the run in `MERGEBACK_PENDING`.
A disabled docs stage is no longer skipped while such a finding is open, and `status` in `REVIEW_GREEN` or `DOCS_GREEN` returns `respond` for it.
The transition table adds `REOPEN_FINDINGS`, from `TEST_GREEN` and `MERGEBACK_PENDING` to `REVIEW_GREEN`. A `state_transition` event can name it.
`mergeback` uses it, or a return to review, to recover a run that an earlier release left in `TEST_GREEN` or `MERGEBACK_PENDING` with such a finding.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## `logs` returns a bounded window

`logs --stage` no longer returns the whole stage log by default.
`data.output` now holds the last 200 lines, cut to at most 64 KiB.
The `logs` envelope adds `data.truncated`, `data.total_bytes`, and `data.total_lines`, which describe the whole stored log.
New flags choose the window: `--tail N`, `--head N`, and `--all`. `--all` restores the earlier whole-log output.
A wrapper that read the whole log from `data.output` must pass `--all` or read the file at `data.log_path`.
The stored log, its redaction, and its location are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Grounding reads the merge base

`data.grounding` now reads `docs/**`, `AGENTS.md`, `CLAUDE.md`, the files that `[context] extra_paths` selects, and the `[context]` section itself from `.agentic-preflight.toml` at the run's merge base.
Earlier releases read them from the branch head and the working-tree configuration.
When the merge base sets no `[context]` section, the user file's section applies.
A branch's edits to those files reach the reviewer only through the diff, and a branch's `[context]` change applies to later runs after it merges.
For a branch that edits those files or `[context]`, the grounding and `grounding_sha256` change, so review and docs evidence recorded for that branch before the upgrade is not reused and those stages run again.
For other branches the grounding is unchanged.
Run schema 2, attestation schema 7, fingerprint version 2, and the configuration digest are unchanged.

## Hook path names a symlink, not its target

`init` now replaces a symlink at the pre-push hook path with a regular file instead of writing through it.
A dangling link, or a link to exactly the current hook script, is replaced without `--force`. Any other link to an existing file is judged by that file's content, as before.
`data.hook_path` from `init` and the hook path that `status` reports now name the hook path itself, without following a symlink there.
Earlier releases reported the link's target, or the missing target of a dangling link.
The hooks directory, including `core.hooksPath`, is still resolved to an absolute path.

## Hosted workflow hardening

The `high-risk human approval` job now fails unless the policy job reports `requires_approval` as exactly `true` or `false`.
The checkouts in `attestation.yml` and `human-approval.yml` set `persist-credentials: false`. A private repository that copies them must authenticate the later `git fetch` itself; see [Portable attestations and CI enforcement](docs/attestations-and-ci.md#required-github-check).
`human-approval-rerun.yml` now has an `authorize` job and a `rerun` job, and re-runs the policy only for a review from an owner, member, or collaborator, an action by a user with write access, or an action by a bot account that has dismissed an owner, member, or collaborator review of the pull request. When a lookup that decides this fails, it re-runs the policy with a warning instead of skipping it. A project that copied the companion should copy the new version.
The required check keeps its name, and `approval-check`, its outputs, and the attestation schema are unchanged.

## The reviewer's own files need human review

Risk classification now treats the repository paths that `[review] command` and `[review.contracts] planner_command` name as human-review paths, together with every file under each named file's directory.
Directories that a command changes to or adds to `PYTHONPATH` or `NODE_PATH`, and the top-level package of a dotted `-m` module, are protected as whole directories. `-m name` protects `name.py` and `name/**`.
Shell control operators (`;`, `&`, `|`, `(`, `)`, `<`, `>`) split the command only outside quotes, so a quoted path such as `'reviewers (local)/rev.py'` is protected as one path.
The new optional `[review] trusted_paths` list adds more patterns.
This applies to local runs, to attestation verification, and to `approval-check`, which reads both from the protected base.
A pull request that edits a configured reviewer script, or a sibling file such as the example wrappers' `_reviewer_common.py`, is now high risk and needs the configured approval. Earlier releases classified it by `[policy]` alone.
Such paths appear in `data.path_reasons` with the existing kind `human_review_path`. No envelope field or reason kind is new.
They also appear in the policy entries of `data.grounding`. For a branch whose changed files match a newly derived pattern, the grounding and `grounding_sha256` change, so review and docs evidence recorded for that branch before the upgrade is not reused and those stages run again.
Earlier releases reject a configuration file that sets `trusted_paths`, as an unknown key. Upgrade the protected-base verifier and every machine that shares a clone before you set it.
Configuration snapshots omit `trusted_paths` when it is empty, so snapshots and configuration digests for existing configurations are unchanged.
A snapshot that sets it cannot be read by an earlier release.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

The example reviewer configurations now run their wrapper with `python3 -S -B`.
The Claude wrapper now passes `--safe-mode` and `--setting-sources user`, checked against Claude Code 2.1.292.
The Codex wrapper now passes `--config project_doc_max_bytes=0` and `--ignore-rules`, checked against Codex CLI 0.163.0.
An older CLI that lacks one of these options fails the review with a visible error. Update the CLI, or remove the option from your copy of the wrapper and protect the files that it would read with `trusted_paths`.
With `--safe-mode`, the Claude reviewer also ignores your user `CLAUDE.md`, skills, hooks, and MCP servers.

## Outcome fields in run summaries and the report

The run summary keeps `schema_version = 1`. The new keys are inside objects that the summary already has:
`findings.review_blocking_fixed`, `findings.docs_blocking_fixed`, `stages.<name>.red_then_green`, `timing.tool_seconds`, and `timing.agent_seconds`.
Earlier tool versions validate the top level of a summary strictly but read these objects as open maps, so they read the new summaries and ignore the new keys.
A reader must count a missing key as `null`, as for earlier summary additions.
`telemetry rebuild` adds the keys to every summary of the clone. `timing.tool_seconds` and `timing.agent_seconds` stay `null` for runs that an earlier version recorded.

The run event log can contain the new `tool_command_started` and `tool_command_finished` events, and the `run_created` event adds `command_timing`.
The events never bind evidence. No fingerprint, evidence digest, configuration digest, or attestation reads them.
They count against `events --limit` like other events, so the same limit shows fewer state transitions than before.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

In `agentic-preflight report`, each group of statistics adds `caught` and `tool_time`, and the Markdown has two new sections, "Caught before push" and "Tool time".

## The pre-push hook recognizes the SHA-256 zero object ID

`hook-check` now treats Git's 64-character zero object ID as a ref that does not exist, as it treats the 40-character one.
In a SHA-256 repository it no longer blocks every new branch as a force push.
It blocks a branch push that needs an attestation with the reason code `missing_attestation` and a message that says SHA-256 repositories are not supported.
It gives that message, not the force-push message, for a push that rewrites a remote branch there with a commit the remote does not have.
A rewind to a commit the remote already has is still refused as a force push unless `[hook] allow_force_push = true`.
Deletions, tags, the attestation notes ref, evidence refs, and pushes of a commit the remote already has are allowed as in a SHA-1 repository.
The exit codes, the reason codes, and the hook decision lines are unchanged. Behavior in a SHA-1 repository is unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Packaged command reviewer

`agentic-preflight reviewer run --provider claude|codex` is a new command.
Like `hook-check`, it prints no JSON envelope and keeps Click's usage output and exit code 2 on a usage error.
Its stdout, stderr, exit codes, and `AP_*` environment variables are those of the former example wrappers.
Unlike them, it reads its own stdin and writes its own stdout and stderr as UTF-8 rather than in the locale encoding, which matters on Windows. The packaged reviewer and the current example wrappers also send the prompt to the provider CLI as UTF-8 and decode its output as UTF-8, replacing undecodable bytes, where earlier wrappers used the locale encoding. An unwritable `AP_REVIEWER_EVENTS_FILE` now fails the review with the one-line diagnostic instead of a traceback. Like them, it does not need Git: commands in the `reviewer` group skip the Git version check.
It passes the same provider CLI options as the example wrappers, including `--safe-mode` and `--setting-sources user` for Claude and `project_doc_max_bytes=0` and `--ignore-rules` for Codex.
The example wrappers in `docs/examples/reviewers/` stay as deprecated standard-library copies for one release. A test keeps them identical to the packaged reviewer, and they still run with `python3 -S -B`.
The configuration file, the review submission, and the attestation schema are unchanged.

## The baseline check runs outside the Git directory

`stage run --baseline` now checks out the base commit under the validation-worktree root in every mode, including `in_place`, instead of under `.git` in `in_place` mode.
The root is the user cache by default, or `[worktree] root`.
In `in_place` mode, a `[worktree] root` inside the repository now makes `--baseline` fail with `setup_failed` (kind `worktree`). Isolated modes already refuse such a root at `start`.
A `[worktree] root` inside the Git common directory is now refused in every mode: at `start` in isolated modes, and as a `--baseline` setup failure in `in_place` mode. From a linked checkout such a root used to pass, because the common directory is outside the checkout.
The `stage run` envelope adds an optional `data.baseline_cleanup_failed` object (`worktree_path`, `branch`, `detail`) when the scratch worktree could not be removed, and the run's event log adds a matching `baseline_cleanup_failed` event. Both are additive.
A baseline setup failure can carry the same `data.baseline_cleanup_failed` object.
The `stage-run.json` schema gains that property. Run schema 2 and attestation schema 7 are unchanged.

## Review patches no longer depend on Git configuration

The review digest hashes the patch text of the change, and a verifier accepts a note only when it can rebuild that text.
Earlier releases built it with the Git configuration of the machine that ran the command.
A producer and a verifier with different settings then disagreed about the same commits, and `verify`, `push`, and the pre-push hook failed with "review fingerprint does not match the Git diff".

Review patches now have one form on every machine:

- The `index` line of each patch names full object IDs. Earlier releases abbreviated them to a length that `core.abbrev` sets and that Git otherwise raises as a clone gains objects.
- These settings no longer change the patch text or the list of changed files: `core.quotePath`, `core.bigFileThreshold`, `core.precomposeUnicode`, `diff.noprefix`, `diff.srcPrefix`, `diff.dstPrefix`, `diff.algorithm`, `diff.context`, `diff.interHunkContext`, `diff.indentHeuristic`, `diff.suppressBlankEmpty`, `diff.submodule`, `diff.ignoreSubmodules`, `diff.relative`, `diff.orderFile`, and `diff.default.binary`.
  Each is fixed to Git's default.
  `core.bigFileThreshold` turned a larger text file's patch into "Binary files ... differ", and so did `diff.default.binary = true` for every file without a `diff` attribute.
  `core.precomposeUnicode`, which Git on macOS sets at `git init`, stopped `context` with an internal error for a path whose name the tree stores in decomposed Unicode.
- `submodule.<name>.ignore` no longer drops a submodule pointer change, whether it is set in local configuration or in a `.gitmodules` file that the branch itself commits.
- The `GIT_DIFF_OPTS` environment variable no longer changes the number of context lines.
- Replacement objects under `refs/replace` are ignored. They exist in one clone only, and a review built with them could show content that the commits do not hold.
- The user-level attributes file (`core.attributesFile`, by default `$XDG_CONFIG_HOME/git/attributes`) is not read for review patches.

Attributes in the checkout, and `diff.default.xfuncname` and `diff.default.funcname`, are covered by [the Git attributes entry](#review-patches-no-longer-depend-on-git-attributes).

What changes at upgrade:

- `diff_sha256`, the review manifest, and the digest of each file-level review unit change for every diff that has an `index` line, because that line changed. A diff with only mode changes or an empty diff is unchanged.
  A file-level unit is a patch without hunks, such as a binary change. Hunk unit digests are unchanged.
- A note that 0.8.1 or an earlier release wrote still verifies when its producer left each setting above at Git's default, or when a setting it changed did not change that note's patch text, when the verifier's clone abbreviates object IDs to the same length as the producer's clone did, and when the attributes and driver settings that still apply, as described above, give the verifier the same patch text as they gave the producer.
  For example, a `-diff` attribute in the producer's `$GIT_DIR/info/attributes` makes a file's patch binary there, while a verifier clone without that attribute builds a text patch, so neither text matches the note.
  When the full-ID text does not reproduce a note's digest, the verifier also builds the abbreviated text and accepts the note if that text matches.
  The other settings above are fixed to Git's default for that text too, and the note does not record the producer's values, so they cannot be restored.
  A producer that set one of those settings to another value may therefore have written a note that fails, even on the machine that wrote it, if the value changed the patch. For example, `diff.context = 5` adds context lines to a hunk that has more unchanged lines around it, but leaves a one-line file's patch unchanged.
  The verifier builds the abbreviated text with Git's default length, whatever its own `core.abbrev` says.
  The verifier tries that text for any note, not only for notes from 0.8.1 and earlier. Until the fallback goes in 0.10.0 (row 12 of [the removal plan](docs/maintainers/compatibility-removal-plan.md)), a file-level patch in that format, such as a binary change, binds its content only by an abbreviated object ID. Hunks carry their full content, so they are not affected. A verifier clone that holds both blobs disambiguates them, because Git lengthens an abbreviation until it is unique.
  Git picks that default length from the number of objects in the clone, so the note can fail in a clone whose object count gives a different length, even when its producer used every default.
  A note for a change whose committed `.gitmodules` set `submodule.<name>.ignore = all` on a changed submodule fails on every machine, because 0.8.1 left that pointer change out of the patch and both texts now include it.
  When verification fails for any of these reasons, validate the commit again to replace the note.
- Review evidence that an earlier release recorded does not carry to a new head. The review runs again.
- A run that an earlier release reviewed and that is still open reopens review at its next command.
  The shell stages run again after the new review.
- 0.8.1 and earlier verifiers reject a note that 0.9.0 writes, with "review fingerprint does not match the Git diff".
  Upgrade the protected verifier before, or together with, the local publisher and the hook.

A repository that sets `diff.ignoreSubmodules = all`, or `submodule.<name>.ignore = all` in its configuration or its committed `.gitmodules`, now sees submodule pointer changes in the review diff, the changed-file list, risk classification, and the list of paths a fix commit changes.
Earlier releases left them out.

Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## An oversized diff is measured with a bounded read

`context` and `review run` can stop reading the diff early once its patch text passes four times `[diff] max_bytes`.
The `diff_too_large` envelope from `context` adds `data.complete` and `data.unmeasured_files`.
`data.complete` is `false` only when the read stopped early. Then `data.total_bytes` and the sizes in `data.by_file` are lower bounds over the files read, `data.unmeasured_files` lists the files that were not read, `next.instruction` says to check them too, and the error message reads "the diff is at least N bytes".
A report with `data.complete` set to `true` is exact, as before. Every diff of up to four times the limit is reported that way.
A wrapper that treated `data.total_bytes` as the exact diff size must check `data.complete`.
A diff within the limit produces the same patch text, `diff_sha256`, and review manifest as before.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Abort protects uncommitted changes in the validation worktree

In `strict` and `reusable` modes, releasing the validation worktree removes it or resets and cleans the runner, and a plain `abort` used to discard uncommitted changes there without asking.
`abort` now exits 5 with `unmerged_work` when the validation worktree has uncommitted changes, untracked files included, apart from ignored files and the files `start` copied in: the rule `gc` already applies before it releases a worktree.
The refusal adds `data.uncommitted_paths`, which lists up to 200 such paths, `data.uncommitted_path_count`, which counts them all, and `data.inspection_error`, and keeps `data.fix_commits` and `data.worktree_path`; when the run also has unmerged fix commits, one refusal names both.
A validation worktree that exists but cannot be inspected also exits 5 with `unmerged_work`; then `data.inspection_error` holds Git's error and `data.uncommitted_paths` and `data.uncommitted_path_count` are `null`, so a wrapper must not read an empty list as a clean worktree.
A validation worktree that no longer exists does not stop `abort`.
`--force` still discards the changes, as before.
A wrapper or agent that ran a plain `abort` after leaving files in the validation worktree, for example after a setup command that writes untracked files, must handle exit 5 or pass `--force`.
`in_place` runs are unchanged. The abort schema adds the optional `uncommitted_paths`, `uncommitted_path_count`, and `inspection_error` properties.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Abort protects commits that nothing registered

In isolated worktree modes, `respond` and a red stage's restart register fix commits, and merge-back registers the validated history at `TEST_GREEN`.
A commit made in the validation worktree outside those points lived only on the validation branch, and a plain `abort` deleted it with the branch.
`abort` from any state between the end of base synchronization and merge-back now counts such commits, read from both the checkout's `HEAD` and the validation branch, and exits 5 with `unmerged_work`; `--force` still discards them and lists them in `data.discarded_fix_commits` and the `aborted` event.
A recorded fix commit always counts, even when the validation checkout is detached or reset.
When the branch history cannot be read, for example because the validation branch holds a merge commit or no longer descends from the reviewed head, a plain `abort` also exits 5 with `unmerged_work` instead of deleting the branch, as long as some non-empty commit follows the reviewed head or Git cannot open the validation checkout; `data.fix_commits` then lists those commits without further checks, reading the branch from the shared repository, so after a rebase onto another base it can include commits the new base brought in. `--force` still succeeds, except for a `reusable` runner whose checkout Git cannot open, which fails to release with `git_error` as it did before.
A `reusable` runner whose directory is gone keeps its branch when released, so its commits are not counted.
In-place runs, states before synchronization finishes, and merge-back states are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## `start --replace` and `gc` read the validation branch

`start --replace` (and a moved source head) in `reusable` mode and `gc` in `strict` and `reusable` modes release a validation checkout only after checking its `HEAD`, and the release deletes the validation branch.
A checkout detached at the reviewed head therefore hid commits on `ap/<run_id>`, which the release deleted.
Both now also read the branch tip.
When the branch holds a commit after the reviewed head, or its history cannot be read, `start` orphans the run as before but puts the clean runner back on that branch, so it stays leased: the fresh run's `start` exits 4 with `needs_human` because the runner is still leased, and `gc` then decides.
Before, a detached runner counted as released, so the fresh run would have leased and reset it.
`gc` retains such a run in `data.retained` with reason `validation worktree holds unrecorded commits`, or `validation worktree could not be inspected` when the branch holds a merge commit or no longer descends from the recorded head; `gc --force` releases it as before.
The `gc` instruction for retained validation worktrees now also points at each run's `ap/<run_id>` branch.
A checkout whose `HEAD` is on its branch, the normal case, behaves as before.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Review patches no longer depend on Git attributes

Review patches followed the Git attributes of the checkout that built them.
A branch that committed `.gitattributes` with `* -diff` turned every patch into "Binary files ... differ", so the reviewer saw no content while each file still counted as reviewed.
Git reads `.gitattributes` from the checkout, not from the commits, so a verifier with a different checkout also rebuilt different text.
`$GIT_DIR/info/attributes`, `attr.tree`, `GIT_ATTR_SOURCE`, the system attributes file, and the `diff.<driver>.*` configuration of a driver that an attribute names did the same without leaving a trace in the diff.

Review patches now have one form whatever the attributes:

- Git builds them without `.gitattributes` files, the index, `attr.tree`, `GIT_ATTR_SOURCE`, the user attributes file, or the system attributes file. Each of these works from Git 2.38, the supported minimum.
- Text files are diffed with `--text`. The tool decides which files are binary with Git's default rule: either side is larger than 512 MiB or has a NUL byte in its first 8,000 bytes. A binary file's patch is Git's "Binary files ... differ" line, and its content is never printed.
- Hunk headers use Git's default rule, which picks the nearest earlier line that starts with a letter, `_`, or `$`. A `diff=python` or other driver attribute no longer changes them.
  `diff.default.xfuncname` or `diff.default.funcname` would replace that rule for every file, and Git has no value that restores it, so either setting stops `context` and review with `review_diff_attribute` (exit 3). Remove the setting to continue.
  The same applies to a hunk-header pattern, or `binary` set to false, for a driver named `set`, `unset` or `unspecified`: `git check-attr` prints `diff=unset` exactly as it prints `-diff`, yet Git applies the driver, so the tool cannot tell whether an attribute names it.
- Git always reads `$GIT_DIR/info/attributes`. `-diff` there has no effect, and neither has a driver that only sets `textconv` or `command`. A driver there with a hunk-header pattern, built in or set by `diff.<driver>.xfuncname` or `diff.<driver>.funcname`, `diff` set on a binary file, or a driver that prints a binary file as text, such as one with `diff.<driver>.binary = false`, stops `context` and review with `review_diff_attribute` (exit 3), and verification on that machine rejects the note. Remove that entry to continue. A built-in driver that a Git release after 2.54 adds is not refused; its hunk headers then differ on that machine only, and a verifier elsewhere rejects the note.

What changes at upgrade:

- `diff_sha256`, the review manifest, and the affected unit digests change for a repository whose attributes changed a patch: a `-diff`, `binary`, or `diff` attribute, or a diff driver with a hunk-header pattern. A repository without such attributes gets the same text as [the Git configuration entry](#review-patches-no-longer-depend-on-git-configuration) describes.
- A note that 0.8.1 or an earlier release wrote still verifies when its text can be rebuilt, as [the Git configuration entry](#review-patches-no-longer-depend-on-git-configuration) describes. That text follows the verifier's attributes, as 0.8.1 did.
  The verifier now rejects such a note when its text shows "Binary files ... differ" for a file whose content is text. The message names the file.
  This applies only when that earlier text reproduces the note's digest. A digest that neither text reproduces fails with "review fingerprint does not match the Git diff", whatever the checkout's attributes.
  That text uses Git's default `core.bigFileThreshold`, as [the Git configuration entry](#review-patches-no-longer-depend-on-git-configuration) describes, so a lowered threshold on the verifier does not cause this.
- Review evidence recorded against attribute-shaped text does not carry to a new head. The review runs again and now sees the content.
- Which files are binary is decided from the blobs the commits hold. A replacement object under `refs/replace` does not change that decision, as it does not change the patch.
- The bounded read that checks `[diff] max_bytes`, described in [the bounded read entry](#an-oversized-diff-is-measured-with-a-bounded-read), builds the same attribute-free text. To decide which files are binary it still reads every changed blob, keeping only the first 8,000 bytes of each, before it reads any patch text.
- Earlier verifiers rebuild attribute-shaped text, so they reject a note that 0.9.0 writes for a repository whose attributes changed a patch. Upgrade the protected verifier before, or together with, the local publisher and the hook, as [the Git configuration entry](#review-patches-no-longer-depend-on-git-configuration) already requires.

Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Refuse unsupported repository object formats up front

`start` and `init` now exit 3 with `unsupported_object_format` for any object
format other than `sha1`. The message links the [Repository shapes](#repository-shapes) compatibility statement.
Previously a SHA-256 repository could start a run but failed when recording review
coverage. No run is created or resumed; `init` refuses before writing configuration
or installing a hook, including with `--force` or `--no-hook`. Existing SHA-1
repositories are unaffected. SHA-256 support is not added by this change.

## Distinguish missing merge bases from empty diffs

`start` no longer maps every merge-base failure to `empty_diff`.
A shallow repository whose available history has no common ancestor with the base
now exits 3 with `missing_merge_base` and fetch/deepening guidance in `next`.
A non-shallow repository with unrelated histories exits 3 with `unrelated_histories`.
Other Git failures keep `git_error` (exit 1) and the original diagnostic.
Consumers matching `empty_diff` for these failures must handle the new codes.
Actual empty diffs retain `empty_diff`. Neither refusal creates a run.

## Documentation surfaces use committed blobs

Documentation existence, size, and content now come from HEAD's blobs rather than
checkout files. Sparse and full checkouts of the same commit produce the same
surface and `doc_surface_sha256`. Symlinks contribute link text, including dangling
and directory-target links; LFS pointers and normalized EOL bytes are hashed as
committed, without filters or target traversal.

Fingerprint version 2 and the digest's JSON fields and ordering stay unchanged.
We chose one-time invalidation where the surface differs, rather than globally
versioning the digest or maintaining a legacy checkout-based fallback. Old run
records remain readable, and this change rewrites no record or note and makes no
note unreadable; [attestation schema 8](#attestation-schema-8) later ends the
reading of schema 7 notes. Reuse
that encounters a different digest reports `doc_surface_changed` and reruns docs;
regular files whose checkout bytes already match Git keep their digest. See
[the migration decision](docs/fingerprint-contract.md#documentation-blob-migration).

## Require response reasons in the CLI

`respond --action dismissed|accepted` now requires `--reason` as well as `--note`.
Omitting the reason exits 6 with `usage_error` before opening or mutating a run.
Older skills and scripts must add a value from the existing reason vocabulary;
use `other` when no specific term fits, and explain the decision in `--note`.
`fixed` still refuses `--reason`. `abort` and the direct Python response API keep
their existing optional-reason behavior. Stored historical findings and events
without reasons remain readable; their telemetry stays `unspecified`. No record,
attestation, fingerprint, or summary schema version changes.

## Hosted escape observations

Summary schema version 1 gains nullable hosted activity counts, failed-check
counts by class, and an optional revert boolean. Old hosted files rebuild with
null new observations until refreshed; saved new observations rebuild without
network access. Unknown is never silently converted to zero.
The user-only `[telemetry.hosted] local_equivalent_checks` mapping does not
affect configuration digests or attestations. Repository values are ignored.
`telemetry sync-hosted` adds `--reverts` and `--reverts-limit` (1–50, default 10).
All GitHub requests remain GETs. Report components have separate named
denominators and are never combined into an escape score.

Optional hosted escape request failures preserve the PR/check outcome and any
escape observations already fetched. The failed observation remains null.
Missing optional resources, malformed responses, and ordinary HTTP errors are
reported in `data.errors`; other observations and runs continue. Authentication,
permission, rate-limit, network, and process failures stop further requests after
saving the partial outcome and rebuilding its summary. Use `--refresh` after
recovery to retry unknown observations in already-synced runs. Core PR/check
fetch failures retain their existing behavior.

## Packaged reviewer orchestration and adjudication

The packaged reviewer adds opt-in `--retries` and `--verify`, with a shared
`--budget-seconds` limit. Defaults retain one discovery call and no verification.
The default total budget stays at `AP_REVIEWER_TIMEOUT` even with retries or
verification enabled; timeout retries need an explicitly larger budget.
Malformed findings and ambiguous or undelivered units fail before submission.
Events now hold versioned call records and wrapped raw output, with rejected
candidates outside findings; status describes the complete review. Legacy copied
wrappers keep raw streams. Eval metrics accept both formats. Request counters
count CLI calls, not internal API requests.

Adjudication schema 1 binds complete labels to an inventory digest. Precision
includes duplicates in its denominator; recall counts distinct seeded defects.
Both have Wilson 95% intervals and null empty denominators. Stage evidence and
fingerprint wire formats are unchanged.

## Event logs are hash-chained

Each event appended to a run's `events.jsonl` now carries `prev_sha256`, the SHA-256 of the bytes of the line before it, or `null` on the first line.
Events that an earlier release wrote have no such key. `events --verify` counts them in `unchained_lines`, and the chain starts at the first line this release writes.
An earlier release that appends to the same run after this release writes a line without the key. That log is then broken, so do not run two releases against one run.
`events --verify` adds `data.chain` and exits 3 with the new error code `event_log_broken` for a broken chain. `events` without the flag is unchanged apart from the new key on each event.
`mergeback` now exits 3 with `event_log_broken`, before it changes anything, when the chain is broken. Abort that run and start a fresh one.
A torn line does not break the chain.
The check catches a partial edit of the log. It does not catch a deliberate rewrite: a removed tail, a log rewritten from the edit to its end, a log with `prev_sha256` removed from every line, or a deleted log. Run schema 2 has no field that records that a log must be chained, and nothing outside the log holds its last digest.
Since [attestation schema 8](#attestation-schema-8), the note holds the log's last digest and line counts at merge-back, so `events --verify` also reports a rewrite of that prefix as `anchor_mismatch`.

New events: `gate_minted`, `push_authorized`, `reviewer_reported`, and `reviewer_report_ignored`.
`push` adds the optional `--authorization user_request|standing_instruction`. Without it, `push_authorized` records `null`.
`reviewer run` now writes one `agentic_preflight_reviewer` JSON line on stderr after a successful review. Its stdout is unchanged. The line is part of the captured review output, so it enters the stage log and its `output_sha256`.
Run schema 2 and fingerprint version 2 are unchanged. These changes add no field to the attestation note; [attestation schema 8](#attestation-schema-8) does.

## Review transcripts

`submit-findings` adds the optional `--transcript PATH`, a UTF-8 text file of at most 1 MiB with the agent's own review notes.
An accepted submission stores a copy with copied-file values redacted as `logs/review-transcript.txt` or `logs/docs-transcript.txt` in the run directory, and replaces an earlier copy for the same stage.
New event: `transcript_recorded`, with `stage`, `sha256`, `bytes`, and `source` `flag`.
The `findings_submitted` event adds `transcript`: the SHA-256 and byte size of the transcript that came with that submission, or `null`. A stage's transcript is the one its last submission names. The file is moved into place after that event, so if the process stops in between, the stored file is still the earlier one or absent; compare its SHA-256 with the digest the event names. A submission without a transcript leaves an earlier file in place, so ignore the file when the event names `null`.
`submit-findings` data adds the optional `transcript_sha256`, `transcript_bytes`, and `transcript_path`. Without `--transcript`, the command behaves as before.
The transcript is the agent's claim. Nothing checks it, and `review run` takes none.
Run schema 2 and fingerprint version 2 are unchanged. The transcript does not enter the stage fingerprint or any evidence digest.
Under [attestation schema 8](#attestation-schema-8), the note records each transcript's digest and size, and an opt-in records bundle publishes the text.

## HTML audit report

`audit` is a new command. It reads a run's record, findings, event log, and the head's attestation note, and writes one self-contained HTML file. Its envelope adds the `audit` data schema.
It leaves the run record, the findings, and the event log unchanged, and appends no event. It writes only the report, by default `audit.html` in the run's directory.
Reading a run still takes the store's locks, so the command needs write access to the run directory even when `--output` points elsewhere.
`--output` inside the repository's Git directory, other than that default file, exits 3 with the new error code `output_refused`, because a report there could overwrite a run record or a Git file. A worktree's `.git` file and a destination that is a directory are refused the same way.
The envelope's `attested` means that the head carries a well-formed note. `note_verified` says whether that note verifies in this clone, as `verify` decides. In a partial clone that lacks the base's `.agentic-preflight.toml`, that check makes Git fetch the file from the promisor remote, as `verify` always has.
The report checks that the run did not change while it was read, and says so in the page when a command was running or the check could not be made. It cannot show that the last command wrote both `findings.json` and `run.json`, because the store writes them as two files.
It reads records from earlier releases, including logs without `prev_sha256` and runs without the newer events, which the report marks as not recorded.
By default the report leaves out absolute local paths and the user-level configuration sections and commands that the published note leaves out.
A path outside the repository, its worktrees, the run store, and the home directory is matched by its shape, so read the report before you share it. A run that does not record which sections came from the user configuration file withholds its whole configuration snapshot and its stage commands.
The report's layout is not a stable interface; parse the run's JSON records or the command envelopes instead.
The command changes no stored schema.

## Attestation schema 8

This is a compatibility break.
`mergeback` writes attestation schema 8, and `verify`, the pre-push hook, `gate`, `push`, and `approval-check` read schema 8 only.
A schema 7 note fails verification with `incompatible_schema`. There is no period in which both schemas are read.
This supersedes every earlier entry in this file that says a note from 0.9.0 or an earlier release still verifies. Those entries describe the releases they were written for.
A schema 8 note must hold four new keys: `producer`, `events`, `transcripts`, and `records`. None may be missing, and each has its own empty form:

- `producer` is an object, or `null` for a run without that record. An empty object is not valid.
- `events` is always an object with `head_sha256`, `lines`, `chained_lines`, and `prefix_sha256`. For a run with no event log it is `{"head_sha256": null, "lines": 0, "chained_lines": 0, "prefix_sha256": null}`. `null` is not valid.
- `transcripts` is always an object. It is `{}` when no stage has a transcript. `null` is not valid.
- `records` is `{"commit": "<SHA>"}`, or `null` when the run published no bundle. An empty object is not valid.

A note without one of them fails verification with `invalid_evidence`.
See [Portable attestations](docs/attestations-and-ci.md) for what each key holds.

Run schema 2, stage evidence version 1, fingerprint version 2, and the configuration digests are unchanged.
None of the new keys enters a fingerprint or an evidence digest, so stage evidence from a run that the previous release finished can still be reused.

Upgrade these together, to the same release or source revision:

- the CLI that writes notes,
- the pre-push hook, which runs that CLI,
- the protected verifier in hosted CI.

A protected verifier on the previous release rejects every schema 8 note, and this release rejects every schema 7 note.
Open pull requests whose head carries a schema 7 note must be validated again with this release, which replaces the note.
The pull request that introduces this release into a repository's protected base must be attested with the previous release.
Its protected base still runs the previous verifier, which reads schema 7 only.
After it merges, the protected verifier reads schema 8, and every later pull request needs a note from this release.

`events --verify` and `mergeback` add the break reason `anchor_mismatch` when the run's head commit carries this run's note and the local log no longer matches the note's `events` anchor.
The recovery is unchanged: abort the run and start a fresh one.

### Records bundle

The new `[records] publish` setting defaults to `false`, so a note's `records` is `null` unless you opt in.
`[records]` is excluded from run snapshots and configuration digests, so adding it changes no existing digest.
The previous release rejects `[records]` as an unknown section. Upgrade every machine that shares a clone before you commit it.
`verify` adds three failure reasons: `missing_records`, `records_mismatch`, and `records_required`.
Its envelope adds `data.records`.
The bundle commit is published under the existing `refs/agentic-preflight/evidence/*` namespace, so the pre-push hook and the documented fetch commands need no change.

### Signature check

`verify` adds the optional `--signature` flag, which needs a SHA.
Its envelope adds `data.signature`, which is `null` without the flag, and `data.statement`, which every `verify SHA` reports.
The protected verifier workflow, `.github/workflows/attestation.yml`, adds a `sign` job with `id-token: write` and `attestations: write`.
A repository that copied the workflow keeps working without the job; its notes read `absent`.
`verify` adds one failure reason, `signature_invalid`.
The flag runs the `gh` CLI. A machine without `gh`, one that is not signed in, or one without access to the forge gets the state `absent` and exit 0.
`data.statement` is `null` when it cannot be computed, and its `root_commit` is `null` in a shallow clone.
Without the flag, `verify` contacts nothing and its result is unchanged.
The note schema, the run schema, and every digest are unchanged.
