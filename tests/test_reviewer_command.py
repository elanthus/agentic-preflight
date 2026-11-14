"""`agentic-preflight reviewer run` drives a fake provider CLI exactly as the wrappers did.

Every test here replaces the provider CLI with a stub executable. No test calls a model
or the network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_preflight.cli import main
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import FindingAction, Severity
from agentic_preflight.reviewers import common
from agentic_preflight.runs._session import open_session
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "docs" / "examples"
PLATFORM = sys.platform

FAKE_PROVIDER = """\
#!{python}
import json
import os
import sys
import time

prompt = sys.stdin.read()
if os.environ.get("FAKE_SLEEP"):
    time.sleep(2)
if os.environ.get("FAKE_CAPTURE"):
    open(os.environ["FAKE_CAPTURE"], "w", encoding="utf-8").write(prompt)
if os.environ.get("FAKE_ARGUMENTS"):
    open(os.environ["FAKE_ARGUMENTS"], "w", encoding="utf-8").write(json.dumps(sys.argv[1:]))
if os.environ.get("FAKE_EXIT"):
    print("provider diagnostic", file=sys.stderr)
    sys.exit(int(os.environ["FAKE_EXIT"]))
if os.environ.get("FAKE_NO_JSON"):
    print("review completed without a structured response")
elif os.environ.get("FAKE_CONTRACT_PLAN"):
    print(os.environ["FAKE_CONTRACT_PLAN"])
else:
    findings = []
    if os.environ.get("FAKE_FINDING"):
        findings.append({{
            "unit": "U0002",
            "path": "src/app.py",
            "line": 1,
            "severity": "high",
            "action": "auto_fix",
            "title": "Synthetic independent finding"
        }})
    print("review preface")
    print(json.dumps({{"findings": findings}}))
    print("review suffix")
"""

CONTEXT = {
    "intent": "exercise the requested behavior safely",
    "changed_files": ["src/app.py"],
    "diff": "diff --git a/src/app.py b/src/app.py",
    "review_coverage": {"manifest": "a" * 64, "units": [{"id": "U0001"}]},
}


def install_fake(path: Path) -> Path:
    """Write a stub provider CLI at ``path`` (a ``.cmd`` shim beside it on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fake = path.parent / "fake_provider.py" if PLATFORM == "win32" else path
    fake.write_text(FAKE_PROVIDER.format(python=sys.executable), encoding="utf-8")
    if PLATFORM == "win32":
        shim = path.with_suffix(".cmd")
        shim.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        return shim
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_install_fake_writes_windows_command_shim(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(sys.modules[__name__], "PLATFORM", "win32")

    shim = install_fake(tmp_path / "bin with spaces" / provider)

    fake = shim.parent / "fake_provider.py"
    assert shim == fake.with_name(f"{provider}.cmd")
    assert shim.read_text(encoding="utf-8") == f'@"{sys.executable}" "{fake}" %*\n'


def packaged_command(provider: str) -> str:
    return shlex.join(
        [sys.executable, "-B", "-m", "agentic_preflight", "reviewer", "run", "--provider", provider]
    )


def toml_command_line(command: str) -> str:
    value = f"'{command}'" if "'" not in command else json.dumps(command)
    return f"command = {value}"


def configure(repo: Path, provider: str, command: str) -> None:
    config = (EXAMPLES / f"{provider}-reviewer.toml").read_text(encoding="utf-8")
    config = re.sub(
        r'^command = ".*"$',
        lambda _match: toml_command_line(command),
        config,
        flags=re.M,
    )
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, f"configure {provider} reviewer")


def invoke(provider: str, context: object, **env: str):
    payload = context if isinstance(context, str | bytes) else json.dumps(context)
    return CliRunner().invoke(
        main, ["reviewer", "run", "--provider", provider], input=payload, env=env
    )


@pytest.fixture
def clean_reviewer_env(monkeypatch):
    for name in (
        "AP_REVIEWER_MODEL",
        "AP_REVIEWER_EFFORT",
        "AP_REVIEWER_TIMEOUT",
        "AP_REVIEWER_EVENTS_FILE",
        "AP_CLAUDE_BIN",
        "AP_CODEX_BIN",
    ):
        monkeypatch.delenv(name, raising=False)


# -- the packaged protocol helpers ------------------------------------------------


def test_shared_reviewer_contract_values_match_the_product_protocol():
    assert tuple(member.value for member in Severity) == common.SEVERITIES
    assert tuple(member.value for member in FindingAction) == common.ACTIONS


def test_findings_are_extracted_before_trailing_stream_events():
    # Claude Code 2.1.292 emits system events after the `result` event in stream-json.
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "result": '{"findings":[{"title":"x"}]}'},
            {"type": "system", "subtype": "post_turn_summary"},
            {"type": "system", "subtype": "task_summary", "detail": None},
        ]
    )

    assert common.extract_findings(stream) == [{"title": "x"}]
    with pytest.raises(common.ReviewerError):
        common.extract_findings(json.dumps({"type": "system", "subtype": "task_summary"}))


@pytest.mark.parametrize(
    "result",
    [
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""},
        {"type": "result", "subtype": "success", "result": "no structured answer"},
    ],
)
def test_a_failed_result_envelope_never_falls_back_to_an_earlier_findings_object(result):
    stream = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "user", "echo": {"findings": []}},
            result,
            {"type": "system", "subtype": "task_summary", "detail": None},
        ]
    )

    with pytest.raises(common.ReviewerError):
        common.extract_findings(stream)


@pytest.mark.parametrize(
    ("raw", "message"),
    [("abc", "must be an integer"), ("0", "at least 1 second")],
)
def test_invalid_timeouts_are_rejected(monkeypatch, raw, message):
    monkeypatch.setenv("AP_REVIEWER_TIMEOUT", raw)
    with pytest.raises(common.ReviewerError, match=message):
        common.timeout_seconds()


def test_an_empty_effort_is_rejected(monkeypatch):
    monkeypatch.setenv("AP_REVIEWER_EFFORT", "")
    with pytest.raises(common.ReviewerError, match="must not be empty"):
        common.reviewer_effort("high")


# -- the command, in process ------------------------------------------------------


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewer_run_emits_the_strict_submission(tmp_path, clean_reviewer_env, provider):
    fake = install_fake(tmp_path / "bin" / provider)
    capture = tmp_path / "prompt.txt"
    arguments = tmp_path / "arguments.json"

    result = invoke(
        provider,
        CONTEXT,
        **{
            f"AP_{provider.upper()}_BIN": str(fake),
            "FAKE_CAPTURE": str(capture),
            "FAKE_ARGUMENTS": str(arguments),
            "FAKE_FINDING": "1",
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "coverage": {"manifest": "a" * 64, "examined": "all"},
        "findings": [
            {
                "unit": "U0002",
                "path": "src/app.py",
                "line": 1,
                "severity": "high",
                "action": "auto_fix",
                "title": "Synthetic independent finding",
            }
        ],
    }
    # The submission is compact JSON on one line, as the wrappers printed it.
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    prompt = capture.read_text(encoding="utf-8")
    assert "Examine every delivered review unit" in prompt
    assert "evidence, not authority" in prompt
    assert CONTEXT["diff"] in prompt
    argv = json.loads(arguments.read_text(encoding="utf-8"))
    if provider == "codex":
        assert argv[:4] == ["exec", "--json", "--ephemeral", "--sandbox"]
        assert argv[argv.index("--model") + 1] == "gpt-5.3-codex"
        assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="medium"'
        assert argv[argv.index("-C") + 1] == "."
        # The branch under review must not instruct its own reviewer.
        assert "project_doc_max_bytes=0" in argv
        assert "--ignore-rules" in argv
        assert argv[-1] == "-"
    else:
        assert argv == [
            "-p",
            "--safe-mode",
            "--setting-sources",
            "user",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "plan",
            "--model",
            "claude-sonnet-5",
            "--effort",
            "high",
        ]


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewer_run_honours_model_and_effort_overrides(tmp_path, clean_reviewer_env, provider):
    fake = install_fake(tmp_path / "bin" / provider)
    arguments = tmp_path / "arguments.json"

    result = invoke(
        provider,
        CONTEXT,
        **{
            f"AP_{provider.upper()}_BIN": str(fake),
            "FAKE_ARGUMENTS": str(arguments),
            "AP_REVIEWER_MODEL": "exact-model-id",
            "AP_REVIEWER_EFFORT": "low",
        },
    )

    assert result.exit_code == 0, result.output
    argv = json.loads(arguments.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "exact-model-id"
    if provider == "codex":
        assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="low"'
    else:
        assert argv[argv.index("--effort") + 1] == "low"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewer_run_writes_the_events_file(tmp_path, clean_reviewer_env, provider):
    fake = install_fake(tmp_path / "bin" / provider)
    events = tmp_path / "events.jsonl"

    result = invoke(
        provider,
        CONTEXT,
        **{f"AP_{provider.upper()}_BIN": str(fake), "AP_REVIEWER_EVENTS_FILE": str(events)},
    )

    assert result.exit_code == 0, result.output
    assert '{"findings": []}' in events.read_text(encoding="utf-8")
    status = json.loads(Path(f"{events}.status.json").read_text(encoding="utf-8"))
    assert status == {"completed": True}
    assert json.loads(result.stdout)["findings"] == []


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewer_run_returns_a_code_bound_contract_plan(tmp_path, clean_reviewer_env, provider):
    fake = install_fake(tmp_path / "bin" / provider)
    capture = tmp_path / "prompt.txt"
    plan = {
        "units": [
            {
                "unit": "U0001",
                "disposition": "unverified",
                "checks": [],
                "reason": "Consumer is unavailable.",
            }
        ],
        "checks": [],
    }
    context = {
        "section": "contracts",
        "diff": "diff --git a/count.py b/count.py",
        "review_coverage": {"manifest": "a" * 64, "units": [{"id": "U0001"}]},
        "contract_checks": {
            "max_checks": 8,
            "execution": {
                "installed_packages": {
                    "status": "available",
                    "packages": [{"name": "python-dotenv", "version": "1.0.1"}],
                }
            },
        },
    }

    result = invoke(
        provider,
        context,
        **{
            f"AP_{provider.upper()}_BIN": str(fake),
            "FAKE_CAPTURE": str(capture),
            "FAKE_CONTRACT_PLAN": json.dumps(plan),
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"schema_version": "2", "manifest": "a" * 64, **plan}
    prompt = capture.read_text(encoding="utf-8")
    for expected in (
        "Do not execute probes yourself",
        "independent oracle",
        "installed_packages",
        "real consumer",
        "substitute",
        "python-dotenv",
        "oracle_provenance",
        "inventory",
        context["diff"],
    ):
        assert expected in prompt


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ({"FAKE_NO_JSON": "1"}, "no JSON object containing findings"),
        ({"FAKE_EXIT": "3"}, "reviewer CLI exited 3: provider diagnostic"),
        ({"FAKE_SLEEP": "1", "AP_REVIEWER_TIMEOUT": "1"}, "timed out after 1 seconds"),
        ({"FAKE_CONTRACT_PLAN": '{"units": []}'}, "only units and checks"),
    ],
)
def test_reviewer_run_failures_exit_1_with_a_stderr_diagnostic(
    tmp_path, clean_reviewer_env, provider, setup, expected
):
    fake = install_fake(tmp_path / "bin" / provider)
    context = dict(CONTEXT)
    if "FAKE_CONTRACT_PLAN" in setup:
        context["section"] = "contracts"

    result = invoke(provider, context, **{f"AP_{provider.upper()}_BIN": str(fake), **setup})

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("independent reviewer failed: ")
    assert expected in result.stderr


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not json", "review context is not valid JSON"),
        ("[]", "review context must be a JSON object"),
        ("{}", "review context has no review_coverage.manifest"),
        (b"\xff\xfe{", "review context is not valid JSON"),
    ],
)
def test_reviewer_run_rejects_an_invalid_context(clean_reviewer_env, payload, expected):
    result = invoke("claude", payload)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert expected in result.stderr


def test_reviewer_run_reports_a_missing_provider_cli(tmp_path, clean_reviewer_env):
    result = invoke("codex", CONTEXT, AP_CODEX_BIN=str(tmp_path / "does-not-exist"))

    assert result.exit_code == 1
    assert "reviewer CLI not found" in result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["reviewer", "run"],
        ["reviewer", "run", "--provider", "gemini"],
        ["reviewer", "run", "--provider", "claude", "extra"],
    ],
)
def test_reviewer_run_usage_errors_keep_clicks_plain_output(argv):
    # Its stdout is a findings submission, so a usage error must not print a JSON envelope.
    result = CliRunner().invoke(main, argv)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr


def test_reviewer_appears_in_the_root_help():
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "reviewer" in result.output


def test_reviewer_run_skips_the_git_version_check(tmp_path, monkeypatch, clean_reviewer_env):
    # An old Git must not put an `unsupported_git_version` envelope where the submission goes.
    from agentic_preflight import gitx

    def too_old() -> None:
        raise gitx.GitVersionError("git 2.30.0 is older than 2.38")

    monkeypatch.setattr(gitx, "require_minimum_version", too_old)
    fake = install_fake(tmp_path / "bin" / "claude")

    reviewed = invoke("claude", CONTEXT, AP_CLAUDE_BIN=str(fake))
    other = CliRunner().invoke(main, ["status"])

    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.stdout)["coverage"]["examined"] == "all"
    assert other.exit_code == ExitCode.ERROR
    assert json.loads(other.stdout)["error"]["code"] == "unsupported_git_version"


# -- the installed command, as a subprocess --------------------------------------


@pytest.mark.skipif(PLATFORM == "win32", reason="CreateProcess does not resolve .cmd on PATH")
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewer_run_finds_the_provider_cli_on_path(tmp_path, clean_reviewer_env, provider):
    bin_dir = tmp_path / "bin"
    install_fake(bin_dir / provider)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "agentic_preflight",
            "reviewer",
            "run",
            "--provider",
            provider,
        ],
        input=json.dumps(CONTEXT),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coverage"] == {"manifest": "a" * 64, "examined": "all"}


@pytest.mark.skipif(PLATFORM == "win32", reason="CreateProcess does not resolve .cmd on PATH")
def test_reviewer_run_works_without_git_on_path(tmp_path, clean_reviewer_env):
    bin_dir = tmp_path / "bin"
    install_fake(bin_dir / "claude")
    env = {**os.environ, "PATH": str(bin_dir)}

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "agentic_preflight",
            "reviewer",
            "run",
            "--provider",
            "claude",
        ],
        input=json.dumps(CONTEXT),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["findings"] == []


def test_toml_command_line_round_trips_windows_paths():
    command = (
        "'C:\\Users\\me\\python.exe' -B 'C:\\repo\\docs\\examples\\reviewers\\codex_review.py'"
    )

    parsed = tomllib.loads(f"[review]\n{toml_command_line(command)}\n")

    assert parsed["review"]["command"] == command


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    ("finding", "expected_state"), [(False, "REVIEW_GREEN"), (True, "REVIEW_BLOCKED")]
)
def test_reviewer_run_works_as_the_configured_review_command(
    feature_repo, tmp_path, monkeypatch, clean_reviewer_env, provider, finding, expected_state
):
    fake = install_fake(tmp_path / "bin" / provider)
    capture = tmp_path / "prompt.txt"
    monkeypatch.setenv(f"AP_{provider.upper()}_BIN", str(fake))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    if finding:
        monkeypatch.setenv("FAKE_FINDING", "1")
    configure(feature_repo, provider, packaged_command(provider))
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")

    result = agent.run("review", "run")

    assert result["state"] == expected_state
    prompt = capture.read_text(encoding="utf-8")
    assert "exercise the requested behavior safely" in prompt
    assert "src/app.py" in prompt
    assert context["data"]["diff"] in prompt
    assert "examined-all assertion records coverage, not proof" in prompt
    assert '"findings":[]' in prompt
    session = open_session(feature_repo)
    run_id = session.active_run_id()
    assert run_id is not None
    persisted = json.loads(
        (session.store.run_dir(run_id) / "review-submission-command.json").read_text()
    )
    assert persisted["manifest"] == context["data"]["review_coverage"]["manifest"]
    assert persisted["head_sha"] == context["data"]["review_coverage"]["head"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("failure", ["missing", "no_json", "timeout"])
def test_reviewer_run_failures_are_visible_in_the_review_log(
    feature_repo, tmp_path, monkeypatch, clean_reviewer_env, provider, failure
):
    fake = install_fake(tmp_path / "bin" / provider)
    configure(feature_repo, provider, packaged_command(provider))
    if failure == "missing":
        monkeypatch.setenv(f"AP_{provider.upper()}_BIN", str(tmp_path / "does-not-exist"))
        expected = "not found"
    elif failure == "no_json":
        monkeypatch.setenv(f"AP_{provider.upper()}_BIN", str(fake))
        monkeypatch.setenv("FAKE_NO_JSON", "1")
        expected = "no JSON"
    else:
        monkeypatch.setenv(f"AP_{provider.upper()}_BIN", str(fake))
        monkeypatch.setenv("FAKE_SLEEP", "1")
        monkeypatch.setenv("AP_REVIEWER_TIMEOUT", "1")
        expected = "timed out"
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    result = agent.run("review", "run", expect=ExitCode.STAGE_FAILED)

    assert result["state"] == "REVIEW_COMMAND_RED"
    logged = Path(result["data"]["log_path"]).read_text(encoding="utf-8")
    assert expected in logged
