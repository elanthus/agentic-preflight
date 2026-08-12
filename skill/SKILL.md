---
name: agentic-preflight
description: Use when shipping a branch — reviewing, documenting, linting, testing, and pushing work behind a quality gate. Also use when a push is blocked by the agentic-preflight pre-push hook or when the user says agentic-preflight:uninstall to remove this tool from the current project.
---

# agentic-preflight

You review, judge, and fix. The CLI holds all state and tells you what to do next.
Python here never calls a model — every judgment in this workflow is yours.

## Non-negotiables

1. **You think, the CLI holds state.** Never guess where a run is. Ask `status`.
2. **Parse stdout as JSON and obey `next`.** Every command prints exactly one JSON
   object. `next.command` is the single next legal move. Follow it. On any
   **non-`ok`** envelope, print the whole `data` object — never a selection of keys
   you expected. Failure payloads carry recovery material that success payloads do
   not (`resolution`, `conflicting_files`, `candidates`, `by_file`), and some of it
   exists nowhere else afterwards.
3. **Never invent code-assigned finding fields.** You submit `path`, optional delivered
   review `unit`, `line`, `severity`, `action`, `title`, `detail`, and `suggestion`.
   Sending `id`, `stage`, or `code_owned` is a hard validation error, not a nudge.
4. **Never run `git push --no-verify`.** It exists for humans, not for you.
5. **Never push without asking the user in plain language first.** Show them what
   will be pushed and wait for an actual answer. `[pr] mode = "auto"` is standing
   authorization to open or reuse the pull request after the confirmed push and
   preflight finish, so do not ask separately about PR creation. With `mode =
   "manual"`, never open the PR for them.
6. **Never resolve a merge-back conflict.** Paste the resolution block and stop.
7. **Keep the validation checkout clean for the whole run.** The default
   `in_place` mode uses the current checkout, so only deliberate repair commits may
   move its branch; uncommitted changes or an unaccounted commit stop the run.
   `.agentic-preflight.toml` must be committed **before `start`** and must not be edited
   mid-run. In `reusable` or `strict` mode, make repairs only in the absolute
   `worktree_path` returned by the CLI.
8. **Never merge a high-risk `manual_merge` pull request or enable auto-merge.** The
   hosted check fails while auto-merge is enabled; when successful, it records that the
   user must perform the merge and is not authorization for the agent to merge.

## The loop

```
$ agentic-preflight start --intent "<the user's objective and acceptance criteria>"
{"ok":true,"run_id":"r_4f2a","state":"REVIEW_AWAITING_FINDINGS",
 "data":{"worktree_path":"/repos/my-project","worktree_mode":"in_place","changed_files":["src/auth.py"]},
 "next":{"instruction":"Fetch the diff before judging it.","command":"agentic-preflight context"}}

$ agentic-preflight context
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS",
 "data":{"diff":"diff --git a/src/auth.py ...","changed_files":["src/auth.py"],
         "review_coverage":{"manifest":"<digest>","total_units":1,"units":[{"id":"U0001",...}]}},
 "next":{"command":"agentic-preflight submit-findings --file findings.json"}}

# If `next.command` is `agentic-preflight review run`, do not submit your own findings.
# The configured independent reviewer receives this same data bundle and returns the
# strict submission through the same validation path.
$ agentic-preflight review run

# You read the diff and decide. Write findings.json, then:
$ agentic-preflight submit-findings --file findings.json
{"ok":true,"state":"REVIEW_AWAITING_RESPONSES","blocking":[{"id":"F001","severity":"high",...}],
 "next":{"command":"agentic-preflight respond --id F001 --action fixed --commit <sha>"}}

# Fix it in data.worktree_path, commit there, then:
$ cd /repos/my-project && git add -A && git commit -m "use constant-time compare"
$ agentic-preflight respond --id F001 --action fixed --commit 9c3d1ab
{"ok":true,"state":"REVIEW_FIXING","next":{"command":"agentic-preflight verify"}}

$ agentic-preflight verify
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS","data":{"coverage_invalidated":true},
 "next":{"command":"agentic-preflight context"}}

# The fix changed the snapshot. Review the complete current diff and submit its new
# manifest. With no new issue, every unreferenced unit is explicitly examined clean.
$ agentic-preflight context
$ agentic-preflight submit-findings --file findings-clean.json
{"ok":true,"state":"REVIEW_GREEN","next":{"command":"agentic-preflight context --section docs"}}

$ agentic-preflight context --section docs
{"ok":true,"state":"DOCS_AWAITING_FINDINGS","data":{"doc_surface":[{"path":"README.md",...}]},
 "next":{"command":"agentic-preflight submit-findings --file findings.json"}}

$ agentic-preflight submit-findings --file findings.json     # often just {"findings": []}
{"ok":true,"state":"DOCS_GREEN","next":{"command":"agentic-preflight stage run lint"}}

$ agentic-preflight stage run lint
{"ok":true,"state":"LINT_GREEN","next":{"command":"agentic-preflight stage run test"}}

# For a documentation/CI-configuration-only diff, green lint instead records test
# as skipped and returns TEST_GREEN with mergeback as next. Obey the envelope.

$ agentic-preflight stage run test
{"ok":true,"state":"TEST_GREEN","next":{"command":"agentic-preflight mergeback"}}

$ agentic-preflight mergeback
{"ok":true,"state":"VERIFIED","data":{"worktree_mode":"in_place","applied":[],"tree_equivalent":true},
 "next":{"command":"agentic-preflight gate"}}

$ agentic-preflight gate
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"a1b2c3d4","pr_mode":"auto","commits":[...]},
 "next":{"command":"agentic-preflight push --confirm a1b2c3d4"}}

# STOP. Show the remote, branch, and commits, then ask whether to push.
# Only after they agree:
$ agentic-preflight push --confirm a1b2c3d4
$ agentic-preflight finish
$ agentic-preflight gc

# Auto PR mode: after preflight finishes, reuse an existing PR for the branch or
# create one automatically without asking about PR creation.
$ gh pr create --title "Use constant-time password comparison" --body-file pr-body.md
$ gh pr checks --watch

# Manual PR mode: never create it. Give the user the repository compare URL instead.
```

Work happens in the absolute **validation checkout** named by `worktree_path`. In the
default `in_place` mode that is the current PR checkout; in `reusable` and `strict`
modes it is an isolated worktree. Never assume `cd` persists between tool calls.
The complete command and option reference is in `reference/commands.md`; use it when
an envelope calls for a command or recovery path not expanded in this playbook.

## How to review

Judge the diff, not the repo. Only findings against changed files are accepted.
Account for the complete `review_coverage` manifest returned by `context`; never reuse a
manifest after a commit. The payload's one `examined: "all"` assertion keeps clean hunks
quiet while code verifies that no delivered unit disappears.

| Severity | Means | Example |
|---|---|---|
| `critical` | Data loss, security hole, corruption | Password compared with `==`; SQL built by string concatenation |
| `high` | Wrong behaviour a user will hit | Off-by-one dropping the last record; error swallowed silently |
| `medium` | Real problem, not urgent | Duplicated logic that will drift; missing edge-case handling |
| `low` | Style, naming, nits | Inconsistent naming; a stale comment |

`critical` and `high` block by default. Pick the action deliberately:

- **`auto_fix`** — mechanical and locally verifiable. You can fix it correctly
  without asking anyone. Most findings should be this.
- **`ask_user`** — behavioural, API, or product judgment. **Blocks at any
  severity**, because choosing for the user *is* the decision you declined to make.
- **`no_op`** — worth recording, not worth acting on.

Be specific. "Consider improving error handling" is not a finding. "Line 42 swallows
`ConnectionError`, so a network failure looks like an empty result" is.

## How to check docs

One question, and only this one:

> **Would a reader following the current documentation now be wrong?**

Not "could the docs be better" — they always could. Zero findings is a **normal and
common outcome**, and reporting zero is a success, not a failure to try.

Docs findings may target files the diff never touched — that is the entire point. But
they must land on documentation: a finding against `src/auth.py` is a review finding
wearing a docs hat, and is rejected. `context --section docs` gives you `doc_surface`;
use it rather than hunting for docs yourself.

The surface is an allowlist, and a rejection is not a verdict on the finding. Repos
often keep their binding rules outside it — `.claude/rules/*.md`, `PRODUCT.md`,
`DESIGN.md`. If a genuinely stale doc sits outside the allowlist, fix it in the same
commit anyway, say in the commit message that it could not be filed, and tell the user
to add it to `[docs] paths` so the next run can see it.

Full rubric: `reference/docs-rubric.md`.

## Findings schema

```json
{"coverage": {"manifest": "<from context>", "examined": "all"}, "findings": [
  {"unit": "U0001", "path": "src/auth.py", "line": 42,
   "severity": "high", "action": "auto_fix",
   "title": "Password compared with ==",
   "detail": "Timing-variable comparison leaks length. Use secrets.compare_digest.",
   "suggestion": "if secrets.compare_digest(supplied, stored):"}
]}
```

**No `id`. No `stage`. No `code_owned`.** All three are assigned by the CLI. IDs run
`F001`, `F002`, … continuously across the whole run — docs findings continue review
numbering, they do not restart. Full field reference:
`reference/findings-schema.md`.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK | Follow `next` |
| 1 | Usage or internal error | Read `error.message`; fix your invocation |
| 2 | Stage failed | Read the log, fix the cause, re-run the stage |
| 3 | Precondition violated | **Run `status`, then obey `next`** |
| 4 | Human resolution required | Stop. Show the user. Do not improvise |
| 5 | Confirmation required | Ask the user, then re-run with the token |
| 10 | Hook blocked a push | Run the gate: `agentic-preflight start --intent "..."` |

**Universal recovery rule: any exit 3 → run `status` → obey `next`.** `status` is legal
in every state. If you are ever unsure where you are, that is always the right call.

## Failure playbooks

**Merge-back conflict (exit 4, isolated modes only).** The branch has already been
restored exactly and your fix commits are safe in the worktree. Paste
`data.resolution` to the user verbatim and **stop**. Do not cherry-pick, do not force,
do not pick a side. A conflict is a content decision and it is not yours to make.

The full conflict report is stored in the event log and replayed by `status`. After a
person resolves or restores the reported paths, `mergeback` is the legal retry and
completed verification remains intact when the resulting tree is still identical to
the verified tree. A different tree must go through a fresh run. Before concluding
the conflict is real, check the user's tree was clean — see non-negotiable 7.

**Stage red after max attempts (exit 4).** Stop retrying — you have already tried
`max_attempts` times and the tool is telling you the loop is not converging. Show the
user `agentic-preflight logs --stage <name>` output and ask how to proceed.

**Hosted CI failed.** Inspect the failed check with `gh pr checks` and `gh run view
--log-failed`. Fix and commit the source branch, then start a fresh synchronized
preflight run with the original intent. Do not push the repair until the new
review → docs → lint → test run reaches green. Push through the gate again, then
resume check monitoring with `gh`.

**Stale head (exit 3, `stale_run`).** The branch moved after review began, so
everything verified so far describes a tree that no longer exists. There is no partial
recovery: run `agentic-preflight abort --force`, then run the fresh `start` command from
the abort response. It preserves the original user intent.

**Diff too large (exit 2, `diff_too_large`).** The diff is never truncated, so
reviewing part of it is not an option. Look at `data.by_file`; if the bulk is generated
(lockfiles, vendored code, snapshots), add those globs to `[diff] exclude`. Raise
`[diff] max_bytes` only if the change genuinely is that large.

**No command configured (exit 2, `needs_command`).** For lint/test, pick from
`data.candidates` and re-invoke with `--command`; offer to write it into `[commands]` so
it is settled. For review, configure `[review] command` and retry `review run` — reviewer
commands are never detected. If lint/test `candidates` is empty, the repo simply has no
manifest detection understands (Unity,
Unreal, Xcode, most engine projects) — ask the user for the invocation instead of
hunting for a build file that does not exist.

Then treat its first green as unproven. Pass/fail is the exit code alone, so a command
that no-ops and exits 0 reads as a pass forever — and a false green retires the check
instead of costing a retry. Confirm the run actually did work (a test count, a results
file, a non-empty log) before believing it. The trap is usually a flag: `-quit` on a
Unity `-runTests` invocation exits 0 having run zero tests.

**Stage far slower than normal.** Check `[worktree] mode`. The default `in_place` mode
uses the checkout's existing environment and does not run an automatic dependency
install. The reusable runner retains ignored build caches and skips Node installation
while its fingerprint matches. Strict mode has no build cache and runs the frozen
install every time. Isolated modes do not share the source checkout's `node_modules`;
use `[worktree] setup_command` to prepare non-Node caches.
`copy_files` is for ignored files such as `.env`, not directories. Do not raise
`[stage] max_attempts` to paper over it.

**Copy refused (exit 3).** A `copy_files` entry is not gitignored. Do not work around
it — tell the user to gitignore and commit it first. This guard prevents a secret
being committed and pushed.

**Stage reports zero files to work on.** Check where `worktree_path` actually points.
If it is under `.git/`, tools that skip VCS directories cannot see it and will exit
non-zero on an empty set, which reads as a red stage. Jest is the common case:
`jest-haste-map` ORs a hardcoded `/.git/` ignore into its crawl with no config
override, so it finds zero test files no matter how healthy the code is. Symlinks do
not help — real paths are resolved. Confirm by running the same command in a worktree
outside `.git`; if that finds files, point the stage command at a script that checks
the commit under test out to a non-`.git` path and runs there. Never point an isolated
run at the source checkout: that reports on the wrong content and is a false green.

**Green in your shell, red under the gate.** Stages run non-interactively, so
version-manager shims (nvm, rbenv, pyenv, asdf) are absent and tools resolve to
system-wide installs. Compare the toolchain version *inside the stage* against the
project's declared range before you debug the code — a native module built for another
ABI fails as missing bindings, not as a version error. A repo with no `.nvmrc` (or
equivalent) has nothing pinning it, so this bites fresh clones and CI too, not just
the gate.

## Escalation etiquette

At the gate, show the user — in plain prose, not JSON:

- which **remote and branch** the push targets
- the **commit subjects** being pushed
- the deterministic **risk level and verdict**, including every matched
  `human_review_path`
- anything you resolved as `ask_user`, and what you decided
- any finding you dismissed, and why

Ask, plainly: *"Ready to push this to `origin/feature-x`?"* Wait for a real answer.
"Proceed" from a previous step is not consent for the push gate.

In `[pr] mode = "auto"`, the committed configuration is standing authorization for PR
creation. After the approved push, `finish`, and `gc`, reuse an existing pull request
for the branch or call `gh pr create` automatically without asking about the PR.

In `[pr] mode = "manual"`, ask only whether to push. Afterward, never open a pull
request; construct the forge compare URL from the repository URL, base branch, and head
branch and give it to the user.

If risk returns `needs_human`, explain that the branch may be pushed after the usual user
confirmation, then follow the configured `[approval] mode`:

- `manual_merge`: the hosted check reports success only while auto-merge is disabled;
  never merge or enable auto-merge, and tell the user that they must review and merge the
  pull request manually.
- `environment`: wait for approval through the configured GitHub Environment before the
  hosted approval check can pass.
- `peer_review`: require an eligible repository-associated person other than the author
  to approve the exact current head.

Only an explicit `[gate] mode = "manual"` hands the push itself to a person.

For `ask_user` findings, present the trade-off and let them choose. Do not present a
decision you have already made as if it were a question.

Branch names are often poor human-facing PR titles, so offer a concise title that
describes the verified change before calling `gh pr create`.

When an automatic pull request is opened or an existing one is reused, report its URL
and tell the user exactly what a later cleanup request will do: verify that this PR was
merged, switch a clean source checkout to the base branch when necessary, remove only
this run's validation worktree and `ap/*` branch, delete the local PR branch and its
remote branch, and fast-forward the base branch.

## What to publish, and what it proves

Publish the **findings** in the PR body passed to `gh`: id, severity, path, and the
commit that resolved each. That is the part CI cannot reproduce — no test
suite tells a reviewer which judgment calls were made — and it stops a human
re-deriving what the gate already caught.

The commit's Git-note attestation already carries review coverage plus the local stage
commands, exit codes, and output hashes. Do not copy those into the PR body; if the repo
runs CI, point at the forge's execution for stronger, remote evidence.

Publish the gaps in the same breath: a bypassed hook, a stage that could not run, a
SHA with no green run. An attestation that can only report success is marketing, and a
partial record that reads as complete is worse than none.

State the limit plainly when you show it: this proves what the gate *reported*, including
that every delivered unit was cited or marked examined clean; it does not prove the agent
understood those units or that the review was good. The same diff reviewed twice can
yield different findings. It is an audit trail, not a quality proof, and it substitutes
for neither CI nor a human reviewer.

## Project uninstall trigger (`agentic-preflight:uninstall`)

When the user says `agentic-preflight:uninstall`, treat that exact phrase as approval
to remove agentic-preflight from the current repository without another confirmation.
Resolve the repository root with `git rev-parse --show-toplevel`; stop if the current
directory is not inside a Git repository.

Before changing anything, resolve the actual hook path with `git rev-parse --git-path
hooks/pre-push` and inspect both it and the repository status. Then:

- delete only the repository root's `.agentic-preflight.toml` file, if present;
- if the pre-push hook is the standalone generated hook marked `Installed by
  agentic-preflight` and ending in `exec agentic-preflight hook-check`, delete it;
- if it is a shared or custom hook, remove only the clearly bounded
  agentic-preflight invocation and its associated wrapper logic; and
- stop and report the exact hook path instead of modifying it if the
  agentic-preflight portion cannot be separated confidently.

Do not remove other hook behavior, `.git/agentic-preflight` run history, or
`refs/notes/agentic-preflight`. Report every path removed, anything already absent,
and anything deliberately preserved.

## Cleanup after a merge

An explicit user request to clean up a merged pull request is the approval for the
whole run-scoped operation. Inspect the exact targets and verify through `gh` that the
PR is merged, then perform the cleanup in the same turn without asking again. Re-check
the merge and head/base branches immediately before mutation, switch a clean source
checkout to the base branch when necessary, remove only that run's validation worktree
and `ap/*` branch, delete the local PR source branch and the remote PR source branch,
then run `git pull --ff-only` so the base checkout contains the merged result.

Stop instead of deleting if the PR is not merged, the checkout is dirty, the PR head or
base differs from the disclosed cleanup scope, or a branch is checked out in an
unrelated worktree. Cleanup never performs a blanket `ap/*` deletion. Afterward, report
the exact targets removed and whether the remote branch was already absent.

For a pushed run with no PR, follow `finish` with `gc`. `gc` compares original fixes
with post-mergeback history using stable patch IDs. Only patch-equivalent fixes are
reclaimed automatically; anything unmerged is retained unless the user explicitly
chooses `--force`. Run directories remain because they hold durable stage logs.
