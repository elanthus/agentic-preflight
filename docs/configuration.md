# Configuration

The full example lives in the [README](../README.md#configuration). This page covers the
behaviour behind the keys that need more than a comment.

`.agentic-preflight.toml` sits in the repo root and is committed. It is layered over
`~/.config/agentic-preflight/config.toml`. Unknown keys are errors that name the key
rather than being ignored.

## Configuration is snapshotted per run

The resolved configuration is snapshotted when `start` creates a run. Editing
`.agentic-preflight.toml` afterward does not change that run; the snapshot and its digest
are recorded with the run events.

Commit configuration changes **before** starting the run they should affect. This is also
why the file must be committed before `start` and must not be edited mid-run.

## The documentation surface (`[docs]`)

The docs stage inspects `README*`, `docs/**`, agent instructions such as `.claude/rules/**`
and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`.

Use `[docs] paths` for repository-specific documentation surfaces. The surface is an
allowlist: a docs finding filed against a path outside it is rejected, which is a statement
about the allowlist rather than a verdict on the finding. Repos often keep binding rules
outside the default surface, so add them here rather than working around the rejection.

`require_changelog` makes a changelog entry mandatory for the docs stage.

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

## Review thresholds (`[review]`)

`blocking_severities` decides which findings must be resolved before the run may proceed;
`critical` and `high` block by default. A finding whose action is `ask_user` blocks at any
severity, because choosing on the user's behalf is the decision that was declined.

`max_findings` caps how many findings a single submission may carry.

## Deterministic risk policy (`[policy]`)

Risk and review size answer different questions. `[diff] max_bytes` is only the maximum
complete diff the agent may hold in review context; it never makes a small change safe or
a large change dangerous. `[policy]` classifies the paths changed by the branch:

- `human_review_paths` marks ownership-sensitive paths as high risk and records the
  specific policy match for reviewers.
- `high_risk_paths` assigns high risk. Every high-risk result requires human approval
  before merge, but does not require a person to perform the push.
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
require Code Owner reviews for the protected paths. The job evaluates trusted base code,
rejects bots and the pull-request author, and requires approval of the exact current head.
