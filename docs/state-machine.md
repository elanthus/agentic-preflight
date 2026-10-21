# State-machine guarantees and recovery

Agentic Preflight uses a transition table to order a run.
Coordinators validate each action.
Persistent records let work continue across CLI invocations.
A legal transition establishes ordering.
It does not establish that a review was useful, that evidence is fresh, or that an external Git operation completed.

This guide describes the implemented boundaries.
It is for maintainers who change the workflow or diagnose an interrupted run.

## Flow and ownership

A successful run visits the stages in this order:

```text
start
  CREATED -> WORKTREE_READY -> SYNC_RUNNING -> SYNC_GREEN
                                  |-> SYNC_CONFLICT   (abort only)
                                  '-> SETUP_FAILED    (abort only)

review
  SYNC_GREEN -> REVIEW_AWAITING_FINDINGS -> REVIEW_GREEN
                  |-> REVIEW_BLOCKED -> REVIEW_GREEN
                  '-> REVIEW_COMMAND_RUNNING -> REVIEW_AWAITING_FINDINGS
                        '-> REVIEW_COMMAND_RED -> REVIEW_COMMAND_RUNNING

documentation
  REVIEW_GREEN -> DOCS_AWAITING_FINDINGS -> DOCS_GREEN
       |            '-> DOCS_BLOCKED -> DOCS_GREEN
       '-> DOCS_GREEN   (SKIP_DOCS)

lint and local tests
  DOCS_GREEN -> LINT_RUNNING -> LINT_GREEN -> TEST_RUNNING -> TEST_GREEN
                  '-> LINT_RED       |           '-> TEST_RED
                                     '-> TEST_GREEN   (SKIP_TEST)

merge-back and publication
  TEST_GREEN -> MERGEBACK_PENDING -> VERIFIED -> AWAITING_PUSH_CONFIRM -> PUSHED -> DONE
                  '-> MERGEBACK_CONFLICT -> MERGEBACK_PENDING
```

Three kinds of transition do not appear in that picture:

- **Restart.** `LINT_FIX_RESTART`, `TEST_FIX_RESTART`, and `INVALIDATE_REVIEW` return the run to `REVIEW_AWAITING_FINDINGS`.
  `INVALIDATE_REVIEW` is legal from every review, documentation, green lint, green test, and merge-back state.
  A red lint or test stage restarts after a committed repair.
  `RETRY_LINT`, `RETRY_TEST`, and `RETRY_REVIEW_COMMAND` return a red stage to its running state.
- **Disposition.** `RESPOND` keeps the run in `REVIEW_BLOCKED`, `REVIEW_GREEN`, `DOCS_BLOCKED`, or `DOCS_GREEN`.
  `RESOLVE_GREEN` moves a blocked stage to green.
- **Closing.** `ABORT` and `ORPHAN` are legal from every nonterminal state.
  `DONE`, `ABORTED`, and `ORPHANED` are terminal.

Review and documentation findings can block progress.
Failed shell stages retry within their attempt limits. Committed repairs require renewed review.
A run whose validation returns to review `[stage] max_restarts` times stops for a person.
Then only the closing transitions are legal.
Disabled documentation and inapplicable local tests use explicit skip transitions and keep their reasons.

The table below lists every transition except the closing transitions.
`tests/test_docs_match_cli.py` compares it with `machine.py`.

<!-- transitions:start -->
| State | Action | Next state |
| --- | --- | --- |
| `CREATED` | `CREATE_WORKTREE` | `WORKTREE_READY` |
| `WORKTREE_READY` | `BEGIN_SYNC` | `SYNC_RUNNING` |
| `SYNC_RUNNING` | `SYNC_PASSED` | `SYNC_GREEN` |
| `SYNC_RUNNING` | `SYNC_FAILED` | `SYNC_CONFLICT` |
| `SYNC_RUNNING` | `SETUP_FAILED` | `SETUP_FAILED` |
| `SYNC_GREEN` | `BEGIN_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `REVIEW_AWAITING_FINDINGS` | `SUBMIT_CLEAN` | `REVIEW_GREEN` |
| `REVIEW_AWAITING_FINDINGS` | `SUBMIT_BLOCKING` | `REVIEW_BLOCKED` |
| `REVIEW_AWAITING_FINDINGS` | `RUN_REVIEW_COMMAND` | `REVIEW_COMMAND_RUNNING` |
| `REVIEW_AWAITING_FINDINGS` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `REVIEW_COMMAND_RUNNING` | `REVIEW_COMMAND_PASSED` | `REVIEW_AWAITING_FINDINGS` |
| `REVIEW_COMMAND_RUNNING` | `REVIEW_COMMAND_FAILED` | `REVIEW_COMMAND_RED` |
| `REVIEW_COMMAND_RED` | `RETRY_REVIEW_COMMAND` | `REVIEW_COMMAND_RUNNING` |
| `REVIEW_BLOCKED` | `RESPOND` | `REVIEW_BLOCKED` |
| `REVIEW_BLOCKED` | `RESOLVE_GREEN` | `REVIEW_GREEN` |
| `REVIEW_BLOCKED` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `REVIEW_GREEN` | `RESPOND` | `REVIEW_GREEN` |
| `REVIEW_GREEN` | `BEGIN_DOCS` | `DOCS_AWAITING_FINDINGS` |
| `REVIEW_GREEN` | `SKIP_DOCS` | `DOCS_GREEN` |
| `REVIEW_GREEN` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `DOCS_AWAITING_FINDINGS` | `SUBMIT_CLEAN` | `DOCS_GREEN` |
| `DOCS_AWAITING_FINDINGS` | `SUBMIT_BLOCKING` | `DOCS_BLOCKED` |
| `DOCS_AWAITING_FINDINGS` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `DOCS_BLOCKED` | `RESPOND` | `DOCS_BLOCKED` |
| `DOCS_BLOCKED` | `RESOLVE_GREEN` | `DOCS_GREEN` |
| `DOCS_BLOCKED` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `DOCS_GREEN` | `RESPOND` | `DOCS_GREEN` |
| `DOCS_GREEN` | `RUN_LINT` | `LINT_RUNNING` |
| `DOCS_GREEN` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `LINT_RUNNING` | `LINT_PASSED` | `LINT_GREEN` |
| `LINT_RUNNING` | `LINT_FAILED` | `LINT_RED` |
| `LINT_GREEN` | `RUN_TEST` | `TEST_RUNNING` |
| `LINT_GREEN` | `SKIP_TEST` | `TEST_GREEN` |
| `LINT_GREEN` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `LINT_RED` | `RETRY_LINT` | `LINT_RUNNING` |
| `LINT_RED` | `LINT_FIX_RESTART` | `REVIEW_AWAITING_FINDINGS` |
| `TEST_RUNNING` | `TEST_PASSED` | `TEST_GREEN` |
| `TEST_RUNNING` | `TEST_FAILED` | `TEST_RED` |
| `TEST_GREEN` | `BEGIN_MERGEBACK` | `MERGEBACK_PENDING` |
| `TEST_GREEN` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `TEST_RED` | `RETRY_TEST` | `TEST_RUNNING` |
| `TEST_RED` | `TEST_FIX_RESTART` | `REVIEW_AWAITING_FINDINGS` |
| `MERGEBACK_PENDING` | `MERGEBACK_OK` | `VERIFIED` |
| `MERGEBACK_PENDING` | `MERGEBACK_FAILED` | `MERGEBACK_CONFLICT` |
| `MERGEBACK_PENDING` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `MERGEBACK_CONFLICT` | `MERGEBACK_RETRY` | `MERGEBACK_PENDING` |
| `MERGEBACK_CONFLICT` | `INVALIDATE_REVIEW` | `REVIEW_AWAITING_FINDINGS` |
| `VERIFIED` | `GATE` | `AWAITING_PUSH_CONFIRM` |
| `AWAITING_PUSH_CONFIRM` | `PUSH` | `PUSHED` |
| `PUSHED` | `FINISH` | `DONE` |
<!-- transitions:end -->

| Obligation | Owner | What establishes it |
| --- | --- | --- |
| Required stages occur in order | `machine.py` | Legal `(state, action)` pairs. Graph reachability tests include the explicit skips |
| An action has sufficient evidence | `runs/review.py`, `runs/resolve.py`, `runs/stages.py` | Validated submissions, finding dispositions, command results, and coordinator preconditions |
| Evidence applies to the current inputs | `runs/review_coverage.py`, `runs/evidence.py`, `refresh_validation.py` | Snapshot bindings, review and docs fingerprints, and conservative applicability decisions |
| Concurrent commands do not overwrite a run | `cli_support.py`, `store.py` | Per-run operation locks, record locks, an events lock for appends, and optional sequence checks |
| Each local record replacement is atomic | `store.py` | Findings are replaced before the run under the record lock. The pair is best-effort across an interruption |
| Source content matches the completed validation | `runs/mergeback.py` | Recorded attempt inputs and Git tree equality. Changed resolutions reopen review |
| Publication has the required evidence | `runs/publish.py`, `attestation.py` | Freshness and attestation checks before an atomic branch and notes push |

Duplicate actions within a state declaration are rejected before the transition dictionary is constructed.
Exhaustive reachability tests prove the ordering properties of that graph.
They do not exercise coordinator guards, persistence, external commands, or reviewer judgment.
Integration and failure-injection tests cover those separate obligations.

`RunDoc` contains both the lifecycle state and the supporting evidence.
Its schema validates the record structure.
`store.py` checks the lifecycle relationships on every transaction write.
Reads are not checked, so recovery can inspect a record that the current version would refuse to write.
Coordinators must still establish the necessary evidence before they select a transition.
An action added to the table without those checks is not safe.
`stale` is the one lifecycle flag outside the enum.
It can annotate many states. It always means "start again".

## Local record commits

Finding submission, finding disposition, and evidence import update the findings and the run state together.
They use `Store.transaction(..., findings=...)`:

1. Hold the run-record lock.
2. Prepare and validate the new run without a change to either file.
3. Atomically replace `findings.json`, then `run.json`.

An exception in the transaction body leaves both records unchanged.
Individual replacements use flushed, synchronized temporary files.
A failure between the two replacements can leave new findings beside the previous run state and sequence.
The next read returns the saved records. There is no automatic roll-forward recovery.
Run `status` after an interruption and inspect the findings before you continue.
The next `start` retires interrupted legacy runs that a pending journal identifies.
It starts fresh and preserves user work.
See [compatibility](../COMPATIBILITY.md#saved-pending-run-updates).
Events and reviewer-comparison artifacts are separate diagnostic records.
They can also be absent after an interruption. They do not rebuild the run state automatically.

Use the store APIs to inspect active records.
A direct read of the JSON files can observe an in-progress installation.
Each API read is protected. Separate read calls are not a shared snapshot across concurrent commands.

## Git side effects and retry

Git changes and local records cannot share the file-store transaction.
Thus, recovery checks the external result before it decides whether an action must run again.

| Interruption | Recovery |
| --- | --- |
| Lint inputs change after lint completed | Evidence invalidation can leave `LINT_GREEN`. It preserves the original evidence and requires the affected stages again. Unchanged earlier stages can be reused. |
| Merge-back has not changed the source | Retry against the recorded source and validation snapshot. |
| The source synchronization completed before fix application | If the source has not reached the recorded validation tree, preserve the work and mark the run stale. Follow `status` to start a fresh run for the current source. |
| A conflict retry was recorded before the interruption | Preserve its retry context and compare against the attempt's source commit. An identical resolution keeps the validation. A different resolution reopens review. |
| Merge-back changed the source, but attestation or state persistence failed | With no Git operation in progress, retry accepts the completed result only under these conditions: the source branch and validation snapshot still match the attempt, the source tree equals the recorded validated tree, and the synchronized base is still an ancestor. Existing dirty-path checks still apply. The fixes are not cherry-picked again. |
| A pending merge-back finds different source content, branch, or validation snapshot | Preserve the work, mark the run stale, and direct recovery through `status` to a fresh run. Do not adopt the changed content as verified. |
| Synchronization fails at `start` (conflict, unreachable remote, missing base, Git or OS error) | Record `SYNC_FAILED` into `SYNC_CONFLICT`. Append a `sync_conflict` or `sync_failed` event with the detail. Return `abort --force`. |
| A Git sequence is in progress in the validation worktree at `start` | Record `SYNC_FAILED` into `SYNC_CONFLICT`. Append a `sync_refused` event that names the operation. Return `git status`. |
| `start` was killed while the run was in `SYNC_RUNNING` | `status` and a resuming `start` return `abort --force`. Abort is legal from every nonterminal state. |
| A Git sequence is still in progress | Report the operation. Never finish or abort a user-owned sequence automatically. |
| The remote push succeeded, but local `PUSHED` persistence failed | With an unchanged source and valid evidence, retry the same atomic push and then record completion. Remote changes or a rejected push stay errors. A retry never force-pushes. |

The merge-back attempt records the source commit, validation commit, validation tree, and conflict-retry context before it modifies the source.
A pending attempt can resume against the unchanged source.
It can also reconcile the completed validation tree under the bindings above.
An intermediate source synchronization that has not reached that tree makes the run stale.
A fresh run is required for the current source.

Reconciliation establishes equivalent reviewed content under these bindings.
It does not authenticate who produced the current Git history.
An in-progress Git sequence or an unknown intermediate tree is not treated as a completed merge-back.

`status` is an inspection and recovery command. It is not a read-only endpoint.
Store reads return saved local records.
`status` can discover and import reusable evidence and release terminal ownership pointers.
It does not complete a pending merge-back itself. It directs the caller to `mergeback` for reconciliation.

## Saved-record boundary and limits

Run storage requires schema version 2.
Any other schema is rejected with a diagnostic.
The record, its worktree, its branch, and its ownership pointers stay untouched.
Portable attestations use schema version 7.

Review coverage records a snapshot-bound assertion that every delivered unit was examined.
It does not prove understanding or defect detection.
Git notes stay mutable, unsigned audit records. The local hook stays advisory.
See [limits](limits.md) and [attestation enforcement](attestations-and-ci.md).

The executable recovery examples are in `tests/test_store.py`, `tests/test_stage_recovery.py`, `tests/test_mergeback.py`, `tests/test_evidence_refresh.py`, and `tests/test_publish.py`.
They complement the ordering checks in `tests/test_machine_properties.py`.
