# State-machine guarantees and recovery

Agentic Preflight uses a transition table to order a run, coordinators to validate
each action, and persistent records to resume work across CLI invocations. A legal
transition establishes ordering. It does not, by itself, establish that a review
was useful, that evidence is fresh, or that an external Git operation completed.

This guide describes the implemented boundaries for maintainers changing the
workflow or diagnosing an interrupted run.

## Flow and ownership

```text
sync -> review -> documentation -> lint -> local tests -> merge-back -> VERIFIED
                                     \-> delegated tests -> merge-back -> PUBLICATION_READY

VERIFIED or PUBLICATION_READY -> gate -> push -> finish
```

Review and documentation findings can block progress. Failed shell stages retry
within their attempt limits; committed repairs require renewed review. Disabled
documentation and inapplicable local tests use explicit skip transitions and retain
their reasons. Delegated tests remain pending: publication readiness does not
establish merge readiness.

| Obligation | Owner | What establishes it |
| --- | --- | --- |
| Required gates occur in order | `machine.py` | Legal `(state, action)` pairs; graph reachability tests include explicit skips and delegation |
| An action has sufficient evidence | `runs/review.py`, `runs/resolve.py`, `runs/stages.py` | Validated submissions, finding dispositions, command results and coordinator preconditions |
| Evidence applies to current inputs | `runs/review_coverage.py`, `runs/evidence.py`, `refresh_validation.py` | Snapshot bindings, declared input fingerprints and conservative applicability decisions |
| Concurrent commands do not overwrite a run | `cli_support.py`, `store.py` | Per-run operation locks, record locks and optional sequence checks |
| Related run and finding changes recover together | `store.py` | A committed update journal and recovery before either record is read |
| Source content matches completed validation | `runs/mergeback.py` | Recorded attempt inputs and Git tree equality; changed resolutions reopen review |
| Publication has the required evidence | `runs/publish.py`, `attestation.py` | Freshness and attestation checks before an atomic branch/notes push |
| Delegated tests and approval permit merge | `ci_authority.py`, `ci_merge.py` | Current integration candidate, trusted CI attempt and protected approval policy |

Duplicate actions within a state declaration are rejected before construction of
the transition dictionary. Exhaustive reachability tests prove the ordering
properties of that graph. They do not exercise coordinator guards, persistence,
external commands or reviewer judgment; integration and failure-injection tests
cover those separate obligations.

`RunDoc` contains both lifecycle state and supporting evidence. Its schema validates
record structure, not every relationship between those fields. Coordinators must
check the relevant invariants before choosing a transition. Adding an action to
the table without those checks does not make that action safe.

## Local record commits

Finding submission, finding disposition and evidence import update findings and
run state together through `Store.transaction(..., findings=...)`:

1. Hold the run-record lock and finish any pending update.
2. Prepare the complete new run and findings records without changing either file.
3. Atomically publish `pending-update.json`. This is the logical commit point.
4. Install `findings.json` and `run.json`, then remove the journal.

Before the journal is published, failure leaves the prior records intact. After
publication, an installation error may mean the update committed even though the
CLI returned an error. The next store read or transaction validates the retained
journal and finishes installation under the same lock before returning a record.
Recovery checks run identity and sequence to avoid applying a journal over an
unrelated or newer record. Invalid or unreadable journals remain available for
inspection and prevent reads from silently returning partial state.

If a submission committed before interruption, repeating `submit-findings` is
rejected because the stage already advanced. Run `status` and continue from its
next command; the retry does not append the same findings again. This provides
recovery of the committed effect, not a replay of the original response envelope.

Individual file replacements and the journal use flushed, synchronized temporary
files. These guarantees cover process interruption and recoverable I/O failures;
they are not a claim of recovery from arbitrary filesystem corruption or power
loss. Events and reviewer-comparison artifacts are separate diagnostic records and
can be absent after interruption. They are not a transaction log used to rebuild
authoritative run state.

Use store APIs when inspecting active records. Reading the JSON files directly
can observe an interrupted installation. Each API read is protected, but separate
read calls are not a shared snapshot across concurrent commands.

## Git side effects and retry

Git changes and local records cannot share the file-store transaction. Recovery
therefore checks the external result before deciding whether an action needs to
run again.

| Interruption | Recovery |
| --- | --- |
| Lint inputs change after lint completed | Evidence invalidation can leave `LINT_GREEN`, preserving original evidence and requiring affected stages again. Unchanged earlier stages may be reused. |
| Merge-back has not changed the source | Retry against the recorded source and validation snapshot. |
| The source rebase completed before fix application | If its tree matches the recorded synchronized baseline and contains the synchronized base, continue applying fixes without rebasing again. A different tree is rejected. |
| A conflict retry was recorded before interruption | Preserve its retry context and compare against the attempt's source commit. An identical resolution retains validation; a different resolution reopens review. |
| Merge-back changed the source, but attestation or state persistence failed | With no Git operation in progress, retry accepts the completed result only when the source branch and validation snapshot still match the attempt, the source tree equals the recorded validated tree, and the synchronized base remains an ancestor. Existing dirty-path checks still apply. It does not cherry-pick the fixes again. |
| Pending merge-back encounters different source content, branch or validation snapshot | Preserve the work, mark the run stale and direct recovery through `status` to a fresh run. Do not adopt the changed content as verified. |
| A Git sequence is still in progress | Report the operation. Never automatically finish or abort a user-owned sequence. |
| Remote push succeeded but local `PUSHED` persistence failed | With unchanged source and valid evidence, retry the same atomic push and then record completion. Remote changes or a rejected push remain errors; retry never force-pushes. |

The merge-back attempt records the source commit, validation commit, validation
tree and conflict-retry context before modifying the source. When the original
source needs rebasing, it also records the expected tree from the synchronized
validation baseline, before isolated fixes. This recognizes a completed rebase
even if the process exits before recording its completion. It permits further
fix application, not publication of the intermediate tree. A manually changed
source cannot borrow the original baseline as an expected rebase result.

Reconciliation establishes equivalent reviewed content under these bindings; it
does not authenticate who produced the current Git history. An in-progress Git
sequence or an unknown intermediate tree is not treated as a completed merge-back.

`status` is an inspection and recovery command, not a read-only endpoint. Store
reads can finish committed local updates; status can also discover/import reusable
evidence and release terminal ownership pointers. It does not complete a pending
merge-back itself: it directs the caller to `mergeback` for reconciliation.

## Compatibility and limits

Existing runs without `mergeback_attempt` remain readable. They retain conservative
stale-head behavior because there is no recorded attempt against which to reconcile
a moved source. The journal is local storage only; portable attestation schemas and
their protected-base consumer contract are unchanged.

Use the same compatible CLI version for commands sharing active run storage.
Older tools do not understand the journal protocol and may reject the additional
run field. Do not downgrade while an update or merge-back is pending; preserve the
records and use a compatible version to recover them.

Review coverage records a snapshot-bound assertion that every delivered unit was
examined. It does not prove understanding or defect detection. Git notes remain
mutable, unsigned audit records, and the local hook remains advisory. See
[limits](limits.md) and [attestation compatibility](schema-compatibility.md).

The executable recovery examples are in `tests/test_transaction_recovery.py`,
`tests/test_mergeback.py`, `tests/test_evidence_refresh.py` and
`tests/test_publish.py`. They complement the ordering checks in
`tests/test_machine_properties.py`.
