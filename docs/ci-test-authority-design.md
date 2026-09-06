# Trusted CI test authority

Issue #86 introduces an opt-in publication path. The default remains local review,
documentation review, lint, and tests. The selected CI subject is the integration
commit: GitHub's merge of the current PR head with its current base.

## Two predicates

`verify --purpose publish` accepts complete local evidence or schema 6 evidence
with review, docs, and lint satisfied and tests explicitly delegated. It does not
say tests passed. The gate and pre-push hook use this predicate so the first push
can create CI work. `verify` retains its complete-local default; it rejects a
delegation with instructions to use `ci status`. `ci status --pr N` is the merge
predicate: it retrieves current GitHub evidence and combines it with local
attestation validation and existing approval policy. Missing authority is pending
or unavailable, never success. Publication authorization remains a separate gate.

Schema 6 stores the protected CI policy declaration and its original base revision,
plus the original local evidence for review, docs, and lint. Its test stage is
`delegated`, with no command, exit code, output digest, execution time, or synthetic
test evidence. `green_at` is null; `publication_ready_at` describes publication
readiness. Schemas 4 and 5 retain their existing wire format. Old consumers reject
schema 6. An explicit schema-6 consumer declaration must already be committed to
the protected base before a producer may delegate.

## Protected execution and authority

The protected base declares the repository ID, base branch, numeric workflow ID,
workflow path, dedicated check App ID, required job names (including each matrix leg), and expiry interval.
The PR's effective declaration must match that policy. The current base also
controls mandatory local review, docs, lint, and human approval requirements.
The GitHub branch API must confirm the base is protected at the expected SHA;
an unprotected retarget cannot bootstrap its own trust.

A dedicated `workflow_dispatch` workflow is dispatched on the protected base
branch. Its API run `head_sha` must equal the current protected policy revision;
its repository, workflow ID, path, event, and candidate identity must all match.
The candidate identity in the run title binds PR number, head, base, merge commit,
merge tree, and policy digest. A trusted preparation job validates those inputs
against the PR and commit APIs before allowing tests to run. The evaluator requires
that preparation job and every declared test job and mandatory execution step to
succeed. Thus a matching display name from a PR-defined workflow is insufficient.

Each test job checks out the explicit validated integration SHA with persisted
credentials disabled, verifies its HEAD, and runs the protected workflow's tests.
Test jobs have read-only contents permissions, no secrets, no write credentials,
and no shared caches. Proposed source runs only in those isolated jobs. The
dispatcher and evaluator use protected code; neither checks out or executes PR
code, local actions, dependency installers, or artifact contents. Git objects and
notes may be fetched as data. API responses, not agent JSON, logs, caches, or
uploaded pass artifacts, establish CI results. Fork and Dependabot PRs use the
same protected dispatch path without gaining privileged test credentials.

## State and persistence

The local test command transitions from `lint_green` to `test_delegated` without
spawning a test process. Mergeback writes the partial note and enters
`publication_ready`. Gate, push, and finish retain their publication lifecycle,
but every envelope carries explicit test-authority and merge-readiness fields.
Finishing publication does not record a CI success. `ci status` works after finish,
restart, or in another checkout by fetching notes and querying GitHub; no second
notes-only push is necessary. Local reuse from #85 remains per-stage. Source edits
invalidate affected local evidence normally; a CI rerun alone does not reopen it.

The dispatcher runs on PR creation, synchronization, reopening, retargeting, and
base updates. It first marks the combined required check pending, then dispatches
the current integration candidate. Scheduled reconciliation and explicit
`ci dispatch --pr N` recover merge-ref computation delays and missed events.
Workflow completion triggers evaluation; periodic reconciliation catches reruns,
expiry, approval changes, and API recovery. Unchanged requests are idempotent.

The latest matching workflow run supersedes older successes. Its latest attempt
alone supplies jobs; a partial rerun with missing required legs cannot inherit a
success from another attempt. Pending, skipped, neutral, cancelled, failed,
expired, or unavailable required evidence blocks merge. Run URLs and a specific
next action distinguish rerunning all jobs, dispatching a fresh candidate, fixing
source, and retrying an unavailable API. The evaluator rereads candidate, policy,
run identity, attempt, and job results before returning success.

## Required check and rollout

Install the consumer and workflow templates on the protected default/base branch
first; configure IDs and required matrix jobs there, then enable the producer in
a later PR. This implementation's own PR continues to execute tests locally.
Require the combined check, dismiss stale approvals, and require branches to be
up to date before merging. Keep CODEOWNERS protection for policy and workflows.
Restrict check-writing credentials to trusted evaluators. Require the combined
check from the dedicated App, not the generic Actions app.
Keep its private key in an environment limited to the exact protected base branch,
so a proposed workflow cannot obtain it. The evaluator validates the App identity
on every check write. Strict up-to-date rules
close the interval between an evaluator's final API read and a later base update;
this design does not claim an atomic GitHub read-and-merge transaction. Merge queues
are outside scope. Environment approval is consumed only in the protected,
environment-gated evaluator job; agents cannot grant it. Manual-merge policy
continues to prohibit agent merge and auto-merge.

GitHub documents the distinction between PR and protected-base events in
[workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
and provides authoritative [workflow run](https://docs.github.com/en/rest/actions/workflow-runs)
and [attempt-specific job](https://docs.github.com/en/rest/actions/workflow-jobs)
APIs. The implementation uses these identities rather than treating a job's name
or an uploaded result as sufficient proof of execution.

## Verification

Deterministic fake-API tests cover current integration identity, workflow and
repository substitution, policy weakening, incomplete matrices, latest attempts,
expiry, unavailable APIs, and changes during evaluation. Git fixtures cover head
and integration trees. Process counters demonstrate zero local test invocations
under delegation and unchanged execution under the default policy. Lifecycle tests
exercise persistence and explicit publication/merge results across restarts.
