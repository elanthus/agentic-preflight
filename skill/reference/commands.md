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

It also inspects runtime pins. For Node projects, the response names the detected pin
and manager, or warns that the repository is unpinned. Worktrees default to a hidden
sibling directory outside `.git`; `data.worktree_root` reports the resolved location.

### `agentic-cli integrations install codex|claude... [--scope user|project] [--target PATH] [--force]`
Copies the bundled skill and all of its references into each selected agent's discovery
directory. User scope installs under `~/.agents/skills` for Codex and
`~/.claude/skills` for Claude Code. Project scope uses the corresponding directory at
the repository root. `--target` adds a custom skills root for another compatible agent.

Existing unmanaged or locally modified copies are never overwritten unless `--force`
is explicit. The operation preflights every destination, so a conflict cannot leave
only half of the requested integrations installed.

### `agentic-cli integrations status [codex|claude...] [--scope user|project] [--target PATH]`
Reports each copy as `missing`, `current`, `outdated`, `modified`, or `unmanaged`.
With no agents named, checks Codex and Claude Code.

### `agentic-cli integrations update [codex|claude...] [--scope user|project] [--target PATH] [--force]`
Refreshes installed copies after a CLI upgrade and skips agents where the skill is not
installed. With no agents named, checks Codex and Claude Code. Refuses to replace local
edits unless `--force` is explicit.

### `agentic-cli integrations uninstall codex|claude... [--scope user|project] [--target PATH] [--force]`
Removes copies managed by agentic-cli. Unmanaged or locally modified directories are
preserved unless `--force` is explicit. At least one agent or custom target is required.

## Running a gate

### `agentic-cli start [--base-ref REF]`
Creates a run and a disposable worktree at the current HEAD on branch `ac/<run_id>`.
Refuses a dirty tree (exit 3, `dirty_tree`) and a branch with no changes over the base
(exit 3, `empty_diff`). Copies `[worktree] copy_files` into the worktree, refusing any
entry git is not already ignoring there.

Returns `data.worktree_path` — **absolute**. Use it; do not rely on `cd` persisting.
The default is outside both the repository and its `.git` directory, which avoids
Jest's hard-coded VCS-directory exclusion. Override it with `[worktree] root`.

**A fresh worktree has no build cache.** If a lint or test stage is far slower here
than in the user's tree, that is almost always the cause — not a hanging command. The
worktree is a clean checkout, so every gitignored artifact directory the toolchain
relies on is absent and gets rebuilt from nothing on the first run.

Use `[worktree] setup_command` to install dependencies or prepare build caches. Use
`copy_files` only for ignored files such as `.env`; directories are refused with a
clear setup instruction. Do not raise `[stage] max_attempts` to mask a cold checkout.

Note the interaction with `respond`: a fix commit containing a `copy_files` path is
rejected. Copied caches are inputs to the run, never part of the change.

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

A repo with none of those manifests — Unity, Unreal, Godot, Xcode, most engine and
mobile projects — will *always* exit 2 here with an empty `data.candidates`. That is
detection working correctly, not a broken setup. Ask the user for the invocation and
offer to write it into `[commands]`; do not go hunting for a build file that the
project does not use.

**Pass/fail is the exit code only.** Output is never parsed. Full output goes to
`logs/<stage>.txt`; the envelope carries head 50 and tail 200 lines with a `truncated`
flag. Stops with exit 4 after `[stage] max_attempts` failures.

**A command that no-ops and exits 0 is indistinguishable from one that passed.**
Exit-code-only is deliberate — parsing output is brittle — but it assumes the command
is *capable* of failing. The first green from a newly configured `[commands]` entry
proves nothing until you know the command can go red. Confirm it by checking the run
actually did work: a test count, a results file, a non-trivial log. A misconfigured
invocation that exits 0 without running anything reports `TEST_GREEN` forever, and a
false green retires the check entirely instead of costing you a retry.

The trap is usually a flag, not a typo. Unity is the canonical example: adding `-quit`
to a `-runTests` invocation makes the editor exit before the Test Framework starts —
no results file, no tests run, **exit 0**. Engine and GUI toolchains are dense with
these; treat their first green as unverified.

`--baseline` also runs the command against the base commit, so a pre-existing failure
is reported rather than blamed on the diff. It is also the cheapest way to see the
command produce two different outcomes — if base and head are byte-identical greens on
a diff that touches tested code, suspect the command before trusting it.

Before setup or a shell stage runs, committed Node pins are activated for NVM, Volta,
asdf, mise, fnm, or nodenv. `[runtime] manager = "auto"` is the default. With
`strict = true`, a pin whose manager is unavailable fails with exit 127 instead of
falling back to a different system Node. `manager = "none"` disables activation.

### `agentic-cli mergeback`
Cherry-picks the fix commits onto the user's branch. It blocks only working-tree paths
the fix commits may overwrite; unrelated tracked edits and untracked files are left
alone. The strict clean-tree requirement still applies to `start`.

On conflict: aborts immediately, verifies the branch is byte-for-byte restored, exits 4
with `data.resolution`, and stores that full report in the event log. **Never
auto-resolves.** After a person resolves it, `mergeback` is legal again: an exact tree
is attested without rerunning completed stages; a different tree is retained but must
start a fresh verification run.

On success: compares the branch tree against the worktree tree. `tree_equivalent: true`
means the verified content is byte-identical and green transfers to the ledger. False
means re-verification is needed.

### `agentic-cli gate`
Mints a confirmation token and summarises the remote, refspec, branch, and commits.
With `[gate] mode = "manual"` it exits 4 instead and hands over the literal `git push`
command for a person to run.

### `agentic-cli push --confirm TOKEN [--dry-run]`
Requires the token from `gate`. **Ask the user before running this.**

### `agentic-cli pr [--draft/--no-draft] [--title TITLE]`
Opens a pull request via the `gh` CLI. No credentials are ever handled here — if `gh`
is missing or unauthenticated, exits 4 with a prefilled `compare_url`.

Title precedence is `--title`, `[publish] pr_title`, branch name, then the first commit
subject when no branch name is available.

## Inspection and recovery

### `agentic-cli status`
Legal in **every** state and the universal recovery entry point. Reports state, seq,
findings, staleness, worktree path, and the gate token. Never raises for a wedged run.
In `MERGEBACK_CONFLICT`, it replays the durable conflict report and points back to the
legal `mergeback` retry.

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
