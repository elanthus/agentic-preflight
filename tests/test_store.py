import json

import pytest

from agentic_preflight import store as store_module
from agentic_preflight.machine import State
from agentic_preflight.store import CurrentRunExists, RunReadError, Store, UnknownRun
from tests.conftest import make_run


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-preflight")


def test_created_run_round_trips(store):
    store.create_run(make_run())
    assert store.load_run("r_abc123").branch == "feature/x"


def test_removed_pr_lifecycle_document_is_rejected_as_earlier_release(store):
    store.create_run(make_run())
    raw = json.loads(store.run_path("r_abc123").read_text(encoding="utf-8"))
    raw.update(
        {
            "schema_version": 1,
            "state": "PR_OPEN",
            "pr_url": "https://github.com/owner/repo/pull/1",
            "ci_status": "failed",
        }
    )
    store.run_path("r_abc123").write_text(json.dumps(raw))

    with pytest.raises(RunReadError, match="earlier release") as caught:
        store.load_run("r_abc123")

    assert "associated work have been retained unchanged" in str(caught.value)
    assert "compatible tool version" in str(caught.value)


def test_schema_version_one_document_is_rejected_as_earlier_release(store):
    store.create_run(make_run())
    path = store.run_path("r_abc123")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw))

    with pytest.raises(RunReadError, match="earlier release") as caught:
        store.load_run("r_abc123")

    assert "associated work have been retained unchanged" in str(caught.value)
    assert "compatible tool version" in str(caught.value)


@pytest.mark.parametrize(
    "schema_version",
    [pytest.param("missing", id="missing"), None, True, 2.0, "2", 1, 3],
)
def test_invalid_run_versions_are_classified_and_preserved(store, schema_version):
    run = store.create_run(make_run())
    path = store.run_path(run.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if schema_version == "missing":
        raw.pop("schema_version")
    else:
        raw["schema_version"] = schema_version
    raw["branch"] = "DO_NOT_ECHO_RECORD_VALUES"
    original = json.dumps(raw).encode()
    path.write_bytes(original)

    with pytest.raises(RunReadError) as caught:
        store.load_run(run.run_id)

    assert caught.value.reason == "invalid_or_unsupported_schema"
    assert caught.value.details()["fields"][0]["location"] == ["schema_version"]
    assert "DO_NOT_ECHO_RECORD_VALUES" not in json.dumps(caught.value.details())
    assert path.read_bytes() == original


def test_peek_reads_raw_fields_and_never_raises(store):
    run = store.create_run(make_run("r_peek", branch="feature/peek"))
    assert store.peek(run.run_id, "branch", "missing") == {
        "branch": "feature/peek",
        "missing": None,
    }
    assert store.peek("r_absent", "branch") is None
    store.run_path(run.run_id).write_bytes(b'{"branch": ')
    assert store.peek(run.run_id, "branch") is None
    store.run_path(run.run_id).write_bytes(b'["feature/peek"]')
    assert store.peek(run.run_id, "branch") is None


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


def test_a_failed_transaction_body_leaves_the_document_untouched(store):
    store.create_run(make_run())

    def mutate_then_fail():
        with store.transaction("r_abc123") as run:
            run.state = State.ABORTED
            raise RuntimeError("agent exploded mid-mutation")

    with pytest.raises(RuntimeError, match="agent exploded mid-mutation"):
        mutate_then_fail()

    reloaded = store.load_run("r_abc123")
    assert reloaded.state is State.CREATED
    assert reloaded.seq == 0


def test_a_crash_during_replace_leaves_a_valid_document(store, monkeypatch):
    """Atomicity: os.replace failing must never produce a half-written run.json."""
    store.create_run(make_run())

    def boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr("agentic_preflight.store.os.replace", boom)
    with pytest.raises(OSError, match="disk went away"), store.transaction("r_abc123") as run:
        run.state = State.ABORTED
    monkeypatch.undo()

    raw = json.loads(store.run_path("r_abc123").read_text(encoding="utf-8"))
    assert raw["state"] == "CREATED"
    assert store.load_run("r_abc123").seq == 0


def test_no_temp_files_are_left_behind(store):
    store.create_run(make_run())
    with store.transaction("r_abc123") as run:
        run.state = State.WORKTREE_READY
    leftovers = list(store.run_dir("r_abc123").glob("*.tmp*"))
    assert leftovers == []


def test_active_run_pointer_round_trips(store):
    store.create_run(make_run())
    store.set_active("worktree-a", "r_abc123")
    assert store.get_active("worktree-a") == "r_abc123"


def test_active_is_none_when_never_set(store):
    assert store.get_active("worktree-a") is None


def test_worktree_run_lease_can_only_be_claimed_once(store):
    store.claim_active("worktree-a", "r_first")

    with pytest.raises(CurrentRunExists) as exc:
        store.claim_active("worktree-a", "r_second")

    assert exc.value.run_id == "r_first"
    assert store.get_active("worktree-a") == "r_first"


def test_failed_start_cannot_clear_another_runs_lease(store):
    store.claim_active("worktree-a", "r_winner")

    assert store.clear_active_if("worktree-a", "r_loser") is False
    assert store.get_active("worktree-a") == "r_winner"
    assert store.clear_active_if("worktree-a", "r_winner") is True
    assert store.get_active("worktree-a") is None


def test_different_worktrees_can_claim_runs_concurrently(store):
    store.claim_active("worktree-a", "r_first")
    store.claim_active("worktree-b", "r_second")

    assert store.list_active() == {
        "worktree-a": "r_first",
        "worktree-b": "r_second",
    }


@pytest.mark.parametrize(
    "kind",
    ["unknown_field", "invalid_value", "malformed_json", "invalid_record", "invalid_encoding"],
)
def test_unreadable_run_errors_are_classified_without_values(store, kind):
    from agentic_preflight.store import RunReadError
    from tests.conftest import unreadable_run_bytes

    run = make_run()
    store.create_run(run)
    path = store.run_path(run.run_id)
    original = unreadable_run_bytes(run, kind)
    path.write_bytes(original)
    with pytest.raises(RunReadError) as caught:
        store.load_run(run.run_id)
    details = caught.value.details()
    assert details["reason"] == (
        "invalid_or_unsupported_schema" if kind in {"unknown_field", "invalid_value"} else kind
    )
    assert details["run_id"] == run.run_id
    assert details["path"] == str(path)
    assert "DO_NOT_ECHO_RECORD_VALUES" not in json.dumps(details)
    if kind == "unknown_field":
        assert details["fields"] == [
            {
                "location": ["review_coverage", "future_coverage_revision"],
                "category": "extra_forbidden",
            }
        ]
    assert path.read_bytes() == original


def test_inventory_preserves_record_when_stat_is_denied(store, monkeypatch):
    from pathlib import Path

    store.create_run(make_run())
    path = store.run_path("r_abc123")
    stat = Path.stat

    def denied(record, *args, **kwargs):
        if record == path:
            raise PermissionError(13, "private record value")
        return stat(record, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    assert store.list_runs() == ["r_abc123"]


@pytest.mark.parametrize("inventory", ["list_runs", "list_active"])
def test_global_inventory_io_failure_is_not_an_empty_store(store, monkeypatch, inventory):
    from pathlib import Path

    def denied(path):
        raise PermissionError(13, "inventory denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        getattr(store, inventory)()


def test_unexpected_parser_bug_is_not_an_unreadable_record(store, monkeypatch):
    store.create_run(make_run())

    def bug(payload):
        raise RuntimeError("parser bug")

    monkeypatch.setattr("agentic_preflight.store._parse_run", bug)
    with pytest.raises(RuntimeError, match="parser bug"):
        store.load_run("r_abc123")


@pytest.mark.parametrize("payload", ["null", "42", '"a string"', "[]"])
def test_scalar_and_array_json_roots_are_controlled_errors(store, payload):
    from agentic_preflight.store import RunReadError

    store.create_run(make_run())
    store.run_path("r_abc123").write_text(payload)
    with pytest.raises(RunReadError) as rejected:
        store.load_run("r_abc123")
    assert rejected.value.reason == "invalid_record"


def test_a_torn_final_event_line_is_dropped(store):
    store.create_run(make_run())
    store.append_event("r_abc123", {"event": "one"})
    store.append_event("r_abc123", {"event": "two"})
    with open(store.events_path("r_abc123"), "a", encoding="utf-8") as handle:
        handle.write('{"event": "thr')
    assert [event["event"] for event in store.load_events("r_abc123")] == ["one", "two"]


def test_a_torn_earlier_event_line_is_a_read_error(store):
    store.create_run(make_run())
    store.events_path("r_abc123").write_text('{"event": "on\n{"event": "two"}\n', encoding="utf-8")
    with pytest.raises(RunReadError) as caught:
        store.load_events("r_abc123")
    assert caught.value.reason == "invalid_events"


def test_a_final_event_line_cut_inside_a_multibyte_character_is_dropped(store):
    store.create_run(make_run())
    store.append_event("r_abc123", {"event": "one"})
    store.append_event("r_abc123", {"event": "two"})
    with open(store.events_path("r_abc123"), "ab") as handle:
        handle.write(b'{"event": "caf' + "\u20ac".encode("utf-8")[:2])
    assert [event["event"] for event in store.load_events("r_abc123")] == ["one", "two"]


def test_invalid_utf8_on_an_earlier_event_line_is_a_read_error(store):
    store.create_run(make_run())
    store.events_path("r_abc123").write_bytes(b'{"event": "\xff"}\n{"event": "two"}\n')
    with pytest.raises(RunReadError) as caught:
        store.load_events("r_abc123")
    assert caught.value.reason == "invalid_events"


def test_append_event_waits_for_the_events_lock(store):
    import threading

    from agentic_preflight import filelock

    store.create_run(make_run())
    store.append_event("r_abc123", {"event": "one"})
    appended = threading.Event()

    def append() -> None:
        store.append_event("r_abc123", {"event": "two"})
        appended.set()

    with filelock.exclusive(store.run_dir("r_abc123") / ".events.lock"):
        worker = threading.Thread(target=append)
        worker.start()
        assert not appended.wait(0.5)
        assert [event["event"] for event in store.load_events("r_abc123")] == ["one"]
    worker.join(timeout=10)
    assert appended.is_set()
    assert [event["event"] for event in store.load_events("r_abc123")] == ["one", "two"]


@pytest.mark.parametrize("failures", [0, 1, 2, 3])
def test_windows_replace_retries_permission_failures(tmp_path, monkeypatch, failures):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new")
    target.write_text("old")
    replace = store_module.os.replace
    attempts = 0
    pauses = []
    error = PermissionError("destination held open")

    def replace_with_contention(tmp, path):
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise error
        replace(tmp, path)

    monkeypatch.setattr(store_module.sys, "platform", "win32")
    monkeypatch.setattr(store_module.os, "replace", replace_with_contention)
    monkeypatch.setattr(store_module.time, "sleep", pauses.append)
    if failures == 3:
        with pytest.raises(PermissionError) as caught:
            store_module._replace(source, target)
        assert caught.value is error
        assert source.read_text() == "new"
        assert target.read_text() == "old"
    else:
        store_module._replace(source, target)
        assert not source.exists()
        assert target.read_text() == "new"
    assert attempts == min(failures + 1, 3)
    assert pauses == [0.005] * min(failures, 2)


@pytest.mark.parametrize(
    ("platform", "error"),
    [("linux", PermissionError("denied")), ("win32", OSError("disk failure"))],
)
def test_replace_does_not_retry_other_errors(tmp_path, monkeypatch, platform, error):
    attempts = []
    pauses = []

    def fail_replace(tmp, path):
        attempts.append((tmp, path))
        raise error

    monkeypatch.setattr(store_module.sys, "platform", platform)
    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    monkeypatch.setattr(store_module.time, "sleep", pauses.append)
    with pytest.raises(type(error)) as caught:
        store_module._replace(tmp_path / "source", tmp_path / "target")
    assert caught.value is error
    assert len(attempts) == 1
    assert pauses == []
