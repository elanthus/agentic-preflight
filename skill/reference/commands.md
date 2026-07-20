# Command reference

Every command prints exactly one JSON object to stdout. Human prose goes to stderr.
Parse stdout blindly; every key is always present.

```json
{"ok": true, "run_id": "r_...", "state": "AWAITING_FINDINGS", "stage": "review",
 "data": {}, "blocking": [], "next": {"instruction": "...", "command": "..."},
 "error": null}
```

`next` is `null` only when there is genuinely nothing left to do. `error` is `null`
when `ok` is `true`.

## Setup

### `agentic-cli init [--force] [--no-hook]`
Installs the pre-push hook and writes `.agentic-cli.toml` if absent. Refuses to
replace a pre-push hook it did not write (exit 3, `hook_exists`) — `--force`
overrides. Does not clobber an existing config.

## Running a gate

### `agentic-cli start [--base-ref REF]`
Creates a run and a disposable worktree at the current HEAD on branch `ac/<run_id>`.
Refuses a dirty tree (exit 3, `dirty_tree`) and a branch with no changes over the base
(exit 3, `empty_diff`). Copies `[worktree] copy_files` into the worktree, refusing any
entry git is not already ignoring there.

Returns `data.worktree_path` — **absolute**. Use it; do not rely on `cd` persisting.

### `agentic-cli context [--section review|docs]`
Returns the material for the active stage. Does not change state, so it is safe to
call twice.

- Both sections: `diff`, `changed_files`, `excluded_files`, `worktree_path`.
- `--section docs` adds `doc_surface`: every documentation file with `exists`, `size`,
  and `touched_by_diff`. From `REVIEW_GREEN` this opens the docs stage.

Exits 2 with `data.mode = "diff_too_large"` when the diff exceeds `[diff] max_bytes`.
The diff is never truncated — narrow it with `[diff] exclude` or raise the limit.

### `agentic-cli submit-findings --file PATH`
`PATH` may be `-` for stdin. Accepts `{"findings": [...]}` or a bare list. An empty
list is valid and common.

Rejects (exit 3, `invalid_findings`): agent-supplied `id` or `stage`, paths outside the
worktree, paths outside the changed-file set (review) or documentation allowlist
(docs), line numbers past end of file, and batches over `[review] max_findings`.
All-or-nothing — one bad finding rejects the batch.

### `agentic-cli respond --id F001 --action fixed|dismissed|accepted [--commit SHA] [--note TEXT]`
- `fixed` requires `--commit`. The commit is verified three ways: it exists, it touches
  the finding's file, and it contains no `copy_files` path.
- `dismissed` and `accepted` require `--note`.
- Unknown id exits 3 listing the valid ids. Each finding is resolved once.

### `agentic-cli verify`
Confirms nothing blocks the active stage and advances to green. Exits 2 listing the
outstanding blocking set if anything remains.

### `agentic-cli stage run lint|test [--command CMD] [--record] [--baseline]`
Command resolution: `--command` → `[commands].<name>` → detection. Detection never
guesses: it exits 2 with `data.mode = "needs_command"` and candidates from
`pyproject.toml`, `package.json`, `Makefile`, `justfile`, and CI workflows.

**Pass/fail is the exit code only.** Output is never parsed. Full output goes to
`logs/<stage>.txt`; the envelope carries head 50 and tail 200 lines with a `truncated`
flag. Stops with exit 4 after `[stage] max_attempts` failures.

`--baseline` also runs the command against the base commit, so a pre-existing failure
is reported rather than blamed on the diff.

### `agentic-cli mergeback`
Cherry-picks the fix commits onto the user's branch. Requires a clean tree and an
unmoved branch tip.

On conflict: aborts immediately, verifies the branch is byte-for-byte restored, exits 4
with `data.resolution`. **Never auto-resolves.**

On success: compares the branch tree against the worktree tree. `tree_equivalent: true`
means the verified content is byte-identical and green transfers to the ledger. False
means re-verification is needed.

### `agentic-cli gate`
Mints a confirmation token and summarises the remote, refspec, branch, and commits.
With `[gate] mode = "manual"` it exits 4 instead and hands over the literal `git push`
command for a person to run.

### `agentic-cli push --confirm TOKEN [--dry-run]`
Requires the token from `gate`. **Ask the user before running this.**

### `agentic-cli pr [--draft/--no-draft]`
Opens a pull request via the `gh` CLI. No credentials are ever handled here — if `gh`
is missing or unauthenticated, exits 4 with a prefilled `compare_url`.

## Inspection and recovery

### `agentic-cli status`
Legal in **every** state and the universal recovery entry point. Reports state, seq,
findings, staleness, worktree path, and the gate token. Never raises for a wedged run.

### `agentic-cli logs --stage lint|test`
Full captured output. Copied-file contents are redacted.

### `agentic-cli events [--limit N]`
Run history, oldest first.

### `agentic-cli abort [--force]`
Ends the run and reclaims the worktree. Exits 5 if unmerged fix commits would be lost;
`--force` discards them.

### `agentic-cli gc [--force]`
Reconciles run directories, git worktrees, and `ac/*` branches. Anything holding
unmerged work is reported, never removed without `--force`.

### `agentic-cli hook-check`
The pre-push predicate. Reads git's stdin protocol, consults only `ledger.json`, and
exits 0 or 10. Not for you to call — git calls it.

## Exit codes

`0` ok · `1` usage/internal · `2` stage failed · `3` precondition violated ·
`4` human resolution required · `5` confirmation required · `10` hook block

Any exit 3 → run `status` → obey `next`.
