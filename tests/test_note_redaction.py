"""Copied-file values and user-level configuration stay out of the published note."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

from agentic_preflight import attestation
from agentic_preflight.config import load_config_layers
from agentic_preflight.digests import json_digest
from agentic_preflight.findings import redact_submissions
from agentic_preflight.fingerprints import docs_relevant_config, review_relevant_config
from agentic_preflight.models import Finding, FindingSubmission, Stage
from agentic_preflight.public_config import REDACTED_COMMAND
from agentic_preflight.runs import evidence
from agentic_preflight.runs._session import open_session
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, make_run, write
from tests.driver import ScriptedAgent, findings_json

# Inert fixture values. Each is unique, so its absence from a note is meaningful.
COPIED_VALUE = "fixture-value-7f3a"
USER_VALUE = "user-level-value-91c2"

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
        "title": f"Value {value} is exposed",
        "detail": f"The copied file sets SAMPLE_SETTING={value}.",
        "suggestion": f"Replace {value}.",
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

PROTECTED = "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"


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


def _user_config(body: str) -> None:
    path = Path.home() / ".config" / "agentic-preflight" / "config.toml"
    write(path.parent, path.name, body)


def _note(repo: Path) -> tuple[str, str]:
    """Return the noted commit and the raw note body."""
    head = git("rev-parse", "feature/x", cwd=repo)
    body = git("notes", f"--ref={attestation.NOTES_REF}", "show", head, cwd=repo)
    return head, body


def test_a_copied_value_quoted_by_a_reviewer_is_redacted_in_findings_and_the_note(feature_repo):
    write(feature_repo, ".env", f"SAMPLE_SETTING={COPIED_VALUE}\n")
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
        f"{COPIED_VALUE} is a fixture",
    )
    agent.run("verify")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    assert COPIED_VALUE not in json.dumps(reviewed)
    session = open_session(feature_repo, run_id=run_id)
    findings = (session.store.run_dir(run_id) / "findings.json").read_text(encoding="utf-8")
    assert COPIED_VALUE not in findings
    stored = session.store.load_findings(run_id)[0]
    assert stored.title == "Value [redacted] is exposed"
    # The whole `SAMPLE_SETTING=...` line is a copied value too, as in stage logs.
    assert stored.detail == "The copied file sets [redacted]."
    assert stored.suggestion == "Replace [redacted]."
    assert stored.response_note == "[redacted] is a fixture"
    head, note = _note(feature_repo)
    assert COPIED_VALUE not in note
    assert "Value [redacted] is exposed" in note
    assert attestation.verify(feature_repo, head, base="main").sha == head


def test_a_user_level_review_command_stays_out_of_the_note(feature_repo):
    _protect(feature_repo, CLEAN_REVIEWER, PROTECTED)
    user_command = f"sh -c 'SAMPLE_SETTING={USER_VALUE} {_reviewer_command()}'"
    _user_config(
        "[review]\n"
        "executor = 'command'\n"
        f"command = {json.dumps(user_command)}\n"
        "\n[worktree]\n"
        f"setup_command = 'echo {USER_VALUE}'\n"
    )
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("review", "run")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    head, body = _note(feature_repo)
    assert USER_VALUE not in body
    note = json.loads(body)
    assert "review" not in note["config_snapshot"]
    assert "worktree" not in note["config_snapshot"]
    assert note["config_snapshot"]["commands"] == {"lint": "true", "test": "true"}
    assert note["stages"]["review"]["command"] == REDACTED_COMMAND
    assert note["stages"]["test"]["command"] == "true"
    for item in note["evidence"].values():
        assert "review" not in item["origin"]["config_snapshot"]
    assert attestation.verify(feature_repo, head, base="main").sha == head
    # The run itself still executes the user's configuration.
    run = open_session(feature_repo, run_id=run_id).store.load_run(run_id)
    assert run.config_snapshot["review"]["command"] == user_command
    assert run.user_config_sections == ["review", "worktree"]


def test_a_user_level_test_command_is_published_as_a_marker(feature_repo, tmp_path):
    _protect(feature_repo, CLEAN_REVIEWER, "[docs]\nenabled = false\n")
    _user_config(f"[commands]\nlint = 'true'\ntest = 'SAMPLE_SETTING={USER_VALUE} true'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    head, body = _note(feature_repo)
    assert USER_VALUE not in body
    note = json.loads(body)
    assert note["stages"]["lint"]["command"] == REDACTED_COMMAND
    assert note["stages"]["test"]["command"] == REDACTED_COMMAND
    assert attestation.verify(feature_repo, head, base="main").sha == head


def _as_earlier_release(store: Store, run_id: str) -> None:
    """Rewrite a finished run record the way a release without this fix wrote it."""
    path = store.run_dir(run_id) / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    full = payload["config_snapshot"]
    payload.pop("user_config_sections")
    for item in payload["evidence"].values():
        origin = item["origin"]
        origin["config_snapshot"] = full
        origin["config_sha256"] = json_digest(full)
        relevant = review_relevant_config if origin["stage"] == "review" else docs_relevant_config
        if origin["stage"] in {"review", "docs"}:
            origin["fingerprint"]["config_sha256"] = json_digest(relevant(full))
            item["fingerprint"] = origin["fingerprint"]
        item["origin_sha256"] = json_digest(origin)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_evidence_from_an_earlier_release_record_does_not_carry_user_config(feature_repo, tmp_path):
    _protect(feature_repo, CLEAN_REVIEWER, PROTECTED)
    _user_config(f"[review]\ncommand = 'SAMPLE_SETTING={USER_VALUE} true'\n")
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start")["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    agent.run("abort", "--force")
    store = Store(feature_repo / ".git" / "agentic-preflight")
    _as_earlier_release(store, first)
    legacy = store.load_run(first).evidence[Stage.DOCS]
    assert USER_VALUE in json.dumps(legacy.model_dump(mode="json"))

    started = agent.run("start")
    assert started["data"]["applicability"]["docs"]["disposition"] != "reusable"
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    head, body = _note(feature_repo)
    assert USER_VALUE not in body
    assert {item["origin"]["run_id"] for item in json.loads(body)["evidence"].values()} == {
        started["run_id"]
    }
    assert attestation.verify(feature_repo, head, base="main").sha == head


def test_reuse_refuses_evidence_that_would_publish_withheld_or_unredacted_text():
    finding = Finding(
        id="F001", stage=Stage.REVIEW, path="a.py", severity="low", action="no_op", title="x"
    )
    run = make_run().model_copy(update={"user_config_sections": ["review", "diff"]})

    def item(snapshot: dict, title: str) -> SimpleNamespace:
        found = finding.model_copy(update={"title": title})
        return SimpleNamespace(origin=SimpleNamespace(config_snapshot=snapshot, findings=[found]))

    assert evidence._publishable(item({"diff": {}}, "plain"), run, [COPIED_VALUE])
    assert not evidence._publishable(item({"review": {}}, "plain"), run, [COPIED_VALUE])
    assert not evidence._publishable(item({}, f"has {COPIED_VALUE}"), run, [COPIED_VALUE])
    # Unreadable copied files fail closed.
    assert not evidence._publishable(item({}, "plain"), run, None)


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


def test_finding_redaction_keeps_the_length_caps():
    submission = FindingSubmission(
        path="a.py", severity="low", action="no_op", title="ab " * 66, detail="ab"
    )

    (redacted,) = redact_submissions([submission], ["ab"])

    assert len(redacted.title) <= 200
    assert redacted.title.rstrip().endswith("[redacted]")
    assert "ab" not in redacted.title.replace("[redacted]", "")
    assert redacted.detail == "[redacted]"
    assert redacted.suggestion is None


def test_in_harness_findings_are_redacted(feature_repo, tmp_path):
    write(feature_repo, ".env", f"SAMPLE_SETTING={COPIED_VALUE}\n")
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
                "title": f"Mentions {COPIED_VALUE}",
            }
        ],
    )

    env = agent.run("submit-findings", "--file", path)

    assert env["data"]["accepted"][0]["title"] == "Mentions [redacted]"
    findings = open_session(feature_repo, run_id=run_id).store.load_findings(run_id)
    assert findings[0].title == "Mentions [redacted]"


def test_review_compare_redacts_the_second_submission(feature_repo, tmp_path):
    write(feature_repo, ".env", f"SAMPLE_SETTING={COPIED_VALUE}\n")
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[review]\nexecutor = 'in_harness'\n\n[docs]\nenabled = false\n",
    )
    commit_all(feature_repo, "configure in-harness review")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    context = agent.run("context")
    manifest = context["data"]["review_coverage"]["manifest"]
    unit = context["data"]["review_coverage"]["units"][0]
    first = tmp_path / "first.json"
    first.write_text(
        json.dumps({"coverage": {"manifest": manifest, "examined": "all"}, "findings": []})
    )
    agent.run("submit-findings", "--file", str(first))
    finding = {
        "unit": unit["id"],
        "path": unit["path"],
        "severity": "low",
        "action": "no_op",
        "title": f"Quotes {COPIED_VALUE}",
    }
    second = tmp_path / "second.json"
    second.write_text(
        json.dumps({"coverage": {"manifest": manifest, "examined": "all"}, "findings": [finding]})
    )

    compared = agent.run("review", "compare", "--file", str(second))

    assert COPIED_VALUE not in json.dumps(compared)
    run_dir = open_session(feature_repo, run_id=run_id).store.run_dir(run_id)
    assert COPIED_VALUE not in (run_dir / "review-compare.json").read_text(encoding="utf-8")
    assert COPIED_VALUE not in (run_dir / "events.jsonl").read_text(encoding="utf-8")
