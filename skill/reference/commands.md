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

### `agentic-preflight init [--force] [--no-hook]`
Installs the pre-push hook and writes `.agentic-preflight.toml` if absent. Refuses to
replace a pre-push hook it did not write (exit 3, `hook_exists`) — `--force`
overrides. Does not clobber an existing config.

It also inspects runtime pins. For Node projects, the response names the detected pin
and manager, or warns that the repository is unpinned. Worktrees default to a hidden
sibling directory outside `.git`; `data.worktree_root` reports the resolved location.

### `agentic-preflight integrations install codex|claude... [--scope user|project] [--target PATH] [--force]`
Copies the bundled skill and all of its references into each selected agent's discovery
directory. User scope installs under `~/.agents/skills` for Codex and
`~/.claude/skills` for Claude Code. Project scope uses the corresponding directory at
the repository root. `--target` adds a custom skills root for another compatible agent.

Existing unmanaged or locally modified copies are never overwritten unless `--force`
is explicit. The operation preflights every destination, so a conflict cannot leave
only half of the requested integrations installed.

### `agentic-preflight integrations status [codex|claude...] [--scope user|project] [--target PATH]`
Reports each copy as `missing`, `current`, `outdated`, `modified`, or `unmanaged`.
With no agents named, checks Codex and Claude Code.

### `agentic-preflight integrations update [codex|claude...] [--scope user|project] [--target PATH] [--force]`
Refreshes installed copies after a CLI upgrade and skips agents where the skill is not
installed. With no agents named, checks Codex and Claude Code. Refuses to replace local
edits unless `--force` is explicit.

### `agentic-preflight integrations uninstall codex|claude... [--scope user|project] [--target PATH] [--force]`
Removes copies managed by agentic-preflight. Unmanaged or locally modified directories are
preserved unless `--force` is explicit. At least one agent or custom target is required.

## Running a gate

### `agentic-preflight start --intent TEXT [--base-ref REF]`
Creates a run and prepares its validation checkout. The default `[worktree] mode =
"in_place"` validates directly in the current clean PR checkout. `mode = "reusable"`
uses one serial isolated runner and preserves ignored caches between leases. `mode =
"strict"` creates and removes a fresh isolated worktree per run.
The intent is required and persisted as the user's objective and acceptance criteria.
Before review, the command fetches the configured base from `origin` when available and
rebases the validation checkout onto that exact fresh base. In-place mode therefore
rebases the PR branch itself. A sync conflict is aborted
cleanly and reported; no conflicted rebase is left in progress.
Refuses a dirty tree (exit 3, `dirty_tree`) and a branch with no changes over the base
(exit 3, `empty_diff`). In-place mode protects `[worktree] copy_files` where they are;
isolated modes copy them. Every mode refuses an entry git is not already ignoring.

Returns `data.worktree_path` — **absolute**. Use it; do not rely on `cd` persisting.
For isolated modes the path is outside both the repository and its `.git` directory,
which avoids Jest's hard-coded VCS-directory exclusion. Override that location with
`[worktree] root`.

**Strict mode has no build cache.** If a lint or test stage is far slower there
than in the user's tree, that is almost always the cause — not a hanging command. The
worktree is a clean checkout, so every gitignored artifact directory the toolchain
relies on is absent and gets rebuilt from nothing on the first run.

With `[worktree] dependency_setup = "auto"`, a pnpm lockfile uses
`pnpm install --frozen-lockfile`; npm uses `npm ci`. Reusable mode skips the install
while its dependency/runtime fingerprint matches and `node_modules` remains present.
Strict mode installs on every run. In-place mode uses the checkout's existing
dependencies and performs no automatic install. The source checkout's `node_modules`
is never linked into an isolated mode.
`setup_command` overrides this automatic setup. Use `copy_files` only for ignored files
such as `.env`; directories are refused with a clear setup instruction.

Note the interaction with `respond`: a fix commit containing a `copy_files` path is
rejected. Copied caches are inputs to the run, never part of the change.

### `agentic-preflight context [--section review|docs]`
Returns the material for the active stage. Does not change state, so it is safe to
call twice.

- Both sections: `diff`, `changed_files`, `excluded_files`, `worktree_path`.
- `--section docs` adds `doc_surface`: every documentation file with `exists`, `size`,
  and `touched_by_diff`. From `TEST_GREEN` this opens the docs stage.

Exits 2 with `data.mode = "diff_too_large"` when the diff exceeds `[diff] max_bytes`.
The diff is never truncated — narrow it with `[diff] exclude` or raise the limit.

### `agentic-preflight submit-findings --file PATH`
`PATH` may be `-` for stdin. Accepts `{"findings": [...]}` or a bare list. An empty
list is valid and common.

Rejects (exit 3, `invalid_findings`): agent-supplied `id` or `stage`, paths outside the
worktree, paths outside the changed-file set (review) or documentation allowlist
(docs), line numbers past end of file, and batches over `[review] max_findings`.
All-or-nothing — one bad finding rejects the batch.

### `agentic-preflight respond --id F001 --action fixed|dismissed|accepted [--commit SHA] [--note TEXT]`
- `fixed` requires `--commit`. The commit is verified three ways: it exists, it touches
  the finding's file, and it contains no `copy_files` path.
- `dismissed` and `accepted` require `--note`.
- Unknown id exits 3 listing the valid ids. Each finding is resolved once.

### `agentic-preflight verify [SHA]`
Without an argument, confirms nothing blocks the active review/docs stage and advances
it to green. Exits 2 listing the outstanding blocking set if anything remains.

With a SHA, validates the portable attestation in `refs/notes/agentic-preflight` for
CI. It checks the note schema, exact commit and tree binding, complete stage set, and
process evidence for green lint/test stages. Fetch the notes ref before calling it in
a fresh clone. A missing or invalid note exits 2.

### `agentic-preflight approval-check SHA --base SHA --reviews-file PATH --author LOGIN`
CI-facing merge policy for an attested pull-request head. It recomputes path risk from
the protected base configuration and uses attested finding-severity totals. Low- and
medium-risk changes pass without a review. High-risk handling follows `[approval] mode`:
`manual_merge` reports success while requiring the user to merge manually (the trusted
workflow separately rejects enabled GitHub auto-merge); `environment` exits 4 until
`--environment-approved` is supplied by the trusted Environment job; and `peer_review`
exits 4 until an eligible non-author has an `APPROVED` review for the exact current head.
Later dismissal or changes-requested reviews revoke that person's peer approval.
`--report-only` reports conditional Environment or peer-review state without failing, so
a trusted workflow can dispatch the appropriate hosted job.

### `agentic-preflight stage run lint|test [--command CMD] [--record] [--baseline]`
After review becomes green, the CLI automatically skips the software test command when
every changed path is documentation or standard CI configuration. This is an explicit
state-machine transition, not an agent judgment: `status` and the final attestation note
the test stage as `skipped`. A mixed diff containing any other path still requires the
configured test command. Documentation includes common markup files, the standard docs
surface, and `[docs] paths`; CI configuration includes common hosted-CI workflow paths.

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

### `agentic-preflight mergeback`
In in-place mode, attests the already-verified current SHA without creating or
cherry-picking a commit. The checkout must remain clean. In isolated modes, cherry-picks
the fix commits onto the source branch; only paths those commits may overwrite are
blocked, while unrelated tracked edits and untracked files are left alone.

On conflict: aborts immediately, verifies the branch is byte-for-byte restored, exits 4
with `data.resolution`, and stores that full report in the event log. **Never
auto-resolves.** After a person resolves it, `mergeback` is legal again: an exact tree
is attested without rerunning completed stages; a different tree is retained but must
start a fresh verification run.

On success: compares the branch tree against the worktree tree. `tree_equivalent: true`
means the verified content is byte-identical and a Git-note attestation is written for
the exact commit. False
means re-verification is needed.

### `agentic-preflight gate`
Mints a confirmation token and summarises the remote, refspec, branch, and commits.
The summary also includes the configured PR mode and deterministic risk classification
and verdict. Always ask only whether to push. In `[pr] mode = "auto"`, the committed
configuration is standing authorization to open or reuse the pull request automatically
after the confirmed push and preflight finish. In manual PR mode, provide a compare URL
instead. High risk does not change publication: after user confirmation, token mode may
push it. The summary's `approval_mode` says whether the user must merge manually, a
GitHub Environment must approve, or an eligible peer must approve the exact head. In
`manual_merge`, never merge or enable auto-merge even when the hosted check is green.
Only `[gate] mode = "manual"` exits 4 and hands over the literal `git push` command for a
person to run.

### `agentic-preflight push --confirm TOKEN [--dry-run]`
Requires the token from `gate` and atomically pushes both the branch and
`refs/notes/agentic-preflight`. **Ask the user before running this.** The token is a
non-secret, run-state nonce that prevents an accidental push; it is readable through
`status`, grants no GitHub access, and is not a security boundary.

### `agentic-preflight finish`
Marks a pushed validation run `DONE`. It preserves the run directory and
audit logs, clears the current-run pointer, and directs the next step to `gc`.

Pull-request creation and hosted CI monitoring are deliberately outside this CLI. On
GitHub, automatic PR mode uses `gh pr create`, `gh pr checks`, and `gh run view` after
`finish`; manual PR mode provides a compare URL and never creates the PR. Branch cleanup
remains an explicit, run-scoped host or forge operation authorized by the user's cleanup
request.

## Inspection and recovery

### `agentic-preflight status`
Legal in **every** state and the universal recovery entry point. Reports state, seq,
findings, staleness, worktree path, and the gate token. Never raises for a wedged run.
In `MERGEBACK_CONFLICT`, it replays the durable conflict report and points back to the
legal `mergeback` retry.

### `agentic-preflight logs --stage lint|test`
Full captured output. Copied-file contents are redacted.

### `agentic-preflight events [--limit N]`
Run history, oldest first.

### `agentic-preflight abort [--force]`
Ends the run and releases the worktree. Exits 5 if unmerged fix commits would be lost;
`--force` discards them.

### `agentic-preflight gc [--force]`
Reconciles run directories, git worktrees, and `ap/*` branches. For a terminal run,
each fix commit is compared by stable patch ID with commits in that run's
post-mergeback history. Patch-equivalent cherry-picks are safe to reclaim; anything
with no equivalent remains reported as unmerged and is never removed without
`--force`. Run directories and their audit logs are retained.

### `agentic-preflight hook-check`
The pre-push predicate. Reads git's stdin protocol, consults only the commit's
`refs/notes/agentic-preflight` note, and exits 0 or 10. Not for you to call — git calls
it.

## Exit codes

`0` ok · `1` usage/internal · `2` stage failed · `3` precondition violated ·
`4` human resolution required · `5` confirmation required · `10` hook block

Any exit 3 → run `status` → obey `next`.
