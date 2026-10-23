# Compatibility policy

Agentic Preflight supports Python 3.11, 3.12, and 3.13 on macOS 15 or newer, on Linux, and on Windows 10 or newer.
Git 2.38 or newer is required.

Windows support is native.
It does not use WSL. It does not require a POSIX shell for ordinary use.
Know these two Windows-specific points before you adopt it:

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

- Pull requests and pushes to `main` run on `ubuntu-latest` with Python 3.13. This conserves GitHub Actions minutes.
- A scheduled regression run covers the oldest supported boundary, macOS 15 with Python 3.11, every Monday and Thursday.
- Manual CI runs and release tags cover Python 3.11, 3.12, and 3.13 on `ubuntu-latest`, `macos-latest`, and `windows-latest`.

A platform is supported even when it is not in the pull-request job.
A failure that is specific to a supported combination is a release blocker.
Fix it with the same priority as a pull-request CI failure.

Other POSIX systems and newer Python prereleases can work.
They are best effort until they are added to the supported matrix.
A successful installation on a version outside the matrix does not make that version supported.

## Upgrading from 0.6.0 to 0.7.0

Follow the [upgrade guide](docs/upgrade-0.7.md) for the rollout order, configuration cleanup, saved-run inspection, verifier alignment, and rollback limits.
Run schema 2, attestation schema 7, and fingerprint version 2 are retained.
The schema number alone does not guarantee that an older verifier accepts new policy fields or evidence.

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
A configuration file that sets such a key is still rejected.

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
See [Temporary repositories](docs/local-telemetry.md#temporary-repositories).

An explicit `[telemetry] path` is an opt-in, and keeps the earlier behavior for every repository.
`[telemetry] mirror` and `hook_log` are unchanged.
Rows that the mirror already has are not removed.

The `summary_written` event and `data.telemetry` add `reason`: `temporary_repository`, or `null`.
The `mirror` object of `telemetry rebuild` and of `telemetry sync-hosted` adds `reason` and `temporary_runs`.
In `agentic-preflight report`, `data.source.clone` adds `temporary_runs`, and `unmirrored_terminal_runs` no longer counts those runs.
The summary schema 1, the hook decision lines, run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
No fingerprint, evidence digest, configuration digest, or attestation reads these keys.
