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
overrides. Does not clobber an existing config. `data.hook_path` is the absolute path
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

### `agentic-preflight start --intent TEXT [--base-ref REF] [--replace]`
Creates a run and prepares its validation worktree according to the configured
[worktree mode](https://github.com/elanthus/agentic-preflight/blob/main/docs/worktree-modes.md).
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
rebases the PR branch itself. A sync conflict is aborted
cleanly and reported; no conflicted rebase is left in progress.
`start` classifies complete local evidence per stage, including when synchronization
leaves the exact attested commit unchanged, and returns the next required
command. Equal base/head trees and matching context
can preserve review and docs across rewritten commits. Shell stages require committed
content contracts. Read `data.applicability`; `invalid` and `unknown` require a fresh
stage, and later-stage candidates remain available while earlier stages are pending.
Refuses a dirty tree (exit 3, `dirty_tree`) and a branch with no changes over the base
(exit 3, `empty_diff`). When the branch diff is empty but criss-cross history hides a
change that merging would make, `data.merge_result_files` lists those paths;
synchronizing would rebase them away, so make the change in an ordinary commit, such as
`git revert`. In-place mode protects `[worktree] copy_files` where they are;
isolated modes copy them. Every mode refuses an entry git is not already ignoring.

Returns `data.worktree_path` — **absolute**. Use it; do not rely on `cd` persisting.
See [isolated worktree locations](https://github.com/elanthus/agentic-preflight/blob/main/docs/worktree-modes.md#why-isolated-validation-worktrees-live-outside-git)
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
see [Preparing the validation worktree](https://github.com/elanthus/agentic-preflight/blob/main/docs/worktree-modes.md#preparing-the-validation-worktree).
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

### `agentic-preflight respond --id F001 --action fixed|dismissed|accepted [--commit SHA] [--note TEXT]`
- `fixed` requires `--commit`. The commit is verified four ways: it exists, it touches
  the finding's file, it contains no `copy_files` path, and in `reusable` or `strict`
  mode it is reachable from the validation worktree's current `HEAD`.
- `dismissed` and `accepted` require `--note` and refuse `--commit`: passing
  `--commit` with any action other than `fixed` exits 6 with `usage_error`, and the
  Python `respond` API raises `invalid_response`.
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
cannot invalidate an already-green test result. With local test authority, after lint
the CLI automatically skips
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

A merge-back interrupted after rebasing the source but before reaching the recorded
verified tree is stale. Follow `status` and restart preflight for the current source;
only the unchanged source or the completed verified tree can resume a pending attempt.

### `agentic-preflight gate`
Mints a confirmation token and summarises the remote, refspec, branch, and commits.
The summary also includes the configured PR mode, `automated_cleanup` setting, and
deterministic risk classification and verdict. Whether the agent may push without
asking follows
[push and pull-request authorization](https://github.com/elanthus/agentic-preflight/blob/main/docs/configuration.md#push-and-pull-request-authorization).
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
In `MERGEBACK_CONFLICT`, it replays the stored conflict report and points back to the
legal `mergeback` retry.
`--all` inventories stored and active runs across every linked worktree in the clone.

### `agentic-preflight logs --stage review|lint|test`
Full captured output. Copied-file contents are redacted.

### `agentic-preflight events [--limit N]`
Run history, oldest first.

### `agentic-preflight abort [--force]`
Ends the run and releases the worktree. Exits 5 if unmerged fix commits would be lost;
`--force` discards them.

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
