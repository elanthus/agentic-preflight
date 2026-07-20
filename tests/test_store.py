import json

import pytest

from agentic_cli.machine import State
from agentic_cli.models import RunDoc
from agentic_cli.store import StaleWrite, Store, UnknownRun


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-cli")


def make_run(run_id="r_abc123"):
    return RunDoc(
        run_id=run_id,
        state=State.CREATED,
        branch="feature/x",
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
    )


def test_created_run_round_trips(store):
    store.create_run(make_run())
    assert store.load_run("r_abc123").branch == "feature/x"


def test_loading_an_unknown_run_raises(store):
    with pytest.raises(UnknownRun):
        store.load_run("r_nope")


def test_transaction_bumps_seq_and_persists_the_mutation(store):
    store.create_run(make_run())
    with store.transaction("r_abc123") as run:
        run.state = State.WORKTREE_READY

    reloaded = store.load_run("r_abc123")
    assert reloaded.state is State.WORKTREE_READY
    assert reloaded.seq == 1


def test_transaction_with_a_stale_expect_seq_is_rejected(store):
    store.create_run(make_run())
    with store.transaction("r_abc123") as run:
        run.state = State.WORKTREE_READY

    with pytest.raises(StaleWrite) as exc:
        with store.transaction("r_abc123", expect_seq=0) as run:
            run.state = State.ABORTED
    assert "expected seq 0" in str(exc.value)
    assert store.load_run("r_abc123").state is State.WORKTREE_READY


def test_a_failed_transaction_body_leaves_the_document_untouched(store):
    store.create_run(make_run())
    with pytest.raises(RuntimeError):
        with store.transaction("r_abc123") as run:
            run.state = State.ABORTED
            raise RuntimeError("agent exploded mid-mutation")

    reloaded = store.load_run("r_abc123")
    assert reloaded.state is State.CREATED
    assert reloaded.seq == 0


def test_a_crash_during_replace_leaves_a_valid_document(store, monkeypatch):
    """Atomicity: os.replace failing must never produce a half-written run.json."""
    store.create_run(make_run())

    def boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr("agentic_cli.store.os.replace", boom)
    with pytest.raises(OSError):
        with store.transaction("r_abc123") as run:
            run.state = State.ABORTED
    monkeypatch.undo()

    raw = json.loads(store.run_path("r_abc123").read_text())
    assert raw["state"] == "CREATED"
    assert store.load_run("r_abc123").seq == 0


def test_no_temp_files_are_left_behind(store):
    store.create_run(make_run())
    with store.transaction("r_abc123") as run:
        run.state = State.WORKTREE_READY
    leftovers = list(store.run_dir("r_abc123").glob("*.tmp*"))
    assert leftovers == []


def test_current_run_pointer_round_trips(store):
    store.create_run(make_run())
    store.set_current("r_abc123")
    assert store.get_current() == "r_abc123"


def test_current_is_none_when_never_set(store):
    assert store.get_current() is None
