"""What a schema 8 note records about a run beyond its stage evidence.

That is the transcript digests a note names, and the opt-in records bundle: one
parentless Git commit that holds the intent text, the stage logs, the reviewer
transcripts, and a redacted copy of the event log, which the note pins by commit
SHA (``records.commit``) and ``push`` publishes as an evidence ref.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import gitx, public_config
from .models import (
    TRANSCRIPT_STAGES,
    Attestation,
    EventsAnchor,
    Producer,
    RunDoc,
    Stage,
    TranscriptDigest,
)
from .store import CHAIN_KEY, Store, anchored_prefix

#: The event that closes an accepted review or docs submission. Its ``transcript``
#: key names the transcript that came with that submission, or holds ``None``.
SUBMISSION_EVENT = "findings_submitted"


def transcripts_from_events(events: list[dict[str, Any]]) -> dict[Stage, TranscriptDigest]:
    """The transcript that came with each stage's last accepted submission.

    Only a stage's last ``findings_submitted`` event decides, through its
    ``transcript`` key (``sha256`` and ``bytes``). A later submission without a
    transcript, or a review command, which never has one, leaves the stage with
    none, so a note never names the notes of a review pass that a later pass
    replaced. A ``transcript_recorded`` event on its own names nothing: a
    transcript whose submission never closed belongs to no accepted review.

    A malformed digest or size is treated as no transcript, so a note never names
    one it cannot describe.
    """
    stages = {item.value for item in TRANSCRIPT_STAGES}
    named: dict[Stage, TranscriptDigest] = {}
    for event in events:
        stage = event.get("stage")
        if event.get("event") != SUBMISSION_EVENT or stage not in stages:
            continue
        came_with = _transcript_digest(event.get("transcript"))
        if came_with is None:
            named.pop(Stage(stage), None)
        else:
            named[Stage(stage)] = came_with
    return dict(sorted(named.items()))


def _transcript_digest(value: object) -> TranscriptDigest | None:
    if not isinstance(value, dict):
        return None
    digest = value.get("sha256")
    size = value.get("bytes")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if type(size) is not int or size < 0:
        return None
    return TranscriptDigest(sha256=digest, bytes=size)


# -- the records bundle ---------------------------------------------------------

#: What a bundle's ``manifest.json`` names itself.
BUNDLE_KIND = "agentic-preflight-records"
BUNDLE_SCHEMA_VERSION: Final = 1

#: The identity of every bundle commit. Neutral on purpose: a bundle never carries
#: the user's Git identity. ``.invalid`` is reserved and never resolves.
BUNDLE_NAME = "agentic-preflight"
BUNDLE_EMAIL = "records@agentic-preflight.invalid"

MANIFEST_PATH = "manifest.json"
INTENT_PATH = "intent.txt"
EVENTS_PATH = "events.jsonl"

#: Stages whose stored log a bundle can hold, in the order a note lists them.
LOG_STAGES = (Stage.REVIEW, Stage.DOCS, Stage.LINT, Stage.TEST)

#: What the redacted event copy publishes in place of a value that names a local path.
REDACTED_PATH = "[redacted local path]"
#: What it publishes in place of a line that is not a JSON object, such as a torn append.
TORN_LINE: dict[str, Any] = {"torn_line": True}

OmissionReason = Literal[
    "user_level_command",
    "evidence_reused",
    "log_unavailable",
    "log_digest_mismatch",
    "transcript_unavailable",
    "transcript_digest_mismatch",
]


def log_path(stage: Stage) -> str:
    """Where a bundle holds a stage's stored log."""
    return f"logs/{stage.value}.txt"


def transcript_path(stage: Stage) -> str:
    """Where a bundle holds a stage's reviewer transcript."""
    return f"logs/{stage.value}-transcript.txt"


def transcript_file(store: Store, run_id: str, stage: Stage) -> Path:
    """Where a run keeps a stage's reviewer transcript locally."""
    return store.logs_dir(run_id) / f"{stage.value}-transcript.txt"


class FileDigest(BaseModel):
    """The SHA-256 and byte size of one bundle file."""

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0, strict=True)

    @classmethod
    def of(cls, data: builtins.bytes) -> FileDigest:
        return cls(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


class Omission(BaseModel):
    """A file the bundle could have held and does not, and why."""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: OmissionReason


class EventsCopy(BaseModel):
    """How ``events.jsonl`` relates to the run's original log."""

    model_config = ConfigDict(extra="forbid")

    #: Each original line up to the anchor becomes one line, with only allowlisted keys.
    redacted: Literal[True] = True
    #: ``prev_sha256`` values are the original's, so they do not match the copy's lines.
    chain_verifiable: Literal[False] = False


class RecordsManifest(BaseModel):
    """``manifest.json``: what a records bundle holds and what it binds to."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agentic-preflight-records"] = "agentic-preflight-records"
    schema_version: Literal[1]
    run_id: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    producer: Producer | None
    events: EventsAnchor
    events_copy: EventsCopy
    files: dict[str, FileDigest]
    omitted: list[Omission]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_integer(cls, value: object) -> object:
        if type(value) is not int or value != BUNDLE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be the JSON integer {BUNDLE_SCHEMA_VERSION}")
        return value


# -- the redacted event copy ----------------------------------------------------

#: Keys every published event keeps, whatever its name.
_COMMON_KEYS = frozenset({"event", "at", "seq"})

#: For each known event, the keys beyond ``event``, ``at``, and ``seq`` that may be
#: published. Any other key, and every key of an event not listed here, stays local.
#: Left out on purpose: local paths (``path``, ``log_path``, ``worktree_path``,
#: ``paths``, ``summary_path``, ``mirror_path``), the full configuration digest,
#: remote names, error text, setup commands, and monotonic clock readings
#: (``monotonic_ns``). A command's ``seconds`` is published: the ``at`` of its start
#: and finish events already show how long it ran.
EVENT_KEYS: dict[str, frozenset[str]] = {
    "state_transition": frozenset({"from", "to", "action"}),
    "run_created": frozenset(
        {"head_sha", "config_snapshot", "intent", "intent_source", "producer"}
    ),
    "worktree_ready": frozenset({"mode", "sync", "risk"}),
    "tool_command_started": frozenset({"command"}),
    "tool_command_finished": frozenset({"command", "seconds"}),
    "findings_submitted": frozenset({"stage", "count", "coverage", "risk", "transcript"}),
    "finding_resolved": frozenset({"id", "action", "commit", "reason", "risk"}),
    "review_finished": frozenset({"exit_code"}),
    "lint_finished": frozenset({"exit_code"}),
    "test_finished": frozenset({"exit_code"}),
    "review_interrupted": frozenset({"attempts"}),
    "docs_interrupted": frozenset({"attempts"}),
    "lint_interrupted": frozenset({"attempts"}),
    "test_interrupted": frozenset({"attempts"}),
    "docs_skipped": frozenset({"reason"}),
    "test_skipped": frozenset({"reason", "changed_files"}),
    "stage_reused": frozenset({"stage", "origin_run_id", "origin_sha", "refreshed_at"}),
    "stage_fix_commits_registered": frozenset({"stage", "commits"}),
    "validated_fix_commits_registered": frozenset({"recorded", "commits"}),
    "review_coverage_invalidated": frozenset({"reviewed_head", "current_head"}),
    "findings_reopened": frozenset({"findings"}),
    "review_contracts_checked": frozenset({"manifest", "receipt_sha256", "status"}),
    "review_compared": frozenset(),
    "reviewer_reported": frozenset(
        {"stage", "source", "provider", "model", "effort", "prompts", "prompts_omitted"}
    ),
    "reviewer_report_ignored": frozenset({"stage", "source", "reason"}),
    "transcript_recorded": frozenset({"stage", "sha256", "bytes", "source"}),
    "mergeback_review_reset": frozenset(
        {"post_sha", "verified_tree_sha", "resolved_tree_sha", "counted_restart"}
    ),
    "mergeback_conflict": frozenset(
        {"mode", "pre_sha", "conflicting_commit", "conflicting_files", "fix_commits", "base_sha"}
    ),
    "mergeback_fixes_unmerged": frozenset({"post_sha", "missing_fix_commits"}),
    "mergeback_ok": frozenset({"post_sha", "tree_equivalent"}),
    "gate_minted": frozenset({"sha", "commits", "published_notes", "risk"}),
    "push_authorized": frozenset({"sha", "authorization", "authorization_source"}),
    "pushed": frozenset({"sha"}),
    "finished": frozenset(),
    "aborted": frozenset({"forced", "reason"}),
}

#: The keys of ``worktree_ready``'s ``sync`` object that may be published. The
#: remote name is left out: it can be a URL.
_SYNC_KEYS = frozenset({"base_ref", "base_sha", "head_before", "head_after", "rebased", "merged"})

_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _local_markers(run: RunDoc, store: Store) -> list[str]:
    """Local strings that must not appear in a published value."""
    markers = {
        str(store.root),
        str(Path.home()),
        run.source_worktree_path,
        run.worktree_path or "",
        run.gate_token or "",
    }
    with suppress(OSError):
        markers.add(str(store.root.resolve()))
        markers.add(str(Path.home().resolve()))
    return sorted((item for item in markers if len(item) > 1), key=len, reverse=True)


#: ``~``, ``~/x``, or ``~name/x``. Text such as ``~10% off`` is not a path.
_HOME_RELATIVE = re.compile(r"^~[^/\\\s]*(?:[/\\]|$)")
#: Published keys that hold free text, which the bundle also publishes as written
#: (``intent.txt``). Only the marker match applies to them, so text that merely
#: starts like a path, such as ``/api/users returns 500``, survives.
_FREE_TEXT_KEYS = frozenset({"intent"})


def _names_local_path(value: str) -> bool:
    return (
        value.startswith("/")
        or bool(_HOME_RELATIVE.match(value))
        or bool(_WINDOWS_ABSOLUTE.match(value))
    )


def _scrub(value: Any, markers: list[str], *, free_text: bool = False) -> Any:
    """Replace every string that names a local path, or holds a marker, recursively.

    A marker (the store, the home directory, a worktree, the gate token) redacts
    any value that holds it. The shape rule, an absolute or home-relative path,
    applies to every value but those under :data:`_FREE_TEXT_KEYS`.
    """
    if isinstance(value, str):
        if any(marker in value for marker in markers):
            return REDACTED_PATH
        if not free_text and _names_local_path(value):
            return REDACTED_PATH
        return value
    if isinstance(value, list):
        return [_scrub(item, markers, free_text=free_text) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub(item, markers, free_text=key in _FREE_TEXT_KEYS)
            for key, item in value.items()
        }
    return value


def _published_event(event: dict[str, Any], run: RunDoc, note: Attestation) -> dict[str, Any]:
    name = event.get("event")
    keys = _COMMON_KEYS | (
        EVENT_KEYS.get(name, frozenset()) if isinstance(name, str) else frozenset()
    )
    published: dict[str, Any] = {}
    for key in sorted(keys & set(event)):
        value = event[key]
        if name == "run_created" and key == "config_snapshot":
            # The note's snapshot, without user-level sections.
            value = public_config.published_snapshot(run)
        elif name == "run_created" and key == "producer":
            value = None if note.producer is None else note.producer.model_dump(mode="json")
        elif name == "worktree_ready" and key == "sync" and isinstance(value, dict):
            value = {item: value[item] for item in sorted(_SYNC_KEYS & set(value))}
        published[key] = value
    return published


def redact_events(lines: list[bytes], run: RunDoc, note: Attestation, store: Store) -> bytes:
    """The published copy of an event log's anchored lines, one line for each.

    Each event keeps only the keys :data:`EVENT_KEYS` allows for its name, and an
    unknown event keeps ``event``, ``at``, and ``seq``. The configuration
    snapshot becomes the note's published snapshot. Any string that names an
    absolute or home-relative path, the run store, the worktrees, or the gate
    token becomes :data:`REDACTED_PATH`. ``prev_sha256`` keeps the original's
    value, so a holder of the original log can line the copy up with it; the copy
    cannot be chain-verified on its own.
    """
    markers = _local_markers(run, store)
    out: list[bytes] = []
    for raw in lines:
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            # As the store reads it: an oversized integer raises a plain ValueError
            # and deep nesting a RecursionError, and either marks a torn line.
            event = None
        if not isinstance(event, dict):
            published: dict[str, Any] = dict(TORN_LINE)
        else:
            published = _scrub(_published_event(event, run, note), markers)
            if CHAIN_KEY in event:
                link = event[CHAIN_KEY]
                ok = link is None or (isinstance(link, str) and re.fullmatch(r"[0-9a-f]{64}", link))
                published[CHAIN_KEY] = link if ok else None
        out.append(json.dumps(published, sort_keys=True, ensure_ascii=True).encode() + b"\n")
    return b"".join(out)


# -- building -------------------------------------------------------------------


def _git_date(green_at: str | None) -> str:
    """The bundle commit date: the moment the run went green, in UTC."""
    moment = datetime.fromisoformat(green_at) if green_at else datetime(1970, 1, 1, tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"@{int(moment.timestamp())} +0000"


def _stage_files(run: RunDoc, note: Attestation) -> tuple[dict[str, bytes], list[Omission]]:
    files: dict[str, bytes] = {}
    omitted: list[Omission] = []
    for stage in LOG_STAGES:
        attested = note.stages[stage]
        if attested.output_sha256 is None:
            continue
        path = log_path(stage)
        if note.evidence[stage].origin.run_id != run.run_id:
            omitted.append(Omission(path=path, reason="evidence_reused"))
            continue
        if attested.command == public_config.REDACTED_COMMAND:
            omitted.append(Omission(path=path, reason="user_level_command"))
            continue
        record = run.stages.get(stage)
        source = Path(record.log_path) if record is not None and record.log_path else None
        try:
            data = source.read_bytes() if source is not None else None
        except OSError:
            data = None
        if data is None:
            omitted.append(Omission(path=path, reason="log_unavailable"))
        elif hashlib.sha256(data).hexdigest() != attested.output_sha256:
            omitted.append(Omission(path=path, reason="log_digest_mismatch"))
        else:
            files[path] = data
    return files, omitted


def _transcript_files(
    store: Store, run: RunDoc, note: Attestation
) -> tuple[dict[str, bytes], list[Omission]]:
    files: dict[str, bytes] = {}
    omitted: list[Omission] = []
    for stage, digest in note.transcripts.items():
        path = transcript_path(stage)
        try:
            data: bytes | None = transcript_file(store, run.run_id, stage).read_bytes()
        except OSError:
            data = None
        if data is None:
            omitted.append(Omission(path=path, reason="transcript_unavailable"))
        elif FileDigest.of(data) != FileDigest(sha256=digest.sha256, bytes=digest.bytes):
            omitted.append(Omission(path=path, reason="transcript_digest_mismatch"))
        else:
            files[path] = data
    return files, omitted


def _write_files(repo: Path | str, files: dict[str, bytes]) -> str:
    """Store ``files`` as a tree of at most two levels and return its object ID."""
    top: list[tuple[str, str, str, str]] = []
    nested: dict[str, list[tuple[str, str, str, str]]] = {}
    for path in sorted(files):
        oid = gitx.write_blob(repo, files[path])
        folder, _, name = path.rpartition("/")
        if folder:
            nested.setdefault(folder, []).append(("100644", "blob", oid, name))
        else:
            top.append(("100644", "blob", oid, name))
    for folder, entries in sorted(nested.items()):
        top.append(("040000", "tree", gitx.write_tree(repo, entries), folder))
    return gitx.write_tree(repo, top)


def build_bundle(
    repo: Path | str, store: Store, run: RunDoc, note: Attestation, event_log: bytes
) -> str:
    """Create the records bundle commit for a note that is about to be written.

    ``event_log`` must be the bytes the note's ``events`` anchor was taken from.
    Returns the parentless commit; the caller sets the note's ``records`` and
    the note write retains it as an evidence ref.
    """
    intent = (run.intent or "").encode()
    if hashlib.sha256(intent).hexdigest() != note.intent_sha256:
        raise _mismatch("the run's intent does not match the note's intent digest")
    files, omitted = _stage_files(run, note)
    transcripts, missing = _transcript_files(store, run, note)
    files.update(transcripts)
    omitted.extend(missing)
    files[INTENT_PATH] = intent
    files[EVENTS_PATH] = redact_events(anchored_prefix(event_log, note.events), run, note, store)
    manifest = RecordsManifest(
        schema_version=BUNDLE_SCHEMA_VERSION,
        run_id=run.run_id,
        sha=note.sha,
        tree_sha=note.tree_sha,
        producer=note.producer,
        events=note.events,
        events_copy=EventsCopy(),
        files={path: FileDigest.of(data) for path, data in sorted(files.items())},
        omitted=omitted,
    )
    files[MANIFEST_PATH] = _canonical(manifest.model_dump(mode="json"))
    tree = _write_files(repo, files)
    return gitx.commit_tree(
        repo,
        tree,
        f"agentic-preflight records for run {run.run_id}\n",
        name=BUNDLE_NAME,
        email=BUNDLE_EMAIL,
        date=_git_date(note.green_at),
    )


def _canonical(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


# -- verifying ------------------------------------------------------------------

_ALLOWED_PATHS = frozenset(
    {MANIFEST_PATH, INTENT_PATH, EVENTS_PATH}
    | {log_path(stage) for stage in LOG_STAGES}
    | {transcript_path(stage) for stage in TRANSCRIPT_STAGES}
)


def _mismatch(message: str) -> Exception:
    from .attestation import InvalidAttestation

    return InvalidAttestation(f"records bundle: {message}", reason="records_mismatch")


def _bundle_files(repo: Path | str, commit: str) -> dict[str, bytes]:
    """The files of a bundle commit, refusing any shape a bundle cannot have."""
    if gitx.commit_parents(repo, commit):
        raise _mismatch("the bundle commit has parents")
    blobs: dict[str, str] = {}
    for mode, kind, oid, path in gitx.tree_entries(repo, commit):
        if kind != "blob" or mode != "100644" or path not in _ALLOWED_PATHS:
            raise _mismatch(f"unexpected entry {path!r}")
        blobs[path] = oid
    for path in (MANIFEST_PATH, INTENT_PATH, EVENTS_PATH):
        if path not in blobs:
            raise _mismatch(f"{path} is missing")
    return {path: gitx.read_blob(repo, oid) for path, oid in blobs.items()}


def _check_manifest(value: Attestation, data: dict[str, bytes]) -> RecordsManifest:
    """The manifest, once it binds to the note and lists exactly the bundle's files."""
    try:
        manifest = RecordsManifest.model_validate_json(data[MANIFEST_PATH])
    except ValidationError as exc:
        raise _mismatch("manifest.json is not a valid manifest") from exc
    if (manifest.run_id, manifest.sha, manifest.tree_sha) != (
        value.run_id,
        value.sha,
        value.tree_sha,
    ):
        raise _mismatch("the manifest names another run, commit, or tree")
    if manifest.events != value.events:
        raise _mismatch("the manifest's events anchor differs from the note's")
    if manifest.producer != value.producer:
        raise _mismatch("the manifest's producer differs from the note's")
    if set(manifest.files) != set(data) - {MANIFEST_PATH}:
        raise _mismatch("the manifest does not list exactly the files the bundle holds")
    for path, digest in manifest.files.items():
        if FileDigest.of(data[path]) != digest:
            raise _mismatch(f"{path} does not match its manifest digest")
    omitted = [item.path for item in manifest.omitted]
    if len(set(omitted)) != len(omitted) or set(omitted) & set(data):
        raise _mismatch("the manifest lists an omission twice, or omits a file it holds")
    return manifest


def _check_noted(
    path: str, data: dict[str, bytes], omitted: set[str], expected: str | None, what: str
) -> None:
    """A file the note digests is in the bundle with that SHA-256, or listed as omitted."""
    if path in data:
        if expected is None or hashlib.sha256(data[path]).hexdigest() != expected:
            raise _mismatch(f"{path} does not match the note's {what}")
    elif expected is not None and path not in omitted:
        raise _mismatch(f"{path} is neither in the bundle nor listed as omitted")
    elif expected is None and path in omitted:
        raise _mismatch(f"{path} is listed as omitted, but the note has no {what}")


def _check_omissions(value: Attestation, manifest: RecordsManifest, *, required: bool) -> None:
    """Hold each omission to what the note can confirm.

    ``user_level_command`` and ``evidence_reused`` follow from the note itself, so
    a manifest that states either where the note says otherwise does not match.
    The other reasons say only that the producer could not supply the file. A
    voluntary bundle may say so. A bundle that the protected base requires may
    not, or it could leave out every log and still satisfy the policy.
    """
    from .attestation import InvalidAttestation

    logs = {log_path(stage): stage for stage in LOG_STAGES}
    transcripts = {transcript_path(stage) for stage in TRANSCRIPT_STAGES}
    for item in manifest.omitted:
        stage = logs.get(item.path)
        if stage is not None:
            if item.reason == "user_level_command":
                if value.stages[stage].command != public_config.REDACTED_COMMAND:
                    raise _mismatch(
                        f"{item.path} is left out as a user-level command, "
                        "but the note publishes the stage's command"
                    )
                continue
            if item.reason == "evidence_reused":
                if value.evidence[stage].origin.run_id == value.run_id:
                    raise _mismatch(
                        f"{item.path} is left out as reused evidence, "
                        "but this run executed the stage"
                    )
                continue
            if item.reason not in ("log_unavailable", "log_digest_mismatch"):
                raise _mismatch(f"{item.path} is left out for a reason no stage log has")
        elif item.path in transcripts:
            if item.reason not in ("transcript_unavailable", "transcript_digest_mismatch"):
                raise _mismatch(f"{item.path} is left out for a reason no transcript has")
        else:
            raise _mismatch(f"the manifest leaves out {item.path!r}, which no bundle holds")
        if required:
            raise InvalidAttestation(
                f"the protected policy requires records, and the bundle leaves out "
                f"{item.path} ({item.reason})",
                reason="records_required",
            )


def _check_events_copy(value: Attestation, data: bytes) -> None:
    events = [line for line in data.split(b"\n") if line.strip()]
    if len(events) != value.events.lines:
        raise _mismatch("events.jsonl does not hold one line for each anchored event")
    for line in events:
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise _mismatch("events.jsonl holds a line that is not JSON") from exc
        if not isinstance(parsed, dict):
            raise _mismatch("events.jsonl holds a line that is not an object")


def verify_records(
    repo: Path | str, value: Attestation, *, required: bool
) -> dict[str, Any] | None:
    """Check the records bundle a note names against the note, without a fetch of its own.

    ``required`` is the protected base's ``[records] publish``. Returns what was
    checked, or ``None`` when the note names no bundle and none is required.
    """
    from .attestation import InvalidAttestation

    if value.records is None:
        if required:
            raise InvalidAttestation(
                "the protected policy requires a records bundle, and the note names none",
                reason="records_required",
            )
        return None
    commit = value.records.commit
    present = gitx.run(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    if present.returncode:
        raise InvalidAttestation(
            f"records bundle {commit} is not in the repository", reason="missing_records"
        )
    data = _bundle_files(repo, commit)
    manifest = _check_manifest(value, data)
    if hashlib.sha256(data[INTENT_PATH]).hexdigest() != value.intent_sha256:
        raise _mismatch("intent.txt does not match the note's intent digest")
    omitted = {item.path for item in manifest.omitted}
    for stage in LOG_STAGES:
        expected = value.stages[stage].output_sha256
        _check_noted(log_path(stage), data, omitted, expected, f"{stage.value} output digest")
    for stage in sorted(TRANSCRIPT_STAGES):
        noted = value.transcripts.get(stage)
        path = transcript_path(stage)
        _check_noted(
            path, data, omitted, None if noted is None else noted.sha256, "transcript digest"
        )
        if noted is not None and path in data and len(data[path]) != noted.bytes:
            raise _mismatch(f"{path} does not match the note's transcript size")
    _check_omissions(value, manifest, required=required)
    _check_events_copy(value, data[EVENTS_PATH])
    return {
        "commit": commit,
        "required": required,
        "files": sorted(manifest.files),
        "omitted": [item.model_dump(mode="json") for item in manifest.omitted],
    }
