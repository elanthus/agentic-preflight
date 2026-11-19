"""Assemble one run's audit trail as plain data, for the ``audit`` command.

:func:`build_audit` reads the run record, its findings, its event log, and the
attestation note on its head, and returns a dictionary that
:mod:`.audit_html` renders. It never changes the run and appends no event.

The report is meant to be shared, so by default :func:`build_audit` leaves out
what describes one machine rather than the change: absolute local paths, the
configuration sections that came from the user configuration file, and the
commands those sections define. It uses the same rules as the published note
(:mod:`agentic_preflight.public_config`). ``local_details=True`` keeps them.

Everything that came from the repository or the agent stays untrusted text in
the returned data. Escaping it is the renderer's job.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError

from .. import attestation, gitx, telemetry
from ..machine import TERMINAL_STATES
from ..models import Finding, FindingStatus, RunDoc, Stage, StageRecord
from ..public_config import (
    REDACTED_COMMAND,
    provenance_known,
    published_command,
    withheld_sections,
)
from ..store import EventChain, StoreError

if TYPE_CHECKING:
    from ..runs import Session

#: The version of the dictionary :func:`build_audit` returns.
AUDIT_VERSION = 1

#: The file ``audit`` writes inside the run's directory when no ``--output`` is given.
DEFAULT_FILENAME = "audit.html"

#: What replaces a local value in the shareable report.
WITHHELD_PATH = "[local path withheld]"
WITHHELD_CONFIG = "[withheld: the run does not record which sections came from user config]"

STAGE_ORDER = (Stage.REVIEW, Stage.DOCS, Stage.LINT, Stage.TEST)

PROVES = (
    "The review executor accounted for every included review unit of a diff manifest "
    "bound to the reviewed commit. Excluded files stay outside that coverage.",
    "Each green lint and test command exited zero against the commit it recorded, and "
    "the digest of its redacted output is kept.",
    "Every finding was resolved by a recorded decision: a fix commit, a dismissal, or an "
    "accepted risk, each with its stated reason.",
    "A skipped stage records why it was skipped, not that it succeeded.",
)

DOES_NOT_PROVE = (
    "That the review was good, or that the agent understood every unit it marked clean. "
    "The same diff reviewed twice can yield different findings.",
    "Who acted. The client, the agent's model, the reviewer's model, and the push "
    "authorization are self-reported claims, marked as such below.",
    "That the record is authentic. Neither this report nor the attestation note is "
    "signed; anyone who can update the notes ref can replace a note.",
    "That the event log is complete. Its hash chain catches a partial edit, but not a "
    "removed tail or a log rewritten from the edit to its end, because nothing outside "
    "the log records its last digest.",
    "Anything CI or a human reviewer would check. Treat this as an audit trail, not a "
    "replacement for either.",
)

_CHAIN_BANNERS: dict[str, tuple[str, str, str]] = {
    "intact": (
        "ok",
        "Event log links intact",
        "Every line of the event log names the digest of the line before it, and each "
        "of those links matches. An edit, a removal, or an insertion between two lines "
        "would break a link. This does not show that the log is complete: a line "
        "removed from the end, or a deliberate rewrite of the whole log, leaves every "
        "link matching.",
    ),
    "broken": (
        "bad",
        "Event log broken",
        "Some lines of the event log do not follow from the line before them, so its "
        "history was changed after it was written. Do not rely on this record; see "
        "the integrity section for each break.",
    ),
    "unchained": (
        "warn",
        "Event log not chained",
        "The event log carries no hash chain. An earlier release wrote it, or the chain "
        "was stripped from every line; the two cannot be told apart, so edits to this "
        "log cannot be detected.",
    ),
    "empty": (
        "warn",
        "No event log",
        "The run has no event log, or it was deleted. The timeline below is empty and "
        "nothing here can be checked against the history.",
    ),
    "unreadable": (
        "bad",
        "Event log unreadable",
        "The event log exists but could not be read, so its chain could not be checked.",
    ),
}


def tool_version() -> str:
    try:
        return distribution_version("agentic-preflight")
    except PackageNotFoundError:
        return "unknown"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _duration(start: object, end: object) -> float | None:
    began, ended = _parse_time(start), _parse_time(end)
    if began is None or ended is None or ended < began:
        return None
    return (ended - began).total_seconds()


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _subject(repo: Path, sha: str) -> str | None:
    try:
        return gitx.commit_subject(repo, sha) or None
    except (gitx.GitError, OSError, subprocess.SubprocessError):
        return None


# -- sections ---------------------------------------------------------------


def _chain(session: Session, run_id: str) -> dict[str, Any]:
    try:
        chain: EventChain = session.store.verify_events(run_id)
    except OSError as exc:
        data: dict[str, Any] = {"status": "unreadable", "error": str(exc), "breaks": []}
    else:
        data = chain.as_dict()
    tone, headline, detail = _CHAIN_BANNERS[data["status"]]
    data["banner"] = {"tone": tone, "headline": headline, "detail": detail}
    return data


def _events(session: Session, run_id: str) -> tuple[list[dict[str, Any]], int, str | None]:
    try:
        log = session.store.read_events(run_id)
    except OSError as exc:
        return [], 0, str(exc)
    return log.events, log.skipped_lines, None


def _findings(session: Session, run_id: str) -> tuple[list[Finding], str | None]:
    try:
        findings = session.store.load_findings(run_id)
        path = session.store.findings_path(run_id)
        # The store iterates whatever the file holds, so an empty object reads as no
        # findings. Only a list is a findings file.
        if path.exists() and not isinstance(json.loads(path.read_bytes()), list):
            raise ValueError("the file does not hold a list of findings")
    except (OSError, StoreError, ValidationError, ValueError, TypeError, RecursionError) as exc:
        # TypeError: the file holds JSON the store cannot iterate, such as null.
        return [], f"findings.json could not be read: {exc}"
    return findings, None


def _claim(label: str, value: object, source: str | None) -> dict[str, Any]:
    return {"label": label, "value": value, "kind": "claimed", "source": source}


def _measure(label: str, value: object, source: str = "code") -> dict[str, Any]:
    return {"label": label, "value": value, "kind": "measured", "source": source}


def _producer(run: RunDoc) -> dict[str, Any]:
    producer = run.producer
    if producer is None:
        return {
            "recorded": False,
            "items": [],
            "note": "Not recorded: the run predates producer identity.",
        }
    items = [
        _measure("Agentic Preflight version", producer.tool_version),
        _measure("Python", producer.python_version),
        _measure("Platform", producer.platform),
    ]
    if producer.client is not None:
        items.append(_claim("Coding agent", producer.client, producer.client_source))
    else:
        items.append(_claim("Coding agent", None, None))
    items.append(_claim("Agent's model", producer.reported_model, producer.reported_model_source))
    return {"recorded": True, "items": items, "note": None}


def _reviewers(run: RunDoc, events: list[dict[str, Any]]) -> dict[str, Any]:
    record = run.stages.get(Stage.REVIEW)
    executor = record.executor if record else None
    reports: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "reviewer_reported":
            continue
        source = event.get("source")
        report = {
            "items": [
                _claim("Provider", event.get("provider"), source),
                _claim("Model", event.get("model"), source),
                _claim("Effort", event.get("effort"), source),
            ],
            "prompts": [p for p in _list(event.get("prompts")) if isinstance(p, dict)],
            "prompts_omitted": event.get("prompts_omitted") or 0,
        }
        # A review that ran again with the same reviewer reports the same claim.
        same = next((item for item in reports if item["claim"] == report), None)
        if same is not None:
            same["at"].append(event.get("at"))
        else:
            reports.append({**report, "claim": report, "at": [event.get("at")]})
    for item in reports:
        del item["claim"]
    ignored = [
        {"at": event.get("at"), "reason": event.get("reason")}
        for event in events
        if event.get("event") == "reviewer_report_ignored"
    ]
    return {
        "executor": _measure("Review executor", executor),
        "reports": reports,
        "ignored": ignored,
    }


def _authorizations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "at": event.get("at"),
            "sha": event.get("sha"),
            "claim": _claim(
                "Push authorized by", event.get("authorization"), event.get("authorization_source")
            ),
        }
        for event in events
        if event.get("event") == "push_authorized"
    ]


def _command_seconds(events: list[dict[str, Any]]) -> dict[str, tuple[float, object]]:
    """The wall time of the last run of each timed command, and when it finished."""
    seconds: dict[str, tuple[float, object]] = {}
    for event in events:
        value = event.get("seconds")
        if event.get("event") == "tool_command_finished" and isinstance(value, int | float):
            seconds[str(event.get("command"))] = (float(value), event.get("at"))
    return seconds


def _attempt_seconds(record: StageRecord, timed: tuple[float, object] | None) -> float | None:
    """The command time of the attempt the record shows, or ``None``.

    A stage can be recorded again without its command running, for example when a
    retry fails during baseline setup. The last timed run then belongs to an
    earlier attempt, so it is shown only when it finished after this attempt began.
    """
    if timed is None or not record.command:
        return None
    seconds, finished_at = timed
    started, finished = _parse_time(record.started_at), _parse_time(finished_at)
    if started is None or finished is None or finished < started:
        return None
    return seconds


def _stage(
    run: RunDoc,
    stage: Stage,
    record: StageRecord | None,
    seconds: dict[str, tuple[float, object]],
) -> dict[str, Any]:
    if record is None:
        return {"name": stage.value, "status": "not_reached", "reached": False}
    evidence = run.evidence.get(stage)
    reused_from = None
    if evidence is not None and evidence.origin.run_id != run.run_id:
        reused_from = {"run_id": evidence.origin.run_id, "head_sha": evidence.origin.head_sha}
    return {
        "name": stage.value,
        "reached": True,
        "status": record.status.value,
        "executor": record.executor,
        "command": record.command,
        "exit_code": record.exit_code,
        "output_sha256": record.output_sha256,
        "attempts": record.attempts,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_seconds": _duration(record.started_at, record.finished_at),
        "command_seconds": _attempt_seconds(record, seconds.get(stage.value)),
        "reason": record.reason,
        "head_sha": record.head_sha,
        "log_path": record.log_path,
        "reused_from": reused_from,
    }


def _finding_rows(
    repo: Path, findings: list[Finding], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    resolved_at: dict[str, Any] = {}
    for event in events:
        if event.get("event") == "finding_resolved" and isinstance(event.get("id"), str):
            resolved_at[event["id"]] = event.get("at")
    rows = []
    for finding in findings:
        location = finding.path + (f":{finding.line}" if finding.line else "")
        rows.append(
            {
                "id": finding.id,
                "stage": finding.stage.value,
                "severity": finding.severity.value,
                "action": finding.action.value,
                "location": location,
                "title": finding.title,
                "detail": finding.detail,
                "suggestion": finding.suggestion,
                "status": finding.status.value,
                "code_owned": finding.code_owned,
                "fix_commit": finding.fix_commit,
                "fix_subject": _subject(repo, finding.fix_commit) if finding.fix_commit else None,
                "reason": finding.response_reason,
                "note": finding.response_note,
                "resolved_at": resolved_at.get(finding.id),
            }
        )
    return rows


def _attestation(repo: Path, run: RunDoc) -> dict[str, Any]:
    sha = run.pushed_sha or run.head_sha
    data: dict[str, Any] = {"sha": sha, "present": False, "error": None}
    try:
        value = attestation.read(repo, sha)
    except attestation.InvalidAttestation as exc:
        # A note that cannot be read cannot verify: that is a failed check, not a
        # check that could not run.
        data.update(
            present=True,
            valid=False,
            error=str(exc),
            verification={"ok": False, "reason": exc.reason, "detail": str(exc)},
        )
        return data
    except (gitx.GitError, OSError, subprocess.SubprocessError) as exc:
        data.update(present=None, error=f"the notes ref could not be read: {exc}")
        return data
    if value is None:
        return data
    data.update(
        present=True,
        valid=True,
        schema_version=value.schema_version,
        outcome=value.outcome,
        run_id=value.run_id,
        same_run=value.run_id == run.run_id,
        green_at=value.green_at,
        config_sha256=value.config_sha256,
        findings_summary=value.findings_summary,
        stages={stage.value: item.status for stage, item in value.stages.items()},
        verification=_verification(repo, sha, value),
    )
    return data


def _verification(repo: Path, sha: str, value: attestation.Attestation) -> dict[str, Any]:
    """Whether this note verifies offline in this clone, as ``verify SHA`` decides.

    Reading a note shows only that it is well formed. Verification also binds it
    to the commit's tree, the merge base, and the policy committed on the base
    branch. The note that was read is the one that is verified, so the fields the
    report shows and the verdict describe the same note even if the notes ref
    moves meanwhile. ``ok`` is ``None`` when the check could not run.
    """
    try:
        attestation.verify_value(repo, value, gitx.rev_parse(repo, sha), purpose="local")
    except attestation.InvalidAttestation as exc:
        return {"ok": False, "reason": exc.reason, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a report must render whatever the check hits
        return {"ok": None, "reason": "not_checked", "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "reason": None, "detail": None}


def _publication(repo: Path, run: RunDoc, events: list[dict[str, Any]]) -> dict[str, Any]:
    gates = []
    for event in events:
        if event.get("event") != "gate_minted":
            continue
        commits = _list(event.get("commits"))
        gates.append(
            {
                "at": event.get("at"),
                "sha": event.get("sha"),
                "commits": [
                    {"sha": sha, "subject": _subject(repo, sha)}
                    for sha in commits
                    if isinstance(sha, str)
                ],
                "published_notes": event.get("published_notes") or [],
                "risk": event.get("risk"),
            }
        )
    pushes = [
        {"at": event.get("at"), "sha": event.get("sha")}
        for event in events
        if event.get("event") == "pushed"
    ]
    return {
        "attestation": _attestation(repo, run),
        "gates": gates,
        "pushes": pushes,
        "pushed_sha": run.pushed_sha,
        "fix_commits": [{"sha": sha, "subject": _subject(repo, sha)} for sha in run.fix_commits],
    }


# -- timeline ---------------------------------------------------------------


def _short(value: object) -> str:
    return value[:12] if isinstance(value, str) else "?"


def _count(value: object, noun: str) -> str:
    number = value if isinstance(value, int) else 0
    return f"{number} {noun}{'' if number == 1 else 's'}"


_DESCRIBE: dict[str, Callable[[dict[str, Any]], str]] = {
    "state_transition": lambda e: (
        f"Run created in {e.get('to')}"
        if e.get("from") is None
        else f"{e.get('from')} to {e.get('to')} ({e.get('action')})"
    ),
    "run_created": lambda e: f"Run created at head {_short(e.get('head_sha'))}",
    "worktree_ready": lambda e: f"Validation worktree ready ({e.get('mode')} mode)",
    "findings_submitted": lambda e: (
        f"{_count(e.get('count'), 'finding')} submitted for {e.get('stage')}"
    ),
    "finding_resolved": lambda e: (
        f"{e.get('id')} marked {e.get('action')}"
        + (f" ({e.get('reason')})" if e.get("reason") else "")
        + (f" by {_short(e.get('commit'))}" if e.get("commit") else "")
    ),
    "findings_reopened": lambda e: "Findings reopened",
    "reviewer_reported": lambda e: (
        f"Reviewer reported {e.get('provider')} / {e.get('model')} (self-reported)"
    ),
    "reviewer_report_ignored": lambda e: f"Reviewer report ignored: {e.get('reason')}",
    "review_coverage_invalidated": lambda e: "Review coverage invalidated",
    "docs_skipped": lambda e: f"Docs stage skipped ({e.get('reason')})",
    "test_skipped": lambda e: f"Test stage skipped ({e.get('reason')})",
    "stage_reused": lambda e: f"{e.get('stage')} evidence reused from run {e.get('origin_run_id')}",
    "mergeback_ok": lambda e: "Verified fixes merged back",
    "mergeback_conflict": lambda e: "Merge-back stopped on a conflict",
    "gate_minted": lambda e: (
        f"Gate listed {_count(len(e.get('commits') or []), 'commit')} for {_short(e.get('sha'))}"
    ),
    "push_authorized": lambda e: (
        f"Push authorized: {e.get('authorization') or 'no authorization stated'}"
        + (" (self-reported)" if e.get("authorization") else "")
    ),
    "pushed": lambda e: f"Pushed {_short(e.get('sha'))}",
    "finished": lambda e: "Run finished",
    "aborted": lambda e: f"Run aborted ({e.get('reason') or 'no reason given'})",
    "orphaned": lambda e: f"Run orphaned ({e.get('reason')})",
    "setup_failed": lambda e: f"Setup failed (exit {e.get('exit_code')})",
    "sync_conflict": lambda e: "Base synchronization stopped on a conflict",
    "sync_failed": lambda e: "Base synchronization failed",
    "remote_timeout": lambda e: "A remote operation timed out",
    "tool_command_started": lambda e: f"Started the {e.get('command')} command",
    "tool_command_finished": lambda e: (
        f"Finished the {e.get('command')} command in {e.get('seconds')} s"
    ),
}

_CATEGORIES: dict[str, str] = {
    "state_transition": "state",
    "run_created": "state",
    "findings_submitted": "finding",
    "finding_resolved": "finding",
    "findings_reopened": "finding",
    "reviewer_reported": "claim",
    "push_authorized": "claim",
    "gate_minted": "publish",
    "pushed": "publish",
    "finished": "publish",
    "tool_command_started": "trace",
    "tool_command_finished": "trace",
}


def describe_event(event: dict[str, Any]) -> str:
    """One line of plain text that says what an event records."""
    name = event.get("event")
    describe = _DESCRIBE.get(name) if isinstance(name, str) else None
    if describe is None:
        return str(name or "unnamed event").replace("_", " ").capitalize()
    try:
        return describe(event)
    except (TypeError, ValueError, AttributeError):
        return str(name).replace("_", " ").capitalize()


def _timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index, event in enumerate(events, start=1):
        name = event.get("event")
        rows.append(
            {
                "index": index,
                "at": event.get("at"),
                "event": name,
                "category": _CATEGORIES.get(name, "other") if isinstance(name, str) else "other",
                "summary": describe_event(event),
                "raw": event,
            }
        )
    return rows


# -- privacy ----------------------------------------------------------------


def _local_roots(session: Session, run: RunDoc) -> list[tuple[str, str]]:
    candidates: list[tuple[str | Path | None, str]] = [
        (session.store.root, "<run store>"),
        (run.worktree_path, "<validation worktree>"),
        (run.source_worktree_path, "<source worktree>"),
        (session.repo_root, "<repository>"),
        (session.caller_root, "<repository>"),
        (Path.home(), "~"),
    ]
    roots: dict[str, str] = {}
    for value, marker in candidates:
        if not value:
            continue
        path = Path(value)
        for form in {str(path), path.as_posix()} | _resolved_forms(path):
            if len(form) > 1:
                roots.setdefault(form, marker)
    return sorted(roots.items(), key=lambda item: len(item[0]), reverse=True)


def _resolved_forms(path: Path) -> set[str]:
    try:
        resolved = path.resolve()
    except OSError:
        return set()
    return {str(resolved), resolved.as_posix()}


def _user_commands(run: RunDoc) -> set[str]:
    """Command strings that a user-level configuration section defines.

    Every user-level section counts, not only ``[review]`` and ``[commands]``: a
    ``[worktree] setup_command`` reaches the event log when it fails. A run that
    does not record which sections came from the user file treats them all as
    user-level.
    """
    found: set[str] = set()
    sections = (
        set(run.config_snapshot)
        if run.user_config_sections is None
        else set(run.user_config_sections)
    )

    def collect(value: object) -> None:
        if isinstance(value, str) and value.strip():
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for section in sections:
        section_value = run.config_snapshot.get(section)
        if isinstance(section_value, dict):
            for key, item in section_value.items():
                if "command" in key or section == "commands":
                    collect(item)
    for stage, record in run.stages.items():
        if record.command and published_command(run, stage, record.command) != record.command:
            found.add(record.command)
    return found


#: Keys whose values the tool picks from a fixed set: states, statuses, severities,
#: event and stage names, and the renderer's own selectors. They are never free text.
_CONTROL_KEYS = frozenset(
    {
        "action",
        "category",
        "event",
        "executor",
        "from",
        "kind",
        "name",
        "severity",
        "stage",
        "state",
        "status",
        "to",
        "tone",
    }
)
#: The parts of the report that hold the run's own data, and so are redacted in the
#: shareable view. The rest is the tool's own text: the limits, the banner, and the
#: summary counts. Redacting that could only damage it.
_RUN_DATA_SECTIONS = ("run", "actors", "stages", "findings", "timeline", "publication")

#: What the shareable report shows in place of a remote's name inside a ref.
REMOTE_MARKER = "<remote>"
_REMOTE_REF = re.compile(r"refs/remotes/[^/\s]+/")


class _Scrubber:
    """Remove local paths, user-level configuration and commands, and remote names."""

    def __init__(self, session: Session, run: RunDoc) -> None:
        self.roots = _local_roots(session, run)
        self.commands = _user_commands(run)
        self.known = provenance_known(run)
        self.withheld = withheld_sections(run)

    def text(self, value: str) -> str:
        if value in self.commands:
            return REDACTED_COMMAND
        # A longer string, such as an error or a reason, can quote a user-level
        # command, and the command can hold a secret. Longest first, so a command
        # that contains another is replaced whole. A short command also replaces
        # the same text elsewhere: the report redacts too much rather than too little.
        for command in sorted(self.commands, key=len, reverse=True):
            if command and command in value:
                value = value.replace(command, REDACTED_COMMAND)
        for root, marker in self.roots:
            if root in value:
                value = value.replace(root, marker)
        # A remote-tracking ref names the remote. The name is not a URL, but the
        # shareable view withholds remotes, so it does not keep the name either.
        return _REMOTE_REF.sub(f"refs/remotes/{REMOTE_MARKER}/", value)

    def snapshot(self, value: object) -> object:
        if not self.known:
            return WITHHELD_CONFIG
        if not isinstance(value, dict):
            return value
        return {key: self(item) for key, item in value.items() if key not in self.withheld}

    def __call__(self, value: Any, key: str | None = None) -> Any:
        if key == "config_snapshot":
            return self.snapshot(value)
        if key in _CONTROL_KEYS:
            # A value the tool chose from a fixed set. The renderer selects glyphs,
            # labels and styles by it, so redaction must not rewrite it even when a
            # user-level command happens to spell the same word.
            return value
        if isinstance(value, str):
            cleaned = self.text(value)
            if key is not None and key.endswith("path") and _is_absolute(cleaned):
                return WITHHELD_PATH
            return cleaned
        if isinstance(value, dict):
            return {name: self(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [self(item, key) for item in value]
        return value


def _is_absolute(value: str) -> bool:
    return value.startswith(("/", "\\")) or (len(value) > 2 and value[1] == ":")


def _withheld_summary(run: RunDoc) -> list[str]:
    items = ["absolute local paths, shown as <repository>, <run store>, and similar markers"]
    if not provenance_known(run):
        items.append(
            "the configuration snapshot and stage commands, because the run does not "
            "record which configuration sections came from the user config file"
        )
    elif withheld_sections(run):
        sections = ", ".join(f"[{name}]" for name in sorted(withheld_sections(run)))
        items.append(f"user-level configuration sections: {sections}")
    if _user_commands(run):
        items.append(f"commands from user-level configuration, shown as {REDACTED_COMMAND}")
    return items


# -- assembly ---------------------------------------------------------------


def _summary(run: RunDoc, chain: dict[str, Any], findings: list[Finding]) -> dict[str, Any]:
    counts = Counter(finding.status.value for finding in findings)
    return {
        "state": run.state.value,
        "terminal": run.state in TERMINAL_STATES,
        "chain_status": chain["status"],
        "findings": {status.value: counts.get(status.value, 0) for status in FindingStatus},
        "findings_total": len(findings),
    }


def _run_section(session: Session, run: RunDoc) -> dict[str, Any]:
    identity = telemetry.repo_identity(session.repo_root)
    return {
        "repository": identity.name or "repository",
        "remote": identity.remote,
        "run_id": run.run_id,
        "state": run.state.value,
        "branch": run.branch,
        "base_ref": run.base_ref,
        "merge_base_sha": run.merge_base_sha,
        "head_sha": run.head_sha,
        "source_head_sha": run.source_head_sha,
        "pushed_sha": run.pushed_sha,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "stale": run.stale,
        "orphaned_reason": run.orphaned_reason,
        "validation_restarts": run.validation_restarts,
        "intent": run.intent,
        "intent_source": run.intent_source,
        "changed_files": list(run.changed_files),
        "risk": run.risk.model_dump(mode="json") if run.risk else None,
        "review_coverage": run.review_coverage.summary() if run.review_coverage else None,
        "worktree_path": run.worktree_path,
        "source_worktree_path": run.source_worktree_path,
        "config_digest": run.config_digest,
        "config_snapshot": run.config_snapshot,
    }


#: How often the capture is repeated when the run moves while it is being read.
_CAPTURE_ATTEMPTS = 3


class _Capture(NamedTuple):
    """The run record, findings, and event log, read as one state where possible."""

    run: RunDoc
    events: list[dict[str, Any]]
    skipped: int
    events_error: str | None
    findings: list[Finding]
    findings_error: str | None
    chain: dict[str, Any]
    consistent: bool
    attempts: int


def _capture(session: Session, run: RunDoc) -> _Capture:
    """Read the run's three records so that they describe one moment.

    The report can be made while the run is active, and each record is its own
    file. A command that runs in between would leave the report with a finding's
    new status beside the old state, or a chain verdict for a longer log than the
    timeline shows. So the capture is checked: the run record's sequence must not
    have moved, and the chain must count the lines the timeline was read from. If
    either changed, everything is read again, from the newer record. After
    :data:`_CAPTURE_ATTEMPTS` the last capture is used and marked inconsistent,
    and the report says so.

    No store lock is held across the reads, so this never blocks a run command.
    """
    for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
        events, skipped, events_error = _events(session, run.run_id)
        findings, findings_error = _findings(session, run.run_id)
        chain = _chain(session, run.run_id)
        try:
            current = session.store.load_run(run.run_id)
        except (OSError, StoreError):
            current = run
        log_moved = (
            events_error is None
            and isinstance(chain.get("lines"), int)
            and chain["lines"] != len(events) + skipped
        )
        consistent = current.seq == run.seq and not log_moved
        if consistent or attempt == _CAPTURE_ATTEMPTS:
            return _Capture(
                run,
                events,
                skipped,
                events_error,
                findings,
                findings_error,
                chain,
                consistent,
                attempt,
            )
        run = current
    raise AssertionError("unreachable")  # pragma: no cover


def build_audit(
    session: Session,
    run: RunDoc,
    *,
    local_details: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return the run's audit trail as plain data; see the module docstring."""
    capture = _capture(session, run)
    run = capture.run
    events, skipped, events_error = capture.events, capture.skipped, capture.events_error
    findings, findings_error = capture.findings, capture.findings_error
    chain = capture.chain
    repo = session.caller_root
    section = _run_section(session, run)
    data: dict[str, Any] = {
        "audit_version": AUDIT_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "generator": f"agentic-preflight {tool_version()}",
        "local_details": local_details,
        "capture": {"consistent": capture.consistent, "attempts": capture.attempts},
        "withheld": [] if local_details else _withheld_summary(run),
        "run": section,
        "summary": _summary(run, chain, findings),
        "proves": list(PROVES),
        "does_not_prove": list(DOES_NOT_PROVE),
        "actors": {
            "producer": _producer(run),
            "reviewer": _reviewers(run, events),
            "authorizations": _authorizations(events),
        },
        "stages": [
            _stage(run, stage, run.stages.get(stage), _command_seconds(events))
            for stage in STAGE_ORDER
        ],
        "findings": {
            "items": _finding_rows(repo, findings, events),
            "error": findings_error,
        },
        "timeline": {
            "events": _timeline(events),
            "skipped_lines": skipped,
            "error": events_error,
        },
        "chain": chain,
        "publication": _publication(repo, run, events),
    }
    if local_details:
        return data
    scrub = _Scrubber(session, run)
    for stage_row in data["stages"]:
        if stage_row.get("command") is not None:
            stage = Stage(stage_row["name"])
            command = published_command(run, stage, stage_row["command"])
            if not provenance_known(run):
                command = REDACTED_COMMAND
            stage_row["command"] = command
    section["remote"] = None
    for row in data["timeline"]["events"]:
        row["raw"] = _without_remotes(row["raw"])
    for name in _RUN_DATA_SECTIONS:
        data[name] = scrub(data[name])
    if data["chain"].get("error"):
        # An operating-system error can name a local path.
        data["chain"]["error"] = scrub.text(str(data["chain"]["error"]))
    return data


#: Event keys that name a Git remote. A remote can be a URL, and a URL can hold
#: credentials, so the shareable report drops them from the raw events.
_REMOTE_KEYS = frozenset({"remote", "sync_remote", "remote_url"})


def _without_remotes(value: Any) -> Any:
    """A copy of an event without the keys that name a remote, at any depth."""
    if isinstance(value, dict):
        return {
            key: _without_remotes(item) for key, item in value.items() if key not in _REMOTE_KEYS
        }
    if isinstance(value, list):
        return [_without_remotes(item) for item in value]
    return value


def default_output(session: Session, run_id: str) -> Path:
    return session.store.run_dir(run_id) / DEFAULT_FILENAME


def write_report(path: Path, html_text: str) -> int:
    """Write the report and return its size in bytes."""
    from ..store import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, html_text)
    return os.path.getsize(path)
