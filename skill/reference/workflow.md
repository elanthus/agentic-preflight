# CLI walkthrough

Read [SKILL.md](../SKILL.md) first. Its approval, review, recovery, and cleanup
rules govern this walkthrough; examples do not grant permission to publish or merge.

## The loop

After a restack or base update, run `start` with the original intent and follow
`next.command`. With complete applicable evidence, the CLI
may reuse review, docs, lint, or test independently. `data.applicability` explains
candidate reuse, invalidation, or unknown inputs. Do not manually repeat imported
stages, edit fingerprints, or rewrite original timestamps. `status` resumes the
persisted sequence. Unknown inputs require a fresh stage; content-mode shell reuse
requires a committed repository input contract.

When `start` returns `data.housekeeping.noisy` as true, tell the user in one sentence how
much space preflight checkouts are holding (`reclaimable_bytes`) and that
`agentic-preflight gc` reclaims it. Otherwise say nothing about housekeeping.

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
{"ok":true,"state":"REVIEW_BLOCKED","blocking":[{"id":"F001","severity":"high",...}],
 "next":{"command":"agentic-preflight respond --id F001 --action fixed --commit <sha>"}}

# Fix it in data.worktree_path, commit there, then:
$ cd /repos/my-project && git add -A && git commit -m "use constant-time compare"
$ agentic-preflight respond --id F001 --action fixed --commit 9c3d1ab
{"ok":true,"state":"REVIEW_BLOCKED","next":{"command":"agentic-preflight verify"}}

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
# as skipped and returns TEST_GREEN with `mergeback` as next. Obey the envelope.

$ agentic-preflight stage run test
{"ok":true,"state":"TEST_GREEN","next":{"command":"agentic-preflight mergeback"}}

$ agentic-preflight mergeback
{"ok":true,"state":"VERIFIED","data":{"worktree_mode":"in_place","applied":[],"tree_equivalent":true},
 "next":{"command":"agentic-preflight gate"}}

$ agentic-preflight gate
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"a1b2c3d4","pr_mode":"auto","automated_cleanup":true,"commits":[...]},
 "next":{"instruction":"Substitute data.token for <token> only after user authorization.",
         "command":"agentic-preflight push --confirm <token>"}}

# Show the remote, branch, and commits. Apply non-negotiable 5: push without asking
# again when the summary matches the authorization; otherwise STOP and ask.
# Once authorized, substitute data.token:
$ agentic-preflight push --confirm <token>
$ agentic-preflight finish
$ agentic-preflight gc

# Auto PR mode: after preflight finishes, reuse an existing PR for the branch or
# create one automatically without asking about PR creation. Continue into the
# polling and cleanup flow below only when automated_cleanup is true.
$ gh pr create --title "Use constant-time password comparison" --body-file pr-body.md
$ gh pr checks --watch
$ gh pr view "$PR_URL" --json url,state,mergedAt,headRefName,headRefOid,baseRefName

# While state is OPEN, wait 5 minutes and query those same fields again.
# If it is MERGED, perform the disclosed run-scoped cleanup. If it is CLOSED
# without mergedAt, stop without deleting anything.

# Manual PR mode: never create it. Give the user the repository compare URL instead.
```

Work happens in the absolute **validation worktree** named by `worktree_path`. In the
default `in_place` mode that is the current PR checkout; in `reusable` and `strict`
modes it is an isolated validation worktree. Never assume `cd` persists between tool calls.
The complete command and option reference is in [commands.md](commands.md); use it when
an envelope calls for a command or recovery path not expanded in this playbook.

