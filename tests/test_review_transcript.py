"""`submit-findings --transcript`: the agent's review notes, kept with the run as a claim."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentic_preflight import attestation
from agentic_preflight.envelope import ExitCode
from agentic_preflight.runs import TRANSCRIPT_MAX_BYTES, open_session, review_compare
from agentic_preflight.runs import review as review_module
from agentic_preflight.runs.review import submit_findings
from tests.conftest import configure_base, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json

COPIED_VALUE = "fixture-value-transcript-5d1e"
DOCS_CONFIG = "[commands]\nlint = 'true'\ntest = 'true'\n"


def _logs(repo: Path, run_id: str) -> Path:
    return open_session(repo, run_id=run_id).store.logs_dir(run_id)


def _named(agent: ScriptedAgent, name: str) -> list[dict]:
    return [event for event in agent.run("events")["data"]["events"] if event["event"] == name]


def _notes(tmp_path: Path, text: str, name: str = "notes.txt") -> str:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def test_a_transcript_is_stored_and_the_event_names_its_digest(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    text = "U0001: read every changed line; the guard covers the empty case.\r\nclean\n"

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, text),
    )

    stored = (_logs(feature_repo, run_id) / "review-transcript.txt").read_bytes()
    assert stored == text.encode("utf-8")
    digest = hashlib.sha256(stored).hexdigest()
    (event,) = _named(agent, "transcript_recorded")
    assert {key: event[key] for key in ("stage", "sha256", "bytes", "source")} == {
        "stage": "review",
        "sha256": digest,
        "bytes": len(stored),
        "source": "flag",
    }
    data = env["data"]
    assert data["transcript_sha256"] == digest
    assert data["transcript_bytes"] == len(stored)
    assert Path(data["transcript_path"]) == _logs(feature_repo, run_id) / "review-transcript.txt"


def test_no_transcript_means_no_file_no_event_and_no_fields(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")

    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    assert not (_logs(feature_repo, run_id) / "review-transcript.txt").exists()
    assert _named(agent, "transcript_recorded") == []
    assert not any(key.startswith("transcript_") for key in env["data"])


def test_a_copied_file_value_is_redacted_in_the_stored_transcript(feature_repo, tmp_path):
    write(feature_repo, ".env", f"SAMPLE_SETTING={COPIED_VALUE}\n")
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, f"The copied file sets {COPIED_VALUE}; not quoted in code.\n"),
    )

    stored = (_logs(feature_repo, run_id) / "review-transcript.txt").read_text(encoding="utf-8")
    assert COPIED_VALUE not in stored
    assert "[redacted]" in stored
    assert env["data"]["transcript_sha256"] == hashlib.sha256(stored.encode()).hexdigest()
    assert COPIED_VALUE not in json.dumps(env)


def test_a_transcript_that_redaction_pushes_over_the_limit_is_refused(feature_repo, tmp_path):
    # "zz" is shorter than "[redacted]", so redaction makes this text five times longer.
    write(feature_repo, ".env", "SAMPLE_SETTING=zz\n")
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    text = "zz " * (TRANSCRIPT_MAX_BYTES // 4)
    assert len(text.encode()) < TRANSCRIPT_MAX_BYTES

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, text),
        expect=ExitCode.PRECONDITION,
    )

    assert env["error"]["code"] == "invalid_findings"
    assert "once copied-file values are redacted" in env["error"]["message"]
    assert not (_logs(feature_repo, run_id) / "review-transcript.txt").exists()
    assert agent.run("status")["state"] == "REVIEW_AWAITING_FINDINGS"


def test_a_transcript_is_refused_when_redaction_is_unavailable(feature_repo, tmp_path):
    write(feature_repo, ".env", f"SAMPLE_SETTING={COPIED_VALUE}\n")
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    run_id = started["run_id"]
    agent.run("context")
    (Path(started["data"]["worktree_path"]) / ".env").write_bytes(b"\xff\xfe not text")

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, f"mentions {COPIED_VALUE}\n"),
        expect=ExitCode.PRECONDITION,
    )

    assert env["error"]["code"] == "invalid_findings"
    assert "transcript" in env["error"]["message"]
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert not (_logs(feature_repo, run_id) / "review-transcript.txt").exists()
    assert _named(agent, "transcript_recorded") == []


def _refused_without_change(feature_repo, tmp_path, transcript: Path) -> None:
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    session = open_session(feature_repo, run_id=run_id)
    run_before = session.store.run_path(run_id).read_bytes()
    events_before = session.store.events_path(run_id).read_bytes()

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        str(transcript),
        expect=ExitCode.PRECONDITION,
    )

    assert env["error"]["code"] == "invalid_findings"
    assert "transcript" in env["error"]["message"]
    assert session.store.run_path(run_id).read_bytes() == run_before
    assert session.store.events_path(run_id).read_bytes() == events_before
    assert not (_logs(feature_repo, run_id) / "review-transcript.txt").exists()
    assert agent.run("status")["state"] == "REVIEW_AWAITING_FINDINGS"


def test_an_oversized_transcript_is_refused_and_changes_nothing(feature_repo, tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (TRANSCRIPT_MAX_BYTES + 1))
    _refused_without_change(feature_repo, tmp_path, big)


def test_a_transcript_at_the_limit_is_accepted(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    exact = tmp_path / "exact.txt"
    exact.write_bytes(b"x" * TRANSCRIPT_MAX_BYTES)

    env = agent.run(
        "submit-findings", "--file", findings_json(tmp_path, []), "--transcript", str(exact)
    )

    assert env["data"]["transcript_bytes"] == TRANSCRIPT_MAX_BYTES


def test_a_non_utf8_transcript_is_refused_and_changes_nothing(feature_repo, tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"notes \xff\xfe more")
    _refused_without_change(feature_repo, tmp_path, bad)


def test_a_missing_transcript_file_is_refused_and_changes_nothing(feature_repo, tmp_path):
    _refused_without_change(feature_repo, tmp_path, tmp_path / "absent.txt")


def test_a_rejected_submission_records_no_transcript(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps({"coverage": {"manifest": "0" * 64, "examined": "all"}, "findings": []})
    )

    agent.run(
        "submit-findings",
        "--file",
        str(stale),
        "--transcript",
        _notes(tmp_path, "notes\n"),
        expect=ExitCode.PRECONDITION,
    )

    assert not (_logs(feature_repo, run_id) / "review-transcript.txt").exists()
    assert _named(agent, "transcript_recorded") == []


def test_the_docs_stage_stores_its_own_transcript(feature_repo, tmp_path):
    configure_base(feature_repo, DOCS_CONFIG)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, "review notes\n", "review.txt"),
    )
    agent.run("context", "--section", "docs")
    docs_findings = tmp_path / "docs.json"
    docs_findings.write_text(json.dumps({"findings": []}))

    env = agent.run(
        "submit-findings",
        "--file",
        str(docs_findings),
        "--transcript",
        _notes(tmp_path, "README still matches\n", "docs.txt"),
    )

    logs = _logs(feature_repo, run_id)
    assert (logs / "review-transcript.txt").read_text(encoding="utf-8") == "review notes\n"
    assert (logs / "docs-transcript.txt").read_text(encoding="utf-8") == "README still matches\n"
    assert Path(env["data"]["transcript_path"]) == logs / "docs-transcript.txt"
    assert [event["stage"] for event in _named(agent, "transcript_recorded")] == [
        "review",
        "docs",
    ]


def test_a_later_submission_replaces_the_file_and_appends_an_event(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    run_id = started["run_id"]
    worktree = Path(started["data"]["worktree_path"])
    unit = agent.run("context")["data"]["review_coverage"]["units"][0]
    blocking = {
        "unit": unit["id"],
        "path": unit["path"],
        "severity": "high",
        "action": "auto_fix",
        "title": "Breaks the caller",
    }
    agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, [blocking]),
        "--transcript",
        _notes(tmp_path, "first pass\n", "first.txt"),
    )
    target = worktree / unit["path"]
    target.write_text(target.read_text(encoding="utf-8") + "# fixed\n", encoding="utf-8")
    git("add", unit["path"], cwd=worktree)
    git("commit", "-m", "fix the caller", cwd=worktree)
    fix = git("rev-parse", "HEAD", cwd=worktree)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    assert agent.run("verify")["state"] == "REVIEW_AWAITING_FINDINGS"
    agent.run("context")

    agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, "second pass\n", "second.txt"),
    )

    stored = (_logs(feature_repo, run_id) / "review-transcript.txt").read_text(encoding="utf-8")
    assert stored == "second pass\n"
    events = _named(agent, "transcript_recorded")
    assert len(events) == 2
    assert events[1]["sha256"] == hashlib.sha256(b"second pass\n").hexdigest()


@pytest.mark.parametrize("failure", ["later_step", "transcript_write"])
def test_a_submission_that_fails_partway_keeps_the_transcript_the_log_names(
    feature_repo, tmp_path, monkeypatch, failure
):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    run_id = started["run_id"]
    worktree = Path(started["data"]["worktree_path"])
    unit = agent.run("context")["data"]["review_coverage"]["units"][0]
    blocking = {
        "unit": unit["id"],
        "path": unit["path"],
        "severity": "high",
        "action": "auto_fix",
        "title": "Breaks the caller",
    }
    first = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, [blocking]),
        "--transcript",
        _notes(tmp_path, "first pass\n"),
    )
    target = worktree / unit["path"]
    target.write_text(target.read_text(encoding="utf-8") + "# fixed\n", encoding="utf-8")
    git("add", unit["path"], cwd=worktree)
    git("commit", "-m", "fix the caller", cwd=worktree)
    fix = git("rev-parse", "HEAD", cwd=worktree)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run("verify")
    manifest = agent.run("context")["data"]["review_coverage"]["manifest"]

    # The second submission is accepted, then fails before its closing event: either a
    # later step fails, or the transcript itself cannot be written.
    def failing(*_args, **_kwargs):
        raise OSError("no space left on device")

    if failure == "later_step":
        monkeypatch.setattr(review_compare, "persist_submission", failing)
    else:
        monkeypatch.setattr(review_module, "atomic_write", failing)
    session = open_session(feature_repo, run_id=run_id)
    with pytest.raises(OSError, match="no space left"):
        submit_findings(
            session,
            {"coverage": {"manifest": manifest, "examined": "all"}, "findings": []},
            transcript="second pass\n",
        )

    # The stored file still matches the transcript that the log names for the review.
    stored = (_logs(feature_repo, run_id) / "review-transcript.txt").read_bytes()
    assert stored == b"first pass\n"
    named = [event for event in _named(agent, "findings_submitted") if event["stage"] == "review"]
    assert named[-1]["transcript"]["sha256"] == first["data"]["transcript_sha256"]
    assert hashlib.sha256(stored).hexdigest() == first["data"]["transcript_sha256"]
    assert len(_named(agent, "transcript_recorded")) == 1


def test_the_note_does_not_name_a_transcript_that_a_later_review_replaced(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    worktree = Path(started["data"]["worktree_path"])
    unit = agent.run("context")["data"]["review_coverage"]["units"][0]
    blocking = {
        "unit": unit["id"],
        "path": unit["path"],
        "severity": "high",
        "action": "auto_fix",
        "title": "Breaks the caller",
    }
    agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, [blocking]),
        "--transcript",
        _notes(tmp_path, "first pass, before the fix\n"),
    )
    target = worktree / unit["path"]
    target.write_text(target.read_text(encoding="utf-8") + "# fixed\n", encoding="utf-8")
    git("add", unit["path"], cwd=worktree)
    git("commit", "-m", "fix the caller", cwd=worktree)
    fix = git("rev-parse", "HEAD", cwd=worktree)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run("verify")
    agent.run("context")
    # The second review, the one the run's coverage reflects, hands over no notes.
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    # The log names a transcript for the first pass and none for the second, so a
    # reader takes the stage's transcript from its last submission: none.
    named = [event for event in _named(agent, "findings_submitted") if event["stage"] == "review"]
    assert (
        named[0]["transcript"]["sha256"]
        == hashlib.sha256(b"first pass, before the fix\n").hexdigest()
    )
    assert named[-1]["transcript"] is None


def test_the_note_omits_the_transcript_and_the_chain_stays_intact(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    submitted = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        "--transcript",
        _notes(tmp_path, "examined U0001 completely\n"),
    )
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"

    head = git("rev-parse", "feature/x", cwd=feature_repo)
    note = git("notes", f"--ref={attestation.NOTES_REF}", "show", head, cwd=feature_repo)
    assert submitted["data"]["transcript_sha256"] not in note
    assert "transcript" not in note
    assert "examined U0001" not in note
    chain = agent.run("events", "--verify")["data"]["chain"]
    assert chain["status"] == "intact"
    assert chain["breaks"] == []
