import io
import json

import pytest

from agentic_preflight.cli import command
from agentic_preflight.envelope import Envelope, ExitCode, emit, error_envelope
from agentic_preflight.errors import WrongState


def test_envelope_has_every_contract_key_even_when_empty():
    """The agent parses blindly; missing keys would force defensive `.get()`."""
    payload = json.loads(Envelope(state="CREATED").to_json())
    assert set(payload) == {
        "ok",
        "run_id",
        "state",
        "stage",
        "data",
        "blocking",
        "next",
        "error",
    }


def test_a_successful_envelope_defaults_to_ok():
    payload = json.loads(Envelope(run_id="r_1", state="WORKTREE_READY").to_json())
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["data"] == {}
    assert payload["blocking"] == []


def test_next_carries_instruction_and_command():
    env = Envelope(
        state="REVIEW_AWAITING_FINDINGS",
        next_instruction="Review the diff and submit findings.",
        next_command="agentic-preflight submit-findings --file findings.json",
    )
    payload = json.loads(env.to_json())
    assert payload["next"]["instruction"].startswith("Review the diff")
    assert payload["next"]["command"].endswith("findings.json")


def test_next_is_null_when_there_is_no_legal_move():
    payload = json.loads(Envelope(state="DONE").to_json())
    assert payload["next"] is None


def test_error_envelope_is_not_ok_and_names_the_code():
    env = error_envelope(
        code="wrong_state",
        message="submit-findings is not legal in state CREATED",
        state="CREATED",
    )
    payload = json.loads(env.to_json())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "wrong_state"
    assert "not legal" in payload["error"]["message"]


def test_stateful_errors_inherit_the_declarative_recovery_hint():
    payload = WrongState("wrong command", state="REVIEW_GREEN").to_envelope().to_payload()
    assert payload["next"]["command"] == "agentic-preflight stage run test"


def test_emit_writes_exactly_one_json_object_and_nothing_else():
    out = io.StringIO()
    emit(Envelope(state="CREATED"), stream=out)
    text = out.getvalue()
    assert text.endswith("\n")
    assert len(text.strip().splitlines()) == 1
    json.loads(text)


def test_emit_never_writes_prose_to_the_json_stream():
    out = io.StringIO()
    emit(Envelope(state="CREATED", human="this is for a person"), stream=out)
    assert "this is for a person" not in out.getvalue()


def test_exit_codes_match_the_published_contract():
    assert ExitCode.OK == 0
    assert ExitCode.USAGE == 1
    assert ExitCode.STAGE_FAILED == 2
    assert ExitCode.PRECONDITION == 3
    assert ExitCode.NEEDS_HUMAN == 4
    assert ExitCode.NEEDS_CONFIRM == 5
    assert ExitCode.HOOK_BLOCK == 10


def test_unexpected_internal_error_keeps_the_json_stdout_contract(capsys):
    @command
    def explode():
        raise RuntimeError("sensitive diagnostic detail")

    with pytest.raises(SystemExit) as stopped:
        explode()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert stopped.value.code == ExitCode.USAGE
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": "internal_error",
        "message": "an unexpected internal error occurred",
    }
    assert "sensitive diagnostic detail" not in captured.out
    assert "RuntimeError: sensitive diagnostic detail" in captured.err
