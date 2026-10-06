from __future__ import annotations

import json
import random
import re
import shlex
import sys
import time
from pathlib import Path

import pytest

from agentic_preflight.config import ConfigError, load_config
from agentic_preflight.errors import ExitCode
from agentic_preflight.grounding import (
    _compile_terms,
    _doc_entries,
    _matching_terms,
    _terms,
    digest,
)
from agentic_preflight.runs._session import open_session
from tests.conftest import commit_all, git, write
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


def _concurrent_worktree_on_another_branch(repo: Path, tmp_path: Path, branch: str) -> Path:
    """A linked worktree on its own branch that also touches ``src/app.py``.

    Shares ``repo``'s git-common-dir run store, the way the "reusable" and
    "strict" worktree modes' genuinely concurrent runs do.
    """
    path = tmp_path / branch.replace("/", "-")
    git("branch", branch, "main", cwd=repo)
    git("worktree", "add", str(path), branch, cwd=repo)
    write(path, "src/app.py", "def greet(name):\n    return f'hey {name}'\n")
    commit_all(path, "change the greeting from a concurrent worktree")
    return path


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


def test_context_skips_a_same_branch_prior_run_with_corrupt_findings(grounded_repo):
    repo, prior_run_id = grounded_repo
    session = open_session(repo)
    session.store.findings_path(prior_run_id).write_text("not json", encoding="utf-8")

    agent = ScriptedAgent(repo)
    agent.run("start")
    entries = agent.run("context")["data"]["grounding"]["entries"]

    assert all(entry.get("source") != prior_run_id for entry in entries)


def test_grounding_ignores_a_finding_from_a_concurrent_run_on_another_branch(
    feature_repo, tmp_path
):
    """A concurrent run in another linked worktree must not flip `grounding_sha256`.

    It shares the same git-common-dir run store, and records a finding on a
    path this run also changed, in the window between this run's `context`
    call and its `submit-findings` call.
    """
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")
    concurrent_repo = _concurrent_worktree_on_another_branch(feature_repo, tmp_path, "feature/y")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    first = agent.run("context")["data"]
    first_manifest = first["review_coverage"]["manifest"]
    first_digest = first["review_coverage"]["grounding_sha256"]

    concurrent_agent = ScriptedAgent(concurrent_repo)
    concurrent_agent.run("start")
    concurrent_agent.run("context")
    _submit(
        concurrent_agent,
        tmp_path / "concurrent-findings.json",
        [
            {
                "path": "src/app.py",
                "severity": "low",
                "action": "auto_fix",
                "title": "Recorded from an unrelated concurrent branch",
                "detail": "A different worktree's own review, on a different branch.",
            }
        ],
    )

    second = agent.run("context")["data"]
    assert second["review_coverage"]["grounding_sha256"] == first_digest
    assert second["review_coverage"]["manifest"] == first_manifest
    assert all(entry["kind"] != "prior_finding" for entry in second["grounding"]["entries"])

    payload = tmp_path / "first-findings.json"
    payload.write_text(
        json.dumps({"coverage": {"manifest": first_manifest, "examined": "all"}, "findings": []}),
        encoding="utf-8",
    )
    submitted = agent.run("submit-findings", "--file", str(payload))
    assert submitted["state"] == "REVIEW_GREEN"


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


def test_context_skips_a_tracked_source_staged_but_not_committed(feature_repo):
    _write_sources(feature_repo)
    commit_all(feature_repo, "add repository context")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    write(feature_repo, "docs/staged.md", "src/app.py is mentioned here but never committed.\n")
    git("add", "docs/staged.md", cwd=feature_repo)

    entries = agent.run("context")["data"]["grounding"]["entries"]

    assert all(entry.get("source") != "docs/staged.md" for entry in entries)


def test_terms_treat_any_package_module_like_the_tool_package():
    own = _terms(["agentic_preflight/sub/mod.py"])
    other = _terms(["mypkg/sub/mod.py"])
    assert {"sub/mod.py", "agentic_preflight.sub.mod"} <= set(own)
    assert {"sub/mod.py", "mypkg.sub.mod"} <= set(other)
    assert {term.replace("agentic_preflight", "mypkg") for term in own} == set(other)


def _reference_matches(text: str, terms: list[str]) -> list[str]:
    return [
        term
        for term in terms
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text)
    ]


def _matched(text: str, terms: list[str]) -> list[str]:
    return [term.text for term in _matching_terms(text, _compile_terms(terms))]


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("See src/app.py.", "src/app.py", True),
        ("See mysrc/app.py.", "src/app.py", False),
        ("See src/app.pyc.", "src/app.py", False),
        ("Edit stages.py today.", "stages", True),
        ("The substages module.", "stages", False),
        ("Rules in .github/CODEOWNERS apply.", ".github/CODEOWNERS", True),
        ("Rules in x.github/CODEOWNERS apply.", ".github/CODEOWNERS", False),
        ("Use éapp.py here.", "app.py", True),
        ("src/app.py\nwraps lines", "app.py\nwraps", True),
        ("Call app src/py instead.", "src/app.py", False),
    ],
)
def test_doc_terms_match_whole_tokens_only(text, term, expected):
    assert _matched(text, [term]) == ([term] if expected else [])


def test_doc_term_matching_agrees_with_a_plain_whole_token_regex():
    alphabet = list("ab_1./- \n\té")
    rng = random.Random(3)  # noqa: S311 - seeded test data, not a secret
    for _ in range(2000):
        terms = sorted(
            {"".join(rng.choice(alphabet) for _ in range(rng.randint(4, 8))) for _ in range(6)}
        )
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        planted = rng.choice(terms)
        at = rng.randint(0, len(text))
        text = text[:at] + planted + text[at:]
        assert _matched(text, terms) == _reference_matches(text, terms)


def test_doc_grounding_scales_with_many_terms_and_large_docs():
    changed = [f"pkg{i % 12}/sub{i % 7}/module_{i}.py" for i in range(400)]
    filler = " ".join(f"word{i}" for i in range(2500)) + "\n"
    texts = {f"docs/doc_{index:03d}.md": filler * 2 for index in range(100)}
    texts["docs/doc_050.md"] += "Read pkg2/sub2/module_2.py first.\n"

    started = time.perf_counter()
    entries = _doc_entries(texts, changed, 4000)
    elapsed = time.perf_counter() - started

    assert [entry["source"] for entry in entries] == ["docs/doc_050.md"]
    assert "module_2" in entries[0]["terms"]
    # The per-term regex scan this replaces took tens of seconds at this size.
    assert elapsed < 5


@pytest.mark.parametrize(
    "kind", ["malformed_json", "invalid_record", "invalid_encoding", "invalid_value"]
)
def test_unreadable_run_records_leave_history_grounding_unchanged(grounded_repo, kind):
    from tests.conftest import make_run, unreadable_run_bytes

    repo, prior_run_id = grounded_repo
    agent = ScriptedAgent(repo)
    agent.run("start")
    before = agent.run("context")["data"]["grounding"]
    assert any(entry.get("source") == prior_run_id for entry in before["entries"])

    session = open_session(repo)
    corrupt = make_run(f"r_corrupt_{kind}", branch="feature/x")
    session.store.run_dir(corrupt.run_id).mkdir(parents=True)
    session.store.run_path(corrupt.run_id).write_bytes(unreadable_run_bytes(corrupt, kind))

    after = agent.run("context")["data"]["grounding"]
    assert after == before
    assert digest(after) == digest(before)
