"""The run summary: its schema, and how it is derived from a run's audit record.

Everything here is a pure function of a run record, its findings, its events,
and the repository identity the caller supplies. Reading the run store, writing
``summary.json``, and the user-level mirror live in
:mod:`agentic_preflight.telemetry`.

The summary's rules:

* A summary records states, counts, and digests. It never copies the intent
  text, diffs, finding text, logs, or command output.
* No summary field feeds a fingerprint, an evidence digest, or an attestation.
* Absent evidence is ``None``, never zero. Older records lack many fields.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import timing
from .digests import sha256_text
from .machine import TERMINAL_STATES, State
from .models import Finding, RunDoc, Stage

SUMMARY_KIND = "agentic-preflight-run-summary"
#: Events that :mod:`agentic_preflight.telemetry` appends. They are left out of
#: ``event_counts`` so that a rebuilt summary counts the same events as the one
#: written when the run ended.
TELEMETRY_EVENTS = frozenset({"summary_written", "summary_failed"})
#: Markers that repeat the terminal state. Some commands append them after the
#: terminal commit, so they are also left out of ``event_counts``.
TERMINAL_MARKER_EVENTS = frozenset({"finished", "aborted", "orphaned"})

_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
_STATE_VALUES = frozenset(state.value for state in State)
_STAGE_ORDER = [stage.value for stage in Stage]
_RETIRED_DELEGATED_STATES = ("TEST_DELEGATED", "PUBLICATION_READY")
_RETIRED_DELEGATION_REASON = "CI test delegation was removed; start a new local validation run"
_DEFAULT_BLOCKING = ("critical", "high")
#: Finding statuses that ``respond`` records with an agent-chosen reason.
_REASONED_STATUSES = frozenset({"dismissed", "accepted"})


class RepoIdentity(BaseModel):
    """Which repository a run belongs to, independent of the clone it ran in."""

    model_config = ConfigDict(extra="forbid")

    #: Smallest root commit of ``HEAD``: the same in every clone of a repository.
    root_commit: str | None
    #: ``origin`` as host and path, without scheme, credentials, port, or ``.git``.
    remote: str | None
    #: Last path segment of ``remote``, else the clone's directory name.
    name: str | None
    source_worktree_path: str


class RunSummary(BaseModel):
    """Schema 1 of one finished run's local summary."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agentic-preflight-run-summary"] = "agentic-preflight-run-summary"
    schema_version: Literal[1] = 1
    written_at: str
    run_id: str
    state: str
    record_schema_version: int
    repo: RepoIdentity
    branch: str
    base_ref: str
    head_sha: str
    pushed_sha: str | None
    intent_sha256: str | None
    intent_source: str
    created_at: str
    finished_at: str
    duration_seconds: float | None
    #: Wall-clock seconds per state from the transition events; ``None`` without them.
    timing: dict[str, Any] | None = None
    producer: dict[str, Any] | None
    risk: dict[str, Any] | None
    review: dict[str, Any] | None
    stages: dict[str, dict[str, Any]]
    findings: dict[str, Any]
    fix_commits: int | None
    validation_restarts: int | None
    event_counts: dict[str, int] | None
    terminal: dict[str, Any]
    hosted: dict[str, Any]
    worktree_mode: str | None
    pr_mode: str | None
    config_digest: str


# -- derivation -------------------------------------------------------------
#
# The coercions below read one field of a record that may come from any
# version. A value of another type is unknown, so it reads as ``None``.


def utc_now() -> str:
    """The current UTC time in seconds, as ISO 8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def as_str(value: Any) -> str | None:
    """``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


def as_int(value: Any) -> int | None:
    """``value`` when it is an ``int`` other than a ``bool``, else ``None``."""
    return value if type(value) is int else None


def as_bool(value: Any) -> bool | None:
    """``value`` when it is a ``bool``, else ``None``."""
    return value if isinstance(value, bool) else None


def as_dict(value: Any) -> dict[str, Any] | None:
    """``value`` when it is a ``dict``, else ``None``."""
    return value if isinstance(value, dict) else None


def _length(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _as_record(run: RunDoc | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(run, BaseModel):
        return run.model_dump(mode="json")
    return dict(run)


def _producer(run: RunDoc | Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    producer = getattr(run, "producer", None) if isinstance(run, BaseModel) else None
    if producer is None:
        producer = record.get("producer")
    if isinstance(producer, BaseModel):
        return producer.model_dump(mode="json")
    return as_dict(producer)


def effective_state(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return the state and orphan reason the tool reads, as ``RunDoc`` would load them."""
    state = as_str(record.get("state"))
    retired = state in _RETIRED_DELEGATED_STATES or (
        record.get("test_delegation") is not None
        and state in _STATE_VALUES
        and state not in ("PUSHED", *_TERMINAL)
    )
    if retired:
        return "ORPHANED", _RETIRED_DELEGATION_REASON
    return state, as_str(record.get("orphaned_reason"))


def _tally(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(value if isinstance(value, str) else "unknown" for value in values)
    return dict(sorted(counts.items()))


def _blocking_severities(snapshot: Mapping[str, Any], section: str) -> frozenset[str] | None:
    """The run's configured blocking severities for one findings stage.

    A missing key held the model default when the run started. A value that is
    present but malformed is unknown.
    """
    body = snapshot.get(section)
    if not isinstance(body, dict) or "blocking_severities" not in body:
        return frozenset(_DEFAULT_BLOCKING)
    value = body["blocking_severities"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


def _finding_items(findings: Any) -> list[dict[str, Any]] | None:
    """The findings as objects, or ``None`` when the file is not a list of objects."""
    if not isinstance(findings, (list, tuple)):
        return None
    items: list[dict[str, Any]] = []
    for item in findings:
        if isinstance(item, BaseModel):
            items.append(item.model_dump(mode="json"))
        elif isinstance(item, Mapping):
            items.append(dict(item))
        else:
            return None
    return items


def _is_blocking(item: Mapping[str, Any], review: frozenset[str], docs: frozenset[str]) -> bool:
    """Whether a finding's severity blocks its stage. A non-text severity never does."""
    severity = item.get("severity")
    if not isinstance(severity, str):
        return False
    return severity in (docs if item.get("stage") == "docs" else review)


def _response_reasons(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """The agent's reasons for dismissed and accepted findings. No reason is ``unspecified``."""
    reasons = (
        item.get("response_reason") for item in items if item.get("status") in _REASONED_STATUSES
    )
    return _tally("unspecified" if reason is None else reason for reason in reasons)


def _findings_summary(
    findings: Sequence[Finding | Mapping[str, Any]] | None, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Finding counts. All are ``None`` without a findings file or with a malformed one."""
    keys = (
        "total",
        "by_stage",
        "by_severity",
        "by_status",
        "by_action",
        "by_response_reason",
        "blocking",
        "review_blocking_fixed",
        "docs_blocking_fixed",
    )
    items = _finding_items(findings)
    if items is None:
        return dict.fromkeys(keys)
    review = _blocking_severities(snapshot, "review")
    docs = _blocking_severities(snapshot, "docs")
    blocking: int | None = None
    review_fixed: int | None = None
    docs_fixed: int | None = None
    if review is not None and docs is not None:
        blocking_items = [item for item in items if _is_blocking(item, review, docs)]
        blocking = len(blocking_items)
        caught = [item for item in blocking_items if item.get("status") == "fixed"]
        docs_fixed = sum(1 for item in caught if item.get("stage") == "docs")
        review_fixed = len(caught) - docs_fixed
    return {
        "total": len(items),
        "by_stage": _tally(item.get("stage") for item in items),
        "by_severity": _tally(item.get("severity") for item in items),
        "by_status": _tally(item.get("status") for item in items),
        "by_action": _tally(item.get("action") for item in items),
        "by_response_reason": _response_reasons(items),
        "blocking": blocking,
        # Blocking findings that were fixed before the push: what review and docs caught.
        "review_blocking_fixed": review_fixed,
        "docs_blocking_fixed": docs_fixed,
    }


def _stage_order(name: str) -> tuple[int, str]:
    return (_STAGE_ORDER.index(name) if name in _STAGE_ORDER else len(_STAGE_ORDER), name)


#: Shell stages whose red state the gate can leave for green within one run.
_SHELL_STAGES = ("lint", "test")


def _red_then_green(
    events: Sequence[Mapping[str, Any]] | None, complete: bool
) -> dict[str, bool | None]:
    """Whether each shell stage failed and later passed in the same run.

    ``stages.<name>.attempts`` counts failures but not whether a later attempt
    passed, so the transitions decide: a visit to ``<STAGE>_RED`` before a visit
    to ``<STAGE>_GREEN``. A red that interrupted-command recovery committed (the
    next event is ``<stage>_interrupted``) is not a failure of the change. Unless
    the transition chain is complete (:func:`timing.state_timing`), a red can be
    missing, so the answer is unknown.
    """
    if not complete:
        return dict.fromkeys(_SHELL_STAGES)
    items = list(_event_items(events))
    result: dict[str, bool | None] = {}
    for stage in _SHELL_STAGES:
        red, green = f"{stage.upper()}_RED", f"{stage.upper()}_GREEN"
        failed = caught = False
        for index, event in enumerate(items):
            if event.get("event") != "state_transition":
                continue
            target = event.get("to")
            if target == red:
                following = items[index + 1] if index + 1 < len(items) else {}
                failed = failed or following.get("event") != f"{stage}_interrupted"
            elif target == green and failed:
                caught = True
        result[stage] = caught
    return result


def _stages(
    record: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]] | None = None,
    *,
    complete: bool = False,
) -> dict[str, dict[str, Any]]:
    stages = as_dict(record.get("stages")) or {}
    red_then_green = _red_then_green(events, complete)
    evidence = as_dict(record.get("evidence"))
    summary: dict[str, dict[str, Any]] = {}
    for name in sorted(stages, key=_stage_order):
        stage = as_dict(stages[name]) or {}
        status = as_str(stage.get("status"))
        reused: bool | None = None
        if evidence is not None:
            item = as_dict(evidence.get(name))
            reused = item is not None and item.get("derivation_reason") is not None
        started_at = as_str(stage.get("started_at"))
        finished_at = as_str(stage.get("finished_at"))
        # Only a stage that ran here has a duration: a reused result started in
        # another run, and a skipped stage never started.
        ran = status != "skipped" and reused is False
        summary[name] = {
            "status": status,
            "attempts": as_int(stage.get("attempts")),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration(started_at, finished_at) if ran else None,
            "reused": reused,
            # Skip reasons are code-assigned. Failure reasons can quote command
            # or reviewer output, which a summary never copies.
            "reason": as_str(stage.get("reason")) if status == "skipped" else None,
            # Only the shell stages have a red state that a repair turns green.
            "red_then_green": red_then_green.get(name),
        }
    return summary


def _risk(record: Mapping[str, Any]) -> dict[str, Any] | None:
    risk = as_dict(record.get("risk"))
    if risk is None:
        return None
    return {
        "level": as_str(risk.get("level")),
        "verdict": as_str(risk.get("verdict")),
        "requires_human_review": as_bool(risk.get("requires_human_review")),
    }


def _review(record: Mapping[str, Any]) -> dict[str, Any] | None:
    coverage = as_dict(record.get("review_coverage"))
    stage = as_dict((as_dict(record.get("stages")) or {}).get("review"))
    if coverage is None and stage is None:
        return None
    coverage = coverage or {}
    return {
        "total_units": as_int(coverage.get("total_units")),
        "cited_count": _length(coverage.get("cited_units")),
        "clean_count": _length(coverage.get("clean_units")),
        "excluded_count": _length(coverage.get("excluded_files")),
        "executor": as_str((stage or {}).get("executor")),
    }


def _event_items(events: Iterable[Any] | None) -> Iterator[Mapping[str, Any]]:
    """The event log's objects. A line that holds any other JSON value is skipped."""
    for event in events or ():
        if isinstance(event, Mapping):
            yield event


def _event_counts(events: Sequence[Mapping[str, Any]] | None) -> dict[str, int] | None:
    if events is None:
        return None
    names = (event.get("event") for event in _event_items(events))
    return _tally(
        name
        for name in names
        if isinstance(name, str)
        and name not in TELEMETRY_EVENTS
        and name not in TERMINAL_MARKER_EVENTS
    )


def _finished_at(record: Mapping[str, Any], events: Sequence[Mapping[str, Any]] | None) -> Any:
    """The time of the terminal commit.

    A later write, such as ``gc`` recording a released worktree, moves
    ``updated_at``. The terminal transition event keeps the original time, and
    its ``seq`` shows whether ``updated_at`` still belongs to that commit.
    """
    updated = as_str(record.get("updated_at"))
    for event in _event_items(events):
        target = event.get("to")
        if (
            event.get("event") == "state_transition"
            and isinstance(target, str)
            and (target in _TERMINAL)
        ):
            if updated and event.get("seq") is not None and event.get("seq") == record.get("seq"):
                return updated
            return as_str(event.get("at")) or updated
    return updated


def _duration(start: str | None, end: str | None) -> float | None:
    try:
        delta = datetime.fromisoformat(end or "") - datetime.fromisoformat(start or "")
    except (TypeError, ValueError):
        return None
    seconds = delta.total_seconds()
    return seconds if seconds >= 0 else None


def _aborted_from_transition(events: Sequence[Mapping[str, Any]]) -> str | None:
    """The ``from`` of the last transition into ``ABORTED``, for aborts without a marker."""
    for event in reversed(events):
        if event.get("event") == "state_transition" and event.get("to") == "ABORTED":
            return as_str(event.get("from"))
    return None


def _terminal(
    state: str | None, reason: str | None, events: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """How the run ended. ``abort_reason`` is the agent's label; the rest is code-derived."""
    forced = abort_reason = aborted_from = None
    if state == "ABORTED":
        items = list(_event_items(events))
        for event in reversed(items):
            if event.get("event") == "aborted":
                forced = as_bool(event.get("forced"))
                abort_reason = as_str(event.get("reason"))
                aborted_from = as_str(event.get("aborted_from_state"))
                break
        # Older markers, and the aborts that ``start`` commits itself, carry no
        # state; the transition event records the same fact.
        aborted_from = aborted_from or _aborted_from_transition(items)
    return {
        "aborted_forced": forced,
        "abort_reason": abort_reason,
        "aborted_from_state": aborted_from,
        "orphaned_reason": reason,
    }


def _synced(record: Mapping[str, Any], hosted: Mapping[str, Any] | None) -> dict[str, Any]:
    """The keys joined from ``hosted.json``. All are ``None`` until the run is synced.

    A file whose ``sha`` is not the run's pushed commit describes another
    commit, so it is ignored.
    """
    pushed = as_str(record.get("pushed_sha"))
    if not isinstance(hosted, Mapping) or not pushed or hosted.get("sha") != pushed:
        hosted = {}
    pr = as_dict(hosted.get("pr"))
    checks = as_dict(hosted.get("checks")) or {}
    return {
        "pr_number": as_int(pr.get("number")) if pr else None,
        "pr_state": as_str(pr.get("state")) if pr else None,
        "merged": bool(as_str(pr.get("merged_at"))) if pr else None,
        "checks_conclusion": as_str(checks.get("conclusion")),
        "fetched_at": as_str(hosted.get("fetched_at")),
    }


def _hosted(record: Mapping[str, Any], hosted: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The GitHub outcome that ``telemetry sync-hosted`` joined, or nulls before a sync."""
    return _synced(record, hosted)


def summarize(
    run: RunDoc | Mapping[str, Any],
    findings: Sequence[Finding | Mapping[str, Any]] | None,
    events: Sequence[Mapping[str, Any]] | None,
    repo: RepoIdentity,
    *,
    written_at: str | None = None,
    hosted: Mapping[str, Any] | None = None,
) -> RunSummary:
    """Summarize one run record from code-derived facts.

    ``run`` is a loaded run document or the raw ``run.json`` object of any
    schema version. ``findings`` and ``events`` are ``None`` when they are not
    available, and the fields derived from them are then ``None`` too.
    ``hosted`` is the run's ``hosted.json`` object, when ``sync-hosted`` wrote one.
    Raises :class:`pydantic.ValidationError` when the record lacks a field that
    every summary requires.
    """
    record = _as_record(run)
    state, orphaned_reason = effective_state(record)
    snapshot = as_dict(record.get("config_snapshot")) or {}
    intent = record.get("intent")
    created_at = as_str(record.get("created_at"))
    finished_at = _finished_at(record, events)
    run_timing = timing.state_timing(events, state)
    complete = bool(run_timing and run_timing["complete"])
    return RunSummary.model_validate(
        {
            "written_at": written_at or utc_now(),
            "run_id": record.get("run_id"),
            "state": state,
            "record_schema_version": as_int(record.get("schema_version")),
            "repo": repo,
            "branch": record.get("branch"),
            "base_ref": record.get("base_ref"),
            "head_sha": record.get("head_sha"),
            "pushed_sha": as_str(record.get("pushed_sha")),
            "intent_sha256": sha256_text(intent) if isinstance(intent, str) and intent else None,
            "intent_source": record.get("intent_source"),
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_seconds": _duration(created_at, as_str(finished_at)),
            "timing": run_timing,
            "producer": _producer(run, record),
            "risk": _risk(record),
            "review": _review(record),
            "stages": _stages(record, events, complete=complete),
            "findings": _findings_summary(findings, snapshot),
            "fix_commits": _length(record.get("fix_commits")),
            "validation_restarts": as_int(record.get("validation_restarts")),
            "event_counts": _event_counts(events),
            "terminal": _terminal(state, orphaned_reason, events),
            "hosted": _hosted(record, hosted),
            "worktree_mode": as_str((as_dict(snapshot.get("worktree")) or {}).get("mode")),
            "pr_mode": as_str((as_dict(snapshot.get("pr")) or {}).get("mode")),
            "config_digest": record.get("config_digest"),
        }
    )
