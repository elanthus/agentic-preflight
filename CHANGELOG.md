# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-11-17

### Upgrade highlights

- Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged. Run records
  that 0.8.1 wrote stay readable, and a note that 0.8.1 wrote still verifies in the usual
  case.
- Upgrade the protected-base CI verifier before, or together with, the local CLI and the
  pre-push hook. A 0.8.1 verifier rejects a note that 0.9.0 writes, because review patches
  no longer depend on Git configuration or Git attributes.
- Push or abort open runs before you upgrade. 0.9.0 refuses to merge back, gate, or push a
  run that 0.8.1 started (`needs_human`, `data.reason` `unknown_config_provenance`), and
  0.8.1 cannot read a run record that 0.9.0 starts.
- `respond --action dismissed|accepted` now requires `--reason`. Run
  `agentic-preflight integrations update` so that installed skills pass it.
- A project that copied the hosted workflows must copy the new versions and add
  `human-approval-review.yml` and `human-approval-rerun.yml`.
- This release contains the security fixes listed under "Security". See
  [Upgrading from 0.8.1 to 0.9.0](COMPATIBILITY.md#upgrading-from-081-to-090) for the
  rollout order and the changes that affect wrappers and scripts.

### Added

- Python 3.14 is supported. CI tests it on every pull request, and the built-wheel smoke
  test installs the tool on Python 3.11 and Python 3.14.

- Add packaged reviewer `--retries`, `--budget-seconds`, and optional `--verify`, with per-call measurements and rejected candidates in `AP_REVIEWER_EVENTS_FILE`, plus digest-bound eval adjudication with Wilson precision/recall intervals. Tests use fake provider CLIs without network or model calls.
- Keep usage partial for unmatched call starts, and retain known provider usage when a parsed response exhausts the total budget.
- Keep the default total review budget at `AP_REVIEWER_TIMEOUT`; CLI help explains that timeout retries require a larger explicit `--budget-seconds`.
- Keep verification output instructions separate from discovery, reject malformed scoring inventories before hashing and non-object adjudication documents, and mark reviewer completion only after delivering stdout.
- Fail immediately on explicit contract-planner provider errors, clear stale telemetry before timeout validation, and report Unicode failures without a traceback.

- `agentic-preflight reviewer run --provider claude|codex` runs the independent command
  reviewer from the package. Configure it as `[review] command` (and as
  `[review.contracts] planner_command`). It is the former example wrapper, moved into
  the package with the same prompts, provider CLI arguments, `AP_*` environment
  variables, defaults, events file, and output. Unlike the wrapper, it reads and writes
  its own standard streams as UTF-8 instead of the locale encoding. It shells out to the `claude` or `codex` CLI and adds
  no model SDK dependency. Like `hook-check`, it prints no JSON envelope, and a usage
  error exits 2 with Click's usage message. See ADR 0003 and
  `docs/independent-review.md`.

- Add nullable hosted follow-up commit, human review, classified failed-check, and opt-in capped revert observations, with GET-only sync, rebuild support, and separately denominated escape reporting.
- Preserve fetched hosted outcomes when an optional escape request fails; keep its observation null and stop further requests on authentication, rate-limit, or connection failures.
- Add `--reverts` and `--reverts-limit` to `telemetry sync-hosted`, the user-only `telemetry.hosted.local_equivalent_checks` mapping, and `checks.settled` in saved hosted observations.
- Count unknown revert observations only among synced merged DONE runs, matching the revert component’s named eligible population.

- Run summaries record what the gate caught before the push:
  `findings.review_blocking_fixed`, `findings.docs_blocking_fixed`, and
  `stages.lint.red_then_green` and `stages.test.red_then_green`.
- The tool times each external command that it runs on its own (sync, setup, the review
  command, the contract planner, the `review compare` shadow reviewer, the baseline
  check, lint, test, and merge-back) with new `tool_command_started` and
  `tool_command_finished` events. Summaries add `timing.tool_seconds` and, for runs with
  a complete transition history, `timing.agent_seconds`.
- `report` adds `caught` (the share of `DONE` runs with any catch, and a breakdown by
  source) and `tool_time` (median and p90 tool seconds per `DONE` run and per synced pull
  request), with "Caught before push" and "Tool time" Markdown sections.

- `gc` deletes the run directory of a reclaimed run whose record last changed more than
  `[retention] run_days` days ago (default 90; `0` keeps every record). The envelope lists
  them in `data.pruned`. `[retention]` is excluded from run snapshots and digests.

### Changed

- `context` and `review run` stop reading a diff once its patch text passes four times
  `[diff] max_bytes`, so a very large diff, such as an unexcluded generated file, no longer
  costs a full read before it is refused. The `diff_too_large` envelope adds `data.complete`
  and `data.unmeasured_files`; when the read stopped, `data.total_bytes` and the `data.by_file`
  sizes are lower bounds over the files read. A diff within the limit is unchanged, and so is
  its review digest.

- `logs --stage` now returns a bounded window of the stage log instead of the whole log:
  by default the last 200 lines, at most 64 KiB. `--tail N` and `--head N` choose another
  line count under the same byte limit, and `--all` returns the whole log. The envelope
  adds `data.truncated`, `data.total_bytes`, and `data.total_lines`. A cut never splits a
  UTF-8 character or a `[redacted]` marker.

- `hook-check` is faster. Verifying one pushed ref now spawns 17 Git processes instead of
  38: 14 decide the push, and 3 identify the repository for the hook decision log on a
  background thread. Each lookup of a full object ID runs once per verification, and the
  commits a note names are resolved in one batch. The CLI imports only the module of the
  command it runs. Measured on an Apple M4 Pro with macOS 27, for one new branch with a
  green note: 1.19 to 0.61 seconds, and 0.45 to 0.37 seconds for a push with no ref to
  check. Every hook decision, exit code, and message is unchanged.

- The package now declares `requires-python = ">=3.11,<3.15"`. `pip`, `pipx`, and `uv`'s
  default interpreter selection no longer install the tool on a Python version that CI does
  not test. `COMPATIBILITY.md` states how the cap is raised.
- Pull-request CI runs the test job on Python 3.11 and Python 3.14, the oldest and newest
  supported versions, as well as on 3.13.

- Require `--reason` for CLI finding dismissals and acceptances; missing reasons now produce `usage_error` before run mutation. Historical reasonless records remain readable.

- `hook-decisions.jsonl` rotates to `hook-decisions.jsonl.1` at 4 MiB, keeping one previous
  generation. `report` reads both files.

- A pull request review now re-runs the protected-base `Human approval policy` runs for
  the reviewed head instead of starting a second run beside it. GitHub counts every run
  of a required check name on a head, so the old failed result used to keep blocking
  merge after approval. `human-approval.yml` runs only on `pull_request_target`; the new
  `human-approval-review.yml` records the reviewed pull request number, and the new
  `human-approval-rerun.yml` verifies it against the API and re-runs each policy run for
  the head when the evaluation that run recorded in its new `human-approval-policy`
  artifact shows `peer_review` or `owner_review` mode and a required human approval.
  The waiting `high-risk human approval` job now says which head needs review, and the
  passing job names the approved head.

- Each captured stage, setup, and review command stream keeps at most 8 MiB: its first and
  last 4 MiB around an `[output truncated: N bytes omitted]` line. Output goes to temporary
  files instead of pipes, so a timed-out stage keeps what it printed even when a descendant
  escaped the kill; the old `[output truncated: a process outside the stage's group kept
  its pipe open]` marker is gone.

### Deprecated

- The example reviewer wrappers in `docs/examples/reviewers/` (`claude_review.py`,
  `codex_review.py`, and `_reviewer_common.py`) are deprecated in favour of
  `agentic-preflight reviewer run --provider claude|codex`, and are planned for removal in
  0.10.0. Until then they stay as standard-library copies of the packaged reviewer, which a
  test keeps identical, so a configured `python3 -S -B docs/examples/reviewers/<provider>_review.py`
  command keeps working. A wrapper that you copied from an earlier release keeps working
  but does not receive later fixes.

### Removed

- Run summaries and `agentic-preflight report` drop the recorded hosted outcome: the
  `hosted.last_ci_outcome` and `hosted.pr_url_recorded` summary fields, the "Hosted
  outcomes recorded" report section, and the `outcome_recorded`, `outcomes`,
  `pr_url_recorded`, `pr_url_excluded_runs`, and `note` keys of `data.overall.hosted`.
  The "Hosted outcomes synced" section and the two `data.limits` entries about hosted
  checks appear only when at least one run in the report was synced.

### Fixed

- In `reusable` and `strict` worktree modes, the fix commits that `mergeback` replays are
  now the validation worktree's history from the reviewed head, in commit order, rather
  than the commits `respond --commit` cited in response order. Responses out of commit
  order, an uncited regression-test commit, and a commit made after the last `respond` no
  longer cause a merge-back conflict or a validation reset that drops commits.
  `respond --commit` still records the cited commit as the finding's `fix_commit`.
  Empty commits in that history are not replayed, and a merge commit is refused with
  `nonlinear_fix_history` (exit 3) and a rebase instruction instead of failing as a
  merge-back conflict.
- A repair committed after a lint or test process was interrupted is now registered as a
  fix commit and returns the run to review, in every worktree mode. Previously the
  recovery stamped the repaired head as the tested one, so the repair was never
  registered, and in `in_place` mode the retry was refused as stale.

- A `mergeback` retry after a cherry-pick conflict no longer mistakes merge-back's own
  rebase of the source branch for a human resolution. Previously an untouched retry reset
  the validation branch to the unfixed source, cleared `fix_commits`, and could go on to
  attest findings as fixed on a head without the fixes. The retry now replays the fix
  commits again, and the rebased head is recorded as the run's source head.
- A conflict retry on a source branch that moved but lacks a recorded fix commit now stops
  with `fix_commits_unmerged` (exit 4), names the missing commits, and keeps the
  validation branch and `fix_commits`. A fix counts as present when every path it changes
  holds the fix's or the validated content, or when the branch has the fix by ancestry,
  stable patch ID, or cherry-pick and every path it changes holds that content or differs
  from the fix's parent commit. A human resolution that meets this still returns the run
  to review on the resolved tree.

- `mergeback` now writes the attestation note to Git on standard input instead of as a
  command-line argument. A large note, such as one listing thousands of excluded files,
  exceeded the operating system's argument limit, failed with `internal_error`, and left
  the run in `MERGEBACK_PENDING`. Git stores the same note bytes as before, so existing
  notes still verify.

- The run store validates every run and findings record by parsing it back through
  its own loader before writing it, and writes neither file when either is invalid.
  A record the loader would reject can no longer strand a run.
- `respond --note` longer than 4,000 characters exits 6 with `usage_error` and states
  the limit. It used to exit 0 and write a findings record that `status` and `verify`
  could not read.
- Evidence reuse no longer writes a run record that predates its original execution
  when the system clock is behind. The refresh time is clamped to the original
  execution time.
- A command refused with `run_record_unreadable`, including `abort --force` and
  `start --replace`, now lists the run's ownership pointers in `data.pointer_paths`
  and names them in `next.instruction` when the record names the `in_place` or
  `strict` worktree mode. Deleting those files is the manual recovery that lets the
  worktree start again while the record stays retained. A `reusable` run, which may
  still lease the clone's runner, or a record whose mode cannot be determined gets an
  instruction to keep everything and stop for the user instead.

- A run with an open `auto_fix` finding below the blocking severity no longer leaves
  review or documentation. `context --section docs`, `stage run lint`, and `mergeback`
  now exit 3 with `findings_unresolved`, name the findings, and return `respond` as the
  next command. `status` returns `respond` too, a disabled docs stage is not skipped,
  and reused docs evidence does not move the run past review.
  Previously the run reached `mergeback`, which failed with `internal_error` after
  recording the attempt and left the run in `MERGEBACK_PENDING`, where `respond` is not
  legal.
- `mergeback` recovers a run that an earlier release left in `TEST_GREEN` or
  `MERGEBACK_PENDING` with such a finding. Before any Git change, the new
  `REOPEN_FINDINGS` transition returns it to `REVIEW_GREEN` and keeps the review result.
  When an isolated merge-back has already cherry-picked the fixes, the run returns to
  review on the merged source commit instead. Neither return counts toward
  `[stage] max_restarts`.

- `gc` without `--force` no longer releases a `strict` or `reusable` validation worktree
  that holds uncommitted changes or a commit not yet answered with `respond`. It retains
  the run in `data.retained` with the worktree path and the reason. A clean worktree whose
  `HEAD` is a different commit with the tree of the recorded head or the last recorded
  fix, such as one a sync rewrote, holds no unrecorded changes and is still released.
- `gc` no longer reports the branch of a healthy isolated run as an orphan named
  `+ ap/<run-id>` and no longer suggests `gc --force` for it.

- `start` now resolves the current branch from its full ref, so a branch that shares its name
  with a tag is recorded as `<name>` instead of `heads/<name>`. Previously `push` then
  reported success after creating `refs/heads/heads/<name>` on the remote and left the
  real branch unchanged.
- `start` refuses a `HEAD` that points at a ref outside `refs/heads/`, such as a tag, with
  `no_branch` (exit 3), as it does for a detached `HEAD`.
- A run started before this release on a branch that shares a tag's name recorded
  `heads/<name>`, so after upgrading `mergeback` stops it as stale; abort it and start a new run.

- The fresh-base rebase no longer follows `rebase.updateRefs`, so `start` never rewrites a
  branch stacked under the validated branch. It also ignores `rebase.autoStash`,
  `rebase.rebaseMerges`, and `pull.twohead`, which made it rebase a dirty checkout, keep
  merge commits, or drop the branch's changes. It now pins `--no-autosquash` too, as a
  guard: Git applies `rebase.autoSquash` only to interactive rebases today.
- The fallback base merge ignores `pull.twohead`, `merge.verifySignatures`, and `merge.log`,
  and the merge-back cherry-pick ignores `pull.twohead`, which emptied every fix commit.
- The fallback base merge also ignores `branch.<name>.mergeOptions`, where `-s ours` made a
  clean synchronization report a conflict.

- `start` now recovers a run that an interrupted `start` left in `CREATED` or
  `WORKTREE_READY`. The next `start` in that source worktree, with or without `--replace`,
  orphans the interrupted run and creates a fresh one, instead of resuming a run that could
  never advance. It first reclaims a clean validation checkout the interrupted run
  provisioned under the worktrees directory (a reusable runner left on its `ap/` branch, or
  a strict checkout). When that checkout holds changes or commits, `start` exits 4
  (`needs_human`) naming the checkout and the head it must return to, and leaves the run in
  place. A later `start` reclaims the runner, or a leftover strict checkout, once it is
  clean, even if the run was aborted in between. It still refuses while the interrupted
  run's `start` holds its operation lock. The `WORKTREE_READY` hint now points to `start`
  rather than to `status`.

- The fetches in `start` and in a manual-mode `gate`, and the pushes, notes fetch, and remote
  query in `push`, can no longer wait on a prompt while they hold the clone-wide locks. Git's
  terminal credential prompt is off, stdin is empty, on Linux and macOS the command has no
  controlling terminal, and `ssh` runs with `-o BatchMode=yes` unless you configured an SSH
  command. A missing credential now fails the command. Credential helpers, askpass programs,
  and SSH agents still work. The pre-push hook inside `push` has no terminal either.
- The new `[general] network_timeout_seconds` key (default 600) stops each of those commands,
  with the processes it started, when it runs longer. For a push the limit includes the
  pre-push hook. The command then exits 4 with the new error code `remote_timeout`, naming
  the remote, and `start` records a `remote_timeout` event.
- A signal or a terminal hangup that stops the tool now also stops a running fetch or push,
  and a running lint or test stage on a hangup. A signal that the tool inherited as ignored,
  for example under `nohup`, stays ignored and leaves the child running.
- A command that waits more than five seconds for a clone-wide lock now says on stderr which
  lock it waits for and which process holds it, and which run when a run's command holds it.

- A run whose configuration snapshot holds a key that a later release removed, such as
  `[hook] enabled`, is governed by that snapshot again. Commands used to reject the snapshot
  and apply the current user and repository configuration files instead, so `status` could
  report the files' `pr_mode` and `submit-findings` could treat a finding as non-blocking
  when the snapshot's `blocking_severities` would block it. A snapshot that cannot be
  loaded at all still fails state-changing commands with `run_record_unreadable`, and
  `status` reports the run as unreadable.

- An event appended after an interrupted append no longer joins the torn line, which
  made `events` and `status` fail with `run_record_unreadable`. Event log lines that
  are not JSON objects are now skipped, and `events` reports how many as
  `skipped_lines`. Telemetry summaries read the same log.

- The pre-push hook recognizes Git's 64-character zero object ID. In a repository that
  uses the SHA-256 object format it no longer blocks every new branch as a force push.
  It blocks a branch push with a message that says SHA-256 repositories are not
  supported, and still allows deletions and the other exempt updates.

- `stage run --baseline` in `in_place` mode now checks out the base commit under the external validation-worktree root (the user cache by default, or `[worktree] root`) instead of inside `.git`, so test runners that ignore VCS directories, such as Jest, no longer report a false pre-existing failure. In-place runs themselves still execute in the current checkout. In `in_place` mode, a `[worktree] root` inside the repository, which isolated modes already refuse at `start`, now makes `--baseline` a setup failure. A `[worktree] root` inside the Git common directory, which a linked checkout keeps outside itself, is now refused the same way in every mode.
- A baseline scratch worktree that cannot be removed, for example because it was locked, no longer replaces the baseline result with an error, and a scratch directory that survives removal is reported the same way. Its `ap/<run-id>-baseline` branch is still deleted, the stage envelope reports `data.baseline_cleanup_failed`, and the run's event log records it. A later `--baseline` check reports the locked path as a setup failure instead of a bare `git worktree add` error, and once the worktree is unlocked it removes the leftover and proceeds.
- A `--baseline` check reclaims what a killed earlier check of the same run left behind: a scratch path that Git still registers as a worktree, even if its directory is gone, while its `HEAD` is still the run's base commit (a scratch worktree whose `HEAD` moved is refused by name and kept), and an `ap/<run-id>-baseline` branch that still points at the run's base commit and is not checked out. A clean, unlocked `<run-id>-baseline` checkout at the base that an earlier release left under `.git` is removed too. It never deletes an unregistered directory at the scratch path. A branch that points anywhere else is refused by name and left in place, and one checked out in another worktree is refused with that worktree's path; neither is deleted during cleanup any more.

- `gc` and the `[retention] run_days` prune now reclaim a run that `abort --force` ended.
  That abort already discarded the run's fix commits, so `gc` no longer retains the run as
  "unmerged fix commits" forever.

- Removing a strict validation worktree that `git worktree lock` protects no longer deletes
  its directory. `abort` and `finish` stop with an error that names the locked worktree, and
  `gc` retains it with the reason `validation worktree is locked`.

- A stage or setup command that runs through the login shell now starts in the validation
  directory even when the login profile changes directory.

- `status` on a run whose independent review command was interrupted now points to
  `agentic-preflight review run`, the command that records the interruption and retries.
  It used to point back to `status`, which does not recover the run.

- `telemetry sync-hosted` now counts a check run with the conclusion `startup_failure` as
  failed. A workflow that could not start used to count toward a `success` outcome.

- Reading a run ID that does not exist no longer creates an empty `runs/<id>/` directory
  with a lock file that `gc` never removed.

- The command reviewer (the packaged `reviewer run` and the example wrappers) sends the
  prompt to the provider CLI as UTF-8 and decodes the CLI's output as UTF-8, replacing
  undecodable bytes, instead of using the locale encoding. A non-ASCII diff under an
  ASCII or cp1252 locale no longer crashes the reviewer with a traceback.
- An `AP_REVIEWER_EVENTS_FILE` that cannot be written, such as a path in a missing or
  read-only directory, now fails the review with one `independent reviewer failed: ...`
  line instead of a traceback or a misleading "reviewer CLI not found" message.
- A provider CLI that cannot be started, for example because `AP_CLAUDE_BIN` or
  `AP_CODEX_BIN` names a file that is not executable, now fails the review with one
  `independent reviewer failed: ...` line instead of a traceback.

- Report missing merge bases in shallow repositories as `missing_merge_base` with history-fetch guidance, distinguish `unrelated_histories`, and preserve operational Git errors instead of calling them empty diffs.

- Build documentation inventories and surface digests from HEAD blobs, making sparse and full checkouts agree and avoiding symlink targets, smudged LFS bytes, and checkout EOL conversion. Prior docs evidence reruns once where its surface digest changes.

- Refuse non-SHA-1 repositories in `start` and `init` with `unsupported_object_format` and a compatibility link, before creating a run or writing configuration and hooks.
- Align the contributor test command with the protected preflight policy (`uv run pytest`); it runs the same suite as the former quiet invocation.

- In `strict` and `reusable` worktree modes, a plain `abort` no longer deletes
  uncommitted changes in the validation worktree. It exits 5 with `unmerged_work` and
  lists up to 200 paths in the new `data.uncommitted_paths`, with the total in
  `data.uncommitted_path_count`, alongside any unmerged fix commits; copied files and
  ignored files do not count, as in `gc`. A worktree that cannot be inspected also
  stops it, with `data.inspection_error` set and the path fields `null`. `--force`
  still discards them. `in_place` runs are unchanged.

- In isolated worktree modes, `abort` no longer deletes validation-branch commits that no
  `respond` or stage registered, from the end of base synchronization until merge-back.
  It stops with `unmerged_work` (exit 5) and lists them in `data.fix_commits`; `--force`
  discards them and reports them in `data.discarded_fix_commits` and the `aborted` event.
  A recorded fix commit always counts, and a checkout detached from the validation
  branch no longer hides the branch's commits. A history that cannot be read, such as a
  merge on the validation branch, also stops a plain `abort` when a non-empty commit
  follows the reviewed head, or when Git cannot open the validation checkout; its listing
  can then include commits that a rebase onto another base brought in.

- `stage run --baseline` creates its scratch worktree with the validation worktrees. For an
  `in_place` run it used to create it next to the source checkout.

- The documentation inventory lists only files committed at the head commit. Files not
  committed there, such as an ignored Sphinx `docs/_build/` tree, an uncommitted
  generated file, or a nested worktree, no longer enter `doc_surface`, the docs
  allowlist, or `doc_surface_sha256`. A committed generated file still counts.

- Correct the owner-review and telemetry documentation, clarify configured command review, and align the skill severity examples.
- Add 0.7-to-0.8 upgrade steps, refresh bundled skill links to 0.8.1, and document the Claude Code 2.1.292 stream-result handling.

- In `reusable` mode, `start --replace` and a `start` after the source head moved, and
  `gc` in both isolated modes, no longer delete commits on a validation branch when the
  validation checkout is detached at the reviewed head. They read the branch tip as well
  as `HEAD` before releasing the checkout. `start` puts the runner back on its branch and
  keeps it leased, so the fresh run exits 4 (`needs_human`) instead of leasing and
  resetting it. `gc` retains the run with reason `validation worktree holds unrecorded
  commits`, or `validation worktree could not be inspected` when the branch history
  cannot be read, and its instruction names the run's `ap/<run_id>` branch.
  `gc --force` still releases it.

- The review diff no longer depends on the Git configuration of the machine that builds it.
  Before, `core.abbrev`, `core.quotePath`, `core.bigFileThreshold`, `core.precomposeUnicode`,
  `diff.noprefix`, `diff.srcPrefix`, `diff.dstPrefix`, `diff.algorithm`, `diff.context`,
  `diff.interHunkContext`, `diff.indentHeuristic`, `diff.suppressBlankEmpty`,
  `diff.submodule`, `diff.orderFile`, `diff.default.binary`, the
  `GIT_DIFF_OPTS` environment variable, and the user-level attributes file each changed
  the patch text that the review digest hashes. `verify`, `push`, and the pre-push hook then rejected a valid note with
  "review fingerprint does not match the Git diff" on any machine whose settings differed
  from the producer's. Git also lengthens abbreviated object IDs as a clone gains objects, so
  a fresh CI clone could reject a note that verified locally. Review patches now carry full
  object IDs, and every such setting is fixed to Git's default. `diff.relative` is fixed too,
  as a precaution: it has no effect when Git runs from the repository root, as preflight
  does. Git attributes still shape
  the text: those in `.gitattributes`, `$GIT_DIR/info/attributes`, `attr.tree`,
  `GIT_ATTR_SOURCE`, and the system-wide attributes file, together with the `diff.<driver>.*`
  configuration of a driver an attribute names. So do `diff.default.xfuncname` and
  `diff.default.funcname`. `COMPATIBILITY.md` lists them.
- Replacement objects under `refs/replace` no longer change the review diff, the changed-file
  list, or risk classification. They exist in one clone only.
- `diff.ignoreSubmodules = all`, and `submodule.<name>.ignore = all` in local configuration,
  no longer hide a submodule pointer change from the review diff, the changed-file list,
  risk classification, or the list of paths a fix commit changes.
- `diff.submodule = log` or `diff` no longer stops `context` with an internal error when a
  change moves a submodule pointer.
- `core.precomposeUnicode`, which Git on macOS sets at `git init`, no longer stops `context`
  with an internal error for a path stored in decomposed Unicode.
- Notes written by 0.8.1 and earlier still verify when their producer left these settings
  at Git's default and the Git attributes and diff-driver settings that still apply give
  the verifier the same patch, unless the verifier's clone holds a number of objects for
  which Git picks a different abbreviation length; the earlier text then differs. A note
  written with another value, such as `diff.context = 5`, may fail verification, even on the
  machine that wrote it, when that value changed the patch. A note for a commit whose
  committed `.gitmodules` sets `submodule.<name>.ignore = all` on a changed submodule always
  fails, because the earlier patch left that pointer out. If verification fails, validate
  the commit again to replace the note.
  The verifier's own `core.abbrev` no longer matters for such a note. Until that format is
  removed in 0.10.0, a binary or other file-level patch in it binds its content only by an
  abbreviated object ID.
  `COMPATIBILITY.md` lists what changes at upgrade.

- The example Claude reviewer wrapper now reads findings from the last `result` envelope even
  when Claude Code streams further events after it. Claude Code 2.1.292 ends `stream-json`
  output with system events such as `task_summary`, which made `review run` fail with
  "reviewer CLI returned no JSON object containing findings". A result envelope marked
  `is_error` now fails the review with its subtype instead of falling back to another object.

- `start` holds the new run's operation lock until the run is ready for review. It used to
  release it right after creating the record, so a concurrent `start --replace`, `gc`, or
  `abort` could orphan the run while its checkout was still being provisioned.

### Security

- `verify`, `approval-check`, `push`, and the pre-push hook take attestation policy from the
  `.agentic-preflight.toml` committed at the trusted base instead of the note's own
  configuration snapshot. When the caller passes the protected base with `--base`, as
  `approval-check` and the hosted workflow do, a branch can no longer skip docs, swap a
  configured lint, test, or review command, widen review exclusions, or lower the review
  executor for itself. `push` and the pre-push hook have no `--base`, so they trust the
  branch the note names; a forged note can still name a branch with a weaker policy there.
- Without `--base`, a note's fully qualified `base_ref` must name a branch under
  `refs/heads/` or `refs/remotes/origin/`. A tag or another ref namespace fails with
  `base_mismatch`, so a note cannot pin the policy of an old tag.
- `start` refuses a base that verification without `--base` cannot resolve to an existing
  branch, such as a tag, `upstream/main`, a commit SHA, or `main~1`, with exit 3 and
  `unsupported_base_ref` before any stage runs, because `push` and the pre-push hook
  could not verify a note that names it.
- Verification recomputes the merge base against the trusted base instead of trusting the
  note's `merge_base_sha`, and rejects a mismatch or an ambiguous merge base with the new
  `base_mismatch` reason. `verify` gains `--base REV`, and the hosted attestation workflow
  passes the pull request's base SHA.
- The README and SECURITY.md now state that attestations are unsigned records, not proof that
  the stages ran.

- The hosted Dependabot approval exemption and the Dependabot auto-merge and attestation
  exception now also require that `dependabot[bot]` set the current head. Signed bot
  commit metadata shows only that GitHub created a commit. The run's event must be a
  `pull_request_target` `opened` or `synchronize` event for the exact head, sent by the
  `dependabot[bot]` Bot account with account id `49699333`, on a pull request into the
  repository's default branch. `.github/scripts/verify_dependabot.py` takes the new
  required `--event-name` and `--event-file` arguments and reports
  `head_set_by_other_actor`, `head_actor_unknown`, `event_mismatch`, or
  `base_not_default_branch` when it fails closed. A head that a person pushed, including a
  clean **Update branch** merge, and runs from `reopened`, `ready_for_review`, or
  auto-merge events now follow the ordinary policy: a preflight attestation for the head,
  plus owner approval where the approval policy requires it. `@dependabot rebase` or
  `@dependabot recreate` restores eligibility instead. `docs/limits.md`
  describes the exemption's guarantee. (GHSA-v55v-mgfw-4j8q)

- Git attributes no longer hide content from review or change the review patch.
  Before, a branch that committed `.gitattributes` with `* -diff` turned every patch into
  "Binary files ... differ": the reviewer saw no content, each file still counted as
  reviewed, and local verification and the pre-push hook passed. `$GIT_DIR/info/attributes`,
  `attr.tree`, `GIT_ATTR_SOURCE`, and a `diff=<driver>` attribute with `diff.<driver>.*`
  configuration also changed the patch text. Review patches are now built without
  attributes: text files are diffed as text, and only a file that Git's default rule calls
  binary gets a "Binary files ... differ" line. Hunk headers no longer follow a diff driver.
  (GHSA-m8wg-p58p-cqmf)
- A diff driver with a hunk-header pattern in `$GIT_DIR/info/attributes`, `diff` set
  there on a binary file, or a driver there that prints a binary file as text, now stops the command with `review_diff_attribute` (exit 3),
  because no other machine can rebuild that text. So does `diff.default.xfuncname` or
  `diff.default.funcname`, which would rewrite every hunk header, and a hunk-header
  pattern or `binary = false` for a driver named `set`, `unset` or `unspecified`, which
  Git's attribute output cannot tell from those states.
- A note from 0.8.1 or earlier whose review text marked a text file as binary is now
  rejected. A note that neither text reproduces still fails as an ordinary mismatch, even
  where the checkout's attributes mark a text file as binary. `COMPATIBILITY.md` lists
  what changes at upgrade.

- `approval-check` now chooses each reviewer's latest decisive review across every head
  of the pull request before checking which head it approves. A changes-requested review,
  a dismissal, or (for an `owner_review` self-approval) a later Comment review with other
  text on a different head now revokes an approval of the current head, so returning the
  branch to an approved commit no longer restores a revoked approval. A later approval of
  a head with different content also replaces an earlier approval. Previously the
  exact-head check ignored reviews submitted on other heads. (GHSA-h78r-2g3v-5227)
- A revoking review now counts even when GitHub reports its `commit_id` as `null`, for
  example after the reviewed commit is garbage-collected. A decisive review by an
  eligible reviewer without an integer `id` now fails the check with
  `attestation_failed` instead of sorting before every other review.
- In `owner_review` mode on the reviewer's own pull request, a review-thread reply (an
  empty-body Comment review) on any head now removes an earlier `approved` self-review.

- The `high-risk human approval` job now passes without an approval only when the policy
  job reports exactly `false` for `requires_approval`. An empty, `null`, or other value
  fails the job instead of being read as "no approval required".
- Every `actions/checkout` step in a `pull_request_target` workflow sets
  `persist-credentials: false`, so the workflow token is not left in the checkout's Git
  configuration. A governance test keeps new checkouts in those workflows the same.
- `init` replaces a symlink at the pre-push hook path instead of writing through it.
  A dangling link no longer creates its target, and `--force` no longer overwrites the
  file a link points to, which could be a tracked file when `core.hooksPath` points
  into the repository. A link to exactly the current hook script is replaced with a
  regular file too. `data.hook_path` now names the link rather than its target, and a
  `core.hooksPath` that ends in whitespace is kept as configured.
- Review grounding reads `docs/**`, `AGENTS.md`, `CLAUDE.md`, `[context] extra_paths`,
  and the `[context]` settings themselves from the run's merge base instead of the branch
  under review. A change cannot rewrite the conventions shown to its own reviewer, or turn
  grounding off or shrink it for its own review; a `[context]` change takes effect after
  it merges. The branch's edits to those files, including files it adds, reach the
  reviewer through the diff. For a branch that edits those files, the grounding changes,
  so its earlier review evidence is not reused.
- `human-approval-rerun.yml` re-runs the approval policy only when the user who submitted,
  edited, or dismissed the review is an owner, member, or collaborator with a review on
  the pull request, or has write access. A bot account, such as a GitHub App, re-runs it
  once it has dismissed an owner, member, or collaborator review of the pull request.
  When a lookup that decides authorization fails, the policy is re-run, with a warning,
  rather than skipped, so a stale success cannot stay authoritative. The re-run job
  retries its own lookups, re-runs the reviewed head's policy runs when the pull request
  cannot be read, and fails with an error when the policy runs cannot be listed. Comment reviews from other users on a public pull request no longer queue
  policy re-runs. Eligibility is decided in an `authorize`
  job before the re-run job joins its concurrency group, which is keyed by the pull
  request and its head, so an ineligible review, or a re-run for another pull request
  with the same head, cannot replace an eligible re-run that is waiting in that group.
- The configuration that `init` writes, and the Claude and Codex reviewer example
  configurations, add `docs/CODEOWNERS` and the reviewer wrapper directory
  `docs/examples/reviewers/**` to `human_review_paths`. Existing configuration files are
  not changed; add these paths by hand to apply them.

- The trusted `pull_request_target` attestation and human-approval workflows no longer run
  `uv tool install .`. They install only the runtime dependencies pinned in the protected
  base's `uv.lock` with `uv sync --locked --no-dev --no-install-project`, and run the
  checker from the base checkout with
  `uv run --locked --no-dev --no-sync python -m agentic_preflight`. Nothing is resolved
  from PyPI or built in these jobs, so a new or compromised release of a runtime
  dependency, the build backend, or a build dependency can no longer run inside them.
  (GHSA-vp48-82vp-gr6c)
- Those jobs pin the uv release in `astral-sh/setup-uv`. Maintainers bump it by hand.
- `pyproject.toml` pins the build backend to `hatchling==1.33.0`, because `uv.lock` does
  not record it.
- `install.sh`, `install.ps1`, and `uv tool install agentic-preflight` still resolve runtime
  dependencies fresh. The installation guide now says so and shows how to run the locked
  versions.

- The run store (`$GIT_COMMON_DIR/agentic-preflight`) and the telemetry directory are
  created owner-only (`0700`) on POSIX. An existing run store you own is narrowed when the
  next run is created, and an existing telemetry directory on its next write. Run records,
  stage logs, events, and telemetry rows were readable by other local users under a default
  umask. On Windows these directories still inherit the parent folder's access list.
- `copy_files` creates each copy at `0600` instead of copying under the umask and narrowing
  it afterwards, and replaces a file or symlink already at the destination instead of
  writing through it.

- `push` no longer publishes the whole local `refs/notes/agentic-preflight` ref. It builds a notes
  commit from the remote notes tip plus only the notes of the commits being pushed (the run's
  merge base to head). A note on any commit outside that range, with its branch name, findings,
  file paths, and configuration, stays local. The guarantee is per commit: a pushed commit's
  note is published even when a run on another branch, such as an aborted one, wrote it, and it
  then carries that branch's name. A concurrent notes update from another clone is kept: `push`
  leases the remote notes ref to the fetched tip, so any change to it, including a deletion or
  rewind, rejects the push, and `push` rebuilds on the new remote tip and retries. In manual
  gate mode, `gate` rejects a fetch that changes the head's attestation. After the push, the published notes commit is
  merged into the local notes ref. `gate` and dry runs list the pushed commits that carry a local
  note in `data.published_notes`, `push` reports the notes it added or changed there, and the
  manual gate command names the built notes commit. `gc` and publication delete temporary notes
  refs that an interrupted command left behind.
  (GHSA-r5jg-frwr-44qc)

- Finding titles, details, suggestions, and `respond --note` text are now redacted for
  `[worktree] copy_files` values before they are saved, as stage logs already were. A
  reviewer that quotes a copied secret no longer places it in `findings.json` or in the
  published attestation note (GHSA-8c37-fpgq-vp36).
- The published attestation note now leaves out configuration sections from the user
  configuration file, except `[diff]`, and records `[redacted user-level command]` in
  place of a command that depends on such a section. Evidence reuse refuses earlier
  evidence that would carry withheld configuration or unredacted finding text into a
  note. Attestation schema 7 is unchanged. See `COMPATIBILITY.md`, and
  `docs/attestations-and-ci.md` for exactly what a note contains.
- `review compare` redacts copied-file values in compared findings before it writes its
  summary and event. Its refusals no longer quote rejected values and are redacted for
  copied-file values.
- `mergeback`, `gate`, and `push` refuse a run that an earlier release started (exit 4,
  `needs_human`, `data.reason` `unknown_config_provenance`), because such a run did not
  record which configuration came from the user file. Abort it and start a fresh run. Run
  records now always carry `user_config_sections`, so earlier releases cannot read new run
  records.

- A repository's `.agentic-preflight.toml` can no longer set `[telemetry] path`. Before,
  a repository's `[telemetry]` section replaced the user's, so a cloned repository could
  make every `git push` append `hook-decisions.jsonl`, and every finished run append
  `runs.jsonl`, to a directory it chose, including a Windows UNC path that opens an
  outbound SMB connection. The repository's `path` is now ignored with a warning, and a
  repository section can only turn `mirror` and `hook_log` off, never on.
- `[telemetry] mirror` and `hook_log` accept only `true` and `false`. Before, values
  such as `1` or `"yes"` were read as `true`, so a repository could use them to turn on a
  file that the user turned off.
- Attestation verification applies the same rule to the protected base's configuration,
  so a `[telemetry] path` there no longer fails verification of a configuration that
  normal loading accepts.

- A branch can no longer hide a submodule pointer change from review and risk
  classification by committing a `.gitmodules` file that sets `submodule.<name>.ignore = all`.
  Git read that file from the checkout and left the path out of the changed-file list and
  the review diff. The review now lists every submodule pointer change, and so does the list
  of paths a fix commit changes, which `respond` and merge-back check.

- A branch can no longer replace the reviewer that reviews it without human approval.
  `[review] command` and `[review.contracts] planner_command` run from the worktree under
  review, and verification compared only the command string, so a branch could rewrite a
  repository reviewer script to report nothing and pass with no owner review. Risk
  classification now treats every repository path these commands name, and every file
  under that path's directory, as a human-review path. Files beside a script at the
  repository root are not covered; list them in `[review] trusted_paths`.
  `approval-check` and attestation verification read the commands from the protected
  base. (GHSA-3372-q2xp-jr7q)
- New `[review] trusted_paths` lists further reviewer files, such as prompts or modules
  loaded by name, whose changes need human review.
- This repository adds `docs/examples/reviewers/**` to `[policy] human_review_paths` and
  CODEOWNERS.
- `docs/limits.md` and `docs/independent-review.md` now say that verification compares
  command strings, not the scripts that they run.
- Reviewer path detection also protects directories that a command changes to (`cd`,
  `pushd`, `-C`, `--directory`, `--cwd`, `--prefix`, `--project`, `--chdir`), directories
  in `PYTHONPATH` and `NODE_PATH`, the top-level package of a dotted `-m` module (or
  `name.py` and `name/**` for `-m name`), and the literal
  path after a leading `$VAR/` or `$(...)/` expansion. Control operators split the command
  only outside quotes, so a quoted path such as `'reviewers (local)/rev.py'` stays whole.
- The example Claude wrapper passes `--safe-mode --setting-sources user`, and the example
  Codex wrapper passes `--config project_doc_max_bytes=0 --ignore-rules`, so the branch
  under review cannot instruct or reconfigure its reviewer through `CLAUDE.md`,
  `AGENTS.md`, or `.claude/settings.json`. Codex has no option to skip a project
  `.codex/config.toml` or repository skills under `.agents/skills`; the Codex example
  lists `.codex/**` and `.agents/**` in `trusted_paths`. The example configurations
  run the wrapper with `python3 -S -B`, so a branch-added `sitecustomize` module does
  not run.
- This repository adds a root `sitecustomize.py` to `human_review_paths` and CODEOWNERS,
  because its reviewer command starts with `uv run`.

## [0.8.1] - 2026-10-28

### Added

- A `mergeback` conflict retry on an unchanged branch whose recorded fix commits never landed now exits 4 with `fixes_not_landed` instead of repeating the same conflict.

- Added a JSON Schema for the stdout envelope and for each command's `data` object under
  `skill/reference/schema/`, generated from typed models by `tools/generate_schema.py`.
  Payloads are unchanged; tests keep the schemas and the command reference in step.

### Changed

- The trusted preflight attestation check now runs in its own `pull_request_target` workflow, so a pull request can no longer rewrite the job that verifies it. Pull request CI also gains a macOS Python 3.13 test leg.

- The default telemetry mirror and hook decision log now leave out every repository without a network remote, wherever it lives, not only one under the system temporary directory. A preflight run gates a push, so a repository that cannot push is a test fixture; fixtures that a harness creates under the home directory no longer bury real repositories in `agentic-preflight report`. An explicit `[telemetry] path` still records them. The reason code `temporary_repository` is now `no_network_remote`, and `mirror.temporary_runs` and `data.source.clone.temporary_runs` are now `excluded_runs`.

- `telemetry rebuild`, `telemetry sync-hosted`, and `report` now ignore an individual
  configuration key that a later release removed, such as `[hook] enabled`, with a
  warning on stderr that names the file and the key. These commands only read derived
  data, so a stale key in a repository file no longer keeps the user from their own
  telemetry. Every other command still rejects the key, and every command still rejects
  a key the tool never knew.

- The `publish` command help now uses US spelling ("Summarize"). The README states the
  problem, shows a component diagram, and links the design documents; the dogfooding case
  study labels its one private repository and reports public-only totals. The approval
  runbook and the compatibility removal plan moved to `docs/maintainers/`.

### Fixed

- A file containing a lone carriage return before a `diff --git` line no longer splits into a phantom patch that fails `context` with `internal_error`. Patch output is now split on raw line feeds; the patch text, review-unit digests, and `diff_sha256` are unchanged, so existing evidence stays valid.

- Command detection skips a non-UTF-8 `Makefile`, `justfile`, or `pyproject.toml` and a `package.json` whose top level is not an object instead of failing with `internal_error`. A script or target now matches a stage only when one of its words, split at any character other than a letter or digit, is the stage name or its plural (`test:unit`, `unit-tests`) or a known tool for that stage (`pytest` and `unittest` for test; `eslint`, `ruff`, `flake8`, `pylint`, and `biome` for lint), so `latest` and `contest` are no longer offered as test commands.

- A changed path that is not valid UTF-8 now round-trips to Git as the same bytes, so its patch is found instead of failing with a pathspec that matches nothing. Output, digests, and stored records still show the path with `\\xNN` escapes, as before.

- A rebase that fails during base synchronization for a reason other than a conflict, such as a rejecting `pre-rebase` hook or a failing commit signer, is now reported as a non-conflict failure that includes Git's error output, and the checkout is restored to its original head. Previously it was reported as a conflict with no paths and no detail.

- In `reusable` worktree mode, a run orphaned by a restart releases its runner at once only when it has no fix commits, the runner is clean, and the runner is still at the reviewed head, so the next `start` no longer fails with `needs_human` because the runner is still leased. Uncommitted or unrecorded work keeps the runner leased, and a failed release is recorded as a `runner_release_failed` event.

- `start` refuses a detached HEAD or a branch with no commits with exit 3 and `no_branch`, and `push` now sends the attested head SHA to `refs/heads/<branch>` instead of `<branch>:<branch>`.

- On Windows, Git history containing non-UTF-8 path bytes now produces patches without sending those bytes through Unicode command-line arguments. A single unfiltered diff is matched to the requested raw inventory entries; path display and evidence-digest encoding are unchanged.

- Windows raw-path diffs now stream patch output and discard excluded patch bodies, so large excluded generated files do not inflate Python memory use before the requested review patches are returned.

### Security

- In `strict` and `reusable` worktree modes, `respond --action fixed --commit` now requires a new commit on top of the reviewed head, and a `mergeback` conflict retry no longer attests an unchanged branch whose recorded fix commits never landed. A blocking finding can no longer be closed by citing a commit that review already covered.

- The pre-push hook exempts only pushes whose remote destination is `refs/notes/agentic-preflight`. Pushing the notes ref onto a branch is now checked like any other branch update.

## [0.8.0] - 2026-10-22

### Upgrade highlights

- Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged. Notes
  that 0.7.0 wrote stay verifiable, and run records that 0.7.0 wrote stay readable.
- New configuration keys (`[telemetry]`, `hook_log`, `[review.contracts]`) and new run
  record keys (`producer`, `started_at`, `response_reason`) are rejected by 0.7.0.
  Upgrade every machine that shares a clone before enabling the new configuration, and
  use 0.8.0 or later to inspect or clean up runs that 0.8.0 touched.
- Three compatibility paths for data no release wrote are removed; see "Removed" and
  [the compatibility removal plan](docs/maintainers/compatibility-removal-plan.md) for what follows
  in 0.9.0.

### Added

- List the distributions installed in the pinned contract-check image in one bounded,
  snapshot-free container per image digest and deliver them to the contract planner as
  `contract_checks.execution.installed_packages`, cached under the run's
  `contracts/images/` directory. Unavailable Docker, images, or listings are explicit
  entries with a reason, never an empty list. The listing states availability only. The
  worked planner prompt now prefers a listed real consumer over a substitute
  implementation and records a limitation when the consumer is absent.

- Record full evaluation and snapshot runtimes, accepted findings, workflow state visits
  and durations, and Codex/Claude CLI token usage in public regression reports. Preserve
  failed-run diagnostics, unknown counts, and partial observations separately from exact
  totals; normalize provider cache accounting without double counting.
- Append state-transition audit events for run creation and committed transitions,
  including intermediate states within a transaction, without changing run schema 2.

- Log each pre-push hook decision to `hook-decisions.jsonl` in the local telemetry
  directory: the time, the repository identity, the remote name, whether the push was
  allowed, a machine-readable reason (`attested`, `already_on_remote`, `deletion`,
  `exempt`, `no_updates`, `config_unavailable`, `force_push_refused`, or
  `missing_attestation`), and each ref update with a seven-character SHA and whether it
  rewrites remote history. The write is best effort, bounded in time, and never changes
  the hook's exit code or message. Nothing is sent over the network.

- Add `[telemetry] hook_log` (default `true`) to turn the hook decision log off.

- `agentic-preflight report` adds `data.hook_decisions` and a "Hook decisions" Markdown
  section: allowed and blocked counts, blocked decisions by reason, and per-repository
  counts, with the report's scope and its `--repo`, `--since`, and `--until` filters.
  Malformed log lines are counted. The limits now state that `git push --no-verify`
  bypasses and pushes allowed because the tool was missing from `PATH` are not logged.

- Add `agentic-preflight telemetry sync-hosted`, which joins GitHub pull-request and
  check outcomes onto the pushed `DONE` runs of the clone. It is the only telemetry
  command that talks to a network service, and it only reads: `gh api` GET requests for
  the pull requests and the check runs of each pushed commit. It writes `hosted.json` in
  each run directory, then regenerates the summary and the mirror row of that run. The
  summary's `hosted` object gets `pr_number`, `pr_state`, `merged`, `checks_conclusion`,
  and `fetched_at`. `--limit` (default 50), `--run`, `--refresh`, and `--dry-run` select
  the runs. A missing or logged-out `gh`, an `origin` that is not on github.com, a rate
  limit, or a network failure stops the command with an error code in the envelope, and
  the runs synced before it keep their outcomes. A failure that concerns one commit is
  listed in `data.errors`, and the older runs are still read. No workflow command calls
  it.

- `agentic-preflight report` adds synced hosted outcomes, overall and for each
  repository: synced runs, "preflight green, hosted checks failed" over synced runs with
  settled checks, pull requests found, the merged share over synced runs with a pull
  request, and the check conclusions. Two new limits say that a failed hosted check is
  not proof of an escaped defect, and that only synced runs count.

- Add a `telemetry sync-hosted` step to the "Summarize preflight history" playbook, for
  when the user wants hosted outcomes.

- Record producer identity on every new run: the Agentic Preflight version, the Python
  version, the platform, the coding-agent client, and the model that the agent reports.
  `start` accepts `--client NAME` and `--model NAME`. Flags win over detection, and
  `CLAUDECODE=1` is the only detected client marker. Unknown values stay `null`, and the
  model is never inferred. `start`, single-run `status`, and the `run_created` event
  return the `producer` object. It is informational only and is not in the attestation,
  fingerprints, or configuration digests.
- Instruct the skill to pass `--client` and, when the agent knows its exact model id,
  `--model` on `start`.

- Publish the 2026-10-17 low-effort Opus 5.5 and GPT-6.1 Sol public smoke comparison,
  including snapshot evidence, runtime, findings, workflow state timing, token usage,
  provenance, and limitations; link it from the project README and eval documentation.

- Add `agentic-preflight report`, which counts finished runs from the local run
  summaries for this repository, or for every repository with `--all-repos`. It reports
  run states, completion, findings, blocking findings fixed, repair commits, validation
  restarts, stage retries and reuse, median and p90 duration, and selected events,
  overall and for each repository, client, and reported model. Filters are `--since`,
  `--until`, `--repo`, and `--state`. Each ratio names its denominator, null values are
  counted as excluded, and malformed mirror lines and unreadable run records are
  reported. When the mirror has no rows for the clone, the report summarizes the
  clone's run records in memory. `--format markdown` adds compact tables in
  `data.markdown`. Every report lists its limits. The command writes nothing and makes
  no network request.

- Add the "Summarize preflight history" playbook to the skill: rebuild stale mirrors,
  run `report --all-repos --format markdown`, and show the result with its limits.

- Add opt-in contract plans for every delivered review unit and bounded offline
  execution of ordinary, boundary and composition probes in digest-pinned containers.
  Retain head-bound execution receipts, surface failed and unverified checks to the
  final reviewer, and bind the receipt digest to accepted review coverage.

- Write a compact, versioned `summary.json` (summary schema 1) of code-derived facts
  for each run when it reaches `DONE`, `ABORTED`, or `ORPHANED`, and append the same
  summary as one line to a local, user-level `runs.jsonl` mirror under
  `$XDG_DATA_HOME/agentic-preflight/telemetry` or
  `~/.local/share/agentic-preflight/telemetry`. Summaries record states, counts, and
  digests only: never the intent text, diffs, finding text, logs, or command output.
  Nothing is sent over the network, and a telemetry failure never fails the command.

- Add the `[telemetry]` configuration section (`mirror`, `path`), intended for the user
  configuration file. It is excluded from run snapshots, so it never changes a
  configuration digest, stage evidence, or an attestation.

- Add `agentic-preflight telemetry rebuild`, which regenerates the summaries of every
  terminal run record in a clone (run schema 1 and 2) and rewrites that repository's
  mirror rows, keeping the newest row per run and leaving other repositories' rows
  unchanged. `finish`, `abort`, and `gc` report the summary outcome in `data.telemetry`.

- Add an optional `--reason` to `respond --action dismissed|accepted`, from the closed
  list `false_positive`, `out_of_scope`, `duplicate`, `already_fixed`, `accepted_risk`,
  `deferred`, and `other`, and refuse it with `--action fixed`. The finding stores it as
  `response_reason`, and the `finding_resolved` event repeats it. It is telemetry only:
  stage evidence and attestations never contain it.
- Add an optional `--reason` to `abort`, from `user_cancelled`, `superseded`,
  `wrong_base`, `environment_failure`, `tool_error`, `no_longer_needed`, and `other`.
  The `aborted` event records it with `aborted_from_state`, the state that the abort left.
- Record `started_at` on each stage record when the stage begins: review at
  `BEGIN_REVIEW` and again at each return to review, docs at `BEGIN_DOCS`, lint at
  `RUN_LINT`, and test at `RUN_TEST`. Stage evidence, fingerprints, and attestations do
  not contain it.
- Run summaries add `findings.by_response_reason`, `terminal.abort_reason`,
  `terminal.aborted_from_state`, `stages.<name>.started_at` and `duration_seconds`, and
  `timing`: visits and wall-clock seconds per state from the `state_transition` events,
  with review and shell totals. An incomplete transition history is marked, and absent
  values stay `null`.
- `agentic-preflight report` adds "Finding responses" (reasons, and a perceived
  false-positive share with its denominator), "Abort reasons" (by reason and by state),
  the median and p90 duration of each stage, and the share of run time in the review and
  shell stages, for all runs and for each repository.
- The skill tells agents to always pass the closest `--reason` when they dismiss or
  accept a finding or abort a run, and to put the specifics in `--note`.

### Changed

- Require behavioral input-class inventories and structured oracle provenance in schema-2 contract plans.
  Probe references are validated, and explicit coverage gaps remain unverified even when assertions pass.
- Include consumer evidence, oracle provenance, inventory, and probe code in the bounded final-review handoff.
  Report inventory and check omissions separately and direct reviewers to the full receipt.
- Deliver failed and unverified checks before passed ones in the handoff, cap the inventory's share of the budget,
  shorten long probe output streams in the summary, and count the input classes that rely on each probe.
- Refuse a receipt whose results do not match its plan's checks and probes instead of failing while building context.
- Preserve schema-1 receipt files and their digest-bound representation.
  Require a schema-2 plan before a new findings submission; ordinary disabled-contract review is unchanged.

- Re-record the README workflow animation for 0.7.0 and publish it from the `badges`
  branch instead of `main` history. The documentation frame now shows the `doc_surface`
  fields that 0.7.0 reports.

- Verified Dependabot updates, including major versions and GitHub Actions, no longer
  require human approval. The protected-base hosted workflow verifies signed bot
  contributions and clean base merges; required attestations, CI checks, and the
  narrower auto-merge eligibility policy remain in place.

- The installed pre-push hook passes the remote name to `hook-check` in the
  `AGENTIC_PREFLIGHT_PUSH_REMOTE` environment variable, which older versions ignore.
  `init` now replaces a pre-push hook that is exactly one an earlier version installed;
  any other hook is still left unchanged.

- Use ASD-STE100 style in the contract checks guide and in the contract checks section
  of the configuration reference. Facts, headings, configuration keys, and examples are
  unchanged.

- Use ASD-STE100 style in the README, evaluation guides, and published reports.
  Generated evaluation summaries explain location matches and partial token counts
  with shorter sentences. Scoring rules and recorded measurements are unchanged.

- Use ASD-STE100 style in the remaining user documentation: installation, configuration,
  worktree modes, the pre-push hook, attestations and CI enforcement, limits, the 0.7
  upgrade guide, the fingerprint contract, grounded context, change scope, the state
  machine, prior art, both ADRs, and the support, security, compatibility, contributing,
  and releasing guides. The state-machine guide now lists every declared transition, and
  a test keeps that table equal to `machine.py`. The demo fixture warms the `uvx` caches
  before a recording, and the tape waits longer after `mergeback`. Behavior and
  configuration are unchanged.

### Removed

- Remove three compatibility paths that guarded data no release wrote: the schema-1
  contract plan and receipt readers, and the serializers that kept `producer` and a
  stage's `started_at` out of rewritten run records. A schema-1 receipt is now reported
  as unreadable and left in place, and a rewritten record stores `producer` and
  `started_at` as `null` when they are unknown.

### Fixed

- Preserve measured state visits and intervals when an eval audit log ends inside a
  UTF-8 character, while retaining incomplete-history and unknown-tail timing markers.

- Repeat `--client` and `--model` in the `start` command that `next.command` suggests
  for a refused, stale, interrupted, or aborted run when those values were given as
  flags, so a replaced or restarted run records the same client and model. A client
  detected from the environment is not turned into a flag: the next `start` detects it
  again.

- Use a container-local monotonic deadline for contract probes so host/engine clock skew cannot shorten the execution budget.
- Preserve review state and attempt counts when copied-file redaction prevents the contract planner from launching; report zero reviewer invocations and the contracts phase.
- Include the host-run contract planner in the configuration trust warning.

- Keep runs and hook decisions of temporary repositories out of the user-level telemetry
  directory. A repository is temporary when it is under the system temporary directory
  and has no network remote: no `origin`, a local path such as a bare repository, or a
  loopback host. Test and evaluation harnesses that drove the CLI against fixture
  repositories appended their runs to the real `runs.jsonl` mirror. A scratch clone under
  the temporary directory that pushes to a host keeps its telemetry. A run of a temporary
  repository still writes `summary.json` in its run directory, but appends no mirror row, and
  `hook-check` appends no line to `hook-decisions.jsonl`. `data.telemetry` and the
  `summary_written` event report `reason: "temporary_repository"`. `telemetry rebuild`
  and `telemetry sync-hosted` in such a clone leave the mirror unchanged, and in other
  clones they leave out runs of temporary repositories; their `mirror` object adds
  `reason` and `temporary_runs`. `report` counts those runs in
  `data.source.clone.temporary_runs` and no longer suggests a rebuild that cannot add
  them. An explicit `[telemetry] path` is an opt-in and still receives every row.

- The regression eval runner gives every CLI subprocess its own `XDG_DATA_HOME` and
  `XDG_CACHE_HOME` in the eval workspace.

## [0.7.0] - 2026-10-13

### Upgrade highlights

See the [0.6.0 → 0.7.0 upgrade guide](docs/upgrade-0.7.md) before rollout.

- **Breaking:** Remove trusted CI test delegation, `ci` commands, and `hosted-check`.
  Applicable tests finish locally; hosted verification uses explicit fetches and
  offline checks. Retired `[ci]` configuration warns and is ignored.
- **Breaking:** Remove cross-run lint/test reuse and shell input contracts.
  Retired `[reuse]` configuration warns and is ignored; review/docs refresh remains.
- **Breaking:** Remove `[hook] enabled`; delete it from repository and user files.
  Command-line usage errors now exit 6 with a JSON envelope.
- Align producer, hook, copied skills, and protected verifier. Run schema 2,
  attestation schema 7, and fingerprint version 2 remain, with stricter evidence
  acceptance and new configuration fields that older consumers may reject.
- Inspect active runs before upgrading. Pending delegation and interrupted legacy
  journals require fresh validation; historical audit records and user work remain.

### Added

- `start` reports this clone's validation checkout footprint as `data.housekeeping`:
  every directory under the worktrees root and every `ap/*` worktree, with its size,
  registration, lease, owning run, and whether it is `reclaimable` or `retained`, plus
  `total_bytes`, `reclaimable_bytes`, the `ap/*` branch count, and `next_command`
  (`agentic-preflight gc` when anything is reclaimable). `noisy` is true at 1 GiB or
  more reclaimable, and the skill then tells the user once. The report is read-only,
  and a failure inside it sets `data.housekeeping.error` without failing `start`.
- Stop a run for human resolution once validation has restarted `[stage] max_restarts`
  times (default 5). A restart is any return to review that discards progress: a
  committed lint or test repair, a changed reviewed snapshot, changed stage inputs, or a
  non-equivalent merge-back resolution. The stopped run exits 4 with `max_restarts`,
  refuses every command except `status`, `logs`, `events`, and `abort`, and reports
  `validation_restarts`, `max_restarts`, and `needs_human` in `status`.
- Exit code 6 (`USAGE_ERROR`) for command-line usage errors. An unknown option,
  missing argument, bad choice, or unknown command now prints one JSON envelope with
  error code `usage_error` instead of Click's usage text, and exits 6 instead of 2,
  which collided with stage failure. `hook-check` keeps Click's behaviour.

### Changed

- Narrowed this repository's human-review policy to explicit approval, verification,
  publication, and governance paths, with matching CODEOWNERS entries.
  Ordinary package code, tests, and CLI references are medium risk. The skill's CLI
  walkthrough now lives separately from its authoritative approval and recovery rules.
  Routine instruction documentation no longer requires owner approval solely by path;
  substantive authorization, approval-enforcement, and bypass-rule changes still do.
  The configured review approval for high-risk changes and escalation from severe findings
  remain unchanged; installed projects' default policy is unchanged.

- Added opt-in `owner_review` approval with an explicit human reviewer: exact-head
  Approve reviews on other authors' PRs and Comment reviews containing `approved` on
  their own PRs. This repository now uses `owner_review` with `elanthus` as the
  reviewer instead of requiring a GitHub Environment approval.
  Dependency manifest and lockfile changes no longer trigger human review; automated
  dependency audits remain required. The policy migration itself uses the old base mode.

- Moved the two immutable dogfooding evidence ledgers to `elanthus/preflight-eval-results`
  and pinned the case study links to the archive commit. Collection data and case study
  counts are unchanged; the ledgers are no longer stored in this source tree.

- Context grounding derives module terms for every Python package, not only
  `agentic_preflight`. A changed `.py` file under any directory now contributes its path
  relative to the top-level directory and its dotted module name.
- Public regression eval method is now `public-smoke-v5`. A finding with a line is a catch
  only within two lines of the gold range; hunk overlap applies only to findings without a
  line. The substring category measure is renamed to keyword hit rate: `summary.json` keys
  `category_agreement` become `keyword_hit` per case and `keyword_hit_rate` in aggregates.
- Every eval fixed snapshot now changes the gold file, so its false-positive control can
  fire. `plaintext-secret-log` gold lines are corrected to `[2, 2]`, and case loading fails
  when a gold range misses the lines changed from base to vulnerable.
- Eval fixture trees include `CODEOWNERS` and `AGENTS.md`, and grounding-on runs fail when
  `context` returns no grounding entries.
- The pytest configuration no longer passes `-q` in `addopts`, so the documented `uv run
  pytest -q` prints its summary line.
- Running `agentic-preflight` with no arguments prints the root help on stdout and exits
  0 on every supported Click version. Click 8.2 and later previously made it exit 2.
- Eval `summary.md` starts with a mode and executor line and renders booleans as `yes`
  or `no`, missing values as `n/a`, and rates with three decimals. `summary.json` is
  unchanged.
- Documentation now matches current behavior for the docs surface, config section
  replacement, `start --intent`, concurrent worktree modes, and SemVer scope. The push
  and pull-request authorization rule lives in one section of `docs/configuration.md`.
- Building the documentation inventory no longer walks the whole worktree. The walk skips
  the root `.git` and every directory that no documentation pattern can match, such as
  `node_modules` and `.venv` under the default patterns. Every docs fingerprint and docs
  `context` builds this inventory. On a test worktree with 120,000 ignored entries, one
  build fell from about 3 s to 3 ms. The inventory and `doc_surface_sha256` are
  unchanged, so existing docs evidence remains reusable. A pattern that can match at any
  depth, such as `**/*.md`, still needs the full walk.

- Simplify saved-run read diagnostics to `unreadable` or `invalid`, preserving records and ownership on failures without exposing stored values.
- Ignore retired configuration keys in saved runs consistently with attestation verification. Keep full-content cached validation of current settings and preserve original snapshots.

- Check stage origin and configuration snapshot digests at evidence admission and attestation verification instead of rehashing every stage origin when loading run records.
- Report corrupt active stage origins with dedicated stage-evidence recovery guidance before replacing saved evidence or changing the source branch during merge-back.
- Keep status inspectable when active evidence digests are invalid, distinguishing structurally readable run records from invalid evidence and retaining ordinary unmerged-work cleanup protections.
- Keep missing-source garbage-collection guidance ahead of abort recovery while retaining invalid-evidence status diagnostics.

- Cache successful validation of immutable run settings in a bounded cache keyed by
  complete snapshot content and validation policy. Strict restart checks and tolerant
  historical cleanup retain their existing validation rules.

- The advisory pre-push hook now skips updates to evidence refs like tags, including
  replacement and deletion. Evidence objects pushed to branches still pass through
  the ordinary force-push and attestation checks.

- Interrupted merge-back after a source rebase now requires a fresh run when the source has not reached the verified tree. Older saved attempts still load, ignoring the removed intermediate tree field.
- Preserve conflict-abort ownership checks, including canonical commit comparison for refs, so a user cherry-pick started after the initial operation check retains its staged resolution.

- Windows atomic file replacement now makes at most three attempts, with a fixed
  5 ms pause between permission failures, instead of eight attempts with exponential
  backoff. Persistent failures still raise the final error; POSIX replacement and
  owner-only protection for Windows `copy_files` are unchanged.

- Fold eight helper modules into their existing consumers while preserving publication policy, attestation validation, lifecycle invariants, evidence transitions, grounding budgets, test eligibility, and copied-file output protection.

- Replaced integration command factories and lifecycle operation tables with explicit
  commands and install/uninstall functions, preserving CLI options and successful
  results, installed-skill hashes, and symlink handling. Installation now removes and
  copies a bundle after checking all targets for conflicts. A filesystem failure can
  leave an absent or partial copy; correct the failure and rerun installation with
  `--force` to repair it.

- Share protected shell execution across validation stages, independent review, and shadow review while preserving copied-file redaction and failure envelopes.

- Remove unused internal helpers and test-driver history. Documentation inventory entries
  omit the always-true `exists` field; detected commands omit `trust` and retain their
  source for supported project manifests. Workflow run-line detection is removed by
  the subsequent grounding and detection simplification. Click 8.2 or newer is required.
  Bare CLI invocation still prints help and exits 0. Fingerprint evidence is unchanged.

- Simplify stage state declarations, command platform selection, and review submission validation while preserving command planning and review coverage checks.

- Share equivalent text and JSON digest helpers and synchronization result serialization, preserving existing evidence bindings and callable keyword arguments.

- Consolidated equivalent run risk assessments, finding-resolution hints, and publication command/refspec builders without changing stage ordering, policy selection, push ordering, or recovery responses.

- Simplify startup orchestration by passing completed phase results explicitly and persisting synchronized commit and base metadata once before setup.

- Consolidated equivalent scripted test agents, review submissions, and green-run
  helpers, retaining CLI tests of recorded software-test skips and executable
  helpers in documentation and CI directories. Docs submissions
  keep their distinct payload; Git type-change and conditional symlink coverage
  are unchanged. Product behavior is unchanged.

- Consolidated prior art, PR lifecycle, exit-code, worktree setup, and CI matrix documentation into linked canonical sections; workflow behavior is unchanged.

- Keep development dependencies focused on local checks; security CI runs the pinned
  audit tool in an isolated environment. CI and release builds share one wheel smoke
  script, and repository ignores cover the artifacts this project creates.
- Replace randomized state-machine and glob-prefix checks with deterministic
  enumeration, retaining their distinct guarantees without Hypothesis.
- Keep the current README animation available from a pinned existing commit and
  publish future recordings to the badges branch. Recording is optional, and
  badge updates preserve other assets.

- Verified Dependabot Python patch/minor updates limited to the root manifest and lockfile
  can pass hosted attestation and approval gates and request squash auto-merge after required
  checks. Major, Actions, unknown, and broader updates retain the existing workflow.
  Eligibility requires exactly one verified bot commit matching the event and current
  head, so appended human commits retain the ordinary gates. Repository administrators
  must separately enable auto-merge and squash merging.

- Isolated validation worktrees now default to a per-clone directory under
  `$XDG_CACHE_HOME/agentic-preflight/worktrees/` when `XDG_CACHE_HOME` is absolute,
  otherwise `~/.cache/agentic-preflight/worktrees/`. An explicit `[worktree] root`
  remains authoritative. `gc` reclaims eligible unused reusable runners in both
  the cache and old sibling locations while preserving leased, locked, dirty,
  and retained work. Remaining old registrations appear in `data.legacy_worktrees`.

- In `peer_review` and `owner_review` modes, an approval of an earlier pull-request head
  now carries to the current head when the trusted checker proves from Git objects that
  the branch content is unchanged. Both heads must change the same paths with the same
  `git patch-id --stable` identity against the base, and the current tree must equal
  Git's clean merge of the approved head with the base commit it now contains. Both heads
  must also merge into the base with the same clean result and carry the same non-merge
  commit patch identities, so `-s ours` merges cannot hide a revert or add commits. A clean
  merge from the base or a clean rebase keeps approval. Content commits, conflict
  resolution, merges that add content, and base changes next to the contribution still
  need a new review. A later dismissal or changes-requested review on any head revokes
  a carried approval. `environment` and `manual_merge` modes are unchanged.
- An approved head replaced by a rebase is fetched only through its
  `refs/agentic-preflight/evidence/<sha>` ref; without it, the approval does not carry.
- Approval results add `approved_head` and `approval_carried`.

- Contributors now add individual changelog fragments instead of editing the shared
  Unreleased section. A dependency-free release command assembles those fragments
  and existing Unreleased entries into a dated release while preserving history.

- Narrow repository owner-review path triggers for routine skill documentation and
  compatibility notes. Preserve protected enforcement paths and require exact-head owner
  approval for substantive authorization, approval-enforcement, or bypass-rule instructions.

- Prepare 0.7.0 with release-pinned README and bundled-skill links, corrected local
  test and evidence-reuse guidance, verified GitHub settings, and a 0.6.0 upgrade guide
  covering producer/verifier alignment, saved runs and notes, and rollback limits.

- This repository's `human_review_paths` and CODEOWNERS now cover only decision points:
  policy and ownership files, CI workflows, merge approval, attestation verification,
  the pre-push hook, publication gates, and security guidance.
  Configuration, data models, findings, the state machine, run invariants, review
  coverage, change scope, the wire schema, evidence-reuse fingerprints, refresh
  validation, and the release procedure are now classified medium or low risk and no
  longer require owner approval on their own. Product behavior is unchanged.

### Removed

- The `[hook] enabled` key. It was never read, so setting it had no effect. A config that
  still sets it now fails to load with an unknown-key error; delete the line from
  `.agentic-preflight.toml` and the user config. `init` no longer writes it.
- The unused `expect_seq` parameter of `Store.transaction` and its `StaleWrite` error.
  The per-run exclusive lock is unchanged.

- Removed opt-in trusted CI test authority, delegated test states, and the `ci` commands. Tests complete locally before publication, except the existing documentation/CI-only skip. Legacy `[ci]` tables are ignored with a warning; schema-7 verified notes remain readable, while pending-test notes are rejected.

- Remove `hosted-check` and remote note polling. Trusted CI now fetches the exact
  event head, notes, and retained evidence from the contributor remote, then runs
  offline verification or approval evaluation with the protected-base checker.

- Remove opt-in shell input contracts and file/toolchain/environment capture. Retired `[reuse]` configuration is ignored with a warning; lint/test evidence is never reused in a new run, even with identical trees. Same-run tree-equivalent merge-back publication remains supported. Original same-commit notes remain verifiable.
- Preserve frozen historical run policy and committed protected-policy reads when ignoring retired sections. Active pre-upgrade shell evidence may conservatively revalidate once; original snapshot digests remain unchanged.

- Removed the local run/findings write-ahead journal. Transactions now atomically replace findings then the run under the existing lock; interruption between writes can leave new findings with the prior run state and sequence.
- The next `start` retires interrupted legacy validation identified by a pending journal and starts fresh, preserving audit history and user work for safe cleanup. `status` directs these runs to restart and validation commands refuse to continue their evidence.
- Interrupted legacy-run guidance preserves the human stop at the validation restart limit and identifies the source worktree. Direct restart cannot reset that stop; explicit abort remains available.
- Evidence discovery permanently excludes validation from retired interrupted legacy runs, so their preserved green audit records cannot satisfy a fresh run’s stages.

- Remove generated CODEOWNERS grounding and its glob matcher. GitHub ownership policy
  remains intact. Stop scraping lint/test command candidates from CI workflows;
  configured commands and supported manifest candidates remain available.

- Prose-pinning tests for skill wording. Structural CLI/documentation contracts,
  protected workflow configuration checks, executable workflow tests, and built-wheel
  alias coverage remain; product behavior is unchanged.

- Remove the regression eval summary golden and duplicate dry-eval CI job, retaining
  scripted scoring and determinism checks. Git-history isolation now builds one
  representative repository per selected snapshot; every corpus case still receives
  fixture validation.

### Fixed

- Make Windows compatibility tests preserve exact LF/CRLF fixture bytes and
  coordinate transient file sharing within the existing replacement retry budget.
  Fingerprint and storage behavior are unchanged.

- Keep POSIX termination handlers outside the Windows implementation so mypy
  does not reject the supported Windows build as unreachable. Process cleanup
  behavior is unchanged on both platforms.

- Require a single verified Dependabot commit matching the event and current PR head
  before applying the hosted exception; human-appended commits retain ordinary gates.

- Saved runs and attestation snapshots tolerate known removed configuration keys
  while preserving original digest bindings. Unknown keys and invalid current
  settings remain errors; unreadable records, ownership, and unmerged work are
  preserved during inspection and cleanup.
- `gc` and `abort` no longer start two Git processes for every commit in a run's history
  when they check whether its fixes landed. Each commit's stable patch ID came from its
  own `git show` piped into `git patch-id`. One `git log` of the run's range now
  feeds a single `git patch-id`, with the same diff options and combined diffs for
  merges, so every patch ID is unchanged. With 20 commits and 2 fixes, the check drops
  from 45 Git processes to 6. Separately, the review diff text and its byte size are
  now computed once per diff instead of on every access.
- Redacting copied-file secrets from stage output no longer scans the whole output once
  per secret. Every dotenv value, every line longer than three characters, and the
  whole content of each copied file is a secret, and redaction ran one `str.replace`
  over the complete captured output for each of them. Redaction now builds one
  pattern from all secrets, as a prefix tree that prefers the longest secret at each
  position, and replaces them in a single pass. Output is unchanged except where two
  secrets overlap in the output, where the one that starts first is now replaced.
  Either way no secret is left intact. With 2,000 secrets and 3.7 MB of output, one
  redaction drops from 2,000 passes (1.8 seconds) to one (0.6 seconds).
- `stage run` no longer scans the whole worktree with `git status` six or seven times.
  The pre- and post-command shell fingerprints, the post-command check, and evidence
  advancement each checked cleanliness themselves, and each shell fingerprint resolved
  HEAD again. `stage run` now checks cleanliness once before the command and once
  after it, and the post-command result is used for everything that follows. Nothing
  checked before the command is carried past it, so a stage that changes the worktree
  still fails with its inputs marked unavailable. Fingerprints and stage results are
  unchanged. On a toy repository, `stage run lint` drops from 6 `git status` runs to 2
  and from 44 Git subprocesses to 34, and `stage run test` from 7 to 2 and from 49 to
  36.
- Verifying review evidence no longer rebuilds the same review manifests three times.
  Each verification rebuilt the original snapshot's manifest to check its coverage,
  rebuilt it again with the current snapshot's manifest to rebind the coverage, and
  every caller then rebuilt both a third time to obtain that coverage. Each rebuild
  runs a full `git diff`. One verification now builds each snapshot's manifest once
  and returns the rebound coverage to its caller. This also applies to `mergeback`,
  `gate`, `publish`, and the pre-push hook's check of each pushed tip. Every error
  raised for invalid evidence is unchanged, and so are attestations. On a toy
  repository, `mergeback` drops from 6 manifest builds and 7 diff bundles to 2 and 3,
  and from 89 Git subprocesses to 73.
- Runs on other branches no longer slow down every command that reads review history.
  Grounding history and evidence discovery fully loaded and validated every run record
  in the clone's store, including runs on other branches and aborted runs kept until
  `gc`, before checking the branch. They now read each record's branch (and, for
  discovery, its source worktree) first and fully load only runs that match. A record
  that cannot be read that way is still loaded and handled as before, so corrupt
  records are skipped exactly as they were. Grounding output and its digest are
  unchanged. With 3 runs on this branch and 6 on other branches, `context` drops from
  11 run-record loads to 5 and `start` from 9 to 3; other-branch runs no longer add any.
- `start` on a branch with earlier runs no longer slows down with every run it keeps.
  Choosing reusable evidence recomputed a stage's current fingerprint once per earlier
  run instead of once per stage, and verified every earlier run's evidence, rebuilding
  its review manifests from Git, even after a reusable candidate was already found. It
  now computes each stage's current fingerprint at most once and stops looking at
  older evidence for a stage once a newer candidate is reusable, because an older item
  can then never replace it. The chosen evidence is unchanged. On a toy repository,
  `start` with six earlier runs drops from 404 to 156 Git subprocesses and from 24
  fingerprint computations to 8, and stays there as runs accumulate.
- Commands no longer rebuild the same review inputs several times. Recording findings,
  fetching docs context, `status`, and `stage run` each computed the review and docs
  fingerprints more than once, and every computation rebuilt the diff bundle,
  reassembled grounding, and walked and hashed the documentation inventory. Each
  command now keeps these inputs in a memo that lives only as long as the command and
  is keyed on the worktree, its current HEAD, and the configuration that shapes them.
  `stage run` clears the memo after the stage command. Shell-stage fingerprints are
  still recomputed before and after the command. Grounding, its digest, stored
  fingerprints, review manifests, and attestations are unchanged. On a toy repository
  with 40 documents, recording review findings drops from 3 grounding assemblies and
  3 diff bundles to 1 of each, recording docs findings from 3 assemblies, 4 bundles,
  and 3 inventory walks to 1 of each, and `status` and `stage run` from 2 assemblies
  to 1. Git subprocesses for recording docs findings drop from 35 to 23.
- Recording a passed stage no longer stalls for minutes on large changes. Documentation
  grounding searched every document once per changed-path term, and each search scanned
  the whole document because the whole-token regex could not use a literal-prefix search.
  It now tokenizes each document once and runs the regex only for terms whose word runs
  all appear as complete tokens. Grounding output and its digest are unchanged. With
  1,680 terms over 293 documents (5.6 MB), one assembly drops from about 80 seconds to
  under half a second. A stage record assembles grounding up to four times.
- `context` no longer fails with `git diff omitted requested changed paths` on branches
  that move and delete similar files. Git's default rename detection ran separately on
  each batch of the per-file diff, so a batch that held a moved file's destination but
  not its source could pair the destination with an unrelated deletion and hide that
  deleted path. Every diff now runs with `--no-renames`, so a rename is reported as a
  deletion plus an addition and each changed path always has its own patch.
- Restore the five follow-up evidence ledger PR titles to their exact GitHub titles,
  state the README dogfooding claim as 323 of 408 merged pull requests, and note in the
  case study that evidence ledgers are never edited by hand after collection.
- A fetch, Git, or OS failure while synchronizing at `start` now records the run as
  `SYNC_CONFLICT`, appends a `sync_failed` event, and returns a `sync_failed` error
  (exit 4) with `abort --force` as the next command; its diagnostics redact URL
  credentials. Previously the run stayed in `SYNC_RUNNING` and every later `start`
  pointed back at `status`.
- A run left in `SYNC_RUNNING` by an interrupted `start` now reports `abort --force` as
  its next command from `status` and `start`.
- The setup command no longer escapes as an internal error on timeout. It runs through
  the stage runner, kills its whole process group on timeout (exit 124), and writes
  redacted output to `setup.txt` in the run log directory. A failed setup reports
  `timed_out`, `log_path`, and the output tail. A setup command that writes to a copied
  file fails with its output withheld, as a stage does.
- Diffs read for review and fix planning, and the commit patch used for patch identity,
  now pass `--no-ext-diff --no-textconv`, so a user's `diff.external` or textconv driver
  can no longer replace or break the real patch or change a commit's patch identity.
- The worktree cleanliness check now passes `--untracked-files=all
  --ignore-submodules=none`, so `status.showUntrackedFiles=no` can no longer hide
  untracked files that a later `git add -A` would sweep into a fix commit.
- A timed-out stage now returns within a few seconds of its timeout even when a
  descendant that escaped the process-group kill still holds its output pipe. The stage
  still exits 124 with the `[timed out after Ns]` marker; in that case its output is
  discarded and replaced by an
  `[output truncated: a process outside the stage's group kept its pipe open]` line.
- Sending SIGTERM or SIGINT to the CLI while a stage runs now kills the stage's process
  group instead of leaving it running (POSIX only).
- The copied-file mutation check no longer polls every copied file every millisecond
  for the whole stage; it compares file fingerprints, including change time, before and
  after the stage.
- `submit-findings --file` and `review compare --file` report a missing, unreadable,
  or non-UTF-8 file as `invalid_findings` (exit 3) instead of `internal_error`.
- `respond` refuses `--commit` unless `--action fixed`. The CLI exits 6 with
  `usage_error`; the Python API raises `invalid_response`. Previously a dismissal or
  acceptance stored the unverified string as a fix commit.
- The pre-push hook no longer blocks tag pushes, and no longer requires an attestation
  for an update whose new tip the remote tip already contains.
- `approval-check` reads approval policy from the `.agentic-preflight.toml` committed at
  `--base`, so a pull-request head can no longer choose its own approval mode.
- The root `--run` option is refused with `invalid_run_id` (exit 6) unless it matches
  `r_` followed by 10 lowercase hex digits, so a path-like value never reaches the store.
- `events` and `status` tolerate a torn final line in a run's event log. A malformed
  earlier line reports `run_record_unreadable` with reason `invalid_events`.
- `approval-check --reviews-file` reports a reviews file that is not UTF-8 JSON as
  `invalid_findings` (exit 3), like other JSON file inputs, instead of an internal
  error.
- The merge-back overlap check now passes `--ignore-submodules=none`, so
  `submodule.<name>.ignore` or `diff.ignoreSubmodules` can no longer hide a dirty
  submodule on a path the fix commits would overwrite.
- Appending to a run event log takes a per-run events lock, so concurrent appends can no
  longer interleave bytes within a line.
- A `--baseline` stage whose scratch worktree cannot be created now fails with
  `setup_failed` (scope `baseline`, `setup.kind` `worktree`) and keeps the run red with
  the baseline retry as its next command. Previously the unevaluated base was reported
  as passing.
- Attestation verification drops configuration keys that a later release removed (listed
  in `config.REMOVED_CONFIG_KEYS`, such as `[hook] enabled`) from an attested
  `config_snapshot` before validating it, so a verifier built from a newer base no
  longer rejects attestations written by an older CLI. Loading a configuration file that
  sets a removed individual key still fails with an unknown-key error; retired
  `[ci]` and `[reuse]` tables are ignored with warnings.
- A failed `init`, `start`, or `context` in the regression eval now records that case
  and snapshot as unresolved and the run continues, as documented. Previously it aborted
  the whole eval without a summary.

- Retired CI delegation migration preserves invalid saved run states for validation to reject, rather than concealing them as orphaned runs. Exit-code recovery guidance directs invalid approval review JSON back to the reviews file.

- Preserve ports and IPv6 brackets in credential-redacted remote diagnostics,
  including invalid port text, while removing userinfo, queries, and fragments.

- Bound trusted attestation verification to a five-minute job limit and approval
  policy evaluation to a thirty-minute job limit, so stalled setup, transport, or
  verification cannot leave the required checks pending for the default job duration.

- Reusable validation now keeps one runner per clone, shared by its main checkout and
  linked worktrees. The clone name and common Git directory hash identify the runner.
- Garbage collection automatically reclaims eligible unused reusable runners at both
  current and historical locations and reports their paths in `data.reclaimed_runners`.
  Branch-attached, locked, or dirty runners and runners associated with retained fixes
  are preserved. Existing source worktrees preserve their runner caches; unreadable
  records preserve the runner they name, and unreadable top-level records preserve all
  runners. Git removal failures preserve directories instead of deleting them directly.

- Synchronization no longer silently changes what a branch lands. A rebase drops merge
  commits and skips commits the base already has, so it could discard an `-s ours` merge
  that reverts a branch already in the base, or a revert of a change the base also made,
  and validate and publish different content from the branch. When a rebase would change
  the tree that a clean merge of the branch produces, `start` and `mergeback` now restore
  the branch and merge the base in instead, and `start` reports `data.sync.merged: true`.
  The merge always commits and skips commit hooks regardless of local merge
  configuration. If Git cannot compute the merge result for a reason other than unrelated
  histories, synchronization stops before changing the branch.
- `start` now validates a branch whose only change is hidden by criss-cross history
  instead of reporting that it has no changes.
- Synchronization now refuses, before changing anything, when a path added by the base or
  by the branch's merge result already exists as an untracked or ignored local file, and
  lists those files. Previously a rebase could silently overwrite an ignored file, for
  example one the base starts tracking, or one a directory rename moved a branch file onto.

- Loading a saved run that still carries retired CI delegation no longer raises `TypeError` on Python 3.11; the membership check compares against the state values instead of the enum class. This also clears the error reported by mypy 2.4.

- On Windows, Ctrl+C during a stage, setup, or reviewer command now kills its
  process tree before re-raising cancellation, preventing child processes from
  continuing after the CLI exits. POSIX signal handling is unchanged.

### Security

- The hosted approval check now classifies the paths that differ between the base and
  Git's merge of the pull-request head into it, in addition to the paths changed since the
  merge base. With criss-cross history, Git's single chosen merge base could hide a change
  the merge makes, such as an `-s ours` merge that reverts a high-risk side branch already
  in the base, so the pull request no longer required human approval.

- The hosted approval check now also classifies every path touched by a non-merge commit
  in `base..head`. A GitHub rebase merge replays those commits and drops merge commits, so
  a merge commit whose resolution cancelled an earlier high-risk commit hid that commit
  from both the branch diff and the merge result, yet a rebase merge landed it without
  human approval. A branch that changes a high-risk path and later reverts it now requires
  approval.
- Commit-path classification suppresses signature display even when `log.showSignature`
  is enabled, so signature diagnostics cannot obscure a high-risk path.

- Attestation verification now classifies the paths that differ between the attested base
  and Git's merge of the head into it when it checks a test skip and the review-executor
  policy. Previously, criss-cross history could hide a source change behind Git's single
  chosen merge base, so a test skip was accepted even though merging the head changed
  source.

## [0.6.0] - 2026-09-13

### Breaking changes

- Run records and attestation notes written by any earlier release are rejected with a
  clear error. No migration: finish or abort in-flight runs with the previous release,
  then start fresh.
- Removed config keys: `[reuse] attestation_schema`, `[ci] consumer_schema`,
  `[review] require_fix_commits`, and `[worktree] ttl_hours`.
- `[pr] automatedCleanup` was renamed to `automated_cleanup`.
- Docs submissions must be `{"findings": [...]}`.
- The minimum Git version is 2.38.
- Removed Python API: `install_integrations`, `uninstall_integrations`, and the
  `agentic_preflight.cli.command` re-export.
- Eval report `method_version` is now `public-smoke-v4`; `model_calls` was removed.
- Removed the unused `RunDoc.superseded_by` field.

### Removed

- **Breaking:** Remove the compatibility integration install and uninstall wrappers and
  the CLI module's `command` re-export; callers must use `manage_integrations` and import
  `command` from `cli_support` directly.
- **Breaking:** Remove the legacy no-op `[review] require_fix_commits` and
  `[worktree] ttl_hours` configuration fields; configurations that still set either
  field now fail validation as unknown keys.
- **Breaking:** Remove migration of the obsolete clone-wide active-run pointer; stray
  `current` files are ignored and left unchanged.
- **Breaking:** Remove the obsolete `model_calls` compatibility field from public smoke
  evaluation summaries and advance their method version to `public-smoke-v4`.
- **Breaking:** Remove support for installing the pre-push hook into a plain directory;
  hook installation now requires a Git repository.
- **Breaking:** Remove protected-base consumer negotiation, including source-marker
  probing and the `[reuse]` schema-selection and `[ci]` consumer-version settings.
- **Breaking:** Reject run records from earlier releases instead of migrating removed
  hosted pull-request lifecycle fields and states during loading.

### Changed

- Split the remaining complex orchestration and evidence-validation functions into
  bounded internal helpers without changing their command or persistence behavior.
- Split run startup into bounded internal phases and enable Ruff's McCabe complexity gate.
- Rename the review page to engineering notes and move dogfooding counts out
  of the README introduction.
- Retire transition design documents and update the documentation and bundled skill to
  describe only the current configuration, run-record, attestation, and submission formats.
- **Breaking:** Require saved run records and portable attestations to declare the exact
  current schema version as a JSON integer; missing, coerced, older, and future versions
  are rejected without recovery writes or inferred defaults.
- **Breaking:** Collapse attestations to required schema version 7 with an explicit
  `verified` or `tests_pending` outcome and complete refresh evidence; attestations from
  earlier releases are no longer readable.
- **Breaking:** Rename the `[pr] automatedCleanup` configuration key to
  `[pr] automated_cleanup`; the former spelling is now rejected as an unknown key.
- **Breaking:** Require all creation-time run-record fields and reject records that omit
  their source identity, configuration snapshot, creation timestamp, or intent source.
- **Breaking:** Require docs findings submissions to use the strict
  `{"findings": [...]}` object shape; bare lists, missing fields, and unknown fields are
  rejected.
- **Breaking:** Serialize every configuration section in snapshots, including the
  default `[ci]` policy, changing configuration digests for all new runs and attestations.
- **Breaking:** Require Git 2.38 or newer and remove the index-based fallback for
  computing merge trees.

### Fixed

- Record an initial setup failure as `SETUP_FAILED` instead of leaving the run in
  `SYNC_RUNNING`.
- Classify run records with configuration snapshots that no longer validate as
  unsupported saved data, preserving the records and their work for inspection.
- Allow changed lint inputs to invalidate completed lint through the state machine.
- Recover findings and their run-state transition together after an interrupted write,
  preventing duplicate findings when a partially installed submission is retried.
- Reconcile a completed merge-back after attestation or run-record persistence fails,
  without applying fix commits again. Changed source content or validation inputs
  require a fresh run.
- Preserve completed source rebases and recorded conflict-retry context across
  merge-back interruptions; different human resolutions still require renewed review.

### Added

- Run-record invariant checks on every write.
- Failure-injection coverage for local record installation, process interruption,
  merge-back and successful remote publication before local state persistence.
- A maintainer guide separating state-machine ordering guarantees from evidence
  checks, storage commits and Git recovery responsibilities.

## [0.5.3] - 2026-09-08

### Added

- Opt-in trusted GitHub Actions test authority, with separate publication and merge
  predicates, schema-6 pending delegation, protected integration-test workflow
  templates, and live attempt-specific CI retrieval. Local tests remain the default.
- Automatic per-stage evidence refresh after equivalent-content restacks, with
  original execution provenance in v5 attestations. Review/docs compare content
  and context; shell reuse requires committed input contracts. Producers preserve
  v4 compatibility until the protected base supports the new consumer.
- Snapshot-bound repository grounding for review and docs context, including
  configuration, documentation, CODEOWNERS, and branch-scoped review history.
- Independent review comparison with persisted executor submissions, agreement
  reports, and worked Codex and Claude reviewer wrappers.
- A public synthetic regression evaluation with isolated vulnerable/fixed snapshots,
  a deterministic dry mode, case selection, and a CI smoke run.

### Changed

- Pin the worked Codex and Claude reviewer configurations to explicit model and effort
  defaults, strengthen their untrusted-input and complete-coverage contracts, and document
  a reproducible model-and-effort comparison procedure without making unevaluated quality
  or cost claims.
- Explain why managed integration bundles can become outdated and how to refresh reviewed,
  unmodified copies without overwriting local customizations.
- Pull-request merge monitoring and automatic post-merge cleanup are now disabled by
  default. Set `[pr] automatedCleanup = true` to opt in; automatic pull-request creation
  and hosted-check monitoring remain unchanged.
- Routine pull-request and main-branch CI runs use Linux with Python 3.13. Manual
  and release runs retain all nine supported OS/Python combinations; scheduled
  regression covers macOS 15 with Python 3.11 every Monday and Thursday.
- Refresh the README animation against the current CLI, show a readable gate
  summary, and document animation verification and lockfile updates before release.

### Fixed

- Preserve applicable standing user authorization through the gate, status, and recovery
  guidance. Matching PR-feedback fixes can proceed without a redundant confirmation, while
  changes to the remote, branch, merge, history, or requested scope still require separate
  approval.
- Report command-review wrapper launches explicitly and update the public smoke evaluation
  to method `public-smoke-v3`. Real-mode reports no longer present wrapper invocations as
  provider requests: provider request, token, and cost telemetry remain unavailable unless
  measured independently, while dry mode records their known zero values.
- Correct release and upgrade guidance, the pinned CI verifier example, shell-execution
  recovery, Windows copy permissions, and documentation/test-skip contracts.
- Keep review comparison available in the CI-delegated and publication-ready states
  without changing pending test evidence.
- Publish and retain original Git commits referenced by refreshed attestations, and
  retrieve them in fresh hosted consumers. Preserve local shell-command overrides
  in v5 evidence while enforcing protected lint commands for CI delegation.
- Guard publication snapshots and reject replacements of retained evidence refs,
  including when branch force-pushes are allowed.
- Classify attestation failures and add an explicit trusted hosted helper that retries
  only missing remote notes, pins the candidate and notes snapshot, and reports safe
  attempt diagnostics. Both hosted attestation and approval checks use the helper
  from the protected base, display failure JSON, and preserve the failing exit status.
- Retain unreadable shared run records during garbage collection, report classified
  diagnostics, and preserve ownership in status and start recovery. Unreadable records
  remain protected even under forced collection.
- Recover interrupted shell stages without retaining stale command, log, or output
  hashes. Refuse to start over an in-progress Git operation and honor Git's effective
  hook path.
- Validate configuration constraints on direct model construction as well as TOML
  loading, including Windows absolute paths and traversal patterns.
- Public smoke evaluation method v2 isolates each selected snapshot in a neutral Git
  repository, rejects case IDs in reviewer context, and excludes inherited scripted
  answers from real provider processes. Reviewer subprocesses use the active Python
  interpreter.
- Executable examples, MDX components, Jenkinsfiles, and unknown file types no longer
  inherit automatic test skips from documentation or CI directory globs.
- CODEOWNERS grounding honors ownerless overrides and directory-aware matching.
  Grounding limits per-source size, total bytes, and source count, and reports omitted
  sources in snapshot-bound context metadata.

## [0.5.2] - 2026-09-02

### Changed

- `[pr] automatedCleanup` now controls the post-merge cleanup lifecycle and defaults to
  `true`. Automatic pull-request creation remains enabled when it is `false`, but the
  agent stops after hosted checks until the user explicitly requests cleanup. When it is
  enabled, the agent polls the exact PR every 5 minutes and automatically performs the
  pre-disclosed, run-scoped cleanup as soon as GitHub verifies the merge. Closed-unmerged
  PRs and changed cleanup targets are preserved instead of being deleted. Polls select
  the recorded full PR URL, and source branches are deleted only while the PR, local
  branch, and remote branch still reference the disclosed gated head commit.

### Fixed

- Non-blocking review and documentation findings can now be dispositioned after their
  stage turns green. `accepted` and `dismissed` notes remain in the run record without
  invalidating green evidence, while a registered fix commit reopens review so every
  later stage describes the repaired snapshot.
- Removed stale version pins from the Quickstart and installation guide so the
  published-package command installs the current release instead of `0.4.0`.

## [0.5.1] - 2026-08-30

### Added

- Worktree-scoped active-run ownership. Multiple agents can now run independent gates
  concurrently from linked worktrees in one clone. `status --all` inventories the clone,
  and the global `--run RUN_ID` selector provides explicit recovery without making the
  caller's unrelated checkout the mutation target.

### Changed

- Repeating `start` with matching inputs now resumes the active worktree run. A moved
  source head is preserved as `ORPHANED` and replaced automatically; a different intent
  on an unchanged head requires `start --replace`. Terminal, dangling, and abandoned
  ownership pointers no longer block later work, and elapsed time is never used as proof
  of abandonment.
- Clone-wide synchronization is limited to the resources that are actually shared: base
  synchronization, the single reusable runner, and Git-note publication. Per-run commands
  and source-worktree ownership no longer serialize unrelated PR preparation.

## [0.5.0] - 2026-08-27

### Added

- Native Windows support. Windows 10 or newer joins macOS and Linux as a supported
  platform, is covered by the pull-request CI matrix, and installs with the new
  `install.ps1` / `uninstall.ps1` scripts. WSL is no longer required.

- `install.ps1` and `uninstall.ps1`, PowerShell counterparts to the bash installers
  with identical behaviour and ordering, including the deliberate pause before
  uninstalling so repository state can be cleaned up while the skill still exists.

### Changed

- Stage, review, and setup commands are now executed directly as a program and its
  arguments when they contain no shell grammar. A shell is used only for commands
  that need one: pipes, `&&`, redirection, globs, expansions, variable assignments,
  or a program that does not resolve. This removes the hard dependency on a POSIX
  shell for the common case, and takes the shell out of the injection surface of the
  one code path that runs repository-controlled strings.

  **A consequence worth reading before upgrading.** A directly executed command does
  not source your login shell profile, where `bash -lc` did. Where a version manager
  — `nvm`, `pyenv`, `rbenv`, `mise`, `asdf` — puts its shims on `PATH` from that
  profile, a stage can now run a *different build of the same program*. Which case
  you are in depends on whether the program resolves without the profile:

  - **Not on `PATH` without it.** Resolution fails, the command falls back to a
    shell, the profile is sourced, and nothing changes.
  - **On `PATH` without it, and the same program.** Nothing changes.
  - **On `PATH` without it, but the profile would have selected a different one.**
    The system build now runs instead of the managed one, and says nothing about it.
    A suite can go green or red against an interpreter version you did not intend.

  Only the third case is a behaviour change, and it is silent. If a stage depends on
  a version manager, select the interpreter in the command itself — `uv run pytest`,
  `mise exec -- pytest`, or an absolute path — rather than relying on the profile.
  That also makes the stage behave the same way in CI, where no profile is sourced
  either.

  Program *resolution* is likewise now PATH-only: a bare command name is never
  looked up in a working directory, so a repository cannot supply the tool that
  validates it. This closes a Windows-specific hole, where `shutil.which` searches
  the calling process's current directory — the repository under validation — before
  `PATH`.

  On Windows, the shell fallback is the one Git for Windows installs, located through
  the Git installation rather than `PATH`, because `bash.exe` on `PATH` is normally
  the WSL launcher and would run stages against a different filesystem.

- All file and subprocess text is now read and written as UTF-8 explicitly rather
  than in the platform's default encoding, and generated files use Unix line endings.
  Under the Windows default of `cp1252`, a non-ASCII path or review finding could
  previously corrupt git output or raise `UnicodeEncodeError`.

### Fixed

- Restored attestation reuse on Git 2.30 through 2.37. Those releases predate
  `merge-tree --write-tree` and report the unknown flag on stderr *while exiting
  zero*, so the fallback written for them was never reached: `merge_tree` returned
  "no clean merge" for every comparison, and `start` silently reopened review instead
  of reusing a still-valid green attestation after a base synchronization. The
  interface is now chosen from the reported Git version rather than by recognising an
  error message, which also stops the detection breaking under a non-English locale
  where Git's messages are translated.

- Copied `[worktree] copy_files` entries are now restricted to their owner on Windows
  using an ACL, as `os.chmod` there does not affect permissions. A copy that cannot be
  restricted is deleted and refused rather than left readable, so the guarantee that
  makes copying a local `.env` acceptable is never silently unmet.

- Replacing a run document no longer fails when another process briefly holds it open,
  which POSIX `rename` permits but Windows does not. The replace is retried with
  backoff and still raises if the file stays held, so a lost write cannot pass as a
  recorded state transition.

- Git output is now decoded with `backslashreplace`, so one changed file in a legacy
  encoding renders as visible `\xe9`-style escapes in the diff instead of failing the
  whole run with `UnicodeDecodeError`. Patch identities are exempt from decoding:
  the patch travels between `git show` and `git patch-id` as raw bytes, so two
  byte-distinct changes cannot share an identity through escape-text folding.

- Paths changed by a single commit are read NUL-delimited, so a non-ASCII filename is
  reported as itself rather than C-quoted under git's default `core.quotePath` — a
  form that names a file that exists nowhere.

- The git executable itself is resolved on PATH once per process. Handed a bare name,
  Windows' `CreateProcess` searches the parent's current directory first — the
  repository under validation — so a repo-committed `git.exe` could previously have
  become the git that validated the repository shipping it. A git missing from PATH
  entirely now raises an explicit `FileNotFoundError` rather than falling back to the
  bare name, which would have reopened the same search.

## [0.4.0] - 2026-08-13

### Changed

- Batched per-file review diff collection into bounded Git invocations instead of
  launching one process for every retained file. Review bundles keep the same exact
  per-file patches and handle renames, binary changes, file-type changes, non-ASCII
  names, and literal pathspec characters.

- Removed automatic runtime-manager discovery and command wrapping. Setup, review,
  lint, and test commands now run directly in the configured environment, and the
  `[runtime]` configuration section is no longer supported.

- Replaced Node-specific automatic npm/pnpm installation and reusable dependency
  fingerprinting with the language-neutral `[worktree] setup_command`. Setup now fails
  explicitly on a nonzero exit, including in baseline scratch worktrees, where an
  installation failure can no longer be mistaken for a pre-existing red base. The run
  persists setup failure details so `status` returns either the required abort or the
  exact baseline-aware stage retry instead of a dead-end state or nonexistent log.

- Simplified the review and documentation findings sub-machines. Clean and
  blocking submissions now transition directly to each stage's green or blocked
  state, and response handling remains in that single blocked state until every
  blocking finding is resolved. The transient `*_SUBMITTED` states and the
  behaviorally identical `*_AWAITING_RESPONSES`/`*_FIXING` pairs were removed.

- Moved the skill's ten failure playbooks out of `SKILL.md` into
  `reference/playbooks.md`, leaving a symptom-to-playbook index in their place.
  The playbooks are recovery detail an agent needs only after a run stops, and they
  were being loaded into context on every invocation instead. The universal
  `exit 3 → status → next` rule and the merge-back non-negotiable stay in the skill
  body, and a test keeps the index covering every playbook.

- Corrected the README's descriptions of explicit stage skips, observable gaps,
  hosted merge-approval enforcement, and manual gate boundaries. Expanded the
  `no-mistakes` comparison against v1.48.0 to cover review completeness, portable
  evidence, deterministic risk, publication approval, and local architecture.

## [0.3.0] - 2026-08-12

### Added

- Supply-chain maintenance and verification: Dependabot updates Python and GitHub
  Actions dependencies, locked runtime dependencies are audited for published
  vulnerabilities, CodeQL performs Python static analysis, and release artifacts ship
  with a CycloneDX SBOM plus signed build-provenance and SBOM attestations.
- A compatibility policy and Monday/Thursday regression coverage for the oldest
  supported boundary, macOS 15 with Python 3.11. Pull-request CI remains the fast
  Ubuntu/Python 3.13 combination, while manual and release runs retain the broad
  six-way matrix.

- Configurable, attestable review independence. `[review] executor = "command"` runs an
  external reviewer over the same complete bundle returned by `context`, while
  `require_command_for` can require that executor for selected risk levels. Command
  failures have bounded, restart-safe retries, and attestation schema v3 records the
  executor plus redacted command evidence.

- Snapshot-bound review coverage. `context` inventories every changed hunk plus
  non-textual file changes; review submissions must assert examination of that exact
  manifest, findings cite units, and code derives a complete cited/clean receipt.
  Any repair commit invalidates coverage and reopens review before validation continues.

- Conservative rebase tolerance for in-place runs. `start` preserves a prior green
  attestation only when the rewritten commit has an identical complete tree, effective
  configuration, and clean Git merge result against the freshly synchronized base.
- Managed Agent Skill integrations for Cursor, opencode, and Amp, alongside Codex and
  Claude Code.

- An idempotent `install.sh` for installing or updating the CLI and all managed agent
  skill copies directly from a source checkout.
- A conservative `uninstall.sh` that pauses for per-repository
  `agentic-preflight:uninstall` cleanup before removing managed user-scoped skills and
  the CLI. Project cleanup removes configuration and managed hook logic while preserving
  unrelated hooks, run history, and attestations.
- `[pr] mode = "auto" | "manual"`, defaulting to automatic pull-request creation after
  the user approves the push gate. Manual PR mode leaves creation to the user and reports
  a compare URL instead.
- `[approval] mode = "manual_merge" | "environment" | "peer_review"` for high-risk
  pull requests. Manual merge is the default, Environment mode uses a configurable
  GitHub Environment, and peer review retains the exact-head eligible-reviewer policy.
  The trusted check rejects GitHub auto-merge while manual-merge mode is active.
- Deterministic path-based risk classification with separate low, medium, and high
  levels. High risk invokes the configured merge-approval mode, while publication still
  uses the normal confirmation-token gate. Risk remains separate from the diff byte
  budget.
- A pull-request attestation job that runs the verifier from the protected base commit
  and fetches attestations from same-repository branches or public forks. Governance
  surfaces now have a checked-in CODEOWNERS policy.
- A trusted high-risk approval check that dispatches manual-merge, Environment, and peer
  review modes. Peer review rejects bot, unaffiliated, self, stale-head, dismissed, and
  superseded approvals.
- Green records are now portable Git-note attestations in
  `refs/notes/agentic-preflight`. Lint and test evidence includes the exact command,
  exit code, and SHA-256 of redacted captured output, and the normal push path sends
  the branch and attestation ref atomically.
- `agentic-preflight verify <sha>` provides a fail-closed CI check for the note's
  schema, exact commit/tree binding, complete stages, and shell-stage evidence.

### Changed

- CI and release workflows now use `actions/checkout` 7.0.1,
  `actions/upload-artifact` 7.0.1, `actions/download-artifact` 8.0.1, and
  `pypa/gh-action-pypi-publish` 1.14.2. Development dependency floors now require
  Hypothesis 6.165.2 and Ruff 0.16.2, with the lockfile updated accordingly.

- Dependabot version updates are grouped into one weekly Python dependency pull
  request and one weekly GitHub Actions pull request, reducing preflight review and
  attestation overhead without exempting bot-authored commits from the gate. The
  attestation and CI guide documents how to attest the exact bot-authored commit and
  re-run its hosted checks.

- The README now matches the 0.3.0 release surface, uses release-stable links when
  rendered on PyPI, and keeps only the quickstart and core safety contract. Detailed
  hook behavior, portable attestations and CI, configuration, and extended limitations
  live in focused linked guides that are included in the source distribution.
- An explicit request to push, publish, or create/open a pull request now authorizes the
  matching post-verification push. The gate still displays the exact remote, branch,
  commits, and risk summary, but no longer forces a duplicate confirmation unless that
  summary differs materially from what the user authorized.
- Code-owned mechanical findings now block regardless of reviewer severity policy, so
  `[docs] require_changelog = true` cannot be silently weakened by excluding `high`
  from `[docs] blocking_severities`.
- Local validation now runs review → docs → lint → test, leaving the potentially
  expensive test command until all review, documentation, and mechanical lint repairs
  are committed. A lint-only repair therefore cannot force an otherwise unnecessary
  second test run; committed test repairs still invalidate docs and lint before retry.
- Pull requests and pushes to `main` now run one test combination, `ubuntu-latest` on
  Python 3.13, rather than the full six-way matrix. The matrix of Ubuntu and macOS
  against Python 3.11, 3.12, and 3.13 still runs on release tags, where it now gates
  the PyPI upload, and on demand by running the CI workflow manually from the Actions
  tab. Supported platforms and Python versions are unchanged; only the point at which
  every combination is exercised has moved.
- The test matrix now lives in a single reusable workflow that both the CI and release
  workflows call, rather than being copied into each. Contributors will see the test
  checks reported under a nested name, `test / test (ubuntu-latest, py3.13)`.
- The packaged description now states what the tool does rather than what it omits. The
  previous summary led with "Calls no LLM, ever", which is true of this package and
  misleading about a run, in which every judgment is produced by a model. PyPI renders
  that field as the project summary with none of the surrounding explanation, so it has
  to stand on its own. The README intro keeps the underlying point in the form that
  survives being read alone: no API key and no second model.
- The README now leads with what the tool does and defers the detail to linked pages.
  Reference material that a first-time reader does not need moved into `docs/`:
  `installation.md`, `change-scope.md`, `worktree-modes.md`, `configuration.md`, and
  `limits.md`. The prior-art comparison moved below the technical sections, since it
  answers a question a reader has not yet asked. No documented behaviour changed, and
  the complete configuration example stays in `docs/configuration.md`, where the test
  suite requires every config section to appear.
- The `[diff] exclude` example now says that setting the key replaces the eight built-in
  globs rather than extending them. The example lists four of the eight, so copying it
  verbatim silently dropped `**/*.min.css`, `**/__snapshots__/**`, `**/*.pb.go`, and
  `**/*_pb2.py`. The full default list is now in `docs/configuration.md`.
- The README now opens with a recorded demonstration of a run: a push blocked by the
  pre-push hook, a review that catches an unguarded division by zero, the fix verified,
  and the gate stopping to ask before pushing. The recording is real CLI output, and the
  VHS tape that produces it is committed as `docs/demo.tape`, alongside
  `docs/demo-fixture.sh`, which builds the throwaway repository the tape records
  against. `./docs/demo-fixture.sh && vhs docs/demo.tape` regenerates the recording from
  nothing, so the demonstration of a tool that verifies claims can itself be verified
  rather than taken on trust.

### Removed

- The `ap` console-script alias. The supported executable is `agentic-preflight`.
- Built-in GitHub pull-request creation, CI monitoring, and destructive post-merge
  cleanup. The skill now hands these host-level tasks to `gh`; the core retains the
  quality gate, atomic branch-and-attestation push, `finish`, and `gc`.
- The `pr`, `ci`, and `cleanup` commands and their `[publish]` and `[ci]`
  configuration sections.
- `docs/plans/` and the design document it held. It described the architecture as it was
  being decided rather than as it now is, and the parts still worth reading have since
  been said in the README and the `docs/` pages. It remains in git history.

### Fixed

- Cross-stage lint/test repair cycles preserve each stage's failed-attempt counter, so
  alternating repairs cannot reset and evade the configured `max_attempts` stop.
- Attestation reuse now names both mandatory terminal outcomes: lint must be green and
  test must be green or explicitly skipped.

- Synchronizing attestations now preserves locally-ahead and disjoint remote note
  histories. A valid local attestation no longer makes the next preflight start fail
  with a non-fast-forward fetch; conflicting notes for the same commit still fail loud.
- Manual CI runs are no longer cancelled by other CI activity. Every run on a ref
  shared one concurrency group with `cancel-in-progress`, so a manual run of the full
  matrix could be superseded by a push to `main` or by a second manual run. That is
  the run the release process uses to check macOS and Python 3.11 and 3.12 before a
  tag exists, and a cancelled run reports neither pass nor fail. Manual runs now get
  a group of their own; pushes and pull requests still supersede older runs on the
  same ref.

## [0.2.1] - 2026-07-28

First release published to PyPI.

### Added

- Tag-triggered release workflow that publishes to PyPI using Trusted Publishing, so
  no long-lived API token is stored in the repository or in GitHub secrets. The upload
  is bound to a `pypi` GitHub Environment and waits for reviewer approval, and the
  build refuses to proceed when the pushed tag does not match the project version.
- `docs/RELEASING.md`, covering the one-time PyPI publisher registration, the GitHub
  environment setup, and the per-release checklist.
- Community health files: `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`, issue
  templates, and a pull request template.
- Inline type information: the package now ships `py.typed`, so type checkers read its
  annotations directly.
- A coverage badge produced and published by CI without a third-party service, and a
  prior art section in the README.

### Changed

- Documentation-only and CI-configuration-only changes now record the software test
  stage as explicitly skipped instead of requiring a test command to run.
- The skill now instructs a `git pull --ff-only` on the base branch after a confirmed
  cleanup, so the source checkout matches the merged result.
- Attribution moved from the top of the README to a Credits section beside the license,
  and the package author is now the GitHub handle rather than a legal name.

### Fixed

- Skipped GitHub checks are no longer treated as failures. A pull request whose
  workflow correctly skips a job was previously reported as a CI failure.
- The source distribution is now built from an explicit allowlist. It previously
  included everything not matched by the root `.gitignore`, which made the published
  artifact depend on the contents of the local working tree and could fail the build
  outright when a stray virtual environment was present.

## [0.2.0] - 2026-07-23

First tagged pre-release.

### Added

- Deterministic review, test, documentation, lint, merge-back, and push workflow.
- Advisory pre-push hook keyed to the exact verified commit.
- Manual gate mode for workflows where only a person may perform the final push.
- Codex and Claude Code skill installation.
- In-place, reusable, and strict validation checkout modes.
- GitHub pull request publishing and CI monitoring through the `gh` CLI.
- Explicit CI coverage for Python 3.11 through 3.13 on macOS and Linux.

### Changed

- Renamed every pre-release product surface from `agentic-cli` to
  `agentic-preflight`, including the Python package, executable, skill, configuration,
  state directories, and internal validation branch prefix.
- Reframed the product as an advisory guard against accidental unverified pushes.
- Added a 60-second README walkthrough with command output captured from a local demo.

### Supported platforms

- macOS
- Linux

Windows is not supported because the implementation requires `fcntl`, Bash, and POSIX
process groups.

[0.6.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.6.0
[0.5.3]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.5.3
[0.5.2]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.5.2
[0.5.1]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.5.1
[0.5.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.5.0
[0.4.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.4.0
[0.3.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.3.0
[0.2.1]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.1
[0.2.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.0
