# Command reference

Read [SKILL.md](../SKILL.md) first for approval, review, recovery, and cleanup rules.
This reference describes command behavior; it does not grant publication or merge
authorization. See the [CLI walkthrough](workflow.md) for a worked sequence.

Every agent-facing workflow command prints exactly one JSON object to stdout. Human
prose goes to stderr. Parse that stdout blindly; every key is always present. The sole
exception is `hook-check`: Git consumes its exit status and stderr, so it emits no JSON
envelope and is not an agent-facing command.

```json
{"ok": true, "run_id": "r_...", "state": "REVIEW_AWAITING_FINDINGS", "stage": "review",
 "data": {}, "blocking": [], "next": {"instruction": "...", "command": "..."},
 "error": null}
```

`next` is `null` only when there is genuinely nothing left to do. `error` is `null`
when `ok` is `true`.

Place `--run RUN_ID` before any workflow command to select a stored run explicitly.
A `RUN_ID` other than `r_` followed by 10 lowercase hex digits exits 6 with
`invalid_run_id` before any run is opened.
Without it, the command resolves the run owned by the invoking Git worktree. When the
selected run's recorded source checkout no longer exists, inspection commands remain
available, mutating commands exit 3 with `source_worktree_missing`, and `gc` remains the
recovery path from another worktree in the clone.

## Setup

### `agentic-preflight init [--force] [--no-hook]`
Installs the pre-push hook and writes `.agentic-preflight.toml` if absent. Refuses to
replace a pre-push hook it did not write (exit 3, `hook_exists`) — `--force`
overrides. A hook that is byte for byte one an earlier version installed is replaced
with the current script; a hook containing the command with other edits is left as it
is. Does not clobber an existing config. `data.hook_path` is the absolute path
Git will execute, `data.hook_installed` confirms that installation left that path
containing `agentic-preflight hook-check`, and `data.hooks_path_overridden` reports
whether the effective path is outside the common Git directory's `hooks/` directory.
With `--no-hook`, the effective path and override state are still reported, while
`hook_installed` is false.

The default `in_place` mode uses the current checkout and reports
`data.worktree_root: null`. In isolated `reusable` and `strict` modes, worktrees default
to a per-clone directory under `$XDG_CACHE_HOME/agentic-preflight/worktrees/` (or
`~/.cache/agentic-preflight/worktrees/` when `XDG_CACHE_HOME` is unset or relative), and
`data.worktree_root` reports that resolved location.

### `agentic-preflight integrations install AGENT... [--scope user|project] [--target PATH] [--force]`
Copies the bundled skill and all of its references into each selected agent's discovery
directory. `AGENT` is `codex`, `claude`, `cursor`, `opencode`, or `amp`. User scope uses
their documented global directories; project scope uses `.agents/skills`,
`.claude/skills`, `.cursor/skills`, or `.opencode/skills` as appropriate. `--target`
adds a custom skills root for another compatible agent.

Existing unmanaged or locally modified copies are never overwritten unless `--force`
is explicit. The operation preflights every destination, so a conflict cannot leave
only half of the requested integrations installed.

### `agentic-preflight integrations status [AGENT...] [--scope user|project] [--target PATH]`
Reports each copy as `missing`, `current`, `outdated`, `modified`, or `unmanaged`.
With no agents named, checks every supported integration.

### `agentic-preflight integrations update [AGENT...] [--scope user|project] [--target PATH] [--force]`
Refreshes installed copies after a CLI upgrade and skips agents where the skill is not
installed. With no agents named, checks every supported integration. Refuses to replace local
edits unless `--force` is explicit.

### `agentic-preflight integrations uninstall AGENT... [--scope user|project] [--target PATH] [--force]`
Removes copies managed by agentic-preflight. Unmanaged or locally modified directories are
preserved unless `--force` is explicit. At least one agent or custom target is required.

## Running validation

### `agentic-preflight start --intent TEXT [--base-ref REF] [--replace] [--client NAME] [--model NAME]`
Creates a run and prepares its validation worktree according to the configured
[worktree mode](https://github.com/elanthus/agentic-preflight/blob/v0.7.0/docs/worktree-modes.md).
The intent is persisted as the user's objective and acceptance criteria. The parser
accepts `start` without `--intent`, but the command then fails with exit 3 and
`intent_required`.
Different linked source worktrees may have runs active concurrently. Repeating `start` with the
same head, intent, base, and effective configuration resumes that worktree's existing run.
A nonterminal run with a legacy `pending-update.json` is instead orphaned at the next
`start`, its ownership cleared and journal removed, and a fresh run created. The old
audit record and user work remain for normal safe cleanup. `status` reports
`data.interrupted_legacy_update: true` and directs these runs to `start` without
importing evidence; validation commands exit 3 (`wrong_state`) rather than continuing
them. Restart guidance identifies the recorded source worktree. At the validation
restart limit, the existing human stop takes precedence: no restart command is
advertised, and direct `start` refuses the interrupted run until the user decides
to abort it. Explicit `abort` remains available. Completed history and runs without
a journal are unaffected.
A moved source head orphans the stale run without deleting its evidence or fixes and then
starts fresh. On an unchanged head, a different intent or configuration requires
`--replace`, which preserves the replaced run as `ORPHANED`. Age alone never expires a
run.
Before review, the command fetches the configured base from `origin` when available and
rebases the validation worktree onto that exact fresh base. In-place mode therefore
rebases the PR branch itself. When a rebase would change what merging the branch lands,
for example by dropping an `-s ours` merge that reverts a branch already in the base, it
merges the base in instead and reports `data.sync.merged: true`. A sync conflict is aborted
cleanly and reported; no conflicted rebase or merge is left in progress. Synchronization also
refuses, before changing anything, when a path added by the base or by the branch's merge
result already exists as an untracked or ignored local file, and lists those files in
`data.conflicting_files`.
`start` classifies complete local evidence per stage, including when synchronization
leaves the exact attested commit unchanged, and returns the next required
command. Equal base/head trees and matching context
can preserve review and docs across rewritten commits. Applicable lint and test stages
rerun in every new run, including commits with identical trees. Read `data.applicability`;
`invalid` and `unknown` require a fresh stage, and later-stage candidates remain available while earlier stages are pending.
Refuses a dirty tree (exit 3, `dirty_tree`) and a branch with no changes over the base
(exit 3, `empty_diff`). In-place mode protects `[worktree] copy_files` where they are;
isolated modes copy them. Every mode refuses an entry git is not already ignoring.

The new run records its producer. `data.producer` and the `run_created` event contain
the same object: `tool_version`, `python_version`, `platform`, `client`, `client_source`,
`reported_model`, and `reported_model_source`. `--client NAME` names the coding-agent
harness, for example `claude-code`, `codex`, `cursor`, `opencode`, or `amp`. The name is
stored in lower case, and `claude` becomes `claude-code`. Without `--client`, the command
records `claude-code` when `CLAUDECODE=1` is set. Otherwise `client` is `null`.
`client_source` is `flag`, `environment`, or `null`. `--model NAME` is the agent's exact
model id. The command never infers a model, so `reported_model_source` is `flag` or
`null`. These values are self-reports for the user's own telemetry. They are not
evidence: the attestation, the fingerprints, and `config_digest` do not contain them. A
resumed run keeps the producer that it recorded at creation. A `start` command that
`next.command` returns for a refused, stale, interrupted, or aborted run repeats the
`--client` and `--model` values that were given as flags, so the next run records the
same client and model. A client detected from the environment is not repeated: the
next `start` detects it again. A malformed value exits 6 with `usage_error` before a run
is created.

Returns `data.worktree_path` — **absolute**. Use it; do not rely on `cd` persisting.
See [isolated worktree locations](https://github.com/elanthus/agentic-preflight/blob/v0.7.0/docs/worktree-modes.md#why-isolated-validation-worktrees-live-outside-git)
for the default location and `[worktree] root` override.

Returns `data.housekeeping`, a read-only inventory of this clone's validation
checkouts: `checkouts` (each with `path`, `bytes`, `registered`, `leased`, `detached`,
`branch`, `owner_run_id`, `status` of `reclaimable` or `retained`, and `reason`),
`total_bytes`, `reclaimable_bytes`, `ap_branches`, `noisy` (true at 1 GiB or more
reclaimable), and `next_command`, which is `agentic-preflight gc` when anything is
reclaimable and null otherwise. A checkout is reclaimable only when it is not leased on a
branch and no run record naming it as `worktree_path` has a source worktree that still
exists. A released reusable runner whose source remains is `retained` with reason
`reusable cache`, matching `gc`. A run record that cannot be read counts as an owner. Nothing is deleted. If the inventory fails,
`data.housekeeping` is `{"error": "..."}` and `start` still succeeds.

For dependency installation, ignored build inputs, caches, and `setup_command`,
see [Preparing the validation worktree](https://github.com/elanthus/agentic-preflight/blob/v0.7.0/docs/worktree-modes.md#preparing-the-validation-worktree).
Use `copy_files` only for ignored files such as `.env`; directories are refused with a
setup instruction.

The failure details and recovery command are persisted. After initial setup fails,
the run is `SETUP_FAILED` and `status` returns `abort --force` so an isolated lease can
always be released. After baseline setup fails, the run remains in its lint or test red
state and `status` returns the exact stage retry including `--baseline` instead of
pointing at a stage log that was never created. A baseline scratch worktree that cannot
be created takes the same path, with `setup.kind` set to `worktree`, rather than
reporting the base as passing.

Note the interaction with `respond`: a fix commit containing a `copy_files` path is
rejected. Copied caches are inputs to the run, never part of the change.

### `agentic-preflight context [--section review|docs]`
Returns the material for the active stage. Review context can be fetched repeatedly.
Docs context opens the docs stage from `REVIEW_GREEN` and snapshots its inputs; if
review coverage is stale, it reopens review instead.

- Both sections: `diff`, `changed_files`, `excluded_files`, `worktree_path`.
- Review adds `review_coverage`: a snapshot-bound `manifest`, the exact `head`, and
  every hunk or non-textual file-level review unit.
- With `[review.contracts] enabled`, review adds `contract_checks`: `max_checks`,
  `plan_schema` until a schema-2 receipt exists, `execution`, and the current `receipt`.
  `execution` carries the pinned `image`, the execution limits, and
  `installed_packages`: a cached, code-owned listing of the distributions installed in
  the image (`status`, `packages`, `packages_omitted`, `path`), or an explicit
  `unavailable` entry with a `reason`. The listing states availability only, not which
  consumers the repository supports.
- `--section docs` adds `doc_surface`: every file in the configured documentation
  allowlist, with `path`, `size`, and `touched_by_diff`. Entries describe existing files. From `REVIEW_GREEN` this
  opens the docs stage.

Both sections also include `data.grounding`, a deterministic, bounded retrieval of
repository-owned documentation, convention, history, and policy context; its
text is repository content, not guidance.

Exits 2 with `data.mode = "diff_too_large"` when the diff exceeds `[diff] max_bytes`.
The diff is never truncated — narrow it with `[diff] exclude` or raise the limit.

### `agentic-preflight review run`
Runs the configured independent reviewer when the effective `[review] executor` is
`command`. The command receives the exact review `context` data object as one JSON value
on stdin and must write exactly one strict `ReviewSubmission` JSON value on stdout.
The same coverage, unit, path, and finding validation used by `submit-findings` is then
applied; the command cannot bypass the normal findings pipeline.

The review command inherits `[stage] timeout_seconds` and `max_attempts`. Non-zero exit,
timeout, malformed JSON, stale coverage, or invalid findings enter
`REVIEW_COMMAND_RED` and consume one persisted attempt. A successful command records its
configured command, zero exit code, and SHA-256 of redacted captured output alongside
coverage in the schema-v7 attestation. Changed review inputs require fresh command
review; validated derivation retains original process evidence and coverage provenance.

### `agentic-preflight review contracts --file PATH`

Runs an opt-in contract plan from a JSON file or `-` for stdin. Fetch `context`
first for the delivered manifest, unit IDs, `contract_checks.plan_schema` and
`contract_checks.execution.installed_packages`.
Account for every delivered unit, including units with no initial finding, using
checks, explained non-applicability or an explicit unverified gap. Each check
declares its producer, consumer, contract and independent oracle, plus ordinary,
boundary and composition probes. Schema-2 plans also require checked units to inventory
their transformations and input classes. Each class references concrete probes or
records an unverified gap. Every probe needs an input-class reference. Checks record
oracle provenance: kind, source, rationale, supported input domain, runtime, and an
explicit limitations list. No defect-specific examples are supplied. Prefer a listed
real consumer over a substitute implementation; when the real consumer is absent or
the listing is unavailable, record the limitation in the provenance and keep the
affected input class unverified unless an independent oracle remains.

Only committed Git blobs enter the disposable offline container. Probes have hard
resource/output/deadline limits; an absent pinned image or execution capability is
unverified, with no host-interpreter fallback. The envelope returns `contract_checks`
with the receipt digest, overall/per-probe status, bounded details and a private
full-artifact path. Details include the consumer, provenance, and exact assertions.
`handoff_complete` reports whether the inventory and check details are both present;
`inventory_units_omitted` and `details_omitted` count their omissions separately.
`plan_pointer` locates each check in the full receipt. Details are ordered failed,
unverified, then passed; long output streams are shortened in the summary; and
`claimed_classes` counts the input classes that rely on each probe. Unverified input classes keep
the receipt unverified even when all probes pass. Schema-1 artifacts remain readable,
but new submissions require a schema-2 plan. The accepted review binds the receipt as `contracts_sha256`.
Invalid, incomplete or stale plans fail with `invalid_findings` (exit 3).

Checks supply evidence to the final review; failures are candidate counterexamples,
not automatic findings or a replacement for lint/tests. Read omitted evidence and
verify the oracle before reporting a defect. Challenge declared input coverage and
unsupported consumer assumptions for passed checks as well. See
[contract checks](../../docs/review-contracts.md) for execution restrictions and limits.

### `agentic-preflight review compare [--file PATH]`
Compares the run's accepted review submission with an independent second submission while
leaving the state and stored findings unchanged. `--file` accepts a strict review
submission or a persisted executor submission. Without `--file`, a run whose accepted
executor is `in_harness` launches the configured `[review] command` once as a shadow review
over the same context bundle. Shadow output follows the normal copied-file secret redaction
path and is stored in `logs/review-compare.txt`; it does not consume retry attempts.

Both submissions must bind to the accepted manifest, and the worktree `HEAD` must still
equal the reviewed head. A mismatch exits 3. Findings match when they cite the same unit
and path and their line numbers are equal or within three lines. The command writes the
unit counts, agreed and reviewer-only findings, severity disagreements, and agreement rate
to `review-compare.json`, appends a `review_compared` event, and returns the summary as
`data`. It is legal from `REVIEW_GREEN` onward and never advances the run.

### `agentic-preflight submit-findings --file PATH`
`PATH` may be `-` for stdin. Review requires
`{"coverage":{"manifest":"<from context>","examined":"all"},"findings":[...]}`.
The manifest must match the current diff. Docs requires `{"findings": [...]}`; an empty
`findings` list is valid and common.

Rejects (exit 3, `invalid_findings`): missing, stale, or unknown review coverage;
agent-supplied `id` or `stage`; unknown or path-mismatched review units; paths outside
the worktree; paths outside the changed-file set (review) or documentation allowlist
(docs); line numbers past end of file; and batches over `[review] max_findings`.
All-or-nothing — one bad finding rejects the batch.

### `agentic-preflight respond --id F001 --action fixed|dismissed|accepted [--commit SHA] [--note TEXT] [--reason REASON]`
- `fixed` requires `--commit`. The commit is verified four ways: it exists, it touches
  the finding's file, it contains no `copy_files` path, and in `reusable` or `strict`
  mode it is reachable from the validation worktree's current `HEAD`.
- `dismissed` and `accepted` require `--note` and refuse `--commit`: passing
  `--commit` with any action other than `fixed` exits 6 with `usage_error`, and the
  Python `respond` API raises `invalid_response`.
- `--reason` labels a `dismissed` or `accepted` response with one of `false_positive`,
  `out_of_scope`, `duplicate`, `already_fixed`, `accepted_risk`, `deferred`, or `other`.
  It is optional. Any other value exits 6 with `usage_error`. Passing `--reason` with
  `--action fixed` exits 6 with `usage_error`, and the Python `respond` API raises
  `invalid_response`. The finding stores it as `response_reason`, and the
  `finding_resolved` event repeats it as `reason` (`null` when absent). A finding without
  a reason has no `response_reason` key. The reason is telemetry only: it never enters
  stage evidence or the attestation. Put the specifics in `--note`.
- Unknown id exits 3 listing the valid ids. Each finding is resolved once.
- Non-blocking findings may be resolved after their review or docs stage is green. A
  note-only disposition preserves the green state. A fix that changes the reviewed
  snapshot records the repair commit and returns the run to review before lint or test.

### `agentic-preflight verify [SHA] [--purpose local|publish]`
Without an argument, confirms nothing blocks the active review/docs stage and advances
it to green. Exits 2 listing the outstanding blocking set if anything remains.

With a SHA, validates the portable attestation in `refs/notes/agentic-preflight` for
CI. It checks the note schema, exact commit and tree binding, complete stage set, and
process evidence for green lint/test stages. Fetch the notes ref before calling it in
a fresh clone, together with the head and `refs/agentic-preflight/evidence/*`
from the contributor remote. Schema v7 requires an explicit `verified` outcome,
complete local stage evidence, and the full configuration snapshot. It
verifies original execution digests, Git and configuration bindings, stage applicability,
finding dispositions, and original/current review-unit accounting. A missing note, a
note with any other schema, or invalid evidence exits 2. This remains an offline check;
it does not execute stages or
fetch evidence. The retained `--purpose` option accepts `local` or `publish`; both
require complete local validation. Pending-test notes are rejected.

### `agentic-preflight approval-check SHA --base SHA --reviews-file PATH --author LOGIN`
CI-facing merge policy for an attested pull-request head. It recomputes path risk from
the protected base configuration and uses attested finding-severity totals. Path risk
covers the paths changed since the merge base and the paths that differ between the base
and Git's merge of the head into it, so criss-cross history cannot hide a change. It also
covers every path touched by a non-merge commit in `base..head`, which a rebase merge
replays, so a merge commit that cancels an earlier high-risk commit cannot hide it; a
high-risk path changed and later reverted on the branch still counts. All three sets
appear in `data.changed_files`. Low- and
medium-risk changes pass without a review. High-risk handling follows `[approval] mode`:
`manual_merge` reports success while requiring the user to merge manually (the trusted
workflow separately rejects enabled GitHub auto-merge); `environment` exits 4 until
`--environment-approved` is supplied by the trusted Environment job; and `peer_review`
exits 4 until an eligible non-author has an `APPROVED` review for the current head or an
equivalent earlier head. Later dismissal or changes-requested reviews revoke that
person's peer approval. An earlier head is equivalent only when Git objects prove the
branch content unchanged: the same changed paths and `git patch-id --stable` identity
against the base, a tree equal to Git's clean merge of the approved head with the base
commit the current head contains, identical clean merges of both heads into the base, and
the same non-merge commit patch identities since the base. Peer and owner review results
include `human_approvers`, `approved_head` (the commit the counted review was submitted
on, or `null`), and `approval_carried` (`true` only when that commit is an earlier head).
`--report-only` reports conditional Environment or peer-review state without failing, so
a trusted workflow can dispatch the appropriate hosted job.
A reviews file that is not UTF-8 JSON fails with `invalid_findings` (exit 3), as other
JSON file inputs do.

`[approval] mode = "owner_review"` requires the configured `reviewer` GitHub login to
approve the current head or an equivalent earlier head. An Approve review counts on
another author's PR; on their own PR, a Comment review with trimmed body exactly
`approved` counts. Conversation comments, reviews of heads with different content, bots,
and other users do not count. Dismissal, changes requested on any head, or editing a
self-review to other text revokes approval.

### `agentic-preflight stage run lint|test [--command CMD] [--record] [--baseline]`
Stages run in the fixed order docs → lint → test after review becomes green. Running lint
before the potentially expensive test command means a committed mechanical lint repair
cannot invalidate an already-green test result. After lint the CLI automatically skips
the software test command when every changed path is documentation or standard CI
configuration. This is an explicit state-machine transition, not an agent judgment:
`status` and the final attestation note the test stage as `skipped`. A mixed diff
containing any other path still requires the configured test command. Documentation
includes `.md`, `.rst`, `.adoc`, root extensionless README/CONTRIBUTING/CHANGELOG, and
`.txt` on the docs surface. Executable examples, MDX, unknown files, and Jenkinsfiles
keep tests mandatory even under documentation or CI globs; CI configuration is limited
to recognized hosted-CI YAML paths.

Command resolution: `--command` → `[commands].<name>` → detection. Detection never
guesses: it exits 2 with `data.mode = "needs_command"` and candidates from
`pyproject.toml`, `package.json`, `Makefile`, and `justfile`. Every candidate
has `command` and `source` fields. No detected candidate is copied into `next.command`;
show its exact command to the user and obtain approval before first use.

A repo with none of those manifests — Unity, Unreal, Godot, Xcode, most engine and
mobile projects — will *always* exit 2 here with an empty `data.candidates`. That is
detection working correctly, not a broken setup. Ask the user for the invocation and
offer to write it into `[commands]`; do not go hunting for a build file that the
project does not use.

**Pass/fail is the exit code only.** Output is never parsed. Full output goes to
`logs/<stage>.txt`; the envelope carries head 50 and tail 200 lines with a `truncated`
flag. Stops with exit 4 after `[stage] max_attempts` failures.

Separately, the whole run stops with exit 4 and error `max_restarts` once validation has
returned to review `[stage] max_restarts` times (default 5). `status` reports
`validation_restarts`, `max_restarts`, and `needs_human`; only a person may release the
run by aborting it.

If a lint or test process is killed or times out outside the harness, its persisted
`*_RUNNING` state is recoverable. `stage run lint|test` is the legal retry from that state:
it first records the interrupted process as a red attempt with exit code 125 and reason
`interrupted`, then retries normally. The interrupted attempt counts toward `[stage]`
`max_attempts`.

**A command that no-ops and exits 0 is indistinguishable from one that passed.**
Pass/fail uses only the exit code because parsing output is brittle, but it assumes the command
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

### `agentic-preflight mergeback`
In in-place mode, attests the already-verified current SHA without creating or
cherry-picking a commit. The checkout must remain clean. In isolated modes, cherry-picks
the fix commits onto the source branch; only paths those commits may overwrite are
blocked, while unrelated tracked edits and untracked files are left alone. A dirty
submodule on one of those paths blocks merge-back even when Git config ignores it.

On conflict: aborts only when the paused commit belongs to the fix stack, then
checks whether the branch and local changes were restored. A user-owned cherry-pick
and its staged resolution remain intact, with `data.branch_restored: false`. Exits 4
with `data.resolution` and stores the full report in the event log. The CLI never
auto-resolves. First inspect and show the recovery block. An agent may resolve only when
the intended result is unambiguous, recoverable, and directly supported by that block;
it must preserve unrelated edits and user-owned operations, and cannot make a
content-sensitive choice or bypass protected merge policy. Otherwise a person must
decide. After resolution, `mergeback` is legal again: an exact tree is attested without
rerunning completed stages; a different tree becomes the validation worktree's new
snapshot and returns the active run to review before any stage can be trusted again.

On success: compares the branch tree against the worktree tree. `tree_equivalent: true`
means the verified content is byte-identical and a Git-note attestation is written for
the exact commit. False returns to review with all snapshot-bound stage evidence cleared.

A merge-back interrupted after synchronizing the source with the base but before reaching
the recorded verified tree is stale. Follow `status` and restart preflight for the current source;
only the unchanged source or the completed verified tree can resume a pending attempt.

### `agentic-preflight gate`
Mints a confirmation token and summarises the remote, refspec, branch, and commits.
The summary also includes the configured PR mode, `automated_cleanup` setting, and
deterministic risk classification and verdict. Whether the agent may push without
asking follows
[push and pull-request authorization](https://github.com/elanthus/agentic-preflight/blob/v0.7.0/docs/configuration.md#push-and-pull-request-authorization).
Follow the skill's [PR lifecycle and cleanup](../SKILL.md#pull-request-lifecycle-and-cleanup)
and [merge approval guidance](../SKILL.md#escalation-etiquette) for the returned policy.
Only `[gate] mode = "manual"` exits 4 and puts the literal `git push` command in
`data.manual_command` for a person to run; the agent must not run it.

### `agentic-preflight push --confirm TOKEN [--dry-run]`
Requires the token from `gate`. It first publishes the original-commit refs required
by attestation evidence under `refs/agentic-preflight/evidence/<SHA>`, then atomically pushes
the branch and `refs/notes/agentic-preflight`. A dependency publication failure stops
before the branch push; retained evidence refs are safe to retry. Gate summaries, manual commands, and
dry runs disclose the complete refspecs. These evidence refs survive ordinary
run/worktree cleanup. **Require user authorization before running this**, as described
in the authorization rule linked under `gate`. The token is a non-secret, run-state nonce
that prevents an accidental push; it is readable through `status`, grants no GitHub
access, and is not a security boundary. The gate envelope keeps it only in `data.token`,
while gate and dry-run envelopes put the literal `<token>` placeholder in `next.command`;
substitute the gate's `data.token` only after authorization.

### `agentic-preflight finish`
Marks a pushed validation run `DONE`. It preserves the run directory and
audit logs, clears only that run's worktree ownership pointers, and directs the next step
to `gc`.

Pull-request creation, hosted CI monitoring, and cleanup are host or forge operations.
Follow the skill's [PR lifecycle and cleanup](../SKILL.md#pull-request-lifecycle-and-cleanup)
after `gc`.

## Inspection and recovery

### `agentic-preflight status [--all]`

For a structurally readable active run with invalid stage-origin digests, single-run
status succeeds with `data.readable = true`, `data.evidence_valid = false`, and
`data.evidence_failure` containing code `stage_evidence_invalid`, path, stage, and
reason. This inspection applies in every nonterminal state. Follow its recovery:
inspect the record, abort without `--force`, preserve any unmerged work reported by
abort, then start fresh. Commands that consume corrupt active evidence, including
merge-back, instead return exit 3 with error code `stage_evidence_invalid` and the same recovery guidance. `status --all`
checks structural loadability; its readable records have not had their evidence
digests verified. If the source worktree is missing, status retains the invalid-evidence
diagnostics but recommends `gc` through the existing missing-source recovery path.

Legal in **every** state and the universal recovery entry point. Reports state, seq,
findings, staleness, worktree path, the gate token, and `data.hook` with the effective
hook's `path` and `active` state. Hook-resolution failures report `path: null`,
`active: false`, and a short `error` instead of breaking recovery. Never raises for a
wedged run.
The full single-run report also contains `data.producer` from `start`. It is `null` for
a run record that an earlier version wrote.
In `MERGEBACK_CONFLICT`, it replays the stored conflict report and points back to the
legal `mergeback` retry.
`--all` inventories stored and active runs across every linked worktree in the clone.

### `agentic-preflight logs --stage review|lint|test`
Full captured output. Copied-file contents are redacted.

### `agentic-preflight events [--limit N]`
Run history, oldest first.
New runs include `state_transition` events for creation and committed state changes,
including intermediate states within a transaction. Events carry `from`, `to`, `action`,
UTC `at`, and `monotonic_ns`; committed changes also carry the run `seq`. Creation has
null `from` and `action`. Rolled-back transactions emit no transitions. Audit history is
append-only and may be incomplete after an interrupted write; older histories are not
backfilled. Monotonic timestamps are suitable for intervals within one host boot.

### `agentic-preflight abort [--force] [--reason REASON]`
Ends the run and releases the worktree. Exits 5 if unmerged fix commits would be lost;
`--force` discards them.
`--reason` labels the abort with one of `user_cancelled`, `superseded`, `wrong_base`,
`environment_failure`, `tool_error`, `no_longer_needed`, or `other`. It is optional. Any
other value exits 6 with `usage_error`. The `aborted` event records `forced`, `reason`
(`null` when absent), and `aborted_from_state`, the state that the transition left.

### `agentic-preflight gc [--force]`
Unreadable run records are retained and reported individually in `data.retained`, with
their run ID, path, reason, and diagnostic. Collection continues for other eligible runs.
Read failures use `unreadable` for encoding or I/O failures and `invalid` for JSON,
run schema, or configuration failures. A confirmed absent record uses `missing`.
Diagnostics omit record values.

Even `--force` preserves invalid or unreadable records, their branches, worktrees, and
ownership pointers. Saved configuration snapshots ignore keys a later release removed,
while unknown keys and invalid current settings remain errors. Readable nonterminal runs can be orphaned through the normal abandonment checks; a
running command prevents orphaning, and isolated resources with unmerged fixes are
retained unless `--force` is used. Inspect the
indicated record. Neither `status` nor `status --all` treats an unreadable record as
absent: inspection reports
`readable: false`, and single-run status keeps `has_run: true` with `data.read_failure`.
Recovery guidance supplies no executable replacement command. Only confirmed absence
permits stale-pointer recovery. Global inventory and cleanup failures still fail visibly.

Reconciles run directories, git worktrees, and `ap/*` branches. For a terminal run,
each fix commit is compared by stable patch ID with commits in that run's
post-merge-back history. Patch-equivalent cherry-picks are safe to reclaim; anything
with no equivalent remains reported as unmerged and is never removed without
`--force`. Run directories and their audit logs are retained.
It marks a nonterminal run `ORPHANED` when its source worktree disappeared, its source
head moved, or its ownership pointer vanished, but only when no command is executing.
Orphaning releases ownership; cleanup remains a separate preserve-first decision.
It also removes a released `reusable` runner, and prunes its git registration, when no
run record naming it still has an existing source worktree or its registered path is
gone. A runner on a branch is never removed. `data.reclaimed_runners` lists the removed
runner paths; `removed`, `retained`, and `orphans` are unchanged. An unreadable run
record keeps the runner it names; one that is not even a JSON object keeps every runner.

Runner reclamation preserves locked worktrees, tracked or non-ignored untracked changes,
and runners associated with retained runs (including unmerged fixes). Ignored caches
in an otherwise eligible runner are disposable. A refused Git removal preserves the
runner rather than deleting its directory directly.


`gc` also reclaims eligible unused reusable runners at the old sibling location
under the same preservation rules. `data.legacy_worktrees` lists registrations still
remaining under that `.agentic-preflight-worktrees/` default after reclamation;
inspect each before removing it with `git worktree remove <path>`.

### `agentic-preflight hook-check`
The pre-push predicate. Reads git's stdin protocol, consults only the commit's
`refs/notes/agentic-preflight` note, and exits 0 or 10. Not for you to call — git calls
it.

After it decides, it appends one JSON line to `hook-decisions.jsonl` in the user-level
telemetry directory, unless `[telemetry] hook_log = false`, or unless the repository is
temporary and `[telemetry] path` is not set: under the system temporary directory, with
no network remote in `origin` or, without `origin`, in the push remote (see
[Temporary repositories](../../docs/local-telemetry.md#temporary-repositories)). The
line has `at`, `repo` (the summary's repository identity), `remote` (the remote name
from `AGENTIC_PREFLIGHT_PUSH_REMOTE`,
which the installed hook sets from Git's first argument; a URL is reduced to host and
path without credentials; `null` when unset), `allowed`, `reason`, and `updates`
(`local_ref`, `remote_ref`, a seven-character `sha`, and `force`, which is `null` for an
update the hook did not examine). `reason` is one of `attested`, `already_on_remote`,
`deletion`, `exempt`, `no_updates`, and `config_unavailable` when allowed, or
`force_push_refused` and `missing_attestation` when blocked. The write is best effort:
it waits at most 2 seconds after the decision, ignores every failure, and never changes
the exit code or the stderr message. `agentic-preflight report` counts these decisions
in `data.hook_decisions`. See
[Hook decisions](../../docs/local-telemetry.md#hook-decisions).

### `agentic-preflight telemetry rebuild`
Regenerates `summary.json` for every terminal run record in this clone, of run schema 1
or 2. When `[telemetry] mirror` is true, it also rewrites this repository's rows in the
user-level `runs.jsonl` mirror under the mirror lock: one row for each
`(repo.root_commit or repo.source_worktree_path, run_id)`, keeping the newest
`written_at`, then one row for each `(run_id, created_at, repo.source_worktree_path)`,
so a run mirrored once with and once without a root commit stays one row. Event lines
and findings that are not objects never fail a record. Rows of other repositories, and lines that are not summaries, keep their
exact text. Nonterminal runs count as `skipped`. An unreadable record is listed in
`data.unreadable_runs` with its run ID, path, reason, and diagnostic; its earlier summary
and mirror row are kept, never treated as absent. `data` reports `repo`, `summarized`,
`skipped`, `unreadable`, `unreadable_runs`, `write_failures`, and `mirror` (`enabled`,
`path`, `rows`, `preserved_rows`, `temporary_runs`, `reason`, `error`). A repository
is temporary when it is under the system temporary directory and has no network remote
(no `origin`, a local path, or a loopback host). Without an explicit `[telemetry] path`,
a summary of a temporary repository, judged by its `repo.source_worktree_path` and
`repo.remote`, is never mirrored, and `mirror.temporary_runs` counts those summaries. A
clone that is itself temporary does not change the mirror at all: `mirror.reason` is
`temporary_repository`, and `path`, `rows`, and `preserved_rows` are `null`. `reason` is otherwise `null`; `temporary_runs` is `null`
when the mirror is off. A failed write exits 1 with error code `telemetry_write_failed` and the same `data`.
`telemetry rebuild` makes no network request.

The summary is first written when a transaction commits `DONE`, `ABORTED`, or `ORPHANED`:
from `finish`, `abort`, `gc`, or `start` when it orphans the previous run or aborts the
run it was creating. The event log then gains `summary_written` (with `summary_path`,
`mirrored`, `mirror_path`, `reason`, and `error`) or `summary_failed`. `reason` is
`temporary_repository` when the run's source worktree is under the system temporary
directory, its remote is not a network remote, and `[telemetry] path` is not set, so no
mirror row was written; otherwise it is `null`. `finish` and `abort` report the same outcome, with `run_id`, in
`data.telemetry`; `gc` reports a list there, one item
for each run it orphaned. A telemetry failure never changes a command's result or rolls
back its transaction. The `aborted` event is now written with the terminal transition
and carries its `seq`. See [Local run telemetry](../../docs/local-telemetry.md).

### `agentic-preflight telemetry sync-hosted [--limit N] [--run RUN_ID] [--refresh] [--dry-run]`
Joins GitHub pull-request and check outcomes onto this clone's pushed runs. This is the
only telemetry command that talks to a network service. It only reads: it sends `GET`
requests to the GitHub API with `gh api --hostname github.com`, and it writes local files
only. No workflow command calls it. Run it only when the user wants hosted outcomes. It
needs `gh`, logged in for github.com, and an `origin` on `github.com`.

It selects the `DONE` runs that have a `pushed_sha`, newest `created_at` first. A run that
already has `hosted.json` for its pushed commit is skipped unless `--refresh` is given, so
an outcome read while checks were pending or the pull request was open stays until a
`--refresh`. `--limit` (default 50, at least 1) caps the runs that it reads. `--run RUN_ID`,
or the root `--run`, selects one run. `--dry-run` lists the selection in `data.runs` with
`outcome` `planned`, contacts nothing, and writes nothing. `data.gh_found` tells whether
PATH has `gh`.

For each run it reads `/repos/{owner}/{repo}/commits/{sha}/pulls` and
`/repos/{owner}/{repo}/commits/{sha}/check-runs`. Of several pull requests it uses the one
whose head is the commit, then the one whose head branch is the run's branch, then the
first. GitHub does not associate a commit with a pull request that was closed without
merging, so when the list is empty it also reads
`/repos/{owner}/{repo}/pulls?state=all&head={owner}:{branch}` and uses the pull request
whose head is the commit. The check runs reduce to one conclusion: `failure` when any has
the conclusion `failure`, `cancelled`, or `timed_out`; otherwise `pending` when any is not
complete; `none` when there are none; otherwise `success`. Commit statuses are not read.
It writes `hosted.json` in the run directory with `fetched_at`, `sha`, `pr` (`number`,
`url`, `state`, `merged_at`, or `null`), `checks` (`conclusion`, `total`, and the `failed`
check names, or `null`), `missing`, and `source` (`"gh"`). A commit that GitHub does not
know (HTTP 422) is written with `missing: true` and null `pr` and `checks`, so later syncs
skip it. Then it regenerates the `summary.json` and mirror row of each run that it wrote,
with the `telemetry rebuild` code. The summary's `hosted` object gains `pr_number`,
`pr_state`, `merged` (`null` without a pull request), `checks_conclusion`, and
`fetched_at`; all five are `null` for a run that was never synced. A synced run whose
`summary.json` does not carry its `hosted.json`, for example after an interrupted sync, is
regenerated too, without a request. Run records and event logs are not changed.

`data` reports `repo`, `dry_run`, `refresh`, `limit`, `run`, `eligible`, `not_eligible`
(`not_done`, `no_pushed_sha`), `unreadable`, `unreadable_runs`, `already_synced`,
`selected`, `deferred` (eligible runs past `--limit`), `synced`, `commit_not_found`,
`stale_summaries` (synced runs whose summary is regenerated without a request),
`gh_found`, `runs` (`run_id`, `sha`, `outcome`, `pr_number`, `pr_state`, `merged`,
`checks_conclusion`), `errors`, `stopped`, and `summaries` (`regenerated`,
`unreadable_runs`, `write_failures`, and `mirror`, with the `telemetry rebuild` keys; the
mirror is not rewritten when no summary is left to write). After a sync that wrote outcomes or
summaries, `next.command` is `agentic-preflight report`. `next.command` never repeats the
network call.

An HTTP error other than those below, or an answer that cannot be read, concerns one
commit: it is listed in `data.errors`, and the older runs are still read. Any other
failure stops the sync, and `data.stopped` names it. Runs synced before the failure keep
their outcomes. Each entry of `errors` and `stopped` gives `reason`, `run_id`,
`http_status`, and `message`. Each failure exits 1 with the full `data`:

- `gh_unavailable`: `gh` is not on PATH, is not logged in, or its token was refused. Ask
  the user to install `gh` and run `gh auth login`.
- `unsupported_remote`: `origin` is not on github.com. Nothing was read.
- `hosted_rate_limited`: HTTP 429, or HTTP 403 for a rate limit. Wait for the reset; the
  next sync skips the runs already synced.
- `hosted_network_error`: GitHub did not answer.
- `hosted_request_failed`: the sync stopped on HTTP 403 or 404, for example when the `gh`
  account cannot read the repository, or on a `gh` failure without an HTTP status; or it
  finished with entries in `data.errors`.
- `telemetry_write_failed`: a local file could not be written. When `hosted.json` was
  written but a summary or the mirror was not, `next.command` is
  `agentic-preflight telemetry rebuild`.

A `--run` that names no stored run exits 3 with `no_run`. Messages never copy `gh` output
or tokens, and remotes are shown without credentials. See
[Join hosted outcomes](../../docs/local-telemetry.md#join-hosted-outcomes).

### `agentic-preflight report [--since TIME] [--until TIME] [--repo NAME_OR_REMOTE] [--state STATE] [--all-repos] [--format json|markdown] [--rows]`
Counts finished runs from the local run summaries. It reads files only and writes
nothing. It makes no network request.

By default it reads the mirror rows of this repository. A row belongs to this
repository when its `repo.root_commit` is the root commit of this clone. When the row or
this clone has no root commit, the row belongs when its `repo.source_worktree_path` is a
worktree of this clone.
When the mirror has no rows for this repository, the command summarizes the terminal run
records of this clone in memory, with the same code as `telemetry rebuild`.
`--all-repos` reads every mirror row. `--repo` also reads every mirror row, and keeps the
rows whose `repo.name` or `repo.remote` matches. Case is ignored, and a remote URL is
normalized the same way as in a summary. Outside a Git working tree, the command needs
`--all-repos` or `--repo`. Otherwise it exits 1 with error code `not_a_repository`.

`--since` keeps runs whose `finished_at` is at or after the time. `--until` keeps runs
whose `finished_at` is before the time. Each value is an ISO 8601 date or time, and a
time without a zone is UTC. A value that is not ISO 8601 exits 6 with `usage_error`.
`--state` keeps `DONE`, `ABORTED`, or `ORPHANED` runs. Repeat it to keep more than one.

`data.source` tells you where the rows came from: `used` (`mirror` or `clone_records`),
`scope` (`clone` or `all`), and `mirror` (`path`, `enabled`, `exists`, `lines`, `rows`,
`malformed`, `malformed_by_reason`, `malformed_lines`, and `duplicates`). Every malformed
line is counted in `malformed` and `malformed_by_reason`, and `malformed_lines` lists the
first 20 by line number. A malformed line is never treated as absent. Only valid summary
rows are deduplicated. Of several valid rows for one repository and run, the row with the
newest `written_at` is used, and the later line wins a tie, as in `telemetry rebuild`.
Rows with the same run ID, `created_at`, and source worktree are also one run, even when
only one of them has a root commit. `duplicates` counts the rows left out.
Inside a clone, `data.source.clone` gives the repository identity, `mirror_rows`,
`run_records`, `unreadable`, and `unreadable_runs`. A record that cannot be summarized is
listed there with reason `invalid`. It does not fail the report. When the mirror is the
source, it also gives `unmirrored_terminal_runs`: the finished runs of this clone that
the mirror does not have and that `telemetry rebuild` would add. After a fallback, it
gives `summarized` and `skipped` instead. `temporary_runs` counts the finished runs that
were not in the mirror and that a rebuild does not mirror, because the clone or the
run's repository is temporary (under the system temporary directory, without a network
remote) and `[telemetry] path` is not set. When a rebuild can add missing runs, `next.command` is
`agentic-preflight telemetry rebuild`. `data.filters` and `data.filtered_out` tell you
which filters were set and how many rows each filter left out.

`data.overall` holds the statistics of all reported rows. `data.by_repo`,
`data.by_client`, and `data.by_model` hold the same statistics for each repository, each
`producer.client`, and each `producer.reported_model`. A run without that value is in
the group `unknown`. The statistics are:

- run counts by state, and the completion rate;
- findings by stage, severity, status, and action;
- blocking findings, and the range of them that are `fixed`;
- review-stage findings per 100 review units;
- runs with a fix commit, and runs with a validation restart;
- finding responses by reason, and the perceived false-positive share: findings
  dismissed or accepted as `false_positive` over findings with a recorded response,
  a range from `value` to `max` while any response has no reason;
- failed stage attempts and stage reuse, and the median and p90 of stage
  `duration_seconds`, for each stage;
- the median and p90 of `duration_seconds`;
- the share of run time in the review and docs states and in the lint and test
  states, over runs with complete `timing`;
- aborted runs by reason and by `aborted_from_state`;
- hosted outcomes recorded, hosted outcomes synced, and orphaned reasons;
- the counts of `review_coverage_invalidated`, `test_interrupted`, `sync_conflict`, and
  `stage_reused` events.

Each ratio gives its `numerator`, `denominator`, and `denominator_label`.
`findings.blocking.fixed_share` gives `numerator_min` and `numerator_max` instead. A null
value is left out of a statistic. The statistic reports the runs that it left out as
`excluded_runs`, or for each stage as `attempts_excluded_runs` and `reused_excluded_runs`,
and for hosted values as `pr_url_excluded_runs`. A total is null, not 0, when no run in
the group has the value. The Markdown shows a null value as `n/a`. The median and p90
are null when there are fewer than 5 observations. A stage's `attempts` counts its
recorded failed attempts. A summary does not cross finding status with severity, so the
fixed count of blocking findings is a `min` and `max` range. Recorded hosted
outcomes are counts of values that older runs observed. Current versions record none, so do
not use them to count pull requests. `hosted.synced` counts the outcomes that
`telemetry sync-hosted` joined: `runs` (over all runs); `green_checks_failed`, the synced
`DONE` runs whose checks concluded `failure`, over synced runs with settled checks, with
`checks_excluded_runs` for the pending runs and the runs whose commit GitHub did not know;
`pull_request_found`, over synced runs whose commit GitHub knew, with
`commit_unknown_runs`; `merged`, over synced runs with a pull request; `pr_states`; and
`checks_conclusions`, where a synced run whose commit GitHub did not know is `unknown`.

`data.hook_decisions` counts the pre-push hook decisions in `hook-decisions.jsonl`, in
the same scope and with the same `--repo`, `--since`, and `--until` filters, applied to
each decision's `at`; `--state` does not apply. It gives `source` (`path`, `enabled`,
`exists`, `error`, `lines`, `rows`, `malformed`, `malformed_by_reason`,
`malformed_lines`), `filtered_out`, and `overall` and `by_repo` counts of `decisions`,
`allowed`, `blocked`, `blocked_share`, `allowed_by_reason`, and `blocked_by_reason`. A
log that exists but cannot be read sets `source.error` and a null `overall`; it does not
fail the report.

`data.limits` lists what the counts do not measure. `--format markdown` also renders the
report as compact Markdown tables in `data.markdown`, which ends with the limits. The
output is still one JSON envelope. `--rows` includes the filtered summary rows in
`data.rows`. Otherwise, `data.markdown` and `data.rows` are `null`. A mirror that exists
but cannot be read exits 1 with error code `telemetry_mirror_unreadable` and its path in
`data`. See [Read the report](../../docs/local-telemetry.md#read-the-report).

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK | Follow `next` |
| 1 | Invalid input or internal error | Read `error.message`; fix your invocation |
| 2 | Stage failed | Read the log, fix the cause, re-run the stage |
| 3 | Precondition violated | For run commands, run `status`, then obey `next`; for `approval-check` invalid input, correct the reviews file |
| 4 | Human resolution required | Show recovery material; resolve only the bounded, unambiguous merge-back cases permitted by the skill, otherwise stop |
| 5 | Confirmation required | Apply the skill's authorization rules, then re-run with the token |
| 6 | Command-line usage error | Read `error.message`, run `next.command` for valid usage, and fix the invocation |
| 10 | Hook blocked a push | Start a run with `agentic-preflight start --intent "..."` |

A command-line usage error (unknown option, missing argument, bad choice, unknown
command) prints one envelope with error code `usage_error`, Click's message in
`error.message`, and the command's `--help` invocation in `next.command`, then exits 6.
`hook-check` is the exception and keeps Click's own usage output and exit code.
Running `agentic-preflight` with no arguments prints the root help as plain text and
exits 0.

For run-command exit 3 → run `status` → obey `next`. For `approval-check`
`invalid_findings`, correct the reviews file and retry the command.
