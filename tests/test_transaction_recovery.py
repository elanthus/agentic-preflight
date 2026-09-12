"""Failures between local record writes must not split a logical update."""

import json
import subprocess
import sys
import textwrap

import pytest

from agentic_preflight import runs
from agentic_preflight import store as storemod
from agentic_preflight.machine import State
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from agentic_preflight.store import RunReadError, Store
from tests.conftest import make_run
from tests.driver import ScriptedAgent


def _finding():
    return Finding(
        id="F001",
        stage=Stage.REVIEW,
        path="src/app.py",
        severity=Severity.HIGH,
        action=FindingAction.AUTO_FIX,
        title="loud flag is ignored",
    )


@pytest.mark.parametrize("boundary", ["pending-update.json", "findings.json", "run.json", "unlink"])
@pytest.mark.parametrize("reader", ["load_run", "load_findings"])
def test_committed_pair_rolls_forward_after_every_install_boundary(
    tmp_path, monkeypatch, boundary, reader
):
    store = Store(tmp_path)
    store.create_run(make_run())
    original_write = storemod._atomic_write
    original_unlink = type(tmp_path).unlink

    def interrupted_write(path, payload):
        original_write(path, payload)
        if path.name == boundary:
            raise OSError("interrupted after replacement")

    def interrupted_unlink(path, *args, **kwargs):
        if path == store.update_path("r_abc123"):
            raise OSError("interrupted before removing journal")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(storemod, "_atomic_write", interrupted_write)
        if boundary == "unlink":
            fault.setattr(type(tmp_path), "unlink", interrupted_unlink)
        with (
            pytest.raises(OSError, match="interrupted"),
            store.transaction("r_abc123", findings=[_finding()]) as run,
        ):
            run.state = State.REVIEW_BLOCKED

    # A fresh Store represents the next invocation, without in-memory recovery state.
    reopened = Store(tmp_path)
    getattr(reopened, reader)("r_abc123")
    assert reopened.load_run("r_abc123").state is State.REVIEW_BLOCKED
    assert reopened.load_run("r_abc123").seq == 1
    assert reopened.load_findings("r_abc123") == [_finding()]
    assert not reopened.update_path("r_abc123").exists()
    with reopened.transaction("r_abc123", expect_seq=1) as run:
        run.stale = True
    assert reopened.load_run("r_abc123").seq == 2


def test_failure_before_journal_publication_leaves_both_records_unchanged(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run(make_run())
    original = storemod._atomic_write

    def interrupted(path, payload):
        if path == store.update_path("r_abc123"):
            raise OSError("no journal committed")
        original(path, payload)

    monkeypatch.setattr(storemod, "_atomic_write", interrupted)
    with (
        pytest.raises(OSError, match="no journal"),
        store.transaction("r_abc123", findings=[_finding()]) as run,
    ):
        run.state = State.REVIEW_BLOCKED
    assert store.load_run("r_abc123").state is State.CREATED
    assert store.load_findings("r_abc123") == []


def test_process_exit_after_findings_install_recovers_without_finally_blocks(tmp_path):
    store = Store(tmp_path)
    store.create_run(make_run())
    child = textwrap.dedent("""
        import os
        import sys
        from pathlib import Path
        from agentic_preflight import store as module
        from agentic_preflight.machine import State

        original = module._atomic_write
        def stop(path, payload):
            original(path, payload)
            if path.name == "findings.json":
                os._exit(77)
        module._atomic_write = stop
        with module.Store(Path(sys.argv[1])).transaction("r_abc123", findings=[]) as run:
            run.state = State.REVIEW_GREEN
    """)
    result = subprocess.run([sys.executable, "-c", child, str(tmp_path)], capture_output=True)
    assert result.returncode == 77, result.stderr.decode()
    assert json.loads(store.run_path("r_abc123").read_text())["state"] == "CREATED"
    assert store.load_run("r_abc123").state is State.REVIEW_GREEN
    assert not store.update_path("r_abc123").exists()


@pytest.mark.parametrize("damage", ["invalid", "wrong_run", "stale_sequence"])
def test_invalid_journal_is_preserved_without_overwriting_records(tmp_path, damage):
    store = Store(tmp_path)
    current = store.create_run(make_run())
    pending = current.model_copy(update={"seq": 1, "state": State.REVIEW_BLOCKED})
    if damage == "wrong_run":
        pending.run_id = "r_someone_else"
    if damage == "stale_sequence":
        pending.seq = 0
    payload = (
        "not json"
        if damage == "invalid"
        else json.dumps({"run": pending.model_dump(mode="json"), "findings": []})
    )
    store.update_path(current.run_id).write_text(payload)
    with pytest.raises(RunReadError):
        store.load_run(current.run_id)
    assert store.update_path(current.run_id).read_text() == payload
    assert json.loads(store.run_path(current.run_id).read_text())["state"] == "CREATED"
    assert not store.findings_path(current.run_id).exists()


def test_submission_retry_after_findings_write_keeps_one_finding(
    feature_repo, tmp_path, monkeypatch
):
    agent = ScriptedAgent(feature_repo)
    start = agent.run("start")
    agent.run("context")
    payload = tmp_path / "submission.json"
    payload.write_text(
        json.dumps(
            {
                "coverage": {"manifest": "$context", "examined": "all"},
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 1,
                        "severity": "high",
                        "action": "auto_fix",
                        "title": "loud flag is ignored",
                    }
                ],
            }
        )
    )
    original = storemod._atomic_write

    def interrupted(path, contents):
        original(path, contents)
        if path.name == "findings.json":
            raise OSError("interrupted after findings installation")

    with monkeypatch.context() as fault:
        fault.setattr(storemod, "_atomic_write", interrupted)
        agent.run("submit-findings", "--file", str(payload), expect=1)
    # The committed submission is recovered before command eligibility is checked.
    retried = agent.run("submit-findings", "--file", str(payload), expect=3)
    assert retried["state"] == "REVIEW_BLOCKED"
    status = agent.run("status")
    assert status["next"]["command"].startswith("agentic-preflight verify")
    session = runs.open_session(feature_repo)
    findings = session.store.load_findings(start["run_id"])
    assert [(f.id, f.title) for f in findings] == [("F001", "loud flag is ignored")]
    assert status["data"]["findings_summary"]["open"] == 1
