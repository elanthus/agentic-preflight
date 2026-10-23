# ADR 0001: Bound orchestration by protocol and lifecycle

- Status: Accepted
- Date: 2026-08-13
- Amended: 2026-09. Removed the claim that `command` is re-exported from `cli.py`, which no longer holds.
  Removed an implementation-level list of start phase functions that duplicated the source.
- Amended: 2026-10-20. Rewrote the text in ASD-STE100 style. The decisions are unchanged.

## Context

`runs/review.py` and `cli.py` had become change hotspots.
Review mixed wire-format validation, coverage accounting, external process I/O, retry persistence, and stage transitions.
The CLI mixed its one-object stdout contract with unrelated command families.
Thus, small changes required reasoning about the whole workflow. Merge conflicts became more likely.

The public contracts must stay stable: command names, JSON envelopes, durable state transitions, and the functions that `agentic_preflight.runs` exports.

## Decision

Keep thin, stable facades. Split the implementation by reason to change.

### Review boundary

- `runs/review.py` coordinates context, finding submission, and verification.
- `runs/review_protocol.py` owns the canonical executor input and the strict submission schema. It performs no state transitions.
- `runs/review_coverage.py` binds findings to review units, creates complete coverage evidence, and invalidates evidence when `HEAD` changes.
- `runs/review_executor.py` is the external-process adapter.
  It prepares, executes, parses, and hands the submission to the same coordinator that in-harness review uses.
- `stages/shellstage.py` shares copied-file secret snapshots, output withholding, redaction, and log writing between review and shell-stage executors.
  Callers capture secrets before they record the running state.
- `runs/_shell.py` shares protected execution and dirty-worktree checks. Callers keep retry and failure handling.
  Review parses the original stdout only after output protection succeeds. The redacted output is the log and digest input.
- `runs/review_executor.py` also persists command running and red transitions, attempt counts, interrupted-run recovery, and successful process evidence.
- `runs/__init__.py` stays the supported import facade.

The dependency direction is from coordinator and executor toward protocol and coverage.
Protocol and coverage do not import the coordinator.
The executor hands a parsed submission to the existing coordinator.
Thus, command review and in-harness review share one submission path.

### Shell-stage boundary

- `runs/stages.py` coordinates, in this order: freshness and recovery checks, committed repairs, attempt limits, output protection, local execution, and result persistence.
  A dirty validation worktree or a repair that requires renewed review stops before local execution.
- `runs/stage_baseline.py` owns the scratch baseline worktree and setup-failure recovery.
  It returns whether the baseline command failed.
  On setup failure, it records a red attempt and raises with the same baseline retry command. It does not manufacture a test result.
  A failing baseline never turns a failing local stage green.

These helpers depend on shared session and state primitives, not on the coordinator.
Local result persistence and response construction stay in `runs/stages.py`.

### Start boundary

`runs/start.py:start` is a thin coordinator over ordered start phases.
The store transaction boundaries within those phases are part of the durable crash-recovery contract.

### CLI boundary

- `cli.py` defines the root group and registers the command families.
- `cli_support.py` is the single JSON emission and exception-mapping boundary.
- `cli_runs.py`, `cli_policy.py`, `cli_integrations.py`, `cli_telemetry.py`, and `cli_report.py` adapt their
  command families to application functions. They contain no run-state policy.

## Consequences

Command names, envelopes, exit codes, state-machine actions, retry limits, logs, and coverage semantics are unchanged.
New executors must use `review_protocol`.
New retry behavior must be implemented in `review_executor`.
A new CLI command belongs to the narrowest command family.

Related helpers share their consumers.
Protocol and coverage keep focused boundaries that can be tested and reviewed independently.
End-to-end CLI tests stay the contract test across all boundaries.
