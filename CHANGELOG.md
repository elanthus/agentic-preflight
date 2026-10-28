# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
