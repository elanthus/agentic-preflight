# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, 3.13, and 3.14 on macOS 15 or newer, on Linux, and on Windows 10 or newer.
Git 2.38 or newer is required.

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
A run that an earlier release already merged back has a note that may hold user-level configuration; `gate` and `push` refuse it, and the error names the `git notes` command that removes that note.
Abort the run and start a fresh one after upgrading. In isolated modes, cherry-pick any recorded fix commits (`data.fix_commits`) first, because `abort --force` discards them.
The field makes every new run record unreadable to earlier releases, which forbid unknown keys.
Before downgrading, finish or abort the runs that this release started. A record that an earlier release wrote stays without the field and stays readable to that release.

## Compatibility changes

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

Saved schema-v2 runs can contain the former `mergeback_attempt.rebased_tree` field.
It is accepted and ignored on load. It is omitted on the next write.
Other unknown attempt fields stay invalid.
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

Historical run records discard the retired delegation field.
Pending delegated runs become `ORPHANED` and require a new local validation run.
Pushed and terminal records stay readable for inspection and cleanup.
Run history is preserved.

## Saved pending run updates

Agentic Preflight 0.7.0 removes the local run and findings write-ahead journal.
An interrupted nonterminal run with `pending-update.json` cannot resume validation.
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

The sections from here to the end of this file describe the 0.8.0 changes.
[Evaluation report compatibility](#evaluation-report-compatibility) and [Opt-in contract-check evidence](#opt-in-contract-check-evidence) above are also new in 0.8.0.

## Producer identity on run records

New schema-v2 run records carry an optional `producer` object.
It records the tool version, the Python version, the platform, the coding-agent client, and the model that the agent reported.
Records that earlier versions wrote load with `producer` absent, and a rewrite of such a record stores `producer` as `null`.
Run schema 2, attestation schema 7, fingerprints, and configuration digests are unchanged. None of them contain the producer.

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

## Unreleased: repositories without a network remote and telemetry

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

## Unreleased: attestation verification reads the protected base

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

## Unreleased: reviews re-run the hosted approval check

`human-approval.yml` no longer listens to `pull_request_review`.
Two new workflow files handle reviews: `human-approval-review.yml` records the reviewed pull request number, and `human-approval-rerun.yml` re-runs each protected-base policy run for the head.
A project that copied the hosted workflow must add both files, or a review will not update the `high-risk human approval` check until someone re-runs the failed run by hand.
The required check keeps its name. No ruleset change is needed.
The re-run companion needs `actions: write`; it runs from the default branch and never checks out pull request code.
The policy run now uploads its evaluation as the `human-approval-policy` artifact, kept for 30 days; the companion reads the approval mode from it and leaves a run without one alone.
It re-runs only runs whose record shows a review mode and a required human approval.
GitHub allows a re-run for 30 days after the original run. An older pull request needs a new push before approval can land.
The `approval-check` command, its outputs, and the attestation schema are unchanged.

## Unreleased: documentation inventory lists committed files

The documentation inventory now lists only files committed at the head commit.
Files not committed at the head commit that match a documentation pattern, such as a Sphinx `docs/_build/` tree or a nested worktree under `.claude/worktrees/`, no longer appear in `doc_surface`, in the docs allowlist, or in the docs fingerprint.
A docs finding against such a file is now rejected as outside the allowlist.
In a repository with such files, `doc_surface_sha256` changes, so docs evidence recorded before the upgrade does not carry to a new head and the docs stage runs again.
In a repository without them, the inventory and its digest are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Unreleased: repository telemetry settings can only turn telemetry off

A `[telemetry]` section in `.agentic-preflight.toml` no longer replaces the user's section.
Its `path` is ignored, and every command that loads the configuration prints a warning that names the file. The pre-push hook stays silent.
Its `mirror = true` and `hook_log = true` keep the user's value. Its `mirror = false` and `hook_log = false` still apply.
A repository that set `path` to collect its contributors' telemetry no longer does so. Each contributor who wants it must set `path` in their own user file.
A test harness that turns telemetry off with a repository section, as [Isolate a test harness](docs/local-telemetry.md#isolate-a-test-harness) describes, is unaffected.
Invalid `mirror` and `hook_log` values and unknown keys in the repository section are still errors. Its `path` is ignored whatever its value.
`mirror` and `hook_log` now accept only `true` and `false`, in either file. Earlier releases also accepted values such as `1`, `"yes"`, and `"on"`. Such a value is now an error that names the file.
Attestation verification reads the protected base's `[telemetry]` section by the same rule, so a `path` there no longer fails verification.
No configuration digest, snapshot, fingerprint, or attestation changes, because none of them contains `[telemetry]`.

## Unreleased: event logs skip torn lines

An event appended after an interrupted append now starts on a new line instead of joining the torn line.
A run's event log line that is not a JSON object, such as a torn line, is now skipped wherever it appears.
A line that is JSON but not an object, such as `7`, used to make `events` fail with `internal_error`.
Earlier releases skipped only a torn final line and reported a malformed earlier line as `run_record_unreadable` with reason `invalid`, from `events`, from `status` in `MERGEBACK_CONFLICT`, and from `telemetry rebuild`.
The `events` envelope adds `data.skipped_lines`, the number of lines skipped.
Run schema 2, attestation schema 7, and the event format are unchanged.

## Unreleased: push publishes only the pushed commits' notes

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

## Unreleased: run and findings records are validated before they are written

The run store parses each run and findings record back through its loader before writing it, and writes neither file of a transaction when either is invalid.
Such a refusal is reported as `internal_error`; the earlier files stay unchanged.
`respond --note` longer than 4,000 characters now exits 6 with `usage_error`, and the Python `respond` API raises `invalid_response`. Earlier releases exited 0 and wrote a findings record that could not be read.
A `run_record_unreadable` error envelope from a mutating command adds `data.pointer_paths`, a list of ownership pointer file paths, when the record names the `in_place` or `strict` worktree mode. When the mode is `reusable` or the record names the reusable runner, the key is absent and `next.instruction` says to keep the runner and stop. When the mode cannot be determined, the key is absent and `next.instruction` says to keep the record and its pointers and stop. When no pointer names the run, the plain recovery applies.
Single-run `status`, `status --all`, and `gc` output are unchanged.
Run schema 2, attestation schema 7, fingerprint version 2, and the findings format are unchanged.

## Unreleased: `logs` returns a bounded window

`logs --stage` no longer returns the whole stage log by default.
`data.output` now holds the last 200 lines, cut to at most 64 KiB.
The `logs` envelope adds `data.truncated`, `data.total_bytes`, and `data.total_lines`, which describe the whole stored log.
New flags choose the window: `--tail N`, `--head N`, and `--all`. `--all` restores the earlier whole-log output.
A wrapper that read the whole log from `data.output` must pass `--all` or read the file at `data.log_path`.
The stored log, its redaction, and its location are unchanged.
Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.

## Unreleased: grounding reads the merge base

`data.grounding` now reads `docs/**`, `AGENTS.md`, `CLAUDE.md`, the files that `[context] extra_paths` selects, and the `[context]` section itself from `.agentic-preflight.toml` at the run's merge base.
Earlier releases read them from the branch head and the working-tree configuration.
When the merge base sets no `[context]` section, the user file's section applies.
A branch's edits to those files reach the reviewer only through the diff, and a branch's `[context]` change applies to later runs after it merges.
For a branch that edits those files or `[context]`, the grounding and `grounding_sha256` change, so review and docs evidence recorded for that branch before the upgrade is not reused and those stages run again.
For other branches the grounding is unchanged.
Run schema 2, attestation schema 7, fingerprint version 2, and the configuration digest are unchanged.

## Unreleased: hook path names a symlink, not its target

`init` now replaces a symlink at the pre-push hook path with a regular file instead of writing through it.
A dangling link, or a link to exactly the current hook script, is replaced without `--force`. Any other link to an existing file is judged by that file's content, as before.
`data.hook_path` from `init` and the hook path that `status` reports now name the hook path itself, without following a symlink there.
Earlier releases reported the link's target, or the missing target of a dangling link.
The hooks directory, including `core.hooksPath`, is still resolved to an absolute path.

## Unreleased: hosted workflow hardening

The `high-risk human approval` job now fails unless the policy job reports `requires_approval` as exactly `true` or `false`.
The checkouts in `attestation.yml` and `human-approval.yml` set `persist-credentials: false`. A private repository that copies them must authenticate the later `git fetch` itself; see [Portable attestations and CI enforcement](docs/attestations-and-ci.md#required-github-check).
`human-approval-rerun.yml` now has an `authorize` job and a `rerun` job, and re-runs the policy only for a review from an owner, member, or collaborator, an action by a user with write access, or an action by a bot account that has dismissed an owner, member, or collaborator review of the pull request. When a lookup that decides this fails, it re-runs the policy with a warning instead of skipping it. A project that copied the companion should copy the new version.
The required check keeps its name, and `approval-check`, its outputs, and the attestation schema are unchanged.

## Unreleased: the reviewer's own files need human review

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

The packaged reviewer (see the next section) runs from the installed package, and the example reviewer configurations start it with `agentic-preflight reviewer run`. Run a wrapper copied from an earlier release with `python3 -S -B`.
The Claude reviewer now passes `--safe-mode` and `--setting-sources user`, checked against Claude Code 2.1.292.
The Codex reviewer now passes `--config project_doc_max_bytes=0` and `--ignore-rules`, checked against Codex CLI 0.163.0.
An older CLI that lacks one of these options fails the review with a visible error. Update the CLI, or remove the option from your copy of the wrapper and protect the files that it would read with `trusted_paths`.
With `--safe-mode`, the Claude reviewer also ignores your user `CLAUDE.md`, skills, hooks, and MCP servers.

## Unreleased: packaged command reviewer

`agentic-preflight reviewer run --provider claude|codex` is a new command.
Like `hook-check`, it prints no JSON envelope and keeps Click's usage output and exit code 2 on a usage error.
Its stdout, stderr, exit codes, and `AP_*` environment variables are those of the former example wrappers.
Unlike them, it reads its own stdin and writes its own stdout and stderr as UTF-8 rather than in the locale encoding, which matters on Windows. The pipe to the provider CLI is unchanged and still uses the locale encoding. Like them, it does not need Git: commands in the `reviewer` group skip the Git version check.
The example wrappers in `docs/examples/reviewers/` are deprecated pointers to it for one release.
A configured command that runs a pointer needs an interpreter that can import `agentic_preflight`.
The configuration file, the review submission, and the attestation schema are unchanged.
