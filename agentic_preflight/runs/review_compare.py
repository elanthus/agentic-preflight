"""Measure agreement between two review executors without advancing a run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import diff as diffmod
from .. import findings as findingsmod
from .. import gitx
from ..envelope import Envelope
from ..errors import InvalidFindings, StageFailed
from ..machine import State
from ..models import FindingSubmission, RunDoc, Stage
from ..stages import shellstage
from . import review_coverage, review_protocol
from ._session import Session, _envelope_for, _load_current, _require_state, _require_worktree

COMPARABLE_STATES = (
    State.REVIEW_GREEN,
    State.DOCS_AWAITING_FINDINGS,
    State.DOCS_BLOCKED,
    State.DOCS_GREEN,
    State.LINT_RUNNING,
    State.LINT_GREEN,
    State.LINT_RED,
    State.TEST_RUNNING,
    State.TEST_GREEN,
    State.TEST_RED,
    State.MERGEBACK_PENDING,
    State.MERGEBACK_CONFLICT,
    State.VERIFIED,
    State.AWAITING_PUSH_CONFIRM,
    State.PUSHED,
    State.DONE,
)


def submission_path(session: Session, run_id: str, executor: str) -> Path:
    """Keep executor evidence separate so a second review cannot overwrite the first."""
    return session.store.run_dir(run_id) / f"review-submission-{executor}.json"


def persist_submission(
    session: Session,
    run: RunDoc,
    *,
    executor: review_protocol.ReviewExecutor,
    manifest: str,
    head_sha: str,
    findings: list[FindingSubmission],
) -> None:
    """Persist the accepted wire submission before it is mixed with stored findings."""
    payload = {
        "manifest": manifest,
        "head_sha": head_sha,
        "executor": executor,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    submission_path(session, run.run_id, executor).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _refusal(message: str, run: RunDoc) -> InvalidFindings:
    return InvalidFindings(
        message,
        state=run.state.value,
        run_id=run.run_id,
        stage=Stage.REVIEW.value,
        next_instruction="Inspect the current run and review evidence before comparing again.",
        next_command="agentic-preflight status",
    )


def _load_recorded(session: Session, run: RunDoc) -> dict[str, Any]:
    entry = run.stages.get(Stage.REVIEW)
    executor = entry.executor if entry is not None else None
    if executor not in {"in_harness", "command"}:
        raise _refusal("the run has no accepted review executor to compare", run)
    path = submission_path(session, run.run_id, executor)
    if not path.is_file():
        raise _refusal(f"the recorded {executor} review submission is missing", run)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _parse_persisted(payload)
    except (OSError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise _refusal(f"the recorded {executor} review submission is invalid: {exc}", run) from exc


def _parse_persisted(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    manifest = payload.get("manifest")
    head_sha = payload.get("head_sha")
    executor = payload.get("executor")
    if not isinstance(manifest, str) or not isinstance(head_sha, str):
        raise ValueError("manifest and head_sha must be strings")
    if executor not in {"in_harness", "command"}:
        raise ValueError("executor must be in_harness or command")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    parsed = [FindingSubmission.model_validate(item) for item in findings]
    return {
        "manifest": manifest,
        "head_sha": head_sha,
        "executor": executor,
        "findings": parsed,
    }


def _file_submission(
    payload: Any,
    *,
    recorded_executor: review_protocol.ReviewExecutor,
    manifest: diffmod.ReviewManifest,
) -> dict[str, Any]:
    other: review_protocol.ReviewExecutor = (
        "command" if recorded_executor == "in_harness" else "in_harness"
    )
    if isinstance(payload, dict) and {"manifest", "head_sha", "executor"} <= payload.keys():
        return _parse_persisted(payload)
    submissions, submitted_manifest = review_protocol.parse_submission(payload, stage=Stage.REVIEW)
    submissions, _ = review_coverage.validate(
        submissions,
        manifest=manifest,
        submitted_manifest=submitted_manifest,
    )
    return {
        "manifest": submitted_manifest,
        "head_sha": manifest.head_sha,
        "executor": other,
        "findings": submissions,
    }


def _shadow_submission(
    session: Session,
    run: RunDoc,
    *,
    manifest: diffmod.ReviewManifest,
) -> dict[str, Any]:
    command = session.config.review.command
    if not command:
        raise _refusal(
            "comparison needs --file PATH or a configured [review] command for shadow review",
            run,
        )
    bundle = review_protocol.bundle_for(session, run)
    data = review_protocol.context_data(
        session,
        run,
        section="review",
        bundle=bundle,
        review_manifest=manifest,
    )
    stdin_text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    worktree = _require_worktree(run)
    try:
        secrets = shellstage.read_secrets(worktree, run.copied_files)
    except shellstage.SecretRedactionError as exc:
        raise StageFailed(
            "the shadow review cannot run because copied-file redaction is unavailable",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"copied_file": str(exc.path)},
            next_command="agentic-preflight review compare",
        ) from exc

    result = shellstage.run_stage(
        worktree,
        command,
        timeout_seconds=session.config.stage.timeout_seconds,
        stdin_text=stdin_text,
        separate_stderr=True,
        guarded_files=run.copied_files,
    )
    if not gitx.is_clean(worktree):
        result.exit_code = result.exit_code or 1
        result.output += "\n[agentic-preflight] shadow review command changed the worktree"
    try:
        post_secrets = shellstage.read_secrets(worktree, run.copied_files)
    except shellstage.SecretRedactionError:
        post_secrets = []
        redaction_failed = True
    else:
        redaction_failed = result.copied_files_changed
    clean_output = (
        shellstage.REDACTION_FAILURE_OUTPUT
        if redaction_failed
        else shellstage.redact(
            result.output,
            shellstage.combine_secrets(secrets, post_secrets),
        )
    )
    log_path = session.store.logs_dir(run.run_id) / "review-compare.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(clean_output, encoding="utf-8", newline="\n")
    if redaction_failed:
        raise StageFailed(
            "the shadow review output was withheld because redaction became unavailable",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"log_path": str(log_path)},
            next_command="agentic-preflight review compare",
        )
    if not result.passed:
        reason = "timed out" if result.timed_out else f"exited {result.exit_code}"
        raise StageFailed(
            f"the shadow review command {reason}",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"log_path": str(log_path), "exit_code": result.exit_code},
            next_command="agentic-preflight review compare",
        )
    try:
        payload = json.loads(result.stdout or "")
        review_protocol.validate_command_output(payload)
        findings, submitted_manifest = review_protocol.parse_submission(payload, stage=Stage.REVIEW)
        findings, _ = review_coverage.validate(
            findings,
            manifest=manifest,
            submitted_manifest=submitted_manifest,
        )
    except (json.JSONDecodeError, ValidationError, InvalidFindings) as exc:
        raise StageFailed(
            f"the shadow review command returned an invalid submission: {exc}",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"log_path": str(log_path), "exit_code": result.exit_code},
            next_command="agentic-preflight review compare",
        ) from exc
    return {
        "manifest": submitted_manifest,
        "head_sha": manifest.head_sha,
        "executor": "command",
        "findings": findings,
    }


def _same_location(a: FindingSubmission, b: FindingSubmission) -> bool:
    if a.path != b.path:
        return False
    if a.line is None or b.line is None:
        return a.line == b.line
    return abs(a.line - b.line) <= 3


def _finding_dict(finding: FindingSubmission) -> dict[str, Any]:
    return finding.model_dump(mode="json")


def _summarise(
    manifest: diffmod.ReviewManifest,
    a: dict[str, Any],
    b: dict[str, Any],
) -> dict[str, Any]:
    a_findings: list[FindingSubmission] = a["findings"]
    b_findings: list[FindingSubmission] = b["findings"]
    a_units = {finding.unit for finding in a_findings}
    b_units = {finding.unit for finding in b_findings}
    all_units = {unit.id for unit in manifest.units}
    both = a_units & b_units
    only_a_units = a_units - b_units
    only_b_units = b_units - a_units
    flagged = a_units | b_units

    agreed: list[dict[str, Any]] = []
    severity_disagreements: list[dict[str, Any]] = []
    unmatched_b = set(range(len(b_findings)))
    unmatched_a: list[FindingSubmission] = []
    for left in a_findings:
        match = next(
            (
                index
                for index in sorted(unmatched_b)
                if left.unit == b_findings[index].unit and _same_location(left, b_findings[index])
            ),
            None,
        )
        if match is None:
            unmatched_a.append(left)
            continue
        unmatched_b.remove(match)
        right = b_findings[match]
        pair = {"a": _finding_dict(left), "b": _finding_dict(right)}
        agreed.append(pair)
        if left.severity != right.severity:
            severity_disagreements.append(pair)

    denominator = len(flagged)
    return {
        "manifest": manifest.manifest,
        "head_sha": manifest.head_sha,
        "executors": [a["executor"], b["executor"]],
        "units": {
            "total": len(all_units),
            "both_flagged": len(both),
            "only_a": len(only_a_units),
            "only_b": len(only_b_units),
            "neither": len(all_units - flagged),
        },
        "findings": {
            "agreed": agreed,
            "only_a": [_finding_dict(finding) for finding in unmatched_a],
            "only_b": [_finding_dict(b_findings[index]) for index in sorted(unmatched_b)],
            "severity_disagreements": severity_disagreements,
        },
        "agreement_rate": len(both) / denominator if denominator else None,
    }


def compare_reviews(session: Session, payload: Any | None = None) -> Envelope:
    """Compare accepted evidence with a supplied or shadow submission in place."""
    run = _load_current(session)
    _require_state(run, *COMPARABLE_STATES, command="review compare")
    coverage = run.review_coverage
    if coverage is None:
        raise _refusal("the run has no accepted review coverage to compare", run)
    worktree = _require_worktree(run)
    current_head = gitx.rev_parse(worktree, "HEAD")
    if current_head != coverage.head_sha:
        raise _refusal(
            f"worktree HEAD {current_head} does not match reviewed HEAD {coverage.head_sha}", run
        )
    bundle = review_protocol.bundle_for(session, run)
    manifest = review_protocol.grounded_manifest(session, run, bundle)
    if manifest.manifest != coverage.manifest:
        raise _refusal("the current review manifest does not match the accepted submission", run)

    recorded = _load_recorded(session, run)
    if payload is None:
        if recorded["executor"] != "in_harness":
            raise _refusal(
                "automatic shadow review requires a recorded in_harness submission; use --file",
                run,
            )
        second = _shadow_submission(session, run, manifest=manifest)
    else:
        try:
            second = _file_submission(
                payload,
                recorded_executor=recorded["executor"],
                manifest=manifest,
            )
        except (ValueError, ValidationError, InvalidFindings) as exc:
            raise _refusal(f"the comparison submission is invalid: {exc}", run) from exc

    try:
        second["findings"], _ = review_coverage.validate(
            second["findings"],
            manifest=manifest,
            submitted_manifest=second["manifest"],
        )
        findingsmod.validate_and_assign(
            second["findings"],
            stage=Stage.REVIEW,
            worktree_path=worktree,
            allowed_paths=set(bundle.files),
            max_findings=session.config.review.max_findings,
        )
    except (InvalidFindings, findingsmod.FindingRejected) as exc:
        if payload is None:
            raise StageFailed(
                f"the shadow review command returned an invalid submission: {exc}",
                state=run.state.value,
                run_id=run.run_id,
                stage=Stage.REVIEW.value,
                data={"log_path": str(session.store.logs_dir(run.run_id) / "review-compare.txt")},
                next_command="agentic-preflight review compare",
            ) from exc
        raise _refusal(f"the comparison submission is invalid: {exc}", run) from exc

    for candidate in (recorded, second):
        if candidate["manifest"] != manifest.manifest:
            raise _refusal("review submissions have different manifests", run)
        if candidate["head_sha"] != manifest.head_sha:
            raise _refusal("review submissions have different reviewed HEADs", run)
    if recorded["executor"] == second["executor"]:
        raise _refusal("comparison requires one in_harness and one command submission", run)
    a, b = (recorded, second) if recorded["executor"] == "in_harness" else (second, recorded)
    summary = _summarise(manifest, a, b)
    summary_path = session.store.run_dir(run.run_id) / "review-compare.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    session.store.append_event(run.run_id, {"event": "review_compared", **summary})
    return _envelope_for(run, stage=Stage.REVIEW.value, data=summary)
