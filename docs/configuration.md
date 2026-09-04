# Configuration

This is the canonical configuration reference. `agentic-preflight init` writes a
commented starting file; the example below includes every supported section.

`.agentic-preflight.toml` sits in the repo root and is committed. It is layered over
`~/.config/agentic-preflight/config.toml`. Unknown keys are errors that name the key
rather than being ignored.

> **Warning: the configuration executes committed code.** During a run, `[worktree]
> setup_command` runs at `start`, every `[commands]` entry runs at its lint or test
> stage, and `[review] command` runs when command review is in effect (`executor =
> "command"`, or a risk level listed in `require_command_for`). Each may come from
> this committed file or the user config, with the repository file overriding the
> user file section by section — so the repository being validated decides what runs
> with your privileges. Starting a preflight run on a freshly cloned repository is
> equivalent in trust to running that repository's build or test suite; run it only
> on repositories whose code you would build.

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

[review]
blocking_severities = ["critical", "high"]
max_findings = 50
executor = "in_harness"       # default; or "command"
# command = "reviewer --json" # receives review context JSON on stdin
require_command_for = []      # e.g. ["high"] to require independence by risk

[policy]
# These ownership-sensitive paths are high-risk and require human merge approval.
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
root = "/optional/external/path"  # isolated modes only; defaults outside .git
copy_files = [".env"]             # must already be ignored
# setup_command = "uv sync"       # prepare dependencies and ignored build inputs

[gate]
mode = "token"                    # or "manual"

[pr]
mode = "auto"                     # or "manual"
automatedCleanup = true            # default; false requires explicit cleanup

[approval]
mode = "manual_merge"             # or "environment" / "peer_review"
environment = "high-risk-review"  # used by environment mode

[hook]
enabled = true
allow_force_push = false
```

## Configuration is snapshotted per run

The resolved configuration is snapshotted when `start` creates a run. Editing
`.agentic-preflight.toml` afterward does not change that run; the snapshot and its digest
are recorded with the run events.

Commit configuration changes **before** starting the run they should affect. This is also
why the file must be committed before `start` and must not be edited mid-run.

## Pull-request publication (`[pr]`)

`mode = "auto"` is the default and is standing authorization for pull-request creation.
The gate still asks only whether to push. After the user approves that push and preflight
finishes, the agent automatically opens the pull request—or reuses one that already
exists for the branch—without a PR-specific approval prompt.

`mode = "manual"` keeps pull-request creation in the user's hands. The agent may still
push through the configured gate, but it never opens the pull request and provides a
compare URL instead.

`automatedCleanup = true` is the default. After an automatically opened or reused pull
request passes hosted checks, the agent discloses the exact cleanup scope, polls that
pull request every 5 minutes, and removes only the disclosed run-scoped targets after
GitHub verifies the merge. When the field is `false`, automatic pull-request creation
still works, but the agent stops after hosted checks: it does not poll the merge state or
delete anything until the user explicitly requests cleanup.

This is independent of `[gate] mode`. The token gate lets the agent push after explicit
user agreement; the manual gate refuses to push and hands the command to a person.

## High-risk merge handling (`[approval]`)

`mode = "manual_merge"` is the default. The hosted approval check reports success for a
high-risk pull request only while GitHub auto-merge is disabled. It reruns when auto-merge
is enabled or disabled. The agent must never merge the pull request or enable auto-merge;
the user reviews and merges it manually.

`mode = "environment"` routes high-risk approval through the GitHub Environment named by
`environment` (default: `high-risk-review`). Configure that Environment's required
reviewers in the repository settings. The final hosted check passes only after the
Environment job is approved and completes. The policy job fails closed if the Environment
does not exist or has no required reviewer. For a solo repository, select the owner as
the required reviewer and leave **Prevent self-review** disabled. GitHub plan restrictions
may limit required reviewers for private repositories.

`mode = "peer_review"` retains the original pull-request-review policy: an eligible
repository owner, member, or collaborator other than the author must approve the exact
current head.

The policy checker reads trusted configuration from the protected base commit. A pull
request that changes `[approval]` is therefore evaluated under the old base-branch mode;
the new mode applies to subsequent pull requests after merge.

## The documentation surface (`[docs]`)

The docs stage inspects `README*`, `docs/**`, agent instructions such as `.claude/rules/**`
and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`.

Use `[docs] paths` for repository-specific documentation surfaces. The surface is an
allowlist: a docs finding filed against a path outside it is rejected, which is a statement
about the allowlist rather than a verdict on the finding. Repos often keep binding rules
outside the default surface, so add them here rather than working around the rejection.

`require_changelog` makes a changelog entry mandatory for the docs stage. The CLI
records a missing entry as a code-owned finding, which remains blocking regardless of
`[docs] blocking_severities`.

## Grounded context ([context])

`context` retrieves deterministic repository knowledge related to the changed paths and
returns it in `data.grounding`. The default sources are CODEOWNERS, matching files under
`docs/**`, root `AGENTS.md` and `CLAUDE.md`, relevant findings from earlier local runs,
and the deterministic policy reasons for the change. See
[Grounded context](context-grounding.md) for the matching and ordering rules.

`enabled = false` returns an explicit empty grounding block. `max_bytes` limits the sum
of included entry byte counts, and `entry_max_bytes` truncates document excerpts and
convention contents on a line boundary. `extra_paths` adds repository-specific rule files;
entries may be exact repo-relative paths or globs using the same matching rules as policy
paths. Absolute paths and any path containing `..` are rejected.

## Oversized diffs (`[diff]`)

Over `[diff] max_bytes`, `context` **refuses** rather than truncating. An agent that
reviews half a diff believing it saw all of it is exactly how a false green happens.

The envelope lists per-file sizes so the agent can narrow the diff with `[diff] exclude`.
Raise `max_bytes` only when the change genuinely is that large.

**`exclude` replaces the defaults; it does not extend them.** Omit the key entirely to
keep all eight built-in globs:

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

Setting a shorter list — including the abbreviated one in the README example — drops the
globs you leave out. If you want to add a project-specific pattern, copy this list and
append to it rather than writing a fresh one.

## Stage execution (`[stage]`)

`timeout_seconds` bounds a single stage run and `max_attempts` bounds retries. When a
stage is still red after `max_attempts`, the run stops and asks for human resolution
rather than retrying indefinitely.

Treat a first green from a newly configured command as unproven. Pass/fail is the exit
code alone, so a command that no-ops and exits zero reads as a pass forever, and a false
green retires the check instead of costing a retry. Confirm the run actually did work — a
test count, a results file, a non-empty log — before believing it.

## Finding thresholds (`[review]` and `[docs]`)

Each section's `blocking_severities` decides which reviewer-submitted findings must be
resolved before the run may proceed; `critical` and `high` block by default. A finding
whose action is `ask_user` blocks at any severity, because choosing on the user's behalf
is the decision that was declined. Code-owned findings also block at every severity,
because they record mechanical requirements established by the CLI rather than reviewer
judgment.

`max_findings` caps how many findings a single submission may carry.

## Review independence (`[review]`)

`executor = "in_harness"` is the default and preserves the normal agent-driven
`context` → `submit-findings` workflow. `executor = "command"` instead makes
`agentic-preflight review run` send the complete review context JSON to `command` on
stdin. The command must return one strict review submission on stdout, including the
current coverage manifest and `examined = "all"`.

`require_command_for` lists risk levels (`low`, `medium`, `high`) that must use command
review even when the configured executor is `in_harness`. Direct `submit-findings` is
refused for those runs. If command review is effective but `command` is unset, the CLI
stops with `data.mode = "needs_command"`; it never guesses a reviewer.

Command review uses `[stage] timeout_seconds` and `max_attempts`. Attempts persist across
process restarts, and every repair invalidates the prior executor and coverage evidence.
Attestation schema v4 records `executor` for every review; command reviews also record
the configured command, exit code, and redacted output digest.

See [Independent review and agreement](independent-review.md) for worked Codex and Claude
configurations and the two-reviewer comparison report.

## Deterministic risk policy (`[policy]`)

Risk and review size answer different questions. `[diff] max_bytes` is only the maximum
complete diff the agent may hold in review context; it never makes a small change safe or
a large change dangerous. `[policy]` classifies the paths changed by the branch:

- `human_review_paths` marks ownership-sensitive paths as high risk and records the
  specific policy match for reviewers.
- `high_risk_paths` assigns high risk. Every high-risk result uses the configured
  approval mode before merge, but does not require a person to perform the push.
- `medium_risk_paths` assigns medium risk. Unmatched changes start at low risk.

Patterns are repo-relative and use the same gitignore-like glob matching as
`[diff] exclude`. Absolute paths and `..` are rejected. When several patterns match, the highest
risk wins. Recorded `critical` and `high` findings also make the run high-risk, including
after a fix; a `medium` finding makes it at least medium-risk. Open blocking findings
produce `changes_required` until they are resolved; once resolved, high risk produces
`needs_human` for the hosted merge-approval check.

The verdict is derived by ordinary code from the committed policy and stored findings;
the reviewing model cannot submit or override it. Protect the policy file with your
forge's ownership rules so a change cannot quietly remove its own human-review rule. On
GitHub, make the repository's `high-risk human approval` job a required status check and
require Code Owner reviews for the protected paths. The job evaluates trusted base code.
Depending on `[approval] mode`, it records the manual-merge requirement, waits for the
configured Environment, or accepts only an eligible non-bot, non-author approval of the
exact current head.
