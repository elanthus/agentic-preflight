"""Independent review comparison without changing the gate state."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import FindingAction, FindingSubmission, Severity
from agentic_preflight.runs._session import open_session
from agentic_preflight.runs.review_compare import _match_findings
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent
from tests.test_review_executor import REVIEWER


def _finding(line: int) -> FindingSubmission:
    return FindingSubmission(
        path="src/app.py",
        unit="U0001",
        line=line,
        severity=Severity.MEDIUM,
        action=FindingAction.ASK_USER,
        title="finding",
    )


def test_match_findings_prefers_more_matches_over_nearest_line():
    """A at 13 then 10; B at 10 then 16: 10-10 and 13-16 beat a lone 13-10 pair."""
    a_findings = [_finding(13), _finding(10)]
    b_findings = [_finding(10), _finding(16)]

    matches = _match_findings(a_findings, b_findings)

    assert matches == {0: 1, 1: 0}


def test_match_findings_finds_maximum_matching_not_just_the_first_available():
    """A0 can pair with B0 or B1; A1 can only pair with B0. Claiming B0 for A0
    first (its earliest match by index) strands A1, even though reassigning
    A0 to B1 lets both sides match."""
    a_findings = [_finding(10), _finding(8)]
    b_findings = [_finding(10), _finding(12)]

    matches = _match_findings(a_findings, b_findings)

    assert matches == {0: 1, 1: 0}


def toml_command_line(command: str) -> str:
    value = f"'{command}'" if "'" not in command else json.dumps(command)
    return f"command = {value}"


def configure_in_harness(repo: Path, *, shadow_capture: Path | None = None) -> None:
    command = ""
    if shadow_capture is not None:
        write(repo, "reviewer.py", REVIEWER)
        command = toml_command_line(
            shlex.join([sys.executable, "reviewer.py", "valid", str(shadow_capture)])
        )
        command += "\n"
    write(
        repo,
        ".agentic-preflight.toml",
        f"[review]\nexecutor = 'in_harness'\n{command}\n[docs]\nenabled = false\n",
    )
    commit_all(repo, "configure in-harness review")


def submit_review(agent: ScriptedAgent, path: Path, findings: list[dict]) -> dict:
    context = agent.run("context")
    path.write_text(
        json.dumps(
            {
                "coverage": {
                    "manifest": context["data"]["review_coverage"]["manifest"],
                    "examined": "all",
                },
                "findings": findings,
            }
        ),
        encoding="utf-8",
    )
    return agent.run("submit-findings", "--file", str(path))


def test_compare_records_agreement_event_and_summary_file(feature_repo, tmp_path):
    write(
        feature_repo,
        "src/app.py",
        "\n".join(f"value_{line} = {line}" for line in range(1, 21)) + "\n",
    )
    configure_in_harness(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    unit = next(
        item["id"]
        for item in context["data"]["review_coverage"]["units"]
        if item["path"] == "src/app.py"
    )
    manifest = context["data"]["review_coverage"]["manifest"]
    first = tmp_path / "in-harness.json"
    first.write_text(
        json.dumps(
            {
                "coverage": {"manifest": manifest, "examined": "all"},
                "findings": [
                    {
                        "unit": unit,
                        "path": "src/app.py",
                        "line": 2,
                        "severity": "medium",
                        "action": "no_op",
                        "title": "Shared location",
                    },
                    {
                        "unit": unit,
                        "path": "src/app.py",
                        "line": 10,
                        "severity": "medium",
                        "action": "no_op",
                        "title": "Only the in-harness reviewer",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    submitted = agent.run("submit-findings", "--file", str(first))
    assert submitted["state"] == "DOCS_GREEN"

    second = tmp_path / "command.json"
    second.write_text(
        json.dumps(
            {
                "coverage": {"manifest": manifest, "examined": "all"},
                "findings": [
                    {
                        "unit": unit,
                        "path": "src/app.py",
                        "line": 4,
                        "severity": "low",
                        "action": "no_op",
                        "title": "Same location, different severity",
                    },
                    {
                        "unit": unit,
                        "path": "src/app.py",
                        "line": 18,
                        "severity": "medium",
                        "action": "no_op",
                        "title": "Only the command reviewer",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    compared = agent.run("review", "compare", "--file", str(second))

    summary = compared["data"]
    assert compared["state"] == "DOCS_GREEN"
    assert summary["executors"] == ["in_harness", "command"]
    assert summary["units"] == {
        "total": 2,
        "both_flagged": 1,
        "only_a": 0,
        "only_b": 0,
        "neither": 1,
    }
    assert len(summary["findings"]["agreed"]) == 1
    assert len(summary["findings"]["only_a"]) == 1
    assert len(summary["findings"]["only_b"]) == 1
    assert len(summary["findings"]["severity_disagreements"]) == 1
    assert summary["agreement_rate"] == 1.0

    session = open_session(feature_repo)
    run_id = session.active_run_id()
    assert run_id is not None
    stored = json.loads((session.store.run_dir(run_id) / "review-compare.json").read_text())
    assert stored == summary
    event = session.store.load_events(run_id)[-1]
    assert event["event"] == "review_compared"
    assert {key: event[key] for key in summary} == summary


def test_shadow_compare_preserves_state_and_findings(feature_repo, tmp_path):
    capture = tmp_path / "shadow-input.json"
    configure_in_harness(feature_repo, shadow_capture=capture)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    submit_review(agent, tmp_path / "clean.json", [])
    session = open_session(feature_repo)
    run_id = session.active_run_id()
    assert run_id is not None
    before_state = agent.run("status")["state"]
    findings_path = session.store.findings_path(run_id)
    before_findings = findings_path.read_bytes()

    compared = agent.run("review", "compare")

    assert compared["state"] == before_state
    assert agent.run("status")["state"] == before_state
    assert findings_path.read_bytes() == before_findings
    assert compared["data"]["executors"] == ["in_harness", "command"]
    assert (session.store.logs_dir(run_id) / "review-compare.txt").exists()
    assert json.loads(capture.read_text(encoding="utf-8")) == context["data"]


def test_compare_uses_grounded_context_manifest(feature_repo, tmp_path):
    write(feature_repo, "AGENTS.md", "# Repository guidance\n\nReview every changed line.\n")
    configure_in_harness(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    coverage = context["data"]["review_coverage"]
    unit = next(item["id"] for item in coverage["units"] if item["path"] == "src/app.py")
    submitted = submit_review(
        agent,
        tmp_path / "finding.json",
        [
            {
                "unit": unit,
                "path": "src/app.py",
                "line": 1,
                "severity": "high",
                "action": "auto_fix",
                "title": "Record finding-derived risk",
            }
        ],
    )
    assert any(reason["kind"] == "finding" for reason in submitted["data"]["risk"]["reasons"])
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--note",
        "Accepted to keep the reviewed HEAD unchanged.",
    )
    agent.run("verify")
    second = tmp_path / "command.json"
    second.write_text(
        json.dumps(
            {
                "coverage": {"manifest": coverage["manifest"], "examined": "all"},
                "findings": [],
            }
        ),
        encoding="utf-8",
    )

    compared = agent.run("review", "compare", "--file", str(second))

    assert compared["data"]["manifest"] == coverage["manifest"]
    assert re.fullmatch(r"[0-9a-f]{64}", coverage["grounding_sha256"])


def test_compare_refuses_when_reviewed_head_is_stale(feature_repo, tmp_path):
    configure_in_harness(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    submit_review(agent, tmp_path / "clean.json", [])
    write(feature_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit_all(feature_repo, "move head after review")
    second = tmp_path / "command.json"
    second.write_text(
        json.dumps(
            {
                "coverage": {
                    "manifest": context["data"]["review_coverage"]["manifest"],
                    "examined": "all",
                },
                "findings": [],
            }
        ),
        encoding="utf-8",
    )

    refused = agent.run("review", "compare", "--file", str(second), expect=ExitCode.PRECONDITION)

    assert "HEAD" in refused["error"]["message"]
    assert refused["state"] == "DOCS_GREEN"
