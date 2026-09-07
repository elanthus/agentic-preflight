# ADR 0001: Bound orchestration by protocol and lifecycle

- Status: Accepted
- Date: 2026-08-13

## Context

`runs/review.py` and `cli.py` had become change hotspots. Review mixed wire-format
validation, coverage accounting, external process I/O, retry persistence, and stage
transitions. The CLI mixed its one-object stdout contract with unrelated command families.
Small changes therefore required reasoning about the whole workflow and made merge conflicts
more likely.

The public contracts must remain stable: command names, JSON envelopes, durable state
transitions, and the functions exported by `agentic_preflight.runs`.

## Decision

Keep thin, stable facades and split implementation by reason to change.

### Review boundary

- `runs/review.py` coordinates context, finding submission, and verification.
- `runs/review_protocol.py` owns the canonical executor input and strict submission schema.
  It performs no state transitions.
- `runs/review_coverage.py` binds findings to review units, creates complete coverage
  evidence, and invalidates evidence when `HEAD` changes.
- `runs/review_executor.py` is the external-process adapter: prepare, execute, parse,
  and hand the submission to the same coordinator used by in-harness review.
- `stages/protected_output.py` shares copied-file secret snapshots, output withholding,
  redaction, and log writing between review and shell-stage executors. Callers capture
  secrets before recording the running state and retain their own execution, retry,
  and failure handling. Review parses the original stdout only after output protection
  succeeds; redacted output is the log and digest input.
- `runs/review_retry.py` alone persists command running/red transitions, attempt counts,
  interrupted-run recovery, and successful process evidence.
- `runs/__init__.py` remains the supported import facade.

The dependency direction is coordinator/executor toward protocol, coverage, and retry.
Protocol, coverage, and retry do not import the coordinator. The executor hands a parsed
submission to the existing coordinator so command and in-harness review share one submission
path.

### Shell-stage boundary

- `runs/stages.py` coordinates freshness and recovery checks, committed repairs, delegation,
  attempt limits, output protection, local execution, and result persistence in that order.
  A dirty validation worktree or a repair requiring renewed review stops before delegation
  or local execution.
- `runs/stage_delegation.py` checks trusted-CI policy and rejects local execution flags before
  persisting pending test delegation. It returns an envelope when delegated and `None` when
  the coordinator should continue locally.
- `runs/stage_baseline.py` owns the scratch baseline worktree and setup-failure recovery.
  It returns whether the baseline command failed. Failed setup instead records a red
  attempt and raises with the same baseline retry command; no test result is manufactured.
  A failing baseline never turns a failing local stage green.

These helpers depend on shared session and state primitives, not the coordinator. Local
result persistence and response construction stay in `runs/stages.py`.

### CLI boundary

- `cli.py` defines the root group and registers command families.
- `cli_support.py` is the single JSON emission and exception-mapping boundary.
- `cli_runs.py`, `cli_policy.py`, and `cli_integrations.py` adapt their command families to
  application functions; they contain no run-state policy.
- `command` remains re-exported from `cli.py` for compatibility.

## Consequences

Command names, envelopes, exit codes, state-machine actions, retry limits, logs, and coverage
semantics remain unchanged. New executors must use `review_protocol`; new retry behavior must
be implemented in `review_retry`; and new CLI commands belong to the narrowest command family.

There are more modules and a small registration layer, but each module can now be tested and
reviewed against one invariant. End-to-end CLI tests remain the contract test across all
boundaries.
