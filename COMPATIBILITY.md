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

New plans and enabled receipts use schema `"2"` in private run artifacts. Checked
units require a behavioral inventory with input-class probe references or explicit
gaps. Checks require structured oracle provenance. Update the worked wrappers and
their shared helper together with the product. Schema-1 receipts remain readable
without changing their digest-bound plan representation. They cannot satisfy a new
findings submission: context preserves their reference under `previous_receipt` and
requests a schema-2 plan. Existing completed evidence is not reinterpreted as schema 2.

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
