# Worktree modes

This page describes where a run validates and what the run can change while it does so.

## Concurrent source worktrees

Active ownership is scoped to the source worktree's private Git directory.
Thus, linked worktrees in one clone have independent runs.
One agent can wait for review input while another agent reviews, tests, or publishes another PR branch.
Per-run state changes stay serialized.
The shared Git-notes ref is reconciled under a narrow publication lock, so concurrent attestations are preserved.

`agentic-preflight status` resolves the run that the invoking worktree owns.
Use `status --all` to list runs across the clone.
Place `--run RUN_ID` before a command to select a stored run explicitly.
A command selected that way operates on the recorded source checkout when it still exists.
It does not change the unrelated worktree that invoked it.
If that source checkout was deleted, `status`, `events`, and `logs` stay available for inspection.
Other mutating commands fail with `source_worktree_missing` and direct recovery to `gc`.
`gc` is the exception. Run it from a surviving worktree in the same clone.
It can orphan the run and reclaim eligible validation resources. It keeps the run directory and the audit logs.
A `RUN_ID` must be `r_` followed by 10 lowercase hex digits.
Any other value fails with `invalid_run_id` and exit code 6 before the run store is opened.

A run's event log tolerates a torn final line that an interrupted append left.
`events` and `status` drop that line and read the rest.
A malformed earlier line is reported as `run_record_unreadable` with reason `invalid`.

If a stored record cannot be read or validated, `status` reports its identity and diagnostic.
It does not clear the ownership pointer.
`status --all` includes the record as unreadable. It does not call the record corrupt.
`gc` keeps the record and its resources, even with `--force`, while it collects other eligible runs.
Saved configuration snapshots ignore keys that a later release removed. Those keys do not make a run unreadable.
For readable nonterminal runs, `gc` can mark a run orphaned when its source worktree or lease disappears, or when its source head moves after startup.
It does not orphan a run while a run command is still executing.
It keeps isolated resources that contain unmerged fixes unless you use `--force`.
In-place source commits stay intact.
Inspect the reported path. Never delete an alias to make an unreadable run look absent.

A repeated `start` with the same head, intent, base, and effective configuration resumes the matching run.
If the source head moved, `start` marks the old run `ORPHANED` and continues.
It does not delete the old run's evidence, validation worktree, or fix commits.
A different intent on the same unchanged head is ambiguous. It requires `start --replace`.
Replacement has the same preserve-first behavior.
No run expires because it is old.

`reusable` mode has one cached validation worktree. It is serial among its users by design.
That resource lease does not block `in_place` or `strict` runs in other source worktrees.
[ADR 0002](adr/0002-scope-run-ownership-to-worktrees.md) records the ownership and lock-boundary decision.

## `in_place` (default)

Work occurs directly in the current checkout.
This mode is intended for a clean, dedicated worktree with one agent and one PR.
The fresh-base rebase lands directly on the PR branch. So do accepted repair commits.
When a rebase would change what the branch lands, a base merge replaces the rebase.
`mergeback` becomes a no-op attestation of the exact SHA whose local stages passed, were explicitly skipped, or supplied applicable reused review or documentation evidence.
Applicable shell stages complete locally before publication.

An uncommitted change or an unaccounted branch movement stops the run.
In-place mode reuses the checkout's existing dependency environment. It does not run an automatic install.
An explicit `setup_command` still runs.

## `reusable`

This mode leases one validation worktree under the user cache directory.
See [Where isolated worktrees live](#where-isolated-worktrees-live).
The lease is serial across runs.
The mode preserves ignored dependency and build caches.

The runner belongs to the clone, not to the checkout.
Runs started from the main checkout and from any linked worktree of the same clone lease the same runner.
Its parent directory is named after the clone's repository directory plus a hash of the Git common directory, and the runner is the `runner` entry inside it.
Strict-mode worktrees are `<run-id>` entries in the same parent directory.
Thus, clones with identical names stay separate.

`agentic-preflight gc` removes a released runner and prunes its Git registration in two cases:
when no run record that used it still has an existing source worktree, or when its registered path no longer exists.
A runner that is checked out on a branch is never removed.
A runner whose run still has a live source worktree is never removed.

Runner reclamation preserves locked worktrees, tracked or non-ignored untracked changes, and runners of retained runs, including unmerged fixes.
Ignored caches in an otherwise eligible runner are disposable.
A refused Git removal preserves the runner. The directory is not deleted directly.

Between leases, the mode resets tracked files and removes non-ignored untracked files.
It explicitly removes every `[worktree] copy_files` entry. Then it detaches the validation worktree.
Other ignored files are kept, so dependency and build caches survive.

**This is not a hermetic environment.** A test can change an ignored cache.
The mode reduces local disk churn. It does nothing more.

## `strict`

This mode creates a fresh worktree for every run and removes it afterward.
Use it when each local validation must begin with no retained artifacts.

Remote CI must stay the clean verification boundary in both isolated modes.

## Where isolated worktrees live

Both isolated modes default to one directory per clone under `$XDG_CACHE_HOME/agentic-preflight/worktrees/`.
When `XDG_CACHE_HOME` is unset or is not an absolute path, the default is `~/.cache/agentic-preflight/worktrees/`.
The location is outside `.git`, so tools such as Jest discover tests.
Set `[worktree] root` to select another location. It must be outside the repository.

### Migrating from the sibling default

Earlier releases put worktrees in a hidden `.agentic-preflight-worktrees/` directory beside each checkout.
New runs use the cache location.
`agentic-preflight gc` reclaims eligible unused reusable runners in both locations under the same preservation rules.
`data.legacy_worktrees` reports the registrations that remain at the old sibling location after reclamation.
Inspect each one before you remove it with `git worktree remove <path>`.
An explicitly configured root that uses the old directory name stays supported. It is not reported as legacy.

## Seeing how much space checkouts hold

Every `start` reports `data.housekeeping`.
The report gives the size of each directory under the worktrees root and of each `ap/*` worktree.
It marks each one as `retained` or `reclaimable` and gives the totals.
A checkout is retained when it is leased, or when a run whose source worktree still exists names it.
A retained reusable runner is kept.
When at least 1 GiB is reclaimable, `noisy` is true and the agent tells you once.
`agentic-preflight gc` reclaims the space.
The report never deletes anything. A failure in the report never fails `start`.

## Why isolated validation worktrees live outside `.git`

Both isolated modes keep the source checkout untouched during verification.
Both put the validation worktree outside `.git`, so tools that ignore VCS directories can still see it.
Jest is the common case.
`jest-haste-map` adds a hardcoded `/.git/` ignore to its crawl, with no configuration override.
A validation worktree inside `.git` finds zero test files, regardless of the code.

## Switching modes

Commit one of these settings and start a new run.
Each active worktree run keeps the configuration snapshot that it started with.

```toml
[worktree]
mode = "in_place" # default; validate and repair directly in this clean PR checkout
```

```toml
[worktree]
mode = "reusable" # one serial validation worktree; retained ignored caches
```

```toml
[worktree]
mode = "strict"   # fresh worktree with no retained artifacts
```

The first strict run removes an idle reusable validation worktree.
Thus, a later switch back to reusable mode starts with no retained cache.
In-place mode leaves an idle reusable validation worktree alone.

## Secrets in worktrees

Files in `[worktree] copy_files` are used in place or copied into an isolated validation worktree, so tests can run.
Two independent guards protect them:

1. **Preflight refusal.** A file that Git does not already ignore in the validation worktree is never used or copied.
   Add it to `.gitignore` and commit that first.
2. **Commit-content invariant.** `respond` and `mergeback` reject a commit that touches a copied path.
   The check uses the commit content, not the ignore rules.
   Thus, a `.gitignore` edit during a run cannot open the hole.

Isolated copies are owner-only: `0600` on POSIX, and a restricted ACL on Windows.
They are removed explicitly when a reusable validation worktree is released. They are deleted with a strict worktree.
In-place files are never moved or removed.
Dotenv assignment values are redacted when they appear verbatim in captured stage output.
This includes exported, quoted, multiline, and short non-empty values.
Redaction occurs before the output is written to a log or envelope.
This is exact-value redaction. It is not a general secret scanner.
Arbitrary copied-file formats and transformed, encoded, interpolated, or derived values are outside this guarantee.

`copy_files` is for ignored files such as `.env`. It is not for directories.

## Preparing the validation worktree

Agentic Preflight does not install dependencies automatically.
Configure `[worktree] setup_command` when a validation worktree needs preparation.
Examples are `uv sync`, `npm ci`, and `pnpm install --frozen-lockfile`.

The command runs before review in every worktree mode.
It also runs before a `--baseline` stage in its scratch worktree.
A nonzero exit stops the run.
A failed baseline setup is reported as a setup failure. It is not evidence that the base commit is red.

Setup runs through the stage runner with the `[stage] timeout_seconds` limit.
On timeout, the whole process group is killed and the result is exit 124.
Initial setup output is redacted like stage output. It is written to `setup.txt` in the run's log directory.
A failed setup envelope reports `timed_out`, `log_path`, and the tail of the output.
A setup command that writes to a copied file fails, and its output is withheld.
On Windows, Ctrl+C cleans up the setup process tree before cancellation propagates.

Setup failures stay recoverable after the original error scrolls away.
After the initial checkout setup fails, `status` returns `abort --force`. The abort releases an isolated lease.
After a baseline setup fails, `status` keeps the command and the exit code.
It returns the exact stage retry with `--baseline`. It never points at a stage log that was not created.

An isolated mode never copies dependency directories from the source checkout.
Reusable mode keeps ignored dependency and build caches between leases. The setup command still runs.
Strict mode begins without retained artifacts.

## When a stage is much slower than expected

Check the mode first.
Strict mode has no retained build cache.
Reusable mode preserves ignored caches between leases.
In-place mode uses the current checkout.
Do not raise `[stage] max_attempts` to hide a mode-shaped problem.
