"""Worked independent-review examples execute as documented."""

from __future__ import annotations

import json
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentic_preflight.config import Config, load_config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.runs._session import open_session
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "docs" / "examples"
PLATFORM = sys.platform

FAKE_REVIEWER = """\
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
if os.environ.get("FAKE_NO_JSON"):
    print("review completed without a structured response")
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


def install_fake(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fake = path.parent / "fake_reviewer.py" if PLATFORM == "win32" else path
    fake.write_text(FAKE_REVIEWER.format(python=sys.executable), encoding="utf-8")
    if PLATFORM == "win32":
        shim = path.with_suffix(".cmd")
        shim.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        return shim
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_install_fake_writes_windows_command_shim(tmp_path, monkeypatch, reviewer):
    monkeypatch.setattr(sys.modules[__name__], "PLATFORM", "win32")

    shim = install_fake(tmp_path / "bin with spaces" / reviewer)

    fake = shim.parent / "fake_reviewer.py"
    assert shim == fake.with_name(f"{reviewer}.cmd")
    assert shim.read_text(encoding="utf-8") == f'@"{sys.executable}" "{fake}" %*\n'


def toml_command_line(command: str) -> str:
    value = f"'{command}'" if "'" not in command else json.dumps(command)
    return f"command = {value}"


def configure_example(repo: Path, reviewer: str) -> None:
    config = (EXAMPLES / f"{reviewer}-reviewer.toml").read_text(encoding="utf-8")
    wrapper = EXAMPLES / "reviewers" / f"{reviewer}_review.py"
    configured_command = shlex.join([sys.executable, "-B", str(wrapper)])
    config = re.sub(
        r'^command = ".*"$',
        lambda _match: toml_command_line(configured_command),
        config,
        flags=re.M,
    )
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, f"configure {reviewer} reviewer example")


def test_toml_command_line_round_trips_windows_paths():
    command = (
        "'C:\\Users\\me\\python.exe' -B 'C:\\repo\\docs\\examples\\reviewers\\codex_review.py'"
    )

    parsed = tomllib.loads(f"[review]\n{toml_command_line(command)}\n")

    assert parsed["review"]["command"] == command


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize(
    ("finding", "expected_state"), [(False, "REVIEW_GREEN"), (True, "REVIEW_BLOCKED")]
)
def test_reviewer_wrapper_runs_end_to_end(
    feature_repo, tmp_path, monkeypatch, reviewer, finding, expected_state
):
    fake = install_fake(tmp_path / "bin" / reviewer)
    capture = tmp_path / "prompt.txt"
    monkeypatch.setenv(f"AP_{reviewer.upper()}_BIN", str(fake))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    if finding:
        monkeypatch.setenv("FAKE_FINDING", "1")
    configure_example(feature_repo, reviewer)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")

    result = agent.run("review", "run")

    assert result["state"] == expected_state
    prompt = capture.read_text(encoding="utf-8")
    assert "exercise the requested behavior safely" in prompt
    assert "src/app.py" in prompt
    assert context["data"]["diff"] in prompt
    session = open_session(feature_repo)
    run_id = session.active_run_id()
    assert run_id is not None
    persisted = json.loads(
        (session.store.run_dir(run_id) / "review-submission-command.json").read_text()
    )
    assert persisted["manifest"] == context["data"]["review_coverage"]["manifest"]
    assert persisted["head_sha"] == context["data"]["review_coverage"]["head"]


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("failure", ["missing", "no_json", "timeout"])
def test_reviewer_wrapper_failures_are_visible_in_review_log(
    feature_repo, tmp_path, monkeypatch, reviewer, failure
):
    fake = install_fake(tmp_path / "bin" / reviewer)
    configure_example(feature_repo, reviewer)
    if failure == "missing":
        monkeypatch.setenv(f"AP_{reviewer.upper()}_BIN", str(tmp_path / "does-not-exist"))
        expected = "not found"
    elif failure == "no_json":
        monkeypatch.setenv(f"AP_{reviewer.upper()}_BIN", str(fake))
        monkeypatch.setenv("FAKE_NO_JSON", "1")
        expected = "no JSON"
    else:
        monkeypatch.setenv(f"AP_{reviewer.upper()}_BIN", str(fake))
        monkeypatch.setenv("FAKE_SLEEP", "1")
        monkeypatch.setenv("AP_REVIEWER_TIMEOUT", "1")
        expected = "timed out"
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    result = agent.run("review", "run", expect=ExitCode.STAGE_FAILED)

    assert result["state"] == "REVIEW_COMMAND_RED"
    logged = Path(result["data"]["log_path"]).read_text(encoding="utf-8")
    assert expected in logged


def test_example_configs_parse_without_expanding_configuration_sections(tmp_path):
    for reviewer in ("codex", "claude"):
        config = EXAMPLES / f"{reviewer}-reviewer.toml"
        (tmp_path / ".agentic-preflight.toml").write_text(config.read_text(), encoding="utf-8")
        loaded = load_config(tmp_path)
        assert loaded.review.executor == "command"

    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\[([a-z]+)\]$", configuration, re.MULTILINE))
    assert documented == set(Config.model_fields)


def test_agreement_summarizer_aggregates_synthetic_reports(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "one.json").write_text(
        json.dumps(
            {
                "agreement_rate": 0.5,
                "findings": {
                    "only_a": [{}, {}],
                    "only_b": [{}],
                    "severity_disagreements": [{}],
                },
            }
        ),
        encoding="utf-8",
    )
    (reports / "two.json").write_text(
        json.dumps(
            {
                "agreement_rate": 1.0,
                "findings": {
                    "only_a": [],
                    "only_b": [{}, {}],
                    "severity_disagreements": [{}, {}],
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "summarize-agreement.py"), str(reports)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "runs" in result.stdout
    assert "2" in result.stdout
    assert "0.750" in result.stdout
    assert re.search(r"\b2\s+3\s+3\b", result.stdout)
