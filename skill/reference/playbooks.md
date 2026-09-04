# Failure playbooks

Entries are keyed by symptom, with the exit code and error code given where the CLI
supplies one — several of these surface as a slow or wrong-looking stage rather than
as a failed command. The universal recovery rule still comes first: **any exit 3 → run
`status` → obey `next`.** `status` is legal in every state, and when you are unsure
where a run is, it is always the right call.

## Git operation already in progress (exit 3, `operation_in_progress`)

Stop and tell the user that the reported checkout already has a rebase, cherry-pick,
or merge in progress. They must finish or abort their own operation before retrying the
reported command. Agentic Preflight will not abort an operation it did not start.

## Merge-back conflict (exit 4, isolated modes only)

The branch has already been restored exactly and your fix commits are safe in the
worktree. Paste `data.resolution` to the user verbatim and **stop**. Do not
cherry-pick, do not force, do not pick a side. A conflict is a content decision and
it is not yours to make.

The full conflict report is stored in the event log and replayed by `status`. After a
person resolves or restores the reported paths, `mergeback` is the legal retry and
completed verification remains intact when the resulting tree is still identical to
the verified tree. A different tree resets the validation checkout and active run to
review, clearing the old review, lint, and test evidence. Before concluding the conflict
is real, check the user's tree was clean — see non-negotiable 7.

## Stage red after max attempts (exit 4)

Stop retrying — you have already tried `max_attempts` times and the tool is telling
you the loop is not converging. For a stage failure, show the user
`agentic-preflight logs --stage <name>` output and ask how to proceed. For a baseline
setup failure, no stage log exists; show `data.setup_failure` and obey the returned
`agentic-preflight abort --force` command.

## Hosted CI failed

Inspect the failed check with `gh pr checks` and `gh run view --log-failed`. Fix and
commit the source branch, then start a fresh synchronized preflight run with the
original intent. Do not push the repair until the new review → docs → lint → test run
reaches green. Push through the gate again, then resume check monitoring with `gh`.

## Stale head (exit 3, `stale_run`)

The branch moved after review began, so everything verified so far describes a tree
that no longer exists. Run `agentic-preflight start` again with the original intent from
the source worktree. `start` marks the stale run `ORPHANED`, preserves its evidence and
isolated fixes, and creates the fresh run. Use `status --all` and
`agentic-preflight --run RUN_ID status` when the old run needs inspection.

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

Stages run through a non-interactive login Bash shell. Its `PATH` can differ from your
interactive shell: version-manager shims (nvm, rbenv, pyenv, asdf) may be absent, but
inherited or login-profile configuration can also keep them available. Compare `PATH`
and the toolchain version *inside the stage* against the project's declared range before
you debug the code — a native module built for another ABI fails as missing bindings,
not as a version error. A repo with no `.nvmrc` (or equivalent) has nothing pinning it,
so this bites fresh clones and CI too, not just the gate.
