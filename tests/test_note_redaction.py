"""Copied-file secrets and user-level configuration stay out of the published note."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from agentic_preflight import attestation
from agentic_preflight.config import load_config_layers
from agentic_preflight.findings import redact_submissions
from agentic_preflight.models import FindingSubmission
from agentic_preflight.public_config import redact_assignments
from agentic_preflight.runs._session import open_session
from tests.conftest import commit_all, git, make_run, write
from tests.driver import ScriptedAgent, findings_json

SECRET = "s3cr3t-token-value"  # noqa: S105 - inert fixture value
USER_SECRET = "user-level-secret"  # noqa: S105 - inert fixture value

# A reviewer manipulated into quoting a copied file in every free-text field.
QUOTING_REVIEWER = """\
import json
import pathlib
import sys

data = json.loads(sys.stdin.read())
value = pathlib.Path(".env").read_text(encoding="utf-8").strip().split("=", 1)[1]
unit = data["review_coverage"]["units"][0]
json.dump({
    "coverage": {"manifest": data["review_coverage"]["manifest"], "examined": "all"},
    "findings": [{
        "path": unit["path"],
        "unit": unit["id"],
        "severity": "high",
        "action": "auto_fix",
        "title": f"Token {value} is exposed",
        "detail": f"The copied file sets API_TOKEN={value}.",
        "suggestion": f"Rotate {value}.",
    }],
}, sys.stdout)
"""

CLEAN_REVIEWER = """\
import json
import sys

data = json.loads(sys.stdin.read())
json.dump({
    "coverage": {"manifest": data["review_coverage"]["manifest"], "examined": "all"},
    "findings": [],
}, sys.stdout)
"""


def _reviewer_command() -> str:
    return shlex.join([sys.executable, "reviewer.py"])


def _protect(repo: Path, reviewer: str, config: str) -> None:
    """Commit the reviewer and policy on ``main`` and rebase the feature branch onto it."""
    git("switch", "main", cwd=repo)
    write(repo, "reviewer.py", reviewer)
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, "configure the protected policy")
    git("switch", "feature/x", cwd=repo)
    git("rebase", "main", cwd=repo)


def _note(repo: Path) -> tuple[str, str]:
    """Return the noted commit and the raw note body."""
    head = git("rev-parse", "feature/x", cwd=repo)
    body = git("notes", f"--ref={attestation.NOTES_REF}", "show", head, cwd=repo)
    return head, body


def test_secret_quoted_by_a_reviewer_is_redacted_in_findings_and_the_note(feature_repo):
    write(feature_repo, ".env", f"API_TOKEN={SECRET}\n")
    _protect(
        feature_repo,
        QUOTING_REVIEWER,
        "[review]\n"
        "executor = 'command'\n"
        f"command = {_reviewer_command()!r}\n"
        "\n[docs]\nenabled = false\n"
        "\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]

    reviewed = agent.run("review", "run")
    assert reviewed["state"] == "REVIEW_BLOCKED"
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        f"{SECRET} is a test fixture value",
    )
    agent.run("verify")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    assert SECRET not in json.dumps(reviewed)
    session = open_session(feature_repo, run_id=run_id)
    findings = (session.store.run_dir(run_id) / "findings.json").read_text(encoding="utf-8")
    assert SECRET not in findings
    stored = session.store.load_findings(run_id)[0]
    assert stored.title == "Token [redacted] is exposed"
    # The whole `API_TOKEN=...` line is a copied value too, as in stage logs.
    assert stored.detail == "The copied file sets [redacted]."
    assert stored.suggestion == "Rotate [redacted]."
    assert stored.response_note == "[redacted] is a test fixture value"
    head, note = _note(feature_repo)
    assert SECRET not in note
    assert "Token [redacted] is exposed" in note
    assert attestation.verify(feature_repo, head, base="main").sha == head


def test_inline_assignment_in_a_user_level_review_command_stays_out_of_the_note(feature_repo):
    _protect(
        feature_repo,
        CLEAN_REVIEWER,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    user_command = f"REVIEW_TOKEN={USER_SECRET} {_reviewer_command()}"
    user_config = Path.home() / ".config" / "agentic-preflight" / "config.toml"
    write(
        user_config.parent,
        user_config.name,
        "[review]\n"
        "executor = 'command'\n"
        f"command = {user_command!r}\n"
        "\n[worktree]\n"
        f"setup_command = 'echo {USER_SECRET}'\n",
    )
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("review", "run")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    head, body = _note(feature_repo)
    assert USER_SECRET not in body
    note = json.loads(body)
    assert "review" not in note["config_snapshot"]
    assert "worktree" not in note["config_snapshot"]
    assert note["config_snapshot"]["commands"] == {"lint": "true", "test": "true"}
    assert note["stages"]["review"]["command"] == (f"REVIEW_TOKEN=[redacted] {_reviewer_command()}")
    for item in note["evidence"].values():
        assert "review" not in item["origin"]["config_snapshot"]
    assert attestation.verify(feature_repo, head, base="main").sha == head
    # The run itself still executes the user's configuration.
    run = open_session(feature_repo, run_id=run_id).store.load_run(run_id)
    assert run.config_snapshot["review"]["command"] == user_command
    assert run.user_config_sections == ["review", "worktree"]


def test_user_level_sections_are_those_the_repository_does_not_set(tmp_path):
    user_dir = tmp_path / "user"
    write(user_dir, "config.toml", "[review]\nmax_findings = 5\n\n[docs]\nenabled = false\n")
    repo = tmp_path / "repo"
    write(repo, ".agentic-preflight.toml", "[docs]\nenabled = true\n")

    cfg, sections = load_config_layers(repo, user_config_dir=user_dir)

    assert sections == ["review"]
    assert cfg.review.max_findings == 5
    assert cfg.docs.enabled is True


def test_a_run_without_user_level_sections_is_written_as_before():
    assert "user_config_sections" not in make_run().model_dump(mode="json")
    run = make_run().model_copy(update={"user_config_sections": ["review"]})
    assert run.model_dump(mode="json")["user_config_sections"] == ["review"]


def test_assignment_values_are_redacted_wherever_they_appear():
    assert redact_assignments("env A=1 B='two words' cmd --flag=x k=v") == (
        "env A=[redacted] B=[redacted] cmd --flag=x k=[redacted]"
    )
    assert redact_assignments('export T=abc; U="x y"z run && V=$W other') == (
        "export T=[redacted]; U=[redacted] run && V=[redacted] other"
    )
    assert redact_assignments("uv run pytest") == "uv run pytest"


def test_finding_redaction_keeps_the_length_caps():
    submission = FindingSubmission(
        path="a.py", severity="low", action="no_op", title="ab " * 66, detail="ab"
    )

    (redacted,) = redact_submissions([submission], ["ab"])

    assert len(redacted.title) == 200
    assert "ab" not in redacted.title.replace("[redacted]", "")
    assert redacted.detail == "[redacted]"
    assert redacted.suggestion is None


def test_in_harness_findings_are_redacted(feature_repo, tmp_path):
    write(feature_repo, ".env", f"API_TOKEN={SECRET}\n")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "severity": "low",
                "action": "no_op",
                "title": f"Mentions {SECRET}",
            }
        ],
    )

    env = agent.run("submit-findings", "--file", path)

    assert env["data"]["accepted"][0]["title"] == "Mentions [redacted]"
    findings = open_session(feature_repo, run_id=run_id).store.load_findings(run_id)
    assert findings[0].title == "Mentions [redacted]"
