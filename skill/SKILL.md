---
name: agentic-cli
description: Use when shipping a branch — reviewing, documenting, linting, testing, and pushing work behind a quality gate. Also use when a push is blocked by the agentic-cli pre-push hook.
---

# agentic-cli

You review, judge, and fix. The CLI holds all state and tells you what to do next.
Python here never calls a model — every judgment in this workflow is yours.

## Non-negotiables

1. **You think, the CLI holds state.** Never guess where a run is. Ask `status`.
2. **Parse stdout as JSON and obey `next`.** Every command prints exactly one JSON
   object. `next.command` is the single next legal move. Follow it.
3. **Never invent finding IDs or stages.** You submit `path`, `line`, `severity`,
   `action`, `title`, `detail`, `suggestion`. Nothing else. Sending `id` or `stage`
   is a hard validation error, not a nudge.
4. **Never run `git push --no-verify`.** It exists for humans, not for you.
5. **Never push or open a PR without asking the user in plain language first.**
   Show them what will happen and wait for an actual answer.
6. **Never resolve a merge-back conflict.** Paste the resolution block and stop.

## The loop

```
$ agentic-cli start
{"ok":true,"run_id":"r_4f2a","state":"REVIEW_AWAITING_FINDINGS",
 "data":{"worktree_path":"/repo/.git/agentic-cli/worktrees/r_4f2a","changed_files":["src/auth.py"]},
 "next":{"instruction":"Fetch the diff before judging it.","command":"agentic-cli context"}}

$ agentic-cli context
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS",
 "data":{"diff":"diff --git a/src/auth.py ...","changed_files":["src/auth.py"]},
 "next":{"command":"agentic-cli submit-findings --file findings.json"}}

# You read the diff and decide. Write findings.json, then:
$ agentic-cli submit-findings --file findings.json
{"ok":true,"state":"REVIEW_AWAITING_RESPONSES","blocking":[{"id":"F001","severity":"high",...}],
 "next":{"command":"agentic-cli respond --id F001 --action fixed --commit <sha>"}}

# Fix it IN THE WORKTREE (data.worktree_path), commit there, then:
$ cd /repo/.git/agentic-cli/worktrees/r_4f2a && git add -A && git commit -m "use constant-time compare"
$ agentic-cli respond --id F001 --action fixed --commit 9c3d1ab
{"ok":true,"state":"REVIEW_FIXING","next":{"command":"agentic-cli verify"}}

$ agentic-cli verify
{"ok":true,"state":"REVIEW_GREEN","next":{"command":"agentic-cli context --section docs"}}

$ agentic-cli context --section docs
{"ok":true,"state":"DOCS_AWAITING_FINDINGS","data":{"doc_surface":[{"path":"README.md",...}]},
 "next":{"command":"agentic-cli submit-findings --file findings.json"}}

$ agentic-cli submit-findings --file findings.json     # often just {"findings": []}
{"ok":true,"state":"DOCS_GREEN","next":{"command":"agentic-cli stage run lint"}}

$ agentic-cli stage run lint
{"ok":true,"state":"LINT_GREEN","next":{"command":"agentic-cli stage run test"}}

$ agentic-cli stage run test
{"ok":true,"state":"TEST_GREEN","next":{"command":"agentic-cli mergeback"}}

$ agentic-cli mergeback
{"ok":true,"state":"VERIFIED","data":{"tree_equivalent":true},
 "next":{"command":"agentic-cli gate"}}

$ agentic-cli gate
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"a1b2c3d4","commits":[...]},
 "next":{"command":"agentic-cli push --confirm a1b2c3d4"}}

# STOP. Show the user the remote, branch, and commits. Ask. Only then:
$ agentic-cli push --confirm a1b2c3d4
```

Work happens in the **worktree**, never in the user's tree. Use the absolute
`worktree_path` from `context`; do not assume `cd` persists between your tool calls.

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
| 10 | Hook blocked a push | Run the gate: `agentic-cli start` |

**Universal recovery rule: any exit 3 → run `status` → obey `next`.** `status` is legal
in every state. If you are ever unsure where you are, that is always the right call.

## Failure playbooks

**Merge-back conflict (exit 4).** The branch has already been restored exactly and
your fix commits are safe in the worktree. Paste `data.resolution` to the user
verbatim and **stop**. Do not cherry-pick, do not force, do not pick a side. A
conflict is a content decision and it is not yours to make.

**Stage red after max attempts (exit 4).** Stop retrying — you have already tried
`max_attempts` times and the tool is telling you the loop is not converging. Show the
user `agentic-cli logs --stage <name>` output and ask how to proceed.

**Stale head (exit 3, `stale_run`).** The branch moved after review began, so
everything verified so far describes a tree that no longer exists. There is no partial
recovery: run `agentic-cli start` for a fresh run.

**Diff too large (exit 2, `diff_too_large`).** The diff is never truncated, so
reviewing part of it is not an option. Look at `data.by_file`; if the bulk is generated
(lockfiles, vendored code, snapshots), add those globs to `[diff] exclude`. Raise
`[diff] max_bytes` only if the change genuinely is that large.

**No command configured (exit 2, `needs_command`).** Pick from `data.candidates` and
re-invoke with `--command`. Offer to write it into `[commands]` so it is settled.

**Copy refused (exit 3).** A `copy_files` entry is not gitignored. Do not work around
it — tell the user to gitignore and commit it first. This guard prevents a secret
being committed and pushed.

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
