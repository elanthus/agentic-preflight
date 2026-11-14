# Configuration

This page is the canonical configuration reference.
`agentic-preflight init` writes a starting file with comments.
The example below includes every supported section.

`.agentic-preflight.toml` is in the repository root. Commit it.
The CLI combines it with `~/.config/agentic-preflight/config.toml` one section at a time.
A section in the repository file replaces the whole matching user section.
`[telemetry]` is the exception. A repository section can only turn telemetry off. See [Local run telemetry](#local-run-telemetry-telemetry).
Keys are not merged across the two files.
A section that only the user file sets still applies.
The published attestation note leaves out such a section, except `[diff]`. See [Portable attestations](attestations-and-ci.md).
A run that an earlier release started must be restarted before it can publish a note.
Unknown keys are errors that name the key. They are not ignored.
The retired `[ci]` and `[reuse]` sections are the exception. File loading ignores them with a warning.
Removed individual keys, including `[hook] enabled`, still fail file loading. Remove them from both files.
`telemetry rebuild`, `telemetry sync-hosted`, and `report` are the exception. They only read derived data, so they ignore such a key with a warning that names the file and the key.
Historical run and attestation snapshots tolerate known removed keys. Their original digest bindings do not change.
See the [0.7 upgrade guide](upgrade-0.7.md).

The configuration models enforce the same mode, severity, risk-level, approval-environment, and constrained path rules for file loading and for stored snapshots.
Python callers can use `Config(...)` or `Config.model_validate(...)` without a separate validation step.
A file-loading error identifies the affected section and key and the file that supplied it, after section-wise layering.

Runs use the layered configuration, but attestation verification does not.
`verify`, `approval-check`, `push`, and the pre-push hook read the `.agentic-preflight.toml` committed at the protected base.
They ignore the user file and the branch's own edits.
A base commit with no such file uses the defaults.
Thus, the base policy decides what verification and the hosted approval check require, whatever the user file or the branch sets.
A relaxed setting fails verification only when the run's evidence falls short of the base policy.
For example, verification rejects a note whose run skipped docs that the base enables, ran a lint, test, or review command other than the one the base configures, or excluded review files that the base does not exclude.
Verification compares those commands as strings. A branch can still edit a script that a command runs from the repository.
For the review and planner commands, such an edit makes the change high risk; see [Review independence](#review-independence-review).
See [what verification trusts](limits.md#attestations-are-not-signatures).

> **Warning: the configuration executes committed code.**
> During a run, `[worktree] setup_command` runs at `start`.
> Every `[commands]` entry runs at its lint or test stage.
> `[review] command` runs when command review is in effect, that is, when `executor = "command"` or when a risk level in `require_command_for` applies.
> When contract checks are enabled, `[review.contracts] planner_command` runs on the host before command review.
> Each command can come from this committed file or from the user configuration.
> The repository file overrides the user file section by section.
> Thus, the repository under validation decides what runs with your privileges.
> A preflight run on a freshly cloned repository has the same trust as that repository's build or test suite.
> Run it only on repositories whose code you would build.

## Complete example

```toml
[general]
base_ref = "main"

[commands]
lint = "ruff check ."
test = "pytest"

[stage]
timeout_seconds = 600
max_attempts = 5
max_restarts = 5


[review]
blocking_severities = ["critical", "high"]
max_findings = 50
executor = "in_harness"       # default; or "command"
# command = "reviewer --json" # receives review context JSON on stdin
require_command_for = []      # e.g. ["high"] to require independence by risk
trusted_paths = []            # e.g. ["tools/reviewer/**"]; changes need human review

[review.contracts]
enabled = false              # opt-in executable review evidence
# planner_command = "python3 -S -B docs/examples/reviewers/claude_review.py"
# image = "registry.example/runtime@sha256:<64 lowercase hex characters>"
max_checks = 8               # 1..32, three probes per check
timeout_seconds = 10         # 1..60 per probe
total_timeout_seconds = 60   # 1..300 for export and execution
max_output_bytes = 16384     # 256..65536, combined stdout/stderr per probe

[policy]
# The default is []. `init` seeds these ownership-sensitive paths, which are
# high-risk and require human merge approval.
human_review_paths = [
  ".agentic-preflight.toml",
  ".github/workflows/**",
  ".github/CODEOWNERS",
  "CODEOWNERS",
  "docs/CODEOWNERS",
  # Command reviewer wrappers decide what a review reports.
  "docs/examples/reviewers/**",
]
# Every high-risk result requires human merge approval; medium risk does not.
high_risk_paths = ["db/migrations/**", "infra/**"]
medium_risk_paths = ["dependencies/**"]

[docs]
enabled = true
paths = []
require_changelog = false
blocking_severities = ["critical", "high"]

[context]
enabled = true
max_bytes = 24000
entry_max_bytes = 4000
extra_paths = []

[diff]
max_bytes = 200000
# Setting exclude REPLACES the eight built-in globs rather than adding to them.
# Omit the key to keep the built-ins; the full list appears below.
exclude = ["*.lock", "*-lock.json", "vendor/**", "**/*.min.js"]

[worktree]
mode = "in_place"                 # default; or "reusable" / "strict"
root = "/optional/external/path"  # isolated modes only; defaults to the user cache, one per clone
copy_files = [".env"]             # must already be ignored
# setup_command = "uv sync"       # prepare dependencies and ignored build inputs

[gate]
mode = "token"                    # or "manual"

[pr]
mode = "auto"                     # or "manual"
automated_cleanup = false          # true enables merge polling and cleanup

[approval]
mode = "manual_merge"             # or "environment" / "peer_review" / "owner_review"
environment = "high-risk-review"  # used by environment mode
reviewer = ""                     # required GitHub login for owner_review

[hook]
allow_force_push = false

# Set [telemetry] in the user file. A repository section can only turn it off.
[telemetry]
mirror = true                     # append each finished run's summary to runs.jsonl
hook_log = true                   # append each pre-push hook decision to hook-decisions.jsonl
# path = "~/agentic-preflight-telemetry"  # directory override; absolute or ~; also opts in repositories without a network remote

[retention]
run_days = 90                     # gc deletes reclaimed run records older than this; 0 keeps them all
```

## Configuration is snapshotted per run

`start` snapshots the resolved configuration when it creates a run.
A later edit of `.agentic-preflight.toml` does not change that run.
The snapshot and its digest are recorded with the run events.
The snapshot does not contain `[telemetry]` or `[retention]`.

Commit configuration changes **before** you start the run that they must affect.
For the same reason, the file must be committed before `start`. Do not edit it during a run.

`agentic_preflight/fingerprints.py` also scopes that same snapshot per stage.
A review fingerprint hashes only `[general]`, `[review]`, `[policy]`, `[context]`, `[diff]`, and `[stage]`.
`[stage]` is included because the command review executor's timeout and retry bound live there.
A docs fingerprint hashes `[general]`, `[docs]`, `[diff]`, `[context]`, and `[policy]`.
Thus, a change to an unrelated section, for example `[commands]`, cannot by itself invalidate that stage's evidence.
See [`docs/fingerprint-contract.md`](fingerprint-contract.md) for automatic refresh.

## Local run telemetry (`[telemetry]`)

Set this section in the user file, `~/.config/agentic-preflight/config.toml`.
This section describes your machine, not the project, so it does not layer like the other sections.
A `[telemetry]` section in the repository file can only turn telemetry off:

- `mirror = false` or `hook_log = false` there turns that file off, whatever the user file sets.
- `mirror = true` or `hook_log = true` there keeps the user file's value. A repository cannot turn on a file that you turned off.
- `path` there is ignored, and commands that load the configuration print a warning that names the file. The pre-push hook prints nothing, so the output of `git push` does not change.

Thus, a cloned repository cannot choose where your machine writes telemetry, or opt its runs in to the telemetry directory.
`mirror` and `hook_log` accept only `true` and `false`, in either file. Any other value, such as `1` or `"yes"`, is an error, so it cannot turn a file on.
An invalid `mirror` or `hook_log` value or an unknown key in the repository section is still an error. A `path` there is ignored whatever its value.

When a run ends, the CLI writes `summary.json` in the run directory.
`mirror = true` is the default. The CLI then also appends the summary as one line to `runs.jsonl` in the telemetry directory.
`mirror = false` keeps each summary in its run directory only.
`hook_log = true` is the default. The pre-push hook then appends each decision as one line to `hook-decisions.jsonl` in the telemetry directory.
`hook_log = false` stops that log.
`path` sets the telemetry directory. The value must be an absolute path or start with `~`.
Without `path`, the directory is `$XDG_DATA_HOME/agentic-preflight/telemetry` when `XDG_DATA_HOME` is an absolute path.
Otherwise, it is `~/.local/share/agentic-preflight/telemetry`.
Without `path`, runs and hook decisions of repositories without a network remote are not written to the telemetry directory, wherever the repository lives.
This rule reduces fixture noise, but also excludes legitimate local-only work and repositories that push to a local or loopback remote. It classifies the remote, not whether the repository can push or contains real work.
A `path` is an opt-in: that directory also receives them, even when `path` names the default location.
See [Repositories without a network remote](local-telemetry.md#repositories-without-a-network-remote).

The CLI reads this section from the configuration files when a run ends. The pre-push hook reads it on each push.
Run snapshots do not contain it.
Thus, a telemetry setting does not change the configuration digest, stage evidence, or an attestation.
See [Local run telemetry](local-telemetry.md) for the recorded fields and the `telemetry rebuild` command.
See [Hook decisions](local-telemetry.md#hook-decisions) for the hook decision log.

The telemetry directory is created owner-only (mode `0700`) on POSIX.
On Windows it inherits the access list of the folder above it; see [Compatibility](../COMPATIBILITY.md).
An existing directory that you own is narrowed to that mode on the next write.
When `hook-decisions.jsonl` reaches 4 MiB, the next decision first renames it to `hook-decisions.jsonl.1`.
That replaces the older generation. `report` reads both files.

## Run record retention (`[retention]`)

Each run keeps a directory under `$GIT_COMMON_DIR/agentic-preflight/runs/`.
That directory holds its record, events, findings, stage logs, and `summary.json`.
`gc` deletes a run's directory when the run has been reclaimed and its record last changed more than `run_days` days ago.
A run is reclaimed when it is finished, has no unlanded fix commits, and its validation worktree is released.
Fix commits that `abort --force` discarded do not count as unlanded.
The default is 90. `run_days = 0` keeps every record.
A run whose command is still executing is left for the next `gc`.
The deleted run IDs are listed in `data.pruned`.

`report` and `telemetry rebuild` summarize run records, so a deleted run remains only as its `runs.jsonl` row.
A run of a repository without a network remote has no such row unless your user configuration sets `[telemetry] path`.
Set a larger `run_days`, or 0, if you need those summaries.

`gc` reads this section from the configuration files. Run snapshots do not contain it.
Unlike `[telemetry]`, a repository section may set `run_days` to any value, including a shorter window than your user file.
Configuration that cannot be read deletes nothing.
The run store directory is owner-only (mode `0700`) on POSIX.
On Windows it inherits the access list of the folder above it; see [Compatibility](../COMPATIBILITY.md).

## Contract checks

The nested `[review.contracts]` table configures contract checks.
`image` is optional.
When `image` is set, it must be an immutable `sha256:` image ID or repository digest.
An unavailable image or Docker engine yields unverified evidence.
There is no host-code fallback and no image pull.
The planning context lists the distributions installed in `image`, once for each run and image digest.
The listing states availability only.
`planner_command` is optional for locally submitted plans.
A model-backed planner can add a paid invocation and needs explicit authorization.
The planner is a normal host command, subject to `[stage] timeout_seconds`.
Only the probes run in the restricted container.
See [contract checks](review-contracts.md) for the protocol, resource limits, local artifacts, and non-gating evidence semantics.

A new run does not automatically reuse contract-enabled review results.
The plan and receipt bind the current head, manifest, and execution configuration.
New plans use schema 2 and require behavioral coverage and oracle provenance.
These records and the final-review handoff do not add model calls or change the configured execution limits.

## Retired shell evidence reuse

Review and documentation can reuse equivalent evidence automatically.
Lint and test results never transfer into another run, even when its tree is identical.
Each new run executes the applicable shell stages again.
The retired `[reuse]` section, including `[reuse.lint]` and `[reuse.test]`, is ignored with a warning.
Historical configuration snapshots stay readable with their original digests.

## Push and pull-request authorization

This section is the canonical statement of when an agent can push or open a pull request.
The skill and the other documents link here.

An agent can push only with user authorization.
Authorization comes from one of two sources:

- An explicit request in the current task to push, publish, or create or open a pull request.
  That request authorizes the matching push.
- The user's applicable standing instructions.
  For example, standing instructions can permit pushes of fixes for existing PR feedback.
  Then a request to address that feedback authorizes a push of those fixes to the PR's existing head branch.

A request only to implement or commit is not push authorization.
A generic "proceed" is not push authorization.

After `gate`, the agent shows the remote, branch, commits, and risk.
When that summary matches the authorization, the agent pushes without a second question.
When authorization is missing, the agent asks and waits for an answer.
The agent also asks when the summary differs from the authorized scope: another remote or branch, unexpected commits, or a different risk decision.
No authorization covers a force-push, a merge, a destructive action, or work beyond the requested scope.
Each of these needs its own approval.
The merge restrictions in [`[approval]`](#high-risk-merge-handling-approval) still apply.

## Pull-request publication (`[pr]`)

`mode = "auto"` is the default. It is standing authorization for pull-request creation.
After the authorized push and the run finish, the agent opens or reuses the pull request without a second approval prompt.

`mode = "manual"` keeps pull-request creation with the user.
The agent can still push. It never opens the pull request. It supplies a compare URL instead.

`automated_cleanup = false` is the default.
Automatic pull-request creation still works, but the agent stops after the hosted checks.
It does not poll the merge state or delete anything until the user explicitly requests cleanup.
Set the field to `true` for automatic cleanup.
The agent then discloses the exact cleanup scope.
It polls an automatically opened or reused pull request every 5 minutes.
It removes only the disclosed run-scoped targets after GitHub verifies the merge.

This setting is independent of `[gate] mode`.
With `[gate] mode = "token"`, the agent pushes after it has [authorization](#push-and-pull-request-authorization).
With `[gate] mode = "manual"`, the CLI refuses to push and hands the command to a person.

## High-risk merge handling (`[approval]`)

`mode = "manual_merge"` is the default.
The hosted approval check reports success for a high-risk pull request only while GitHub auto-merge is disabled.
It runs again when auto-merge is enabled or disabled.
The agent must never merge the pull request or enable auto-merge.
The user reviews and merges it manually.

`mode = "environment"` routes high-risk approval through the GitHub Environment that `environment` names.
The default name is `high-risk-review`.
Configure that Environment's required reviewers in the repository settings.
The final hosted check passes only after the Environment job is approved and completes.
The policy job fails closed if the Environment does not exist or has no required reviewer.
For a solo repository, select the owner as the required reviewer and leave **Prevent self-review** disabled.
GitHub plan restrictions can limit required reviewers for private repositories.

`mode = "peer_review"` keeps the original pull-request-review policy.
An eligible repository owner, member, or collaborator other than the author must approve the current head or an equivalent earlier head.
Only each reviewer's latest decisive review counts, whichever head they submitted it on.
A later changes-requested review or dismissal on any head revokes the approval, and a later approval of a head with different content replaces it.
Returning the branch to an approved commit does not restore a revoked approval.
A revoking review still counts when GitHub no longer reports the commit it was submitted on.
A comment does not revoke a peer approval.

`mode = "owner_review"` requires the human GitHub login in `reviewer` to review the current head or an equivalent earlier head.
They must be a repository owner, member, or collaborator.
On another author's pull request, they submit an **Approve** review.
On their own pull request, they submit a **Comment** review whose trimmed body is exactly `approved` in lowercase.
An ordinary PR conversation comment does not count.
As in `peer_review`, only the reviewer's latest decisive review counts: a dismissal or a later changes-requested review on any head revokes the approval, and a later approval of a head with different content replaces it.
An edit of a self-review to other text, or a later self-review Comment with other text on any head, also removes its approval.
On their own pull request, replying to a review thread creates a Comment review with an empty body, so a reply also removes the self-approval, even on another head.
Post replies before the final `approved` Comment review, or approve again afterwards.
On another author's pull request, the reviewer's Comment reviews and replies do not revoke an Approve review.
Bots and other reviewers cannot satisfy this mode.

The self-approval proves only that the reviewer's account posted it.
An agent that runs with the reviewer's `gh` login or token can post the `approved` Comment review itself.
Only the agent instructions keep it from doing so.
If the approval must come from a person the agent cannot impersonate, use `peer_review`, or use `environment` with **Prevent self-review** enabled.
See [Approval checks cannot tell a person from an agent](limits.md#approval-checks-cannot-tell-a-person-from-an-agent).

In both review modes, an approval of an earlier head carries to a new head only when the branch content is unchanged.
The trusted checker decides this from Git objects, not from attestation notes.
Both heads must change the same paths against the base, with the same `git patch-id --stable` identity.
The new head's tree must also equal Git's clean merge of the approved head with the base commit that the new head contains.
A merge of either head into the base must produce the same clean tree.
The non-merge commits since the base must have the same patch identities.
Thus, history tricks such as an `-s ours` merge cannot hide a revert or add commits that a rebase merge would replay.
A clean merge from the base or a clean rebase keeps the approval.
A content commit, a merge that resolves conflicts or adds content, or a base change beside the contribution needs a new review.
An approved head is not always an ancestor of the new head, for example after a rebase.
The verifier uses such a head directly when it already has it.
Otherwise, it is fetched only through its `refs/agentic-preflight/evidence/<sha>` ref.
If the head is absent and that ref is unavailable, the approval does not carry.
The result reports `approved_head` and `approval_carried`.

The policy checker reads trusted configuration from the protected base commit.
Thus, a pull request that changes `[approval]` is evaluated under the old base-branch mode.
The new mode applies to subsequent pull requests after the merge.

## The documentation surface (`[docs]`)

The docs stage inspects `README*`, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING*`, `CHANGELOG*`, and `docs/**`.
It also inspects agent instructions under `.claude/rules/**` and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`.
It lists only files committed at the head commit.
Files not committed at the head commit, including ignored, untracked, and uncommitted generated files, are not on the surface, even when they match a pattern.
A generated file that is committed counts like any other committed file.
A committed symlink is on the surface when its target is a file, even outside the repository. See [Committed symlinks on the documentation surface](limits.md#committed-symlinks-on-the-documentation-surface).

Use `[docs] paths` for repository-specific documentation surfaces.
The surface is an allowlist.
A docs finding against a path outside the surface is rejected.
The rejection is a statement about the allowlist, not a verdict on the finding.
Repositories often keep binding rules outside the default surface.
Add those rules here. Do not work around the rejection.

`require_changelog` requires the diff to touch an existing changelog on the docs surface.
The CLI records an untouched changelog as a code-owned finding.
That finding blocks regardless of `[docs] blocking_severities`.
The CLI does not create a missing changelog. It does not judge the content of the entry.
The reviewer checks that content.

## Grounded context ([context])

`context` retrieves deterministic repository knowledge that is related to the changed paths.
It returns that knowledge in `data.grounding`.
The default sources are matching files under `docs/**` and the root `AGENTS.md` and `CLAUDE.md` files.
They also include relevant findings from earlier local runs and the deterministic policy reasons for the change.
The documentation, the convention files, and this `[context]` section are read at the run's merge base, so a branch's change to `[context]` applies to later runs after it merges.
See [Grounded context](context-grounding.md) for the matching and ordering rules.

`enabled = false` returns an explicit empty grounding block.
`max_bytes` limits the sum of the included entry byte counts.
`entry_max_bytes` truncates document excerpts and convention contents on a line boundary.
`extra_paths` adds repository-specific rule files.
Entries can be exact repository-relative paths or globs. Globs use the same matching rules as policy paths.
Absolute paths and paths that contain `..` are rejected.

## Oversized diffs (`[diff]`)

Over `[diff] max_bytes`, `context` **refuses**. It does not truncate.
An agent that reviews half a diff and believes that it saw all of it produces a false green.

The envelope lists the size of each file, so the agent can narrow the diff with `[diff] exclude`.
Raise `max_bytes` only when the change really is that large.

**`exclude` replaces the defaults. It does not extend them.**
Omit the key to keep all eight built-in globs:

```toml
exclude = [
    "*.lock",
    "*-lock.json",
    "vendor/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/__snapshots__/**",
    "**/*.pb.go",
    "**/*_pb2.py",
]
```

A shorter list drops the globs that you leave out.
This includes the abbreviated list in the complete example above.
To add a project-specific pattern, copy this list and append to it.

## Stage execution (`[stage]`)

A stage command with shell grammar runs through a login shell, which sources your login profile first.
The command then changes back to the validation directory, so a profile that changes directory does not move the stage.

`timeout_seconds` bounds a single stage run. `max_attempts` bounds the retries.
When a stage is still red after `max_attempts`, the run stops and asks for human resolution.
It does not retry indefinitely.

When `timeout_seconds` expires, the stage's whole process group is killed and the stage exits 124.
The output captured before the timeout is kept, and it ends with a `[timed out after ...]` line.
A descendant process that left the process group cannot keep the stage open.

Each captured stream keeps at most 8 MiB.
Longer output keeps its first and last 4 MiB around an `[output truncated: N bytes omitted]` line.
The stage log and the envelope's head and tail come from that capped text.
On POSIX, a SIGTERM or SIGINT delivered to the CLI while a stage runs also kills that stage's process group.
On Windows, Ctrl+C kills the stage's process tree before cancellation propagates.

`max_restarts` bounds the whole run, not one stage.
It is new in 0.7.0. Version 0.6.0 rejects it as an unknown key.
Validation restarts when it returns to review after progress.
Examples are a committed lint or test repair and a repair that changes the reviewed snapshot.
Changed stage inputs and a merge-back resolution that differs from the verified tree are also restarts.
When the count reaches `max_restarts`, the restart is still recorded, so stale green evidence cannot survive.
Then the run stops with exit 4 and error code `max_restarts`.
From then on, only `status`, `logs`, `events`, and `abort` work.
The counter never resets within a run.
The limit comes from the run's configuration snapshot, so an edit of the file does not release a stopped run.
A person decides what happens next.
Usually, that is `agentic-preflight abort --force` and a fresh run after the cause of the loop is understood.

Treat a first green from a newly configured command as unproven.
Pass or fail is the exit code alone.
A command that does nothing and exits zero reads as a pass forever.
A false green retires the check instead of a retry.
Confirm that the run did work before you believe it: a test count, a results file, or a non-empty log.

## Finding thresholds (`[review]` and `[docs]`)

Each section's `blocking_severities` decides which reviewer-submitted findings must be resolved before the run can proceed.
`critical` and `high` block by default.
A finding whose action is `ask_user` blocks at any severity.
A choice on the user's behalf is the decision that was declined.
Code-owned findings also block at every severity.
They record mechanical requirements that the CLI established, not reviewer judgment.

`max_findings` caps the number of findings in a single submission.

## Review independence (`[review]`)

`executor = "in_harness"` is the default.
It keeps the normal agent-driven `context` → `submit-findings` workflow.
With `executor = "command"`, `agentic-preflight review run` sends the complete review context JSON to `command` on stdin.
The command must return one strict review submission on stdout.
The submission must include the current coverage manifest and `examined = "all"`.

`require_command_for` lists the risk levels (`low`, `medium`, `high`) that must use command review, even when the configured executor is `in_harness`.
Direct `submit-findings` is refused for those runs.
If command review is in effect but `command` is unset, the CLI stops with `data.mode = "needs_command"`.
It never guesses a reviewer.
Verification compares the attested review command only with a `command` committed at the protected base.
When the base requires command review but sets no `command`, a note from any review command passes, as lint and test commands do when the base sets none.
Commit `command` to control which reviewer satisfies the requirement.

Verification compares the command string, not the program that it runs.
`command` runs in the worktree that holds the change under review, so a script that it names in the repository is the branch's copy of that script.
Thus, risk classification treats the reviewer's own files as human-review paths, in addition to `[policy] human_review_paths`:

- Every repository-relative path that `command` or `[review.contracts] planner_command` names.
  A named file is protected together with every file under its directory, because interpreters load sibling modules from there.
  The example wrappers, for instance, import `_reviewer_common.py` from their own directory.
  A path at the repository root is protected as that file, or as that directory if it is one.
  Its sibling files are not protected, because that would cover the whole repository. List any module that a root-level reviewer imports in `trusted_paths`, or move the reviewer into its own directory.
- Every pattern in `trusted_paths`. Patterns are repository-relative globs with the same matching and restrictions as `[policy]`.

A change to any of these paths is high risk, so it goes through the configured `[approval] mode` before merge.
The hosted approval check and attestation verification use the `[review]` section committed at the protected base, so a branch cannot point the command elsewhere to escape the rule.
A local run classifies risk with the branch's own configuration. Its verdict is advisory.
When the base sets no `command`, for example because the command comes only from a user configuration file, the base names no reviewer paths. Protect that reviewer with `[policy] human_review_paths` committed at the base. A committed `[review]` section that sets only `trusted_paths` would replace the user's whole `[review]` section, including its `command`, so set `trusted_paths` there only together with the command.

Reading paths from a command is approximate:

- A word counts as a path when it contains `/` or `.`, after the value of a `--option=value` or `NAME=value` word is taken.
- Control operators (`;`, `&`, `|`, `(`, `)`, `<`, `>`) separate words only outside quotes, so a quoted path such as `'reviewers (local)/rev.py'` stays one path.
- The word after a shell's `-c` flag, as in `sh -c '...'` or `bash -lc '...'`, is read as a command too.
- A directory that the command changes to is protected as `DIR/**`: the argument of `cd` or `pushd`, and the value of `-C`, `--directory`, `--cwd`, `--prefix`, `--project`, or `--chdir` (also `--option=DIR` and `-CDIR`). Later relative paths are protected both as written and under `DIR`.
- Each directory in a `PYTHONPATH=` or `NODE_PATH=` value is protected as `DIR/**`. A root entry such as `.` is not, because that would cover the whole repository; list the root modules that the reviewer imports in `trusted_paths`.
- `-m a.b` protects the top-level package `a/**`, whose `__init__.py` runs first. `-m reviewer` protects `reviewer.py` and `reviewer/**`. A flag cluster such as `-Bm a.b` and the attached form `-ma.b` count too.
- A leading `$VAR/`, `${VAR}/`, `$(...)/`, or backtick expansion before a slash is dropped, and the literal path after it is kept.
- Bare program names, absolute paths, paths outside the repository, and other words with a shell expansion or a glob are skipped.

List in `trusted_paths` anything else that decides what the reviewer does: a prompt or configuration file in another directory, a path that the command computes, or a file that the program reads on its own.

The interpreter and runner environment is a reviewer input too, and command parsing does not find it.
`uv run`, `poetry run`, and `npm run` load or synchronize the project from its manifest and lockfile, which can run build hooks or path dependencies from the branch.
During startup, Python imports a `sitecustomize` module from any directory on `sys.path`, and an editable install puts the repository there.
Run a reviewer that needs only the standard library directly, with `python3 -S -B`, as the [example configurations](independent-review.md) do. `-S` skips that startup processing.
The installed `agentic-preflight reviewer run` runs the same reviewer from the installed package, not from the worktree.
Otherwise, protect the runner's inputs with `trusted_paths`.
The coding-agent CLI that a wrapper starts can also read instructions and settings from the worktree, such as `CLAUDE.md`, `AGENTS.md`, or `.claude/settings.json`.
The example wrappers turn off those that the CLI lets them. Codex cannot be told to skip a project `.codex/config.toml` or repository skills under `.agents/skills`, so list `.codex/**` and `.agents/**` in `trusted_paths` when you use Codex.
A custom wrapper must turn these sources off too, or list them in `trusted_paths`.

Command review uses `[stage] timeout_seconds` and `max_attempts`.
Attempts persist across process restarts.
Every repair invalidates the prior executor and coverage evidence.
Attestation schema v7 records `executor` and per-stage provenance for every review.
Command reviews also record the configured command, the exit code, and the redacted output digest.
A command from a user-level `[review]` section is recorded as `[redacted user-level command]`.
The local check that builds the note still derives reviewer paths from the real command.

See [Independent review and agreement](independent-review.md) for worked Codex and Claude configurations and the two-reviewer comparison report.

## Deterministic risk policy (`[policy]`)

Risk and review size answer different questions.
`[diff] max_bytes` is only the maximum complete diff that the agent can hold in review context.
It never makes a small change safe or a large change dangerous.
`[policy]` classifies the paths that the branch changes:

- `human_review_paths` marks ownership-sensitive paths as high risk. It records the specific policy match for reviewers.
  The paths of the configured reviewer count as human-review paths too; see [Review independence](#review-independence-review).
- `high_risk_paths` assigns high risk. Every high-risk result uses the configured approval mode before merge. It does not require a person to perform the push.
- `medium_risk_paths` assigns medium risk. Unmatched changes start at low risk.

Patterns are repository-relative. They use the same gitignore-like glob matching as `[diff] exclude`.
Absolute paths and `..` are rejected.
When several patterns match, the highest risk wins.

The hosted approval check also classifies every path that differs between the base and Git's merge of the pull-request head into it.
Thus, criss-cross history with several merge bases cannot hide a change that the branch diff from a single merge base omits.
An example is an `-s ours` merge that reverts a side branch already in the base.
The hosted check also classifies every path that a non-merge commit in `base..head` touches.
A GitHub rebase merge replays those commits and drops merge commits.
A merge whose resolution cancels an earlier high-risk commit hides that commit from both tree comparisons, but a rebase merge lands it.
A branch that changes a high-risk path and later reverts it still needs approval.
Attestation verification classifies the branch diff and the merge-result paths for a test skip and for the review-executor policy.
A local run needs no extra step.
Synchronization makes the base tip an ancestor of the validated head without a change to what the branch lands.
It merges the base in when a rebase would drop such a merge.
Thus, the branch diff that it classifies is the merge result.

Recorded `critical` and `high` findings also make the run high-risk, including after a fix.
A `medium` finding makes the run at least medium-risk.
Open blocking findings produce `changes_required` until they are resolved.
After resolution, high risk produces `needs_human` for the hosted merge-approval check.

Ordinary code derives the verdict from the committed policy and the stored findings.
The reviewing model cannot submit or override it.
Protect the policy file with your forge's ownership rules, so a change cannot quietly remove its own human-review rule.
On GitHub, make the repository's `high-risk human approval` job a required status check.
Copy `human-approval-review.yml` and `human-approval-rerun.yml` along with `human-approval.yml`, so a review from an owner, member, or collaborator, or a dismissal by a user with write access, re-runs the check; see [how a review reaches the check](attestations-and-ci.md#how-a-review-reaches-the-check).
The copied checkouts set `persist-credentials: false`, so in a private repository the later `git fetch` steps need credentials of their own; see [Required GitHub check](attestations-and-ci.md#required-github-check).
Require Code Owner reviews for the protected paths.
The job evaluates trusted base code.
Its behavior depends on `[approval] mode`.
It records the manual-merge requirement, waits for the configured Environment, or requires an eligible approval.
In `peer_review` mode, an eligible approval comes from a non-bot, repository-associated reviewer other than the PR author.
In `owner_review` mode, it comes from the configured human reviewer: an Approve review on another author's PR, or a Comment review whose trimmed body is exactly `approved` on their own PR.
An ordinary conversation comment does not count.
Both modes accept a review of the current head, or an earlier head only when the hosted check proves unchanged branch content and reports `approval_carried: true`.

## Removed CI test authority

Legacy `[ci]` tables in user or repository configuration are ignored with a warning.
Tests run locally before publication, apart from the existing documentation-only and CI-only skip.
Remove the retired table to silence the warning.
Other unknown configuration keys still fail validation.
