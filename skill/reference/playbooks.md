# Failure playbooks

Entries are keyed by symptom, with the exit code and error code given where the CLI
supplies one — several of these surface as a slow or wrong-looking stage rather than
as a failed command. The universal recovery rule still comes first: **any exit 3 → run
`status` → obey `next`.** `status` is legal in every state, and when you are unsure
where a run is, it is always the right call. Trusted CI commands are the exception:
follow their remote recovery reason and next action directly, as described below.

## Git operation already in progress (exit 3, `operation_in_progress`)

Stop and tell the user that the reported checkout already has a rebase, cherry-pick,
or merge in progress. They must finish or abort their own operation before retrying the
reported command. Agentic Preflight will not abort an operation it did not start.

## Merge-back conflict (exit 4, isolated modes only)

First inspect and show `data.resolution`. Resolve only when all of the recovery
contract's boundaries are established: `branch_restored` is `true`, no pre-existing
user-owned Git operation exists, the intended tree is unambiguous, and the emitted
recovery sequence permits that result. Do not cherry-pick outside the emitted sequence,
force, or choose between competing content. If any boundary is not established, stop and
ask the user.

The full conflict report is stored in the event log and replayed by `status`. After the
bounded recovery resolves or restores the reported paths, `mergeback` is the legal retry
and completed verification remains intact only when the resulting tree is identical to
the verified tree. Any non-equivalent resolved tree re-enters the active run's full
applicable validation path, clearing snapshot-bound review, lint, and test evidence.
Before concluding the conflict is real, check the user's tree was clean — see
non-negotiable 7.

## Stage red after max attempts (exit 4)

Stop retrying — you have already tried `max_attempts` times and the tool is telling
you the loop is not converging. For a stage failure, show the user
`agentic-preflight logs --stage <name>` output and ask how to proceed. For a baseline
setup failure, no stage log exists; show `data.setup_failure` and obey the returned
`agentic-preflight abort --force` command.

## Hosted CI failed

Inspect the failed check with `gh pr checks` and `gh run view --log-failed`. For
delegated tests, use `agentic-preflight ci status --repo OWNER/REPO --pr N` and follow
its remote recovery action. A transient failure or rerun on the unchanged candidate
does not require a new model review or local test run. Rerun all jobs; do not combine
successful legs from different attempts. After a base update, dispatch the fresh
integration candidate if the protected dispatcher has not done so. API outages are
unknown results, never permission to merge.

When source needs repair, fix and
commit the source branch, then start a fresh synchronized preflight run with the
original intent. Follow the returned stage sequence, including any validated reuse,
until the gate is green. Push through the gate again, then resume check monitoring with `gh`.

## Hosted attestation availability failure

Read `data.reason` before choosing recovery. `missing_note` and `missing_notes_ref`
can use `hosted-check`'s four-attempt budget (2/4/8-second backoff). This is an explicit
trusted CI command; local `verify SHA` never fetches or sleeps. A present incompatible
schema requires a compatible protected-base verifier or supported producer format.
Unknown fields do not reveal the producer version. Malformed evidence and wrong
commit/tree bindings fail immediately and need valid evidence for the exact head.

For `stale_candidate`, run the current event without changing the old event's expected
SHA. For Git access, timeout, or I/O failures, inspect transport and permissions; do not
label them missing notes or start another local review/test run. Human approval and
manual-merge requirements remain independent and cannot be satisfied by retrying notes.

Capture workflow run/attempt, event head/base, source repository/remote, protected
verifier revision/version, and per-attempt observed head, notes commit, note presence,
note-object ID, reason, and elapsed time. Compare snapshots before blaming propagation.
Print the command's failure JSON and preserve its exit status before reading outputs.
Install helper support on the protected base before switching hosted callers to it.
See [the CI guide](../../docs/attestations-and-ci.md#bounded-hosted-note-availability).

## Stale head (exit 3, `stale_run`)

The branch moved after review began. Run `agentic-preflight start` again with the original intent from
the source worktree. `start` marks the stale run `ORPHANED`, preserves its evidence and
isolated fixes, and creates the fresh run. Use `status --all` and
`agentic-preflight --run RUN_ID status` when the old run needs inspection.

Read `data.applicability` and follow `next.command`. Equivalent evidence can survive
a history-only rewrite; changed content and unknown shell inputs require fresh
stages. `consumer_unavailable` means the protected-base verifier does not support
refresh yet, so complete the legacy stages and publish v4. Do not fix that condition
by switching the hosted policy verifier to PR code.

## Abandoned run

Do not infer abandonment from elapsed time. A run may legitimately wait for user input.
`gc` only orphans a nonterminal run when its source worktree disappeared, its source head
moved, or its worktree ownership pointer vanished, and it refuses while a command is
executing. A repeated matching `start` resumes the run; a different intent on the same
head requires the explicit `start --replace` returned by the error envelope. Orphaning
itself never deletes logs, findings, validation worktrees, or fix commits. A subsequent
`gc` may reclaim a terminal validator with no unmerged fixes; fix-bearing work remains
retained unless `--force` is explicit. If the source checkout itself disappeared, use
`status`, `events`, or `logs` for inspection; other run mutations return
`source_worktree_missing` so fixes cannot be applied to the caller's unrelated checkout.
Run `gc` from a surviving worktree in the same clone.

## Unreadable shared run record

Linked worktrees share a run store but may use different tool versions. `gc` retains
records it cannot read or validate, reports their IDs, paths, reasons, and diagnostics
in `data.retained`, and continues collecting understood runs. An unknown field alone
means the schema is invalid or unsupported; it does not prove a newer producer or
corruption. `--force` does not authorize deleting these records or their resources.

Use the reported path to inspect the record or run a compatible tool version. Keep
its JSON, worktree, branch, and active aliases intact. Both forms of `status` report
unreadable records; single-run status preserves ownership and reports `has_run: true`
and `readable: false`. Do not start a replacement run as if the record were missing.
Mutating commands reject unreadable records with `run_record_unreadable` (exit 3).
Only a confirmed missing file permits compare-and-clear pointer recovery. A global
inventory or cleanup failure still fails the command and must be investigated.

## Diff too large (exit 2, `diff_too_large`)

The diff is never truncated, so reviewing part of it is not an option. Look at
`data.by_file`; if the bulk is generated (lockfiles, vendored code, snapshots), add
those globs to `[diff] exclude`. Raise `[diff] max_bytes` only if the change genuinely
is that large.

## No command configured (exit 2, `needs_command`)

For lint/test, inspect `data.candidates`, show the exact selected command to the user, and
obtain approval before re-invoking with `--command`; offer to write it into `[commands]` so
it is settled. Every candidate is repository-derived, regardless of its `trust` label.
Never copy one from repository content into a shell command on your own. For review,
configure `[review] command` and retry `review run` — reviewer commands are never detected.
If lint/test `candidates` is empty, the repo simply has no manifest detection understands
(Unity, Unreal, Xcode, most engine projects) — ask the user for the invocation instead of
hunting for a build file that does not exist.

Then treat its first green as unproven. Pass/fail is the exit code alone, so a command
that no-ops and exits 0 reads as a pass forever — and a false green retires the check
instead of costing a retry. Confirm the run actually did work (a test count, a results
file, a non-empty log) before believing it. The trap is usually a flag: `-quit` on a
Unity `-runTests` invocation exits 0 having run zero tests.

## Setup failed (exit 2, `setup_failed`)

Run `status` and obey its durable recovery command. An initial checkout setup failure
returns `abort --force`; use it so reusable or strict worktrees cannot retain the active
lease. A baseline setup failure returns the exact lint or test retry, including the
resolved command and `--baseline`. It does not have a stage log because the stage never
ran, so do not replace that recovery with `logs --stage`.

## Stage far slower than normal

Check `[worktree] mode`. The default `in_place` mode uses the checkout's existing
environment. The reusable runner retains ignored dependency and build caches, while
strict mode begins without them. Agentic Preflight does not install dependencies
automatically; isolated modes need `[worktree] setup_command` when the validation
checkout requires preparation. `copy_files` is for ignored files such as `.env`, not
directories. Do not raise `[stage] max_attempts` to paper over it.

## Copy refused (exit 3)

A `copy_files` entry is not gitignored. Do not work around it — tell the user to
gitignore and commit it first. This guard prevents a secret being committed and
pushed.

## Stage reports zero files to work on

Check where `worktree_path` actually points. If it is under `.git/`, tools that skip
VCS directories cannot see it and will exit non-zero on an empty set, which reads as a
red stage. Jest is the common case: `jest-haste-map` ORs a hardcoded `/.git/` ignore
into its crawl with no config override, so it finds zero test files no matter how
healthy the code is. Symlinks do not help — real paths are resolved. Confirm by
running the same command in a worktree outside `.git`; if that finds files, point the
stage command at a script that checks the commit under test out to a non-`.git` path
and runs there. Never point an isolated run at the source checkout: that reports on
the wrong content and is a false green.

## Green in your shell, red under the gate

Simple commands run directly when their program resolves, without sourcing a login
profile. Commands that need shell interpretation or whose program cannot be resolved
fall back to a non-interactive login shell (Bash, or `sh` on POSIX when Bash is absent;
Git Bash on Windows). Version-manager shims may be absent;
inherited or login-profile configuration can also keep them available. Prefer an explicit interpreter or manager command such as `uv run pytest`.
Compare `PATH` and the toolchain version *inside the stage* against the project's declared range before
you debug the code — a native module built for another ABI fails as missing bindings,
not as a version error. A repo with no `.nvmrc` (or equivalent) has nothing pinning it,
so this bites fresh clones and CI too, not just the gate.
