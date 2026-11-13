"""The store never writes a run or findings record that its own loader rejects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_preflight import runs
from agentic_preflight.envelope import ExitCode
from agentic_preflight.errors import InvalidResponse
from agentic_preflight.findings import RESPONSE_NOTE_LIMIT
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from agentic_preflight.runs import evidence
from agentic_preflight.store import (
    RUN_READ_RECOVERY,
    RUN_READ_RUNNER_RECOVERY,
    RecordValidationError,
    RunReadError,
    Store,
)
from tests.conftest import git, make_run
from tests.driver import ScriptedAgent, findings_json
from tests.test_evidence_refresh import _finish, _prepare, _restack

BLOCKING = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "loud flag is never used",
    }
]


def _state_root(repo: Path) -> Path:
    return (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
        / "agentic-preflight"
    )


def _finding(note: str | None = None) -> Finding:
    return Finding(
        id="F001",
        stage=Stage.REVIEW,
        path="src/app.py",
        severity=Severity.HIGH,
        action=FindingAction.AUTO_FIX,
        title="loud flag is never used",
        response_note=note,
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-preflight")


# -- store round trip ---------------------------------------------------------------


def test_a_transaction_producing_an_invalid_run_raises_and_keeps_the_file(store):
    run = store.create_run(make_run())
    path = store.run_path(run.run_id)
    before = path.read_bytes()

    with pytest.raises(RecordValidationError) as caught, store.transaction(run.run_id) as doc:
        doc.validation_restarts = -1

    assert path.read_bytes() == before
    assert caught.value.path == path
    assert "validation_restarts" in str(caught.value)
    assert store.load_run(run.run_id).seq == run.seq


def test_an_invalid_findings_list_blocks_both_writes_of_a_transaction(store):
    run = store.create_run(make_run())
    store.save_findings(run.run_id, [_finding()])
    run_before = store.run_path(run.run_id).read_bytes()
    findings_before = store.findings_path(run.run_id).read_bytes()
    stored = store.load_findings(run.run_id)
    stored[0].response_note = "x" * (RESPONSE_NOTE_LIMIT + 1)

    with (
        pytest.raises(RecordValidationError) as caught,
        store.transaction(run.run_id, findings=stored) as doc,
    ):
        doc.intent = "changed"

    assert caught.value.path == store.findings_path(run.run_id)
    assert store.run_path(run.run_id).read_bytes() == run_before
    assert store.findings_path(run.run_id).read_bytes() == findings_before


def test_an_invalid_run_blocks_the_findings_write_of_its_transaction(store):
    run = store.create_run(make_run())
    store.save_findings(run.run_id, [_finding()])
    findings_before = store.findings_path(run.run_id).read_bytes()

    with (
        pytest.raises(RecordValidationError),
        store.transaction(run.run_id, findings=[_finding("a valid note")]) as doc,
    ):
        doc.validation_restarts = -1

    assert store.findings_path(run.run_id).read_bytes() == findings_before


def test_save_findings_refuses_a_list_its_loader_would_reject(store):
    run = store.create_run(make_run())
    store.save_findings(run.run_id, [_finding()])
    before = store.findings_path(run.run_id).read_bytes()
    invalid = _finding()
    invalid.response_note = "x" * (RESPONSE_NOTE_LIMIT + 1)

    with pytest.raises(RecordValidationError):
        store.save_findings(run.run_id, [invalid])

    assert store.findings_path(run.run_id).read_bytes() == before
    assert store.load_findings(run.run_id)[0].response_note is None


def test_the_validation_diagnostic_omits_record_values(store):
    run = store.create_run(make_run())
    invalid = _finding()
    invalid.response_note = "DO_NOT_ECHO_RECORD_VALUES" * 200

    with pytest.raises(RecordValidationError) as caught:
        store.save_findings(run.run_id, [invalid])

    assert "DO_NOT_ECHO_RECORD_VALUES" not in str(caught.value)
    assert "response_note" in str(caught.value)


# -- respond --note -----------------------------------------------------------------


@pytest.fixture
def blocked(agent, tmp_path):
    env = agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    return agent, env["run_id"]


def test_an_over_long_note_is_a_usage_error_and_findings_stay_loadable(blocked, feature_repo):
    agent, run_id = blocked
    findings_path = _state_root(feature_repo) / "runs" / run_id / "findings.json"
    before = findings_path.read_bytes()

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "x" * (RESPONSE_NOTE_LIMIT + 1),
        expect=ExitCode.USAGE_ERROR,
    )

    assert env["error"]["code"] == "usage_error"
    assert str(RESPONSE_NOTE_LIMIT) in env["error"]["message"]
    assert findings_path.read_bytes() == before
    status = agent.run("status")
    assert status["state"] == "REVIEW_BLOCKED"
    verified = agent.run("verify", expect=ExitCode.STAGE_FAILED)
    assert verified["error"]["code"] == "stage_failed"


def test_a_note_at_the_limit_is_accepted(blocked):
    agent, _ = blocked

    env = agent.run(
        "respond", "--id", "F001", "--action", "dismissed", "--note", "x" * RESPONSE_NOTE_LIMIT
    )

    assert env["data"]["finding"]["status"] == "dismissed"
    agent.run("status")


def test_the_python_api_refuses_an_over_long_note(blocked, feature_repo, monkeypatch):
    monkeypatch.chdir(feature_repo)
    session = runs.open_session()

    with pytest.raises(InvalidResponse, match=str(RESPONSE_NOTE_LIMIT)):
        runs.respond(
            session,
            finding_id="F001",
            action="dismissed",
            note="x" * (RESPONSE_NOTE_LIMIT + 1),
        )

    (finding,) = session.store.load_findings(session.active_run_id())
    assert finding.status.value == "open"


# -- evidence reuse with a clock behind the original execution ----------------------


class _BehindClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2000, 1, 1, tzinfo=tz or UTC)


def test_reuse_with_a_clock_behind_the_origin_leaves_a_loadable_run(
    feature_repo, tmp_path, monkeypatch
):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    _restack(feature_repo)
    monkeypatch.setattr(evidence, "datetime", _BehindClock)

    refreshed = ScriptedAgent(feature_repo)
    start = refreshed.run("start")

    assert start["data"]["applicability"]["review"]["disposition"] == "reusable"
    status = refreshed.run("status")
    assert status["state"] == start["state"]
    run = Store(_state_root(feature_repo)).load_run(start["run_id"])
    for item in run.evidence.values():
        assert item.refreshed_at is not None
        assert item.refreshed_at >= item.origin.finished_at
    refreshed.run("abort", "--force")


# -- recovering a worktree whose run record is unreadable ---------------------------


def _make_unreadable(repo: Path, run_id: str) -> tuple[Path, bytes]:
    path = _state_root(repo) / "runs" / run_id / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["config_snapshot"]["review"]["unknown_setting"] = "DO_NOT_ECHO_RECORD_VALUES"
    original = json.dumps(raw).encode()
    path.write_bytes(original)
    return path, original


@pytest.mark.parametrize("mode", ["in_place", "strict"])
def test_the_unreadable_record_error_names_the_pointer_that_releases_the_worktree(
    feature_repo, mode
):
    _prepare(feature_repo, mode=mode)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    record, original = _make_unreadable(feature_repo, started["run_id"])
    # Isolated modes also alias the validation worktree to the run.
    pointers = sorted((_state_root(feature_repo) / "active").glob("*.run"))
    assert len(pointers) == (1 if mode == "in_place" else 2)

    for args in [("abort", "--force"), ("start", "--replace")]:
        failure = agent.run(*args, expect=ExitCode.PRECONDITION)
        assert failure["error"]["code"] == "run_record_unreadable"
        assert failure["data"]["path"] == str(record)
        assert failure["data"]["pointer_paths"] == [str(path) for path in pointers]
        for pointer in pointers:
            assert str(pointer) in failure["next"]["instruction"]
        assert failure["next"]["command"] is None
        assert "DO_NOT_ECHO_RECORD_VALUES" not in json.dumps(failure)

    for pointer in pointers:
        pointer.unlink()
    fresh = agent.run("start", "--intent", "start over after an unreadable record")

    assert fresh["run_id"] != started["run_id"]
    assert record.read_bytes() == original
    retained = agent.run("gc")["data"]["retained"]
    assert [item["run_id"] for item in retained] == [started["run_id"]]
    agent.run("abort", "--force")


def test_a_reusable_run_offers_no_pointer_because_the_runner_stays_leased(feature_repo):
    _prepare(feature_repo, mode="reusable")
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    record, original = _make_unreadable(feature_repo, started["run_id"])
    pointers = sorted((_state_root(feature_repo) / "active").glob("*.run"))
    assert pointers

    for args in [("abort", "--force"), ("start", "--replace")]:
        failure = agent.run(*args, expect=ExitCode.PRECONDITION)
        assert failure["error"]["code"] == "run_record_unreadable"
        assert "pointer_paths" not in failure["data"]
        assert failure["next"]["instruction"] == RUN_READ_RUNNER_RECOVERY
        assert failure["next"]["command"] is None

    assert all(pointer.exists() for pointer in pointers)
    assert record.read_bytes() == original
    # Why none is offered: without the pointers, the runner is still leased on the
    # unreadable run's branch, so a fresh reusable run cannot start anyway.
    for pointer in pointers:
        pointer.unlink()
    blocked = agent.run("start", "--intent", "start over", expect=ExitCode.NEEDS_HUMAN)
    assert "still leased" in blocked["error"]["message"]
    assert record.read_bytes() == original


@pytest.mark.parametrize("mode", [None, "reusable", "unexpected"])
def test_no_pointer_is_offered_unless_the_mode_is_known_to_be_releasable(tmp_path, mode):
    pointer = tmp_path / "owner.run"
    error = RunReadError(
        "r_0123456789", tmp_path / "run.json", "unreadable", "x", pointer_paths=[pointer]
    )
    error.worktree_mode = mode

    assert error.recovery() == (RUN_READ_RUNNER_RECOVERY, [])


def test_an_unowned_unreadable_record_keeps_the_plain_recovery(tmp_path):
    error = RunReadError("r_0123456789", tmp_path / "run.json", "invalid", "x")
    error.worktree_mode = "in_place"

    assert error.recovery() == (RUN_READ_RECOVERY, [])


@pytest.mark.parametrize(
    ("worktree_path", "offered"),
    [
        (None, True),
        ("/cache/worktrees/repo-1/r_0123456789", True),
        ("/cache/worktrees/repo-1/runner", False),
        (7, False),
    ],
)
def test_a_record_that_names_the_runner_is_never_offered_pointer_deletion(
    store, worktree_path, offered
):
    run = store.create_run(make_run())
    store.set_active("owner", run.run_id)
    path = store.run_path(run.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["config_snapshot"] = {"worktree": {"mode": "strict"}, "unknown": {}}
    raw["worktree_path"] = worktree_path
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RunReadError) as caught:
        store.load_run(run.run_id)

    instruction, pointers = caught.value.recovery()
    assert pointers == ([store.active_path("owner")] if offered else [])
    assert (instruction == RUN_READ_RUNNER_RECOVERY) is not offered
