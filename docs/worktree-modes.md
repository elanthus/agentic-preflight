# Worktree modes

Where a run validates, and what it is allowed to touch while doing so.

## Concurrent source worktrees

Active ownership is scoped to the source worktree's private Git directory. Linked
worktrees in one clone therefore run independent gates: one agent can wait for review
input while another reviews, tests, or publishes another PR branch. Per-run state changes
remain serialized, and the shared Git-notes ref is reconciled under a narrow publication
lock so concurrent attestations are preserved.

`agentic-preflight status` resolves the run owned by the invoking worktree. Use
`status --all` to list runs across the clone, or place `--run RUN_ID` before a command to
select a stored run explicitly. Commands selected that way operate on the recorded source
checkout when it still exists; they do not mutate whichever unrelated worktree happened
to invoke them. If that source checkout was deleted, `status`, `events`, and `logs` remain
available for inspection, but gated mutations fail with `source_worktree_missing` and
direct recovery to `gc` from a surviving worktree in the same clone.

A repeated `start` with the same head, intent, base, and effective configuration resumes
the matching run. If the source head moved, `start` marks the old run `ORPHANED` and
continues without deleting its evidence, validation checkout, or fix commits. A different
intent on the same unchanged head is ambiguous and requires `start --replace`; replacement
has the same preserve-first behavior. No run expires merely because it is old.

`reusable` mode still has one cached validation runner and is intentionally serial among
its users. That resource lease does not block `in_place` or `strict` runs in other source
worktrees. [ADR 0002](adr/0002-scope-run-ownership-to-worktrees.md) records the ownership
and lock-boundary decision.

## `in_place` (default)

Work happens directly in the current checkout. This is intended for a clean, dedicated
one-agent/one-PR worktree: the fresh-base rebase and accepted repair commits land
directly on the PR branch, and `mergeback` becomes a no-op attestation of the exact SHA
that passed every stage.

Any uncommitted change or unaccounted branch movement stops the run. In-place mode reuses
the checkout's existing dependency environment and does not run an automatic install; an
explicit `setup_command` still runs.

## `reusable`

Leases one runner in a hidden sibling directory, serially across runs, preserving ignored
dependency and build caches.

Between leases it resets tracked files, removes non-ignored untracked files, explicitly
removes every `[worktree] copy_files` entry, and then detaches the runner. Other ignored
files survive deliberately.

**This is not a hermetic environment**: a test can mutate an ignored cache. It reduces
local disk churn, nothing more.

## `strict`

Creates a fresh worktree for every run and removes it afterward. Use it when each local
validation must begin with no retained artifacts.

Remote CI should remain the clean verification boundary in either isolated mode.

## Why isolated runners live outside `.git`

Both isolated modes keep the source checkout untouched during verification, and both put
the runner outside `.git` so that tools ignoring VCS directories can still see it. Jest is
the common case: `jest-haste-map` ORs a hardcoded `/.git/` ignore into its crawl with no
config override, so a runner inside `.git` finds zero test files no matter how healthy the
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
mode = "reusable" # one serial isolated runner; retained ignored caches
```

```toml
[worktree]
mode = "strict"   # fresh worktree with no retained artifacts
```

The first strict run removes any idle reusable runner. Switching back to reusable mode
therefore starts with no retained cache. In-place mode leaves an idle reusable runner
alone.

## Secrets in worktrees

Files in `[worktree] copy_files` are used in place or copied into an isolated worktree so
tests can run, and are protected by two independent guards:

1. **Preflight refusal** — a file git is not already ignoring in the validation checkout
   is never used or copied. Add it to `.gitignore` and commit that first.
2. **Commit-content invariant** — any commit touching a copied path is rejected by both
   `respond` and `mergeback`, checked against commit content rather than ignore rules, so
   a `.gitignore` edited mid-run cannot open the hole.

Isolated copies are mode `0600` and are removed explicitly when a reusable runner is
released, or die with a strict worktree. In-place files are never moved or removed. Their
dotenv assignment values (including exported, quoted, multiline, and short non-empty
values) are redacted when they appear verbatim in captured stage output, before that
output is written to a log or envelope. This is exact-value redaction, not a general
secret scanner: arbitrary copied-file formats and transformed, encoded, interpolated,
or derived values are outside this guarantee.

`copy_files` is for ignored files such as `.env`, not for directories.

## Preparing the validation checkout

Agentic Preflight does not install dependencies automatically. Configure
`[worktree] setup_command` when a validation checkout needs preparation, for example
`uv sync`, `npm ci`, or `pnpm install --frozen-lockfile`.

The command runs before review in every worktree mode and before a `--baseline` stage in
its scratch worktree. A nonzero exit stops the run; a failed baseline setup is reported
as a setup failure rather than evidence that the base commit is red.

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
