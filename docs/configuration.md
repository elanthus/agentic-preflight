# Configuration

This page is the canonical configuration reference.
`agentic-preflight init` writes a starting file with comments.
The example below includes every supported section.

`.agentic-preflight.toml` is in the repository root. Commit it.
The CLI combines it with `~/.config/agentic-preflight/config.toml` one section at a time.
A section in the repository file replaces the whole matching user section.
Keys are not merged across the two files.
A section that only the user file sets still applies.
Unknown keys are errors that name the key. They are not ignored.
The retired `[ci]` and `[reuse]` sections are the exception. File loading ignores them with a warning.
Removed individual keys, including `[hook] enabled`, still fail file loading. Remove them from both files.
Historical run and attestation snapshots tolerate known removed keys. Their original digest bindings do not change.
See the [0.7 upgrade guide](upgrade-0.7.md).

The configuration models enforce the same mode, severity, risk-level, approval-environment, and constrained path rules for file loading and for stored snapshots.
Python callers can use `Config(...)` or `Config.model_validate(...)` without a separate validation step.
A file-loading error identifies the affected section and key and the file that supplied it, after section-wise layering.

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

[review.contracts]
enabled = false              # opt-in executable review evidence
# planner_command = "python3 -B docs/examples/reviewers/claude_review.py"
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

# Set [telemetry] in the user file. A repository section replaces it.
[telemetry]
mirror = true                     # append each finished run's summary to runs.jsonl
hook_log = true                   # append each pre-push hook decision to hook-decisions.jsonl
# path = "~/agentic-preflight-telemetry"  # directory override; absolute or ~
```

## Configuration is snapshotted per run

`start` snapshots the resolved configuration when it creates a run.
A later edit of `.agentic-preflight.toml` does not change that run.
The snapshot and its digest are recorded with the run events.
The snapshot does not contain `[telemetry]`.

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
A `[telemetry]` section in the repository file replaces the user section, the same as every other section.
Thus, a committed section sets the preference for every contributor. Do not commit it without a reason.

When a run ends, the CLI writes `summary.json` in the run directory.
`mirror = true` is the default. The CLI then also appends the summary as one line to `runs.jsonl` in the telemetry directory.
`mirror = false` keeps each summary in its run directory only.
`hook_log = true` is the default. The pre-push hook then appends each decision as one line to `hook-decisions.jsonl` in the telemetry directory.
`hook_log = false` stops that log.
`path` sets the telemetry directory. The value must be an absolute path or start with `~`.
Without `path`, the directory is `$XDG_DATA_HOME/agentic-preflight/telemetry` when `XDG_DATA_HOME` is an absolute path.
Otherwise, it is `~/.local/share/agentic-preflight/telemetry`.

The CLI reads this section from the configuration files when a run ends. The pre-push hook reads it on each push.
Run snapshots do not contain it.
Thus, a telemetry setting does not change the configuration digest, stage evidence, or an attestation.
See [Local run telemetry](local-telemetry.md) for the recorded fields and the `telemetry rebuild` command.
See [Hook decisions](local-telemetry.md#hook-decisions) for the hook decision log.

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

`mode = "owner_review"` requires the human GitHub login in `reviewer` to review the current head or an equivalent earlier head.
They must be a repository owner, member, or collaborator.
On another author's pull request, they submit an **Approve** review.
On their own pull request, they submit a **Comment** review whose trimmed body is exactly `approved` in lowercase.
An ordinary PR conversation comment does not count.
A dismissal or a later changes-requested review on any head revokes the approval.
An edit or replacement of a self-review with other text also removes its approval.
Bots and other reviewers cannot satisfy this mode.

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

`timeout_seconds` bounds a single stage run. `max_attempts` bounds the retries.
When a stage is still red after `max_attempts`, the run stops and asks for human resolution.
It does not retry indefinitely.

When `timeout_seconds` expires, the stage's whole process group is killed and the stage exits 124.
Output is collected for at most a few more seconds.
Thus, a descendant process that left the process group and still holds the output pipe cannot keep the stage open.
In that case, the captured output is discarded.
The stage output ends with an `[output truncated: ...]` line after the timeout marker.
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

Command review uses `[stage] timeout_seconds` and `max_attempts`.
Attempts persist across process restarts.
Every repair invalidates the prior executor and coverage evidence.
Attestation schema v7 records `executor` and per-stage provenance for every review.
Command reviews also record the configured command, the exit code, and the redacted output digest.

See [Independent review and agreement](independent-review.md) for worked Codex and Claude configurations and the two-reviewer comparison report.

## Deterministic risk policy (`[policy]`)

Risk and review size answer different questions.
`[diff] max_bytes` is only the maximum complete diff that the agent can hold in review context.
It never makes a small change safe or a large change dangerous.
`[policy]` classifies the paths that the branch changes:

- `human_review_paths` marks ownership-sensitive paths as high risk. It records the specific policy match for reviewers.
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
Require Code Owner reviews for the protected paths.
The job evaluates trusted base code.
Its behavior depends on `[approval] mode`.
It records the manual-merge requirement, waits for the configured Environment, or requires an eligible approval.
An eligible approval comes from a non-bot, non-author reviewer of the current head or of an earlier head with identical branch content.

## Removed CI test authority

Legacy `[ci]` tables in user or repository configuration are ignored with a warning.
Tests run locally before publication, apart from the existing documentation-only and CI-only skip.
Remove the retired table to silence the warning.
Other unknown configuration keys still fail validation.
