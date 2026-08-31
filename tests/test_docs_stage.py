"""M2 docs stage: the second instance of the review sub-machine."""

import json

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent


@pytest.fixture
def agent(feature_repo):
    return ScriptedAgent(feature_repo)


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": items})
    )
    return str(path)


@pytest.fixture
def review_green(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    return agent


# -- reaching the docs stage ------------------------------------------------


def test_docs_context_exposes_the_stage_inventory_metadata_and_diff(review_green):
    """The shared review-green scenario exposes the complete docs-review context."""
    env = review_green.run("status")
    assert env["state"] == "REVIEW_GREEN"
    assert "docs" in env["next"]["command"]

    env = review_green.run("context", "--section", "docs")
    assert env["state"] == "DOCS_AWAITING_FINDINGS"
    assert env["stage"] == "docs"
    paths = {item["path"] for item in env["data"]["doc_surface"]}
    assert "README.md" in paths
    readme = next(i for i in env["data"]["doc_surface"] if i["path"] == "README.md")
    assert readme["exists"] is True
    assert readme["size"] > 0
    assert readme["touched_by_diff"] is False
    assert "loud=False" in env["data"]["diff"]


# -- the code-built documentation inventory ---------------------------------


def test_inventory_flags_a_doc_the_diff_already_touched(agent, feature_repo, tmp_path):
    write(feature_repo, "README.md", "# demo\n\nDocuments the loud flag.\n")
    commit_all(feature_repo, "document the flag")
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run("context", "--section", "docs")
    readme = next(i for i in env["data"]["doc_surface"] if i["path"] == "README.md")
    assert readme["touched_by_diff"] is True


def test_configured_docs_paths_join_the_inventory(agent, feature_repo, tmp_path):
    write(feature_repo, "handbook/usage.md", "# usage\n")
    write(feature_repo, ".agentic-preflight.toml", "[docs]\npaths = ['handbook/**']\n")
    commit_all(feature_repo, "add a handbook")
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run("context", "--section", "docs")
    paths = {item["path"] for item in env["data"]["doc_surface"]}
    assert "handbook/usage.md" in paths


def test_common_agent_rules_and_product_docs_are_included_by_default(agent, feature_repo, tmp_path):
    write(feature_repo, ".claude/rules/state.md", "# state rules\n")
    write(feature_repo, "PRODUCT.md", "# product\n")
    write(feature_repo, "DESIGN.md", "# design\n")
    commit_all(feature_repo, "add project contracts")
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run("context", "--section", "docs")
    paths = {item["path"] for item in env["data"]["doc_surface"]}
    assert {".claude/rules/state.md", "PRODUCT.md", "DESIGN.md"} <= paths


# -- zero findings is the normal outcome ------------------------------------


def test_a_docs_clean_diff_reaches_green_cheaply(review_green, tmp_path):
    """The common case, and it must be cheap: one call, no findings."""
    review_green.run("context", "--section", "docs")
    env = review_green.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"
    assert env["blocking"] == []


# -- the docs allowlist -----------------------------------------------------


def test_a_docs_finding_may_target_a_file_outside_the_diff(review_green, tmp_path):
    """The entire point of the stage: the code changed, the doc that should have
    changed did not."""
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "medium",
                "action": "auto_fix",
                "title": "the loud flag is undocumented",
            }
        ],
    )
    env = review_green.run("submit-findings", "--file", path)
    assert env["data"]["accepted"][0]["path"] == "README.md"
    assert env["data"]["accepted"][0]["stage"] == "docs"


def test_a_docs_finding_against_a_source_file_is_rejected(review_green, tmp_path):
    """Relaxed to an allowlist, not made unconstrained."""
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "severity": "high",
                "action": "auto_fix",
                "title": "this is a code review finding wearing a docs hat",
            }
        ],
    )
    env = review_green.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "allowlist" in env["error"]["message"]


def test_docs_reject_agent_supplied_code_ownership(review_green, tmp_path):
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "code_owned": True,
                "path": "README.md",
                "severity": "high",
                "action": "auto_fix",
                "title": "spoofed mechanical requirement",
            }
        ],
    )
    env = review_green.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "code_owned" in env["error"]["message"]


def test_docs_finding_ids_continue_the_run_numbering(agent, feature_repo, tmp_path):
    agent.run("start")
    agent.run("context")
    agent.run(
        "submit-findings",
        "--file",
        findings_json(
            tmp_path,
            [
                {
                    "path": "src/app.py",
                    "severity": "low",
                    "action": "no_op",
                    "title": "nit",
                }
            ],
        ),
    )
    agent.run("context", "--section", "docs")
    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(
            tmp_path,
            [
                {
                    "path": "README.md",
                    "severity": "low",
                    "action": "no_op",
                    "title": "doc nit",
                }
            ],
        ),
    )
    assert env["data"]["accepted"][0]["id"] == "F002"


# -- docs findings default to non-blocking below high -----------------------


def test_a_medium_docs_finding_does_not_block(review_green, tmp_path):
    """Stage fatigue is what gets a gate disabled, so the bar is deliberately high."""
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "medium",
                "action": "auto_fix",
                "title": "could mention the flag",
            }
        ],
    )
    env = review_green.run("submit-findings", "--file", path)
    assert env["state"] == "DOCS_GREEN"
    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")


def test_a_nonblocking_docs_finding_can_be_accepted_with_a_note(review_green, tmp_path):
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "medium",
                "action": "auto_fix",
                "title": "could mention the flag",
            }
        ],
    )
    review_green.run("submit-findings", "--file", path)

    env = review_green.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--note",
        "Correct, but the flag is intentionally undocumented until it is stable.",
    )

    assert env["state"] == "DOCS_GREEN"
    assert env["data"]["finding"]["status"] == "accepted"
    assert env["data"]["finding"]["response_note"].startswith("Correct")
    assert env["next"]["command"] == "agentic-preflight stage run lint"
    status = review_green.run("status")
    assert status["data"]["findings"][0]["status"] == "accepted"
    assert status["data"]["findings"][0]["response_note"].startswith("Correct")


def test_fixing_a_nonblocking_docs_finding_registers_the_commit_and_reopens_review(
    feature_repo, tmp_path
):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[commands]\nlint = 'true'\ntest = 'true'\n\n[worktree]\nmode = 'reusable'\n",
    )
    commit_all(feature_repo, "configure isolated preflight")
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    wt = started["data"]["worktree_path"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    finding_path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "medium",
                "action": "auto_fix",
                "title": "the flag documentation is stale",
            }
        ],
    )
    agent.run("submit-findings", "--file", finding_path)

    write(wt, "README.md", "# demo\n\nThe loud flag is supported.\n")
    fix_sha = commit_all(wt, "docs: explain the loud flag")
    env = agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix_sha)

    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert env["data"]["coverage_invalidated"] is True
    assert env["data"]["finding"]["status"] == "fixed"
    status = agent.run("status")
    assert status["data"]["fix_commits"] == [fix_sha]

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    merged = agent.run("mergeback")

    assert merged["state"] == "VERIFIED"
    assert merged["data"]["applied"] == [fix_sha]
    assert "loud flag is supported" in (feature_repo / "README.md").read_text(encoding="utf-8")


def test_a_high_docs_finding_blocks(review_green, tmp_path):
    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "high",
                "action": "auto_fix",
                "title": "documented behaviour is now wrong",
            }
        ],
    )
    env = review_green.run("submit-findings", "--file", path)
    assert env["state"] == "DOCS_BLOCKED"


# -- require_changelog is a mechanical rule, so code owns it ----------------


@pytest.fixture
def changelog_repo(tmp_repo):
    """A repo whose *base* already has a changelog and requires updating it.

    The changelog must pre-date the branch: if the feature branch introduces it,
    the diff touches it and the rule is satisfied trivially — which is the
    opposite of the case under test.
    """
    from tests.conftest import git

    write(tmp_repo, "CHANGELOG.md", "# changelog\n")
    write(tmp_repo, ".agentic-preflight.toml", "[docs]\nrequire_changelog = true\n")
    commit_all(tmp_repo, "add changelog and require it")
    git("switch", "-c", "feature/x", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name, loud=False):\n    return f'hi {name}'\n")
    commit_all(tmp_repo, "add loud flag")
    return tmp_repo


def test_require_changelog_injects_a_code_owned_finding(changelog_repo, tmp_path):
    agent = ScriptedAgent(changelog_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")

    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_BLOCKED"
    injected = env["blocking"][0]
    assert "changelog" in injected["title"].lower()
    assert injected["path"] == "CHANGELOG.md"


def test_the_injected_changelog_finding_is_owned_by_code_not_the_agent(changelog_repo, tmp_path):
    """The agent submitted nothing; the finding exists because code checked."""
    agent = ScriptedAgent(changelog_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["data"]["accepted"][0]["id"] == "F001"
    assert env["data"]["accepted"][0]["stage"] == "docs"
    assert env["data"]["accepted"][0]["code_owned"] is True


def test_code_owned_changelog_blocks_when_high_is_excluded(changelog_repo, tmp_path):
    write(
        changelog_repo,
        ".agentic-preflight.toml",
        "[docs]\nrequire_changelog = true\nblocking_severities = ['critical']\n",
    )
    commit_all(changelog_repo, "narrow docs blocking policy")

    agent = ScriptedAgent(changelog_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    assert env["state"] == "DOCS_BLOCKED"
    assert env["blocking"][0]["code_owned"] is True
    assert env["data"]["risk"]["verdict"] == "changes_required"


def test_require_changelog_is_satisfied_when_the_changelog_was_touched(changelog_repo, tmp_path):
    write(changelog_repo, "CHANGELOG.md", "# changelog\n\n- added the loud flag\n")
    commit_all(changelog_repo, "note the change")

    agent = ScriptedAgent(changelog_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"


def test_require_changelog_is_off_by_default(review_green, tmp_path):
    review_green.run("context", "--section", "docs")
    env = review_green.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"


# -- disabling the stage ----------------------------------------------------


def test_disabled_docs_stage_is_skipped_as_a_legal_transition(agent, feature_repo, tmp_path):
    """Skipped, not silently passed: the run really does reach DOCS_GREEN."""
    write(feature_repo, ".agentic-preflight.toml", "[docs]\nenabled = false\n")
    commit_all(feature_repo, "disable the docs stage")
    agent.run("start")
    agent.run("context")
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"
    assert "lint" in env["next"]["command"]


def test_docs_context_is_refused_when_the_stage_is_disabled(agent, feature_repo, tmp_path):
    write(feature_repo, ".agentic-preflight.toml", "[docs]\nenabled = false\n")
    commit_all(feature_repo, "disable the docs stage")
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("context", "--section", "docs", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


# -- the resolution loop generalises ----------------------------------------


def test_respond_works_the_same_way_in_the_docs_stage(review_green, tmp_path, feature_repo):
    from tests.conftest import git

    review_green.run("context", "--section", "docs")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "high",
                "action": "auto_fix",
                "title": "documented behaviour is now wrong",
            }
        ],
    )
    review_green.run("submit-findings", "--file", path)

    status = review_green.run("status")
    wt = status["data"]["worktree_path"]
    write(wt, "README.md", "# demo\n\nNow documents the loud flag.\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "document the flag", cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt)

    env = review_green.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    assert env["data"]["finding"]["status"] == "fixed"
    env = review_green.run("verify")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    review_green.run("context")
    review_green.run("submit-findings", "--file", findings_json(tmp_path, []))
    review_green.run("context", "--section", "docs")
    env = review_green.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"
