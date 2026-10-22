"""Producer identity: which tool, interpreter, and coding agent created a run.

The producer is informational. Flags win over environment detection, unknown
values stay null, the model is only ever a self-report, and nothing about the
producer reaches the portable attestation or any digest.
"""

from __future__ import annotations

import importlib
import json
import platform
import shlex
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

import pytest
from pydantic import ValidationError

from agentic_preflight import runs
from agentic_preflight.attestation import NOTES_REF
from agentic_preflight.config import config_digest
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Attestation, Producer, RunDoc
from agentic_preflight.runs._session import _start_command
from agentic_preflight.runs.start import (
    build_producer,
    detect_client,
    normalize_client,
    normalize_model,
)
from tests.conftest import commit_all, git, make_run, write
from tests.driver import ScriptedAgent, findings_json

# `agentic_preflight.runs` re-exports the `start` function under the module's name.
start_module = importlib.import_module("agentic_preflight.runs.start")


@pytest.fixture(autouse=True)
def no_harness_marker(monkeypatch):
    """The suite itself may run inside a coding agent; start from a neutral environment."""
    monkeypatch.delenv("CLAUDECODE", raising=False)


# -- detection precedence ---------------------------------------------------


def test_the_flag_beats_environment_detection():
    producer = build_producer(client="codex", environ={"CLAUDECODE": "1"})
    assert producer.client == "codex"
    assert producer.client_source == "flag"


def test_the_documented_claude_code_marker_is_detected():
    producer = build_producer(environ={"CLAUDECODE": "1"})
    assert producer.client == "claude-code"
    assert producer.client_source == "environment"


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"CLAUDECODE": "0"},
        {"CLAUDECODE": ""},
        # Unverified markers are deliberately not guessed from.
        {"AI_AGENT": "codex", "CURSOR_AGENT": "1", "OPENCODE": "1", "TERM_PROGRAM": "vscode"},
    ],
)
def test_an_unknown_client_stays_null(environ):
    producer = build_producer(environ=environ)
    assert producer.client is None
    assert producer.client_source is None
    assert detect_client(environ) is None


def test_the_model_is_never_inferred_from_the_environment():
    producer = build_producer(environ={"CLAUDECODE": "1", "ANTHROPIC_MODEL": "claude-opus-4-5"})
    assert producer.reported_model is None
    assert producer.reported_model_source is None


def test_a_reported_model_is_recorded_as_a_flag_self_report():
    producer = build_producer(model=" claude-opus-4-5 ", environ={})
    assert producer.reported_model == "claude-opus-4-5"
    assert producer.reported_model_source == "flag"
    assert producer.client is None


def test_the_tool_fields_are_code_derived():
    producer = build_producer(environ={})
    assert producer.tool_version == distribution_version("agentic-preflight")
    assert producer.python_version == platform.python_version()
    assert producer.platform == sys.platform


def test_a_missing_distribution_records_an_unknown_tool_version(monkeypatch):
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(start_module, "distribution_version", missing)
    assert build_producer(environ={}).tool_version == "unknown"


def test_build_producer_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert build_producer().client == "claude-code"


# -- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("claude-code", "claude-code"),
        ("Claude Code", "claude-code"),
        ("claude", "claude-code"),
        ("  CODEX ", "codex"),
        ("open_code", "open-code"),
        ("gemini-cli", "gemini-cli"),
    ],
)
def test_client_names_are_lower_case_and_canonical(given, expected):
    assert normalize_client(given) == expected


@pytest.mark.parametrize("given", ["", "   ", "-codex", "a/b", "bad\nname", "x" * 65, "café"])
def test_malformed_client_names_are_rejected(given):
    with pytest.raises(ValueError, match="client name"):
        normalize_client(given)


def test_model_ids_keep_their_case_and_spelling():
    assert normalize_model("anthropic/Claude-Opus-4-5[1m]") == "anthropic/Claude-Opus-4-5[1m]"


@pytest.mark.parametrize("given", ["", "  ", "bad\nmodel", "tab\tbed", "m" * 129])
def test_malformed_model_ids_are_rejected(given):
    with pytest.raises(ValueError, match="model id"):
        normalize_model(given)


# -- the model --------------------------------------------------------------


def test_a_value_without_its_provenance_is_invalid():
    base = {"tool_version": "1", "python_version": "3.13.0", "platform": "linux"}
    with pytest.raises(ValidationError, match="client_source"):
        Producer(**base, client="codex")
    with pytest.raises(ValidationError, match="client_source"):
        Producer(**base, client_source="flag")
    with pytest.raises(ValidationError, match="reported_model_source"):
        Producer(**base, reported_model="gpt-5")


def test_producer_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        Producer.model_validate(
            {"tool_version": "1", "python_version": "3", "platform": "linux", "guess": "x"}
        )


# -- historical records -----------------------------------------------------


def test_a_record_written_before_producer_identity_still_loads():
    payload = make_run().model_dump(mode="json")
    del payload["producer"]
    assert RunDoc.model_validate(payload).producer is None


def test_a_null_producer_key_also_loads():
    payload = {**make_run().model_dump(mode="json"), "producer": None}
    assert RunDoc.model_validate(payload).producer is None


def test_a_stored_producer_round_trips():
    run = make_run()
    run.producer = build_producer(client="codex", model="gpt-5", environ={})
    loaded = RunDoc.model_validate_json(run.model_dump_json())
    assert loaded.producer == run.producer


# -- start, events, and status ----------------------------------------------


def test_start_records_flags_in_the_run_event_and_envelopes(agent, feature_repo, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    started = agent.run("start", "--client", "Codex", "--model", "gpt-5-codex")

    expected = {
        "tool_version": distribution_version("agentic-preflight"),
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "client": "codex",
        "client_source": "flag",
        "reported_model": "gpt-5-codex",
        "reported_model_source": "flag",
    }
    assert started["data"]["producer"] == expected

    store = runs.open_session(feature_repo).store
    run = store.load_run(started["run_id"])
    assert run.producer is not None
    assert run.producer.model_dump(mode="json") == expected

    events = agent.run("events")["data"]["events"]
    created = next(event for event in events if event["event"] == "run_created")
    assert created["producer"] == expected

    assert agent.run("status")["data"]["producer"] == expected


def test_start_detects_claude_code_without_flags(agent, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    producer = agent.run("start")["data"]["producer"]
    assert producer["client"] == "claude-code"
    assert producer["client_source"] == "environment"
    assert producer["reported_model"] is None
    assert producer["reported_model_source"] is None


def test_start_without_flags_or_markers_records_no_client(agent):
    producer = agent.run("start")["data"]["producer"]
    assert producer["client"] is None
    assert producer["client_source"] is None
    assert producer["tool_version"]


def test_a_resumed_run_keeps_its_original_producer(agent):
    first = agent.run("start", "--client", "codex")
    resumed = agent.run("start", "--client", "cursor", "--model", "other")
    assert resumed["run_id"] == first["run_id"]
    assert resumed["data"]["resumed"] is True
    assert resumed["data"]["producer"]["client"] == "codex"
    assert resumed["data"]["producer"]["reported_model"] is None


@pytest.mark.parametrize(
    "argv",
    [("--client", "a/b"), ("--client", ""), ("--model", "bad\nmodel"), ("--model", " ")],
)
def test_malformed_flags_are_usage_errors_before_any_run_exists(agent, feature_repo, argv):
    env = agent.run("start", *argv, expect=ExitCode.USAGE_ERROR)
    assert env["error"]["code"] == "usage_error"
    assert runs.open_session(feature_repo).store.list_runs() == []


def test_status_reports_null_for_a_run_without_a_producer(agent, feature_repo):
    started = agent.run("start")
    store = runs.open_session(feature_repo).store
    with store.transaction(started["run_id"]) as doc:
        doc.producer = None
    assert agent.run("status")["data"]["producer"] is None


# -- recovery commands ------------------------------------------------------


def _recovery_argv(producer: Producer | None) -> list[str]:
    """The suggested restart, split the way a shell would."""
    command = _start_command(
        "same objective", base_ref="main", default_base_ref="main", producer=producer
    )
    return shlex.split(command)


# Shell metacharacters and a quote: the command must round-trip the exact value.
_AWKWARD_MODEL = "gpt-5 codex ($HOME; it's)"


def test_recovery_commands_repeat_a_flag_sourced_client_and_model():
    producer = build_producer(client="Codex", model=_AWKWARD_MODEL, environ={})
    assert _recovery_argv(producer) == [
        "agentic-preflight",
        "start",
        "--intent",
        "same objective",
        "--client",
        "codex",
        "--model",
        _AWKWARD_MODEL,
    ]


def test_recovery_commands_leave_a_detected_client_to_the_next_detection():
    producer = build_producer(environ={"CLAUDECODE": "1"})
    assert producer.client_source == "environment"
    assert _recovery_argv(producer) == ["agentic-preflight", "start", "--intent", "same objective"]


def test_recovery_commands_repeat_a_flag_sourced_model_beside_a_detected_client():
    producer = build_producer(model="claude-opus-4-5", environ={"CLAUDECODE": "1"})
    assert _recovery_argv(producer) == [
        "agentic-preflight",
        "start",
        "--intent",
        "same objective",
        "--model",
        "claude-opus-4-5",
    ]


def test_recovery_commands_without_a_producer_are_unchanged():
    assert _recovery_argv(None) == ["agentic-preflight", "start", "--intent", "same objective"]


def test_a_refused_start_suggests_replacing_with_the_flags_just_given(agent):
    first = agent.run("start", "--client", "codex", "--intent", "first objective")

    refused = agent.run(
        "start",
        "--client",
        "cursor",
        "--model",
        "cursor-model",
        "--intent",
        "second objective",
        expect=ExitCode.PRECONDITION,
    )
    command = shlex.split(refused["next"]["command"])
    # The replacement is produced by whoever is running now, not by the run it replaces.
    assert command == [
        "agentic-preflight",
        "start",
        "--replace",
        "--intent",
        "second objective",
        "--client",
        "cursor",
        "--model",
        "cursor-model",
    ]

    replaced = agent.run(*command[1:])
    assert replaced["run_id"] != first["run_id"]
    producer = replaced["data"]["producer"]
    assert (producer["client"], producer["client_source"]) == ("cursor", "flag")
    assert (producer["reported_model"], producer["reported_model_source"]) == (
        "cursor-model",
        "flag",
    )


def test_stale_and_abort_recovery_commands_repeat_the_recorded_flags(agent, feature_repo):
    agent.run(
        "start", "--client", "codex", "--model", _AWKWARD_MODEL, "--intent", "first objective"
    )
    expected = [
        "agentic-preflight",
        "start",
        "--intent",
        "first objective",
        "--client",
        "codex",
        "--model",
        _AWKWARD_MODEL,
    ]

    write(feature_repo, "src/extra.py", "value = 1\n")
    commit_all(feature_repo, "advance the source branch")
    stale = agent.run("status")
    assert shlex.split(stale["next"]["command"]) == expected

    restarted = agent.run(*expected[1:])
    producer = restarted["data"]["producer"]
    assert (producer["client"], producer["client_source"]) == ("codex", "flag")
    assert (producer["reported_model"], producer["reported_model_source"]) == (
        _AWKWARD_MODEL,
        "flag",
    )

    aborted = agent.run("abort")
    assert shlex.split(aborted["next"]["command"]) == expected


def test_recovery_commands_do_not_turn_a_detected_client_into_a_flag(
    agent, feature_repo, monkeypatch
):
    monkeypatch.setenv("CLAUDECODE", "1")
    started = agent.run("start", "--intent", "first objective")
    assert started["data"]["producer"]["client_source"] == "environment"

    write(feature_repo, "src/extra.py", "value = 1\n")
    commit_all(feature_repo, "advance the source branch")
    command = shlex.split(agent.run("status")["next"]["command"])
    assert command == ["agentic-preflight", "start", "--intent", "first objective"]

    restarted = agent.run(*command[1:])
    producer = restarted["data"]["producer"]
    assert (producer["client"], producer["client_source"]) == ("claude-code", "environment")


# -- never evidence -----------------------------------------------------------


def test_the_producer_never_reaches_the_attestation_or_digests(feature_repo, tmp_path):
    assert "producer" not in Attestation.model_fields
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)

    started = agent.run("start", "--client", "producer-probe-client", "--model", "probe-model-x")
    store = runs.open_session(feature_repo).store
    run = store.load_run(started["run_id"])
    assert "producer" not in run.config_snapshot
    assert run.config_digest == config_digest(run.config_snapshot)

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"

    head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert agent.run("verify", head)["data"]["verified"] is True
    note = git("notes", f"--ref={NOTES_REF}", "show", head, cwd=feature_repo)
    entry = json.loads(note)
    assert entry["run_id"] == started["run_id"]
    Attestation.model_validate(entry)
    for marker in ("producer", "producer-probe-client", "probe-model-x", "client_source"):
        assert marker not in note

    verified = store.load_run(started["run_id"])
    assert verified.producer is not None
    assert verified.producer.client == "producer-probe-client"
    for stage, item in verified.evidence.items():
        dumped = item.model_dump_json()
        assert "producer-probe-client" not in dumped, stage
        assert "probe-model-x" not in dumped, stage
