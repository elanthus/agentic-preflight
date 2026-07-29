---
name: agentic-preflight
description: Use when shipping a branch — reviewing, documenting, linting, testing, and pushing work behind a quality gate. Also use when a push is blocked by the agentic-preflight pre-push hook.
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
3. **Never invent finding IDs or stages.** You submit `path`, `line`, `severity`,
   `action`, `title`, `detail`, `suggestion`. Nothing else. Sending `id` or `stage`
   is a hard validation error, not a nudge.
4. **Never run `git push --no-verify`.** It exists for humans, not for you.
5. **Never push or open a PR without asking the user in plain language first.**
   Show them what will happen and wait for an actual answer.
6. **Never resolve a merge-back conflict.** Paste the resolution block and stop.
7. **Keep the validation checkout clean for the whole run.** The default
   `in_place` mode uses the current checkout, so only deliberate repair commits may
   move its branch; uncommitted changes or an unaccounted commit stop the run.
   `.agentic-preflight.toml` must be committed **before `start`** and must not be edited
   mid-run. In `reusable` or `strict` mode, make repairs only in the absolute
   `worktree_path` returned by the CLI.

## The loop

```
$ agentic-preflight start --intent "<the user's objective and acceptance criteria>"
{"ok":true,"run_id":"r_4f2a","state":"REVIEW_AWAITING_FINDINGS",
 "data":{"worktree_path":"/repos/my-project","worktree_mode":"in_place","changed_files":["src/auth.py"]},
 "next":{"instruction":"Fetch the diff before judging it.","command":"agentic-preflight context"}}

$ agentic-preflight context
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS",
 "data":{"diff":"diff --git a/src/auth.py ...","changed_files":["src/auth.py"]},
 "next":{"command":"agentic-preflight submit-findings --file findings.json"}}

# You read the diff and decide. Write findings.json, then:
$ agentic-preflight submit-findings --file findings.json
{"ok":true,"state":"REVIEW_AWAITING_RESPONSES","blocking":[{"id":"F001","severity":"high",...}],
 "next":{"command":"agentic-preflight respond --id F001 --action fixed --commit <sha>"}}

# Fix it in data.worktree_path, commit there, then:
$ cd /repos/my-project && git add -A && git commit -m "use constant-time compare"
$ agentic-preflight respond --id F001 --action fixed --commit 9c3d1ab
{"ok":true,"state":"REVIEW_FIXING","next":{"command":"agentic-preflight verify"}}

$ agentic-preflight verify
{"ok":true,"state":"REVIEW_GREEN","next":{"command":"agentic-preflight stage run test"}}

# For a documentation/CI-configuration-only diff, verify instead records test as
# skipped and returns TEST_GREEN with the docs command as next. Obey the envelope.

$ agentic-preflight stage run test
{"ok":true,"state":"TEST_GREEN","next":{"command":"agentic-preflight context --section docs"}}

$ agentic-preflight context --section docs
{"ok":true,"state":"DOCS_AWAITING_FINDINGS","data":{"doc_surface":[{"path":"README.md",...}]},
 "next":{"command":"agentic-preflight submit-findings --file findings.json"}}

$ agentic-preflight submit-findings --file findings.json     # often just {"findings": []}
{"ok":true,"state":"DOCS_GREEN","next":{"command":"agentic-preflight stage run lint"}}

$ agentic-preflight stage run lint
{"ok":true,"state":"LINT_GREEN","next":{"command":"agentic-preflight mergeback"}}

$ agentic-preflight mergeback
{"ok":true,"state":"VERIFIED","data":{"worktree_mode":"in_place","applied":[],"tree_equivalent":true},
 "next":{"command":"agentic-preflight gate"}}

$ agentic-preflight gate
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"a1b2c3d4","commits":[...]},
 "next":{"command":"agentic-preflight push --confirm a1b2c3d4"}}

# STOP. Show the user the remote, branch, and commits. Ask. Only then:
$ agentic-preflight push --confirm a1b2c3d4

# Open the PR when the workflow calls for one. After it merges, preview cleanup:
$ agentic-preflight pr --title "Use constant-time password comparison"
$ agentic-preflight ci
$ agentic-preflight cleanup

# STOP. Show every worktree and local/remote branch in the preview. Ask. Only then:
$ agentic-preflight cleanup --confirm c4d5e6f7
$ git pull --ff-only

# Without a PR, close and reclaim the run directly:
$ agentic-preflight finish
$ agentic-preflight gc
```

Work happens in the absolute **validation checkout** named by `worktree_path`. In the
default `in_place` mode that is the current PR checkout; in `reusable` and `strict`
modes it is an isolated worktree. Never assume `cd` persists between tool calls.
The complete command and option reference is in `reference/commands.md`; use it when
an envelope calls for a command or recovery path not expanded in this playbook.

## How to review

Judge the diff, not the repo. Only findings against changed files are accepted.

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
{"findings": [
  {"path": "src/auth.py", "line": 42, "severity": "high", "action": "auto_fix",
   "title": "Password compared with ==",
   "detail": "Timing-variable comparison leaks length. Use secrets.compare_digest.",
   "suggestion": "if secrets.compare_digest(supplied, stored):"}
]}
```

**No `id`. No `stage`.** Both are assigned by the CLI. IDs run `F001`, `F002`, …
continuously across the whole run — docs findings continue review numbering, they do
not restart. Full field reference: `reference/findings-schema.md`.

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

**CI failed (`CI_FAILED`).** Read every entry in `data.failed_logs`. Repairs are
host-driven: fix the source branch yourself; agentic-preflight must never invoke a model.
Preserve `data.intent`, abort the completed run, commit the repair, and execute the
provided fresh-start command. Do not push the repair until the new synchronized
review → test → docs → lint run reaches green. Then update the PR and run
`agentic-preflight ci` again. Continue until the PR merges, closes, or monitoring times out.

**Stale head (exit 3, `stale_run`).** The branch moved after review began, so
everything verified so far describes a tree that no longer exists. There is no partial
recovery: run `agentic-preflight abort --force`, then run the fresh `start` command from
the abort response. It preserves the original user intent.

**Diff too large (exit 2, `diff_too_large`).** The diff is never truncated, so
reviewing part of it is not an option. Look at `data.by_file`; if the bulk is generated
(lockfiles, vendored code, snapshots), add those globs to `[diff] exclude`. Raise
`[diff] max_bytes` only if the change genuinely is that large.

**No command configured (exit 2, `needs_command`).** Pick from `data.candidates` and
re-invoke with `--command`. Offer to write it into `[commands]` so it is settled. If
`candidates` is empty, the repo simply has no manifest detection understands (Unity,
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
- anything you resolved as `ask_user`, and what you decided
- any finding you dismissed, and why

Then ask, plainly: *"Ready to push this to `origin/feature-x`?"* Wait for a real
answer. "Proceed" from a previous step is not consent for this one.

For `ask_user` findings, present the trade-off and let them choose. Do not present a
decision you have already made as if it were a question.

The PR title uses an explicit `--title` first, then `[publish] pr_title`, then the
branch name, and only falls back to a commit subject when no branch is available.
Branch names are often poor human-facing titles, so offer a better one at the gate.

## What to publish, and what it proves

Publish the **findings** to the PR body (`pr` passes `--body`): id, severity, path,
and the commit that resolved each. That is the part CI cannot reproduce — no test
suite tells a reviewer which judgment calls were made — and it stops a human
re-deriving what the gate already caught.

The commit's Git-note attestation already carries the local stage commands, exit codes,
and output hashes. Do not copy those into the PR body; if the repo runs CI, point at
the forge's execution for stronger, remote evidence.

Publish the gaps in the same breath: a bypassed hook, a stage that could not run, a
SHA with no green run. An attestation that can only report success is marketing, and a
partial record that reads as complete is worse than none.

State the limit plainly when you show it: this proves what the gate *reported*, not
that the review was good. The same diff reviewed twice can yield different findings.
It is an audit trail, not a quality proof, and it substitutes for neither CI nor a
human reviewer.

## Cleanup after a merge

After a PR is merged, run `cleanup` without a token. It verifies the merge through
`gh` and returns an exact preview of the worktree, `ap/*` branch, PR source branch, and
remote branch. **Show that preview and ask the user.** Only after they agree, run the
returned `cleanup --confirm TOKEN` command. Cleanup re-checks the merge, switches a
clean source checkout to the base branch when necessary, then removes only that run's
worktree and local/remote branches. It never performs a blanket `ap/*` deletion.
After confirmed cleanup succeeds, always run `git pull --ff-only` in the source
checkout so its base branch is fast-forwarded to the merged result.

For a pushed run with no PR, follow `finish` with `gc`. `gc` compares original fixes
with post-mergeback history using stable patch IDs. Only patch-equivalent fixes are
reclaimed automatically; anything unmerged is retained unless the user explicitly
chooses `--force`. Run directories remain because they hold durable stage logs.
