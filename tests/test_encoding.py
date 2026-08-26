"""Text that leaves this process must not depend on the machine's locale.

Every one of these assertions is a Windows bug that reached a user before it
reached a test: the platform default there is ``cp1252``, under which a single
accented character in a branch name, a review finding, or a file path is the
difference between a working command and a traceback.
"""

import pytest

from agentic_preflight import hook
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from agentic_preflight.store import Store
from tests.conftest import make_run

NON_ASCII = "réviser café — naïve ☕"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agentic-preflight")


def test_a_run_document_round_trips_non_ascii(store):
    store.create_run(make_run(branch=NON_ASCII))

    assert store.load_run("r_abc123").branch == NON_ASCII


def test_a_run_document_is_written_as_utf8(store):
    """Read back as bytes, so a locale that happens to match cannot hide a bug."""
    store.create_run(make_run(branch=NON_ASCII))

    raw = store.run_path("r_abc123").read_bytes()

    assert NON_ASCII.encode("utf-8") in raw


def test_events_round_trip_non_ascii(store):
    store.create_run(make_run())
    store.append_event("r_abc123", {"kind": "note", "detail": NON_ASCII})

    assert store.load_events("r_abc123")[0]["detail"] == NON_ASCII


def test_the_events_log_uses_unix_line_endings(store):
    """One event per line, on every platform, for anything that reads the audit log."""
    store.create_run(make_run())
    store.append_event("r_abc123", {"kind": "one"})
    store.append_event("r_abc123", {"kind": "two"})

    raw = store.events_path("r_abc123").read_bytes()

    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 2


def test_findings_round_trip_non_ascii(store):
    store.create_run(make_run())
    finding = Finding(
        id="F001",
        stage=Stage.REVIEW,
        path="src/café.py",
        severity=Severity.LOW,
        action=FindingAction.NO_OP,
        title=NON_ASCII,
    )

    store.save_findings("r_abc123", [finding])

    loaded = store.load_findings("r_abc123")
    assert loaded[0].title == NON_ASCII
    assert loaded[0].path == "src/café.py"


def test_the_pre_push_hook_is_written_with_unix_line_endings(tmp_path):
    """Git runs this with its own ``sh``; a CRLF shebang makes the interpreter unfindable."""
    hook.install(tmp_path)

    raw = (tmp_path / "hooks" / "pre-push").read_bytes()

    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/bin/sh\n")


def test_the_generated_config_is_written_with_unix_line_endings(tmp_repo):
    """The config is committed and shared, so the gate is byte-identical for everyone."""
    from agentic_preflight import initcmd

    initcmd.init(tmp_repo, install_hook=False)

    assert b"\r\n" not in (tmp_repo / ".agentic-preflight.toml").read_bytes()
