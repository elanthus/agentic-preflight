from __future__ import annotations

import json
import shlex
import sys

import pytest

from agentic_preflight.config import ConfigError, load_config
from agentic_preflight.errors import ExitCode
from agentic_preflight.grounding import digest
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent


def _write_sources(repo, *, context: str = "") -> None:
    write(repo, ".github/CODEOWNERS", "/src/ @alice @bob\n")
    write(
        repo,
        "docs/adr/0001-app.md",
        "# Application\n\nThe implementation lives in src/app.py.\n"
        "Review that module carefully when its greeting changes.\n",
    )
    write(repo, "docs/unrelated.md", "# Unrelated\n\nNothing relevant here.\n")
    write(repo, "AGENTS.md", "Keep application changes deterministic.\n")
    config = "[policy]\nhigh_risk_paths = ['src/**']\n"
    if context:
        config += f"\n[context]\n{context}"
    write(repo, ".agentic-preflight.toml", config)


def _submit(agent: ScriptedAgent, path, findings: list[dict]) -> dict:
    path.write_text(
        json.dumps(
            {
                "coverage": {"manifest": "$context", "examined": "all"},
                "findings": findings,
            }
        ),
        encoding="utf-8",
    )
    return agent.run("submit-findings", "--file", str(path))


@pytest.fixture
def grounded_repo(feature_repo, tmp_path):
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")

    prior_agent = ScriptedAgent(feature_repo)
    started = prior_agent.run("start")
    prior_run_id = started["run_id"]
    prior_agent.run("context")
    _submit(
        prior_agent,
        tmp_path / "prior-findings.json",
        [
            {
                "path": "src/app.py",
                "severity": "low",
                "action": "auto_fix",
                "title": "Keep a prior observation",
                "detail": "This finding becomes repository-local review history.",
            }
        ],
    )
    prior_agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--note",
        "Recorded for later grounded review.",
    )
    prior_agent.run("abort")

    write(
        feature_repo,
        "src/app.py",
        "def greet(name, loud=False):\n"
        "    greeting = f'hi {name}'\n"
        "    return greeting.upper() if loud else greeting\n",
    )
    commit_all(feature_repo, "finish loud greeting")
    return feature_repo, prior_run_id


def test_context_retrieves_all_repository_grounding_sources(grounded_repo):
    repo, prior_run_id = grounded_repo
    agent = ScriptedAgent(repo)
    agent.run("start")
    grounding = agent.run("context")["data"]["grounding"]
    entries = grounding["entries"]

    codeowners = [entry for entry in entries if entry["kind"] == "codeowners"]
    assert any(
        entry["path"] == "src/app.py"
        and entry["owners"] == ["@alice", "@bob"]
        and entry["pattern"] == "/src/"
        for entry in codeowners
    )
    docs = [entry for entry in entries if entry["kind"] == "doc"]
    assert any(entry["source"] == "docs/adr/0001-app.md" for entry in docs)
    assert all(entry["source"] != "docs/unrelated.md" for entry in docs)
    assert any(
        entry["kind"] == "convention" and entry["source"] == "AGENTS.md" for entry in entries
    )
    assert any(
        entry["kind"] == "prior_finding"
        and entry["source"] == prior_run_id
        and entry["status"] == "accepted"
        for entry in entries
    )
    assert any(entry["kind"] == "policy" for entry in entries)
    assert all(entry["bytes"] > 0 and isinstance(entry["truncated"], bool) for entry in entries)


def test_codeowners_uses_the_first_file_and_last_matching_rule(feature_repo):
    write(
        feature_repo,
        ".github/CODEOWNERS",
        "/src/ @directory-owner\n/src/app.py @specific-owner\n",
    )
    write(feature_repo, "CODEOWNERS", "/src/ @ignored-owner\n")
    commit_all(feature_repo, "add layered code ownership")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    entries = agent.run("context")["data"]["grounding"]["entries"]
    owner = next(
        entry
        for entry in entries
        if entry["kind"] == "codeowners" and entry["path"] == "src/app.py"
    )

    assert owner["source"] == ".github/CODEOWNERS"
    assert owner["pattern"] == "/src/app.py"
    assert owner["owners"] == ["@specific-owner"]


def test_total_budget_keeps_whole_early_entries_and_reports_later_drops(feature_repo):
    _write_sources(feature_repo, context="max_bytes = 220\n")
    commit_all(feature_repo, "set a small context budget")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    grounding = agent.run("context")["data"]["grounding"]

    assert [entry["kind"] for entry in grounding["entries"]] == ["codeowners"]
    assert grounding["dropped"]["doc"] >= 1
    assert grounding["dropped"]["convention"] >= 1
    assert grounding["dropped"]["policy"] >= 1
    assert sum(entry["bytes"] for entry in grounding["entries"]) <= 220


def test_entry_budget_truncates_doc_excerpt_on_a_line_boundary(feature_repo):
    _write_sources(feature_repo, context="entry_max_bytes = 64\n")
    write(
        feature_repo,
        "docs/adr/0001-app.md",
        "intro context\n"
        "src/app.py is the application module that owns the greeting behavior.\n"
        "outro context that should be omitted from a very small entry budget\n",
    )
    commit_all(feature_repo, "set a small grounding entry budget")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    grounding = agent.run("context")["data"]["grounding"]
    adr = next(
        entry
        for entry in grounding["entries"]
        if entry["kind"] == "doc" and entry["source"] == "docs/adr/0001-app.md"
    )

    assert adr["truncated"] is True
    assert len(adr["excerpt"].encode()) <= 64
    assert not adr["excerpt"] or adr["excerpt"].endswith("\n")


def test_context_is_byte_stable_and_manifest_is_grounding_bound(grounded_repo):
    repo, _ = grounded_repo
    agent = ScriptedAgent(repo)
    agent.run("start")

    first = agent.run("context")["data"]
    second = agent.run("context")["data"]

    assert json.dumps(first["grounding"], sort_keys=True) == json.dumps(
        second["grounding"], sort_keys=True
    )
    assert first["review_coverage"]["manifest"] == second["review_coverage"]["manifest"]
    assert (
        first["review_coverage"]["grounding_sha256"]
        == second["review_coverage"]["grounding_sha256"]
    )


def test_policy_grounding_stays_stable_after_a_finding_is_accepted(feature_repo, tmp_path):
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    review = agent.run("context")["data"]
    submitted = _submit(
        agent,
        tmp_path / "blocking-findings.json",
        [
            {
                "path": "src/app.py",
                "severity": "high",
                "action": "auto_fix",
                "title": "Record a blocking review finding",
                "detail": "The current run's output must not become repository grounding.",
            }
        ],
    )
    assert submitted["state"] == "REVIEW_BLOCKED"
    assert any(reason["kind"] == "finding" for reason in submitted["data"]["risk"]["reasons"])

    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--note",
        "Accepted to keep the reviewed snapshot unchanged.",
    )
    verified = agent.run("verify")
    assert verified["state"] == "REVIEW_GREEN"

    docs = agent.run("context", "--section", "docs")["data"]
    review_policy = [entry for entry in review["grounding"]["entries"] if entry["kind"] == "policy"]
    docs_policy = [entry for entry in docs["grounding"]["entries"] if entry["kind"] == "policy"]

    assert docs_policy == review_policy
    assert all(entry["reason"]["kind"] != "finding" for entry in docs_policy)
    assert digest(docs["grounding"]) == review["review_coverage"]["grounding_sha256"]


def test_committed_convention_change_invalidates_an_old_manifest(feature_repo, tmp_path):
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")
    old_agent = ScriptedAgent(feature_repo)
    old_agent.run("start")
    old_context = old_agent.run("context")["data"]
    old_manifest = old_context["review_coverage"]["manifest"]
    old_digest = old_context["review_coverage"]["grounding_sha256"]
    old_agent.run("abort")

    write(feature_repo, "AGENTS.md", "Keep application changes deterministic and offline.\n")
    commit_all(feature_repo, "strengthen repository convention")
    new_agent = ScriptedAgent(feature_repo)
    new_agent.run("start")
    new_context = new_agent.run("context")["data"]
    assert new_context["review_coverage"]["grounding_sha256"] != old_digest
    assert new_context["review_coverage"]["manifest"] != old_manifest

    payload = tmp_path / "stale-findings.json"
    payload.write_text(
        json.dumps(
            {
                "coverage": {"manifest": old_manifest, "examined": "all"},
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    rejected = new_agent.run(
        "submit-findings", "--file", str(payload), expect=ExitCode.PRECONDITION
    )
    assert "review coverage does not match" in rejected["error"]["message"]


def test_disabled_grounding_is_empty_and_manifest_remains_valid(feature_repo):
    _write_sources(feature_repo, context="enabled = false\n")
    commit_all(feature_repo, "disable grounded context")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    data = agent.run("context")["data"]

    assert data["grounding"] == {"enabled": False, "entries": [], "dropped": {}}
    assert len(data["review_coverage"]["grounding_sha256"]) == 64
    assert len(data["review_coverage"]["manifest"]) == 64


def test_docs_context_carries_the_same_grounding_bundle(feature_repo, tmp_path):
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    review = agent.run("context")["data"]
    _submit(agent, tmp_path / "clean-findings.json", [])

    docs = agent.run("context", "--section", "docs")["data"]

    assert docs["grounding"] == review["grounding"]


def test_review_command_receives_grounding_in_its_stdin_bundle(feature_repo, tmp_path):
    reviewer = tmp_path / "reviewer.py"
    captured = tmp_path / "review-context.json"
    reviewer.write_text(
        "import json, pathlib, sys\n"
        "data = json.load(sys.stdin)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(data, sort_keys=True))\n"
        "json.dump({'coverage': {'manifest': data['review_coverage']['manifest'], "
        "'examined': 'all'}, 'findings': []}, sys.stdout)\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(reviewer), str(captured)])
    write(
        feature_repo,
        ".agentic-preflight.toml",
        f"[review]\nexecutor = 'command'\ncommand = {json.dumps(command)}\n",
    )
    commit_all(feature_repo, "configure grounded command review")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    result = agent.run("review", "run")
    delivered = json.loads(captured.read_text(encoding="utf-8"))

    assert result["state"] == "REVIEW_GREEN"
    assert delivered["grounding"]["enabled"] is True
    assert delivered["review_coverage"]["grounding_sha256"]


def test_context_extra_paths_add_repo_owned_conventions(feature_repo):
    _write_sources(feature_repo, context="extra_paths = ['rules/*.md']\n")
    write(feature_repo, "rules/security.md", "Keep security-sensitive changes auditable.\n")
    commit_all(feature_repo, "add extra grounding rules")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    entries = agent.run("context")["data"]["grounding"]["entries"]

    assert any(
        entry["kind"] == "convention" and entry["source"] == "rules/security.md"
        for entry in entries
    )


def test_context_extra_paths_reject_parent_traversal(feature_repo, tmp_path):
    write(feature_repo, ".agentic-preflight.toml", "[context]\nextra_paths = ['../rules.md']\n")

    with pytest.raises(ConfigError, match=r"\[context\] extra_paths"):
        load_config(feature_repo, user_config_dir=tmp_path / "nowhere")
