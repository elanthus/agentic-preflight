"""Attestation schema 8: producer, the events anchor, and transcript digests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_preflight import attestation as attestationmod
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import EventsAnchor, Stage, TranscriptDigest
from agentic_preflight.records import transcripts_from_events
from agentic_preflight.store import CHAIN_KEY, Store, anchor_break, event_chain, events_anchor
from tests.attestation_helpers import make_attestation
from tests.conftest import configure_base, git
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json
from tests.test_models import _attestation_stages


def _store(repo: Path) -> Store:
    return Store(repo / ".git" / "agentic-preflight")


def _events_path(repo: Path, run_id: str) -> Path:
    return _store(repo).events_path(run_id)


def _content_lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().split(b"\n") if line.strip()]


def _to_test_green(agent: ScriptedAgent, tmp_path: Path, transcript: bytes | None = None) -> str:
    run_id = agent.run("start", "--client", "claude-code", "--model", "claude-test")["run_id"]
    agent.run("context")
    submit = ["submit-findings", "--file", findings_json(tmp_path, [])]
    if transcript is not None:
        notes = tmp_path / "review-notes.txt"
        notes.write_bytes(transcript)
        submit += ["--transcript", str(notes)]
    agent.run(*submit)
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    return run_id


@pytest.fixture
def noted(feature_repo, tmp_path) -> tuple[ScriptedAgent, str]:
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = _to_test_green(agent, tmp_path, transcript=b"the later transcript wins")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    return agent, run_id


# -- what the note records ------------------------------------------------------


def test_the_note_carries_producer_events_anchor_and_transcripts(noted, feature_repo):
    _, run_id = noted
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    note = json.loads(
        git("notes", f"--ref={attestationmod.NOTES_REF}", "show", head, cwd=feature_repo)
    )

    assert note["schema_version"] == 8
    assert note["producer"]["client"] == "claude-code"
    assert note["producer"]["client_source"] == "flag"
    assert note["producer"]["reported_model"] == "claude-test"
    assert note["producer"]["reported_model_source"] == "flag"
    assert note["records"] is None
    later = b"the later transcript wins"
    assert note["transcripts"] == {
        "review": {"sha256": hashlib.sha256(later).hexdigest(), "bytes": len(later)}
    }

    # The anchor is the log as it stood when the note was built: a prefix of it now.
    lines = _content_lines(_events_path(feature_repo, run_id))
    anchor = note["events"]
    assert 0 < anchor["lines"] < len(lines)
    assert anchor["chained_lines"] == anchor["lines"]
    assert anchor["head_sha256"] == hashlib.sha256(lines[anchor["lines"] - 1]).hexdigest()
    names = [json.loads(line)["event"] for line in lines[anchor["lines"] :]]
    assert "mergeback_ok" in names

    assert attestationmod.verify(feature_repo, head).producer is not None


def test_the_producer_stays_out_of_every_evidence_digest(noted, feature_repo):
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    value = attestationmod.read(feature_repo, head)
    assert value is not None
    evidence = json.dumps([item.model_dump(mode="json") for item in value.evidence.values()])
    assert "claude-code" not in evidence
    assert "claude-test" not in evidence
    assert "claude-code" not in json.dumps(value.config_snapshot)


def test_a_schema_7_note_is_rejected_as_incompatible():
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload["schema_version"] = 7
    for key in ("producer", "events", "transcripts", "records"):
        payload.pop(key)

    with pytest.raises(attestationmod.InvalidAttestation) as error:
        attestationmod.decode(json.dumps(payload))
    assert error.value.reason == "incompatible_schema"


@pytest.mark.parametrize("key", ["producer", "events", "transcripts", "records"])
def test_a_schema_8_note_must_hold_every_new_key(key):
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload.pop(key)

    with pytest.raises(attestationmod.InvalidAttestation) as error:
        attestationmod.decode(json.dumps(payload))
    assert error.value.reason == "invalid_evidence"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("events", {"head_sha256": None, "lines": 2, "chained_lines": 0}),
        ("events", {"head_sha256": "a" * 64, "lines": 1, "chained_lines": 2}),
        ("events", {"head_sha256": "short", "lines": 1, "chained_lines": 1}),
        ("transcripts", {"lint": {"sha256": "a" * 64, "bytes": 1}}),
        ("transcripts", {"review": {"sha256": "a" * 64, "bytes": -1}}),
        ("records", {"commit": "abc"}),
        ("producer", {"tool_version": "1", "python_version": "3", "platform": "x", "client": "c"}),
    ],
)
def test_malformed_new_fields_fail_validation(key, value):
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload[key] = value
    with pytest.raises(attestationmod.InvalidAttestation):
        attestationmod.decode(json.dumps(payload))


def _submitted(stage: str, fill: str | None = None, **transcript: object) -> dict:
    """A ``findings_submitted`` event, with the transcript that came with it, if any."""
    named = None if fill is None else {"sha256": fill * 64, "bytes": 1, **transcript}
    return {"event": "findings_submitted", "stage": stage, "transcript": named}


def test_a_stage_takes_the_transcript_of_its_last_submission():
    first_pass = [_submitted("review", "a")]
    assert transcripts_from_events(first_pass) == {
        Stage.REVIEW: TranscriptDigest(sha256="a" * 64, bytes=1)
    }
    # A later pass without a transcript, or a review command, replaces the review that
    # the first transcript described, so the note names none.
    assert transcripts_from_events([*first_pass, _submitted("review")]) == {}
    # A later pass with its own transcript names that one.
    assert transcripts_from_events([*first_pass, _submitted("review", "b")]) == {
        Stage.REVIEW: TranscriptDigest(sha256="b" * 64, bytes=1)
    }
    # Another stage's submission does not clear it.
    assert transcripts_from_events([*first_pass, _submitted("docs")]) == {
        Stage.REVIEW: TranscriptDigest(sha256="a" * 64, bytes=1)
    }
    assert transcripts_from_events([]) == {}


def test_a_transcript_whose_submission_never_closed_names_nothing():
    recorded = {
        "event": "transcript_recorded",
        "stage": "review",
        "sha256": "a" * 64,
        "bytes": 1,
        "source": "flag",
    }
    # The submission was interrupted before its closing event. A later review command
    # submits with no transcript, and must not be credited with the stray one.
    assert transcripts_from_events([recorded]) == {}
    assert transcripts_from_events([recorded, _submitted("review")]) == {}


@pytest.mark.parametrize(
    "event",
    [
        _submitted("review", "a", sha256="not hex"),
        _submitted("review", "a", bytes=True),
        _submitted("review", "a", bytes=-1),
        _submitted("lint", "a"),
        {"event": "findings_submitted", "stage": "review", "transcript": "a" * 64},
        {"event": "findings_submitted", "stage": "review"},
    ],
)
def test_a_malformed_transcript_entry_names_nothing(event):
    assert transcripts_from_events([event]) == {}


# -- the anchor -----------------------------------------------------------------


def _log(store: Store, run_id: str, count: int) -> bytes:
    for number in range(count):
        store.append_event(run_id, {"event": f"e{number}"})
    return store.read_event_bytes(run_id)


def test_the_anchor_covers_a_prefix_that_later_lines_extend(tmp_path):
    store = Store(tmp_path / "store")
    content = _log(store, "r_1", 3)
    anchor = events_anchor(content)
    assert (anchor.lines, anchor.chained_lines) == (3, 3)

    longer = _log(store, "r_1", 2)
    assert anchor_break(longer, anchor) is None
    assert event_chain(longer, anchor).status == "intact"


def test_the_anchor_detects_a_removed_tail(tmp_path):
    store = Store(tmp_path / "store")
    content = _log(store, "r_1", 4)
    anchor = events_anchor(content)
    lines = content.split(b"\n")
    shortened = b"\n".join(lines[:2]) + b"\n"

    # The chain alone cannot see it: what is left still links up.
    assert event_chain(shortened).status == "intact"
    found = anchor_break(shortened, anchor)
    assert found is not None
    assert (found.line, found.reason) == (3, "anchor_mismatch")
    assert event_chain(shortened, anchor).status == "broken"


def test_the_anchor_detects_a_log_rewritten_from_an_earlier_line(tmp_path):
    store = Store(tmp_path / "store")
    content = _log(store, "r_1", 3)
    anchor = events_anchor(content)

    # Rewrite the second event and re-link everything after it, so the chain holds.
    rewritten = Store(tmp_path / "other")
    for number in range(3):
        rewritten.append_event("r_1", {"event": "changed" if number == 1 else f"e{number}"})
    forged = rewritten.read_event_bytes("r_1")
    assert event_chain(forged).status == "intact"

    found = anchor_break(forged, anchor)
    assert found is not None
    assert (found.line, found.reason) == (3, "anchor_mismatch")


def test_the_anchor_detects_a_log_stripped_of_its_links(tmp_path):
    store = Store(tmp_path / "store")
    content = _log(store, "r_1", 2)
    anchor = events_anchor(content)
    stripped = []
    for line in content.split(b"\n"):
        if line.strip():
            event = json.loads(line)
            del event[CHAIN_KEY]
            stripped.append(json.dumps(event, sort_keys=True).encode())
    assert event_chain(b"\n".join(stripped)).status == "unchained"
    assert anchor_break(b"\n".join(stripped), anchor) is not None


def test_an_empty_anchor_matches_any_log(tmp_path):
    store = Store(tmp_path / "store")
    empty = EventsAnchor(head_sha256=None, lines=0, chained_lines=0)
    assert anchor_break(b"", empty) is None
    assert anchor_break(_log(store, "r_1", 1), empty) is None


def test_events_verify_reports_a_removed_tail_once_a_note_exists(noted, feature_repo):
    agent, run_id = noted
    assert agent.run("events", "--verify")["data"]["chain"]["status"] == "intact"
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    anchor = attestationmod.read(feature_repo, head).events  # type: ignore[union-attr]

    path = _events_path(feature_repo, run_id)
    lines = _content_lines(path)
    path.write_bytes(b"\n".join(lines[: anchor.lines - 1]) + b"\n")

    env = agent.run("events", "--verify", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "event_log_broken"
    assert env["data"]["chain"]["breaks"] == [{"line": anchor.lines, "reason": "anchor_mismatch"}]
    assert env["next"]["command"] == "agentic-preflight abort"


@pytest.mark.parametrize("failure", [OSError("notes ref unreadable"), subprocess.SubprocessError()])
def test_a_note_that_cannot_be_read_leaves_the_chain_checked_on_its_own(
    noted, monkeypatch, failure
):
    agent, _run_id = noted

    def unreadable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(attestationmod, "read", unreadable)

    # An unreadable note says nothing about this log, so it gives no anchor. It must
    # not crash the command with an unmapped error.
    chain = agent.run("events", "--verify")["data"]["chain"]
    assert chain["status"] == "intact"


def test_events_verify_reports_a_rewritten_earlier_line_once_a_note_exists(noted, feature_repo):
    agent, run_id = noted
    path = _events_path(feature_repo, run_id)
    lines = _content_lines(path)

    # Rewrite the first event and re-link every later line, as a deliberate forger would.
    relinked: list[bytes] = []
    for number, line in enumerate(lines):
        event = json.loads(line)
        if number == 0:
            event["to"] = "FORGED"
        event[CHAIN_KEY] = None if not relinked else hashlib.sha256(relinked[-1]).hexdigest()
        relinked.append(json.dumps(event, sort_keys=True).encode())
    path.write_bytes(b"\n".join(relinked) + b"\n")
    assert event_chain(path.read_bytes()).status == "intact"

    env = agent.run("events", "--verify", expect=ExitCode.PRECONDITION)
    assert [item["reason"] for item in env["data"]["chain"]["breaks"]] == ["anchor_mismatch"]


def test_a_note_from_another_run_does_not_anchor_this_log(noted, feature_repo):
    agent, _ = noted
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    value = attestationmod.read(feature_repo, head)
    assert value is not None
    other = value.model_copy(
        update={
            "run_id": "r_someoneelse",
            "events": EventsAnchor(head_sha256="f" * 64, lines=1, chained_lines=1),
        }
    )
    git(
        "notes",
        f"--ref={attestationmod.NOTES_REF}",
        "add",
        "-f",
        "-m",
        attestationmod.encode(other),
        head,
        cwd=feature_repo,
    )
    assert agent.run("events", "--verify")["data"]["chain"]["status"] == "intact"
