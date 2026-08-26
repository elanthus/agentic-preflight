"""Replacing the run document when something else has it open.

POSIX ``rename`` cannot fail because a reader holds the destination. Windows
can, and the window is real: the store's own ``load_run`` opens ``run.json``
for the duration of a read, and two parallel agent invocations are the case
this module is built around.
"""

import os
import sys
import threading
import time

import pytest

from agentic_preflight import store as store_module
from agentic_preflight.store import Store
from tests.conftest import make_run

WINDOWS = sys.platform == "win32"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-preflight")


@pytest.mark.skipif(not WINDOWS, reason="only Windows refuses to replace an open file")
def test_a_write_survives_a_reader_that_holds_the_document_briefly(store):
    """The real race: a concurrent ``load_run`` must not cost a state transition.

    The reader here behaves like the store's own: it opens the document, holds
    it for longer than the first retry delay, and closes. Without the retry the
    transaction below fails outright with ``WinError 5``.
    """
    store.create_run(make_run())
    path = store.run_path("r_abc123")
    reader_opened = threading.Event()

    def hold_briefly():
        with open(path, encoding="utf-8") as handle:
            handle.read()
            reader_opened.set()
            time.sleep(0.15)

    reader = threading.Thread(target=hold_briefly)
    reader.start()
    reader_opened.wait(timeout=5)
    try:
        with store.transaction("r_abc123") as run:
            run.branch = "feature/updated"
    finally:
        reader.join(timeout=5)

    assert store.load_run("r_abc123").branch == "feature/updated"


def test_the_replace_is_retried_until_it_succeeds(store, monkeypatch):
    """Simulated on every platform so the retry cannot rot on POSIX-only CI."""
    monkeypatch.setattr(store_module.sys, "platform", "win32")
    monkeypatch.setattr(store_module.time, "sleep", lambda _: None)

    real_replace = os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(src)
        if len(attempts) < 3:
            raise PermissionError(13, "target is in use")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky)
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
