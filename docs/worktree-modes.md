# Worktree modes

Where a run validates, and what it is allowed to touch while doing so.

## Concurrent source worktrees

Active ownership is scoped to the source worktree's private Git directory. Linked
worktrees in one clone therefore have independent runs: one agent can wait for review
input while another reviews, tests, or publishes another PR branch. Per-run state changes
remain serialized, and the shared Git-notes ref is reconciled under a narrow publication
lock so concurrent attestations are preserved.

`agentic-preflight status` resolves the run owned by the invoking worktree. Use
`status --all` to list runs across the clone, or place `--run RUN_ID` before a command to
select a stored run explicitly. Commands selected that way operate on the recorded source
checkout when it still exists; they do not mutate whichever unrelated worktree happened
to invoke them. If that source checkout was deleted, `status`, `events`, and `logs` remain
available for inspection, and other mutating commands fail with `source_worktree_missing`
and direct recovery to `gc`. `gc` is the exception: run from a surviving worktree in the
same clone, it removes the run and its validation worktree.
A `RUN_ID` must be `r_` followed by 10 lowercase hex digits; any other value fails with
`invalid_run_id` and exit code 6 before the run store is opened.

A run's event log tolerates a torn final line left by an interrupted append: `events`
and `status` drop that line and read the rest. A malformed earlier line is reported as
`run_record_unreadable` with reason `invalid_events`.

If a stored record cannot be read or validated, `status` reports its identity and
diagnostic without clearing its ownership pointer. `status --all` includes it as
unreadable rather than calling it corrupt. `gc` retains the record and its resources,
even with `--force`, while collecting other eligible runs. Inspect the reported path;
never delete an alias merely to make an unreadable run look absent.

A repeated `start` with the same head, intent, base, and effective configuration resumes
the matching run. If the source head moved, `start` marks the old run `ORPHANED` and
continues without deleting its evidence, validation worktree, or fix commits. A different
intent on the same unchanged head is ambiguous and requires `start --replace`; replacement
has the same preserve-first behavior. No run expires merely because it is old.

`reusable` mode still has one cached validation worktree and is intentionally serial among
its users. That resource lease does not block `in_place` or `strict` runs in other source
worktrees. [ADR 0002](adr/0002-scope-run-ownership-to-worktrees.md) records the ownership
and lock-boundary decision.

## `in_place` (default)

Work happens directly in the current checkout. This is intended for a clean, dedicated
one-agent/one-PR worktree: the fresh-base rebase and accepted repair commits land
directly on the PR branch, and `mergeback` becomes a no-op attestation of the exact SHA
whose local stages passed, were explicitly skipped, or supplied applicable reused
evidence. With CI test authority, it records publication readiness with tests pending.

Any uncommitted change or unaccounted branch movement stops the run. In-place mode reuses
the checkout's existing dependency environment and does not run an automatic install; an
explicit `setup_command` still runs.

## `reusable`

Leases one validation worktree in a hidden sibling directory, serially across runs, preserving ignored
dependency and build caches.

Between leases it resets tracked files, removes non-ignored untracked files, explicitly
removes every `[worktree] copy_files` entry, and then detaches the validation worktree. Other ignored
files are kept so dependency and build caches survive.

**This is not a hermetic environment**: a test can mutate an ignored cache. It reduces
local disk churn, nothing more.

## `strict`

Creates a fresh worktree for every run and removes it afterward. Use it when each local
validation must begin with no retained artifacts.

Remote CI should remain the clean verification boundary in either isolated mode.

## Seeing how much space checkouts hold

Every `start` reports `data.housekeeping`: the size of each directory under the
worktrees root and each `ap/*` worktree, whether it is `retained` (leased, or owned by
a live run) or `reclaimable`, and the totals. When at least 1 GiB is reclaimable,
`noisy` is true and the agent tells you once; `agentic-preflight gc` reclaims it. The
report never deletes anything, and its failure never fails `start`.

## Why isolated validation worktrees live outside `.git`

Both isolated modes keep the source checkout untouched during verification, and both put
the validation worktree outside `.git` so that tools ignoring VCS directories can still see it. Jest is
the common case: `jest-haste-map` ORs a hardcoded `/.git/` ignore into its crawl with no
config override, so a validation worktree inside `.git` finds zero test files no matter how healthy the
code is.

## Switching modes

Commit one of these and start a new run — each active worktree run keeps the configuration
snapshot it started with:

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

The first strict run removes any idle reusable validation worktree. Switching back to reusable mode
therefore starts with no retained cache. In-place mode leaves an idle reusable validation worktree
alone.

## Secrets in worktrees

Files in `[worktree] copy_files` are used in place or copied into an isolated validation worktree so
tests can run, and are protected by two independent guards:

1. **Preflight refusal:** a file git is not already ignoring in the validation worktree
   is never used or copied. Add it to `.gitignore` and commit that first.
2. **Commit-content invariant** — any commit touching a copied path is rejected by both
   `respond` and `mergeback`, checked against commit content rather than ignore rules, so
   a `.gitignore` edited mid-run cannot open the hole.

Isolated copies are owner-only (`0600` on POSIX, a restricted ACL on Windows) and are
removed explicitly when a reusable validation worktree is released, or die with a strict worktree.
In-place files are never moved or removed. Their
dotenv assignment values (including exported, quoted, multiline, and short non-empty
values) are redacted when they appear verbatim in captured stage output, before that
output is written to a log or envelope. This is exact-value redaction, not a general
secret scanner: arbitrary copied-file formats and transformed, encoded, interpolated,
or derived values are outside this guarantee.

`copy_files` is for ignored files such as `.env`, not for directories.

## Preparing the validation worktree

Agentic Preflight does not install dependencies automatically. Configure
`[worktree] setup_command` when a validation worktree needs preparation, for example
`uv sync`, `npm ci`, or `pnpm install --frozen-lockfile`.

The command runs before review in every worktree mode and before a `--baseline` stage in
its scratch worktree. A nonzero exit stops the run; a failed baseline setup is reported
as a setup failure rather than evidence that the base commit is red.

Setup runs through the stage runner with the `[stage] timeout_seconds` limit. On timeout
the whole process group is killed and the result is exit 124. Initial setup output is
redacted like stage output and written to `setup.txt` in the run's log directory; a
failed setup envelope reports `timed_out`, `log_path`, and the tail of the output. A setup
command that writes to a copied file fails and its output is withheld.

Setup failures remain recoverable after the original error scrolls away. `status`
returns `abort --force` after initial checkout setup fails, releasing an isolated lease.
For baseline setup failures it retains the command and exit code and returns the exact
stage retry with `--baseline`; it never points at a stage log that was not created.

An isolated mode never copies dependency directories from the source checkout. Reusable
mode retains ignored dependency and build caches between leases, although the setup
command still runs. Strict mode begins without retained artifacts.

## When a stage is much slower than expected

Check the mode first. Strict mode has no retained build cache; reusable mode preserves
ignored caches between leases; in-place mode uses the current checkout. Do not raise
`[stage] max_attempts` to paper over a mode-shaped problem.
