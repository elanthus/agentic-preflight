import json

import pytest
from click.testing import CliRunner

from agentic_preflight import runs
from agentic_preflight.cli import main
from agentic_preflight.envelope import ExitCode
from agentic_preflight.errors import InvalidResponse
from tests.driver import ScriptedAgent

BLOCKING = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "loud flag is never used",
    }
]


@pytest.fixture
def agent(feature_repo):
    return ScriptedAgent(feature_repo)


@pytest.fixture
def blocked(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": BLOCKING})
    )
    agent.run("submit-findings", "--file", str(path))
    return agent


def test_usage_exit_code_is_distinct_from_every_other_code():
    values = [int(code) for code in ExitCode]
    assert len(values) == len(set(values))
    assert ExitCode.USAGE_ERROR not in (ExitCode.STAGE_FAILED, ExitCode.USAGE)


@pytest.mark.parametrize(
    "argv",
    [
        ["respond", "--bogus"],
        ["respond", "--id", "F001"],
        ["respond", "--id", "F001", "--action", "ignored", "--note", "x"],
        ["--bogus", "status"],
        ["no-such-command"],
        ["review", "no-such-command"],
    ],
)
def test_usage_errors_emit_one_envelope_with_the_usage_exit_code(agent, argv):
    env = agent.run(*argv, expect=ExitCode.USAGE_ERROR)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage_error"
    assert env["error"]["message"]
    assert env["error"]["message"] in env["next"]["instruction"]
    assert env["next"]["command"].endswith("--help")


def test_usage_errors_keep_the_usage_dump_off_stdout(feature_repo, monkeypatch):
    monkeypatch.chdir(feature_repo)
    result = CliRunner().invoke(main, ["respond", "--bogus"])
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert "Usage:" not in result.stdout
    assert len(result.stdout.strip().splitlines()) == 1
    json.loads(result.stdout)


def test_hook_check_keeps_clicks_own_usage_behaviour(feature_repo, monkeypatch):
    monkeypatch.chdir(feature_repo)
    result = CliRunner().invoke(main, ["hook-check", "--bogus"])
    assert result.exit_code == 2
    assert result.stdout == ""


def test_submit_findings_with_a_missing_file_is_a_structured_error(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    env = agent.run(
        "submit-findings", "--file", str(tmp_path / "missing.json"), expect=ExitCode.PRECONDITION
    )
    assert env["error"]["code"] == "invalid_findings"


def test_submit_findings_with_non_utf8_bytes_is_a_structured_error(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = tmp_path / "latin1.json"
    path.write_bytes(b'{"findings": ["caf\xe9"]}')
    env = agent.run("submit-findings", "--file", str(path), expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"


def test_review_compare_with_non_utf8_bytes_is_a_structured_error(blocked, tmp_path):
    path = tmp_path / "latin1.json"
    path.write_bytes(b'{"findings": ["caf\xe9"]}')
    env = blocked.run("review", "compare", "--file", str(path), expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"


def test_respond_refuses_a_commit_for_a_dismissal_at_the_cli(blocked):
    env = blocked.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--commit",
        "deadbeef",
        "--note",
        "not a bug",
        expect=ExitCode.USAGE_ERROR,
    )
    assert env["error"]["code"] == "usage_error"
    assert "--commit" in env["error"]["message"]


def test_resolve_refuses_a_commit_for_a_dismissal_directly(blocked, feature_repo, monkeypatch):
    monkeypatch.chdir(feature_repo)
    session = runs.open_session()
    with pytest.raises(InvalidResponse, match="--commit"):
        runs.respond(
            session, finding_id="F001", action="dismissed", commit="deadbeef", note="not a bug"
        )
    finding = next(
        f for f in session.store.load_findings(session.active_run_id()) if f.id == "F001"
    )
    assert finding.status.value == "open"


def test_usage_errors_emit_an_envelope_on_click_without_no_args_is_help(agent, monkeypatch):
    import click.exceptions

    from agentic_preflight import cli_support

    monkeypatch.delattr(click.exceptions, "NoArgsIsHelpError", raising=False)
    monkeypatch.setattr(cli_support, "_NO_ARGS_IS_HELP", None)
    env = agent.run("respond", "--bogus", expect=ExitCode.USAGE_ERROR)
    assert env["error"]["code"] == "usage_error"


def _bare_invocation():
    from click.testing import CliRunner

    from agentic_preflight.cli import main

    return CliRunner().invoke(main, [])


def test_bare_invocation_prints_root_help_and_exits_zero():
    result = _bare_invocation()
    assert result.exit_code == 0
    assert "Usage:" in result.stdout


def test_bare_invocation_exits_zero_on_click_without_no_args_is_help(monkeypatch):
    import click.exceptions

    from agentic_preflight import cli_support

    monkeypatch.delattr(click.exceptions, "NoArgsIsHelpError", raising=False)
    monkeypatch.setattr(cli_support, "_NO_ARGS_IS_HELP", None)
    result = _bare_invocation()
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
