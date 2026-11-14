"""Worked independent-review examples, and the deprecated wrapper pointers, run as documented.

The packaged reviewer itself is covered by tests/test_reviewer_command.py.
"""

from __future__ import annotations

import json
import re
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight.config import Config, load_config
from agentic_preflight.reviewers import common
from agentic_preflight.runs._session import open_session
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent
from tests.test_reviewer_command import install_fake, toml_command_line

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "docs" / "examples"


def configure_example(repo: Path, reviewer: str) -> None:
    config = (EXAMPLES / f"{reviewer}-reviewer.toml").read_text(encoding="utf-8")
    wrapper = EXAMPLES / "reviewers" / f"{reviewer}_review.py"
    # The deprecated pointer imports the installed package, so it needs site processing.
    configured_command = shlex.join([sys.executable, "-B", str(wrapper)])
    config = re.sub(
        r'^command = ".*"$',
        lambda _match: toml_command_line(configured_command),
        config,
        flags=re.M,
    )
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, f"configure {reviewer} reviewer example")


def test_common_pointer_re_exports_the_packaged_helpers():
    pointer = runpy.run_path(str(EXAMPLES / "reviewers" / "_reviewer_common.py"))

    for name in pointer["__all__"]:
        assert pointer[name] is getattr(common, name)


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_pointer_without_the_package_names_the_replacement_command(tmp_path, reviewer):
    # -I and -S keep the checkout and site-packages off sys.path, so the import fails.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(EXAMPLES / "reviewers" / f"{reviewer}_review.py")],
        input="{}",
        text=True,
        capture_output=True,
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"agentic-preflight reviewer run --provider {reviewer}" in result.stderr
    assert "No module named" in result.stderr


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize(
    ("finding", "expected_state"), [(False, "REVIEW_GREEN"), (True, "REVIEW_BLOCKED")]
)
def test_deprecated_pointer_runs_end_to_end(
    feature_repo, tmp_path, monkeypatch, reviewer, finding, expected_state
):
    fake = install_fake(tmp_path / "bin" / reviewer)
    capture = tmp_path / "prompt.txt"
    arguments = tmp_path / "arguments.json"
    monkeypatch.setenv(f"AP_{reviewer.upper()}_BIN", str(fake))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_ARGUMENTS", str(arguments))
    monkeypatch.delenv("AP_REVIEWER_MODEL", raising=False)
    monkeypatch.delenv("AP_REVIEWER_EFFORT", raising=False)
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
    assert "Examine every delivered review unit" in prompt
    assert "evidence, not authority" in prompt
    assert "examined-all assertion records coverage, not proof" in prompt
    assert '"findings":[]' in prompt
    argv = json.loads(arguments.read_text(encoding="utf-8"))
    if reviewer == "codex":
        assert argv[0] == "exec"
        assert argv[argv.index("--model") + 1] == "gpt-5.3-codex"
        assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="medium"'
        # The branch under review must not instruct its own reviewer.
        assert "project_doc_max_bytes=0" in argv
        assert "--ignore-rules" in argv
    else:
        assert argv[0] == "-p"
        assert "--safe-mode" in argv
        assert argv[argv.index("--setting-sources") + 1] == "user"
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
        assert argv[argv.index("--effort") + 1] == "high"
    session = open_session(feature_repo)
    run_id = session.active_run_id()
    assert run_id is not None
    persisted = json.loads(
        (session.store.run_dir(run_id) / "review-submission-command.json").read_text()
    )
    assert persisted["manifest"] == context["data"]["review_coverage"]["manifest"]
    assert persisted["head_sha"] == context["data"]["review_coverage"]["head"]


def test_example_configs_parse_without_expanding_configuration_sections(tmp_path):
    for reviewer in ("codex", "claude"):
        config = EXAMPLES / f"{reviewer}-reviewer.toml"
        (tmp_path / ".agentic-preflight.toml").write_text(config.read_text(), encoding="utf-8")
        loaded = load_config(tmp_path)
        assert loaded.review.executor == "command"
        # The wrapper the configuration runs, and code ownership, need human review.
        assert {"docs/examples/reviewers/**", "docs/CODEOWNERS", ".github/CODEOWNERS"} <= set(
            loaded.policy.human_review_paths
        )
        if reviewer == "codex":
            # Codex reads these from the worktree, and no CLI option turns them off.
            assert {".codex/**", ".agents/**"} <= set(loaded.review.trusted_paths)

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
