"""Atomic replacement while an external reader holds a run document.

Store API locks serialize its own readers and writers. An external reader such as
an indexer can still briefly deny replacement on Windows. Test the bounded retry
and preserved failure behavior without relying on thread scheduling delays.
"""

import os
import sys
import threading
from types import SimpleNamespace

import pytest

from agentic_preflight import store as store_module
from agentic_preflight.store import Store
from tests.conftest import make_run

WINDOWS = sys.platform == "win32"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-preflight")


@pytest.mark.skipif(not WINDOWS, reason="only Windows refuses to replace an open file")
def test_a_write_survives_a_reader_that_holds_the_document_briefly(store, monkeypatch):
    """A real Windows sharing violation clears during the configured retry window."""
    store.create_run(make_run())
    path = store.run_path("r_abc123")
    reader_opened = threading.Event()
    release_reader = threading.Event()
    retry_delays: list[float] = []

    def hold_until_retry():
        with open(path, encoding="utf-8") as handle:
            handle.read()
            reader_opened.set()
            release_reader.wait(timeout=5)

    reader = threading.Thread(target=hold_until_retry)

    def release_on_retry(delay):
        retry_delays.append(delay)
        release_reader.set()
        reader.join(timeout=5)
        assert not reader.is_alive()

    monkeypatch.setattr(store_module.time, "sleep", release_on_retry)
    reader.start()
    try:
        assert reader_opened.wait(timeout=5)
        with store.transaction("r_abc123") as run:
            run.branch = "feature/updated"
    finally:
        release_reader.set()
        reader.join(timeout=5)

    assert retry_delays
    assert all(delay == 0.005 for delay in retry_delays)
    assert store.load_run("r_abc123").branch == "feature/updated"


def test_the_replace_is_retried_until_it_succeeds(store, monkeypatch):
    """Simulated on every platform so the retry cannot rot on POSIX-only CI."""
    real_replace = os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(src)
        if len(attempts) < 3:
            raise PermissionError(13, "target is in use")
        return real_replace(src, dst)

    with monkeypatch.context() as simulated:
        # Simulate only the replacement helper's platform. Run creation also
        # appends an audit event, whose lock must use the actual host backend.
        simulated.setattr(store_module, "sys", SimpleNamespace(platform="win32"))
        simulated.setattr(store_module.time, "sleep", lambda _: None)
        simulated.setattr(store_module.os, "replace", flaky)
        store.create_run(make_run(branch="feature/retried"))

    assert len(attempts) == 3
    assert store.load_run("r_abc123").branch == "feature/retried"


def test_a_permanently_held_target_raises_rather_than_losing_the_write(store, monkeypatch):
    """Silence here would mean a state transition that the agent believes happened."""
    monkeypatch.setattr(store_module.sys, "platform", "win32")
    monkeypatch.setattr(store_module.time, "sleep", lambda _: None)

    def always_denied(src, dst):
        raise PermissionError(13, "target is in use")

    monkeypatch.setattr(store_module.os, "replace", always_denied)

    with pytest.raises(PermissionError):
        store.create_run(make_run())


def test_a_failed_replace_leaves_no_temporary_debris(store, monkeypatch, tmp_path):
    monkeypatch.setattr(store_module.sys, "platform", "win32")
    monkeypatch.setattr(store_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        store_module.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "held")),
    )

    with pytest.raises(PermissionError):
        store.create_run(make_run())

    leftovers = list(store.run_dir("r_abc123").glob("*.tmp"))
    assert leftovers == []


def test_posix_does_not_retry_a_permission_error(store, monkeypatch):
    """A denied rename on POSIX is a real permissions fault; grinding hides it."""
    if WINDOWS:
        pytest.skip("platform branch is Windows on this machine")

    attempts = []

    def denied(src, dst):
        attempts.append(src)
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(store_module.os, "replace", denied)

    with pytest.raises(PermissionError):
        store.create_run(make_run())

    assert len(attempts) == 1
