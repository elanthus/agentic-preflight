"""M0 walking skeleton: start -> context -> submit-findings -> verify -> status."""

import json
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
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


# -- start ------------------------------------------------------------------


def test_start_creates_a_run_and_points_at_context(agent):
    env = agent.run("start")
    assert env["ok"] is True
    assert env["run_id"].startswith("r_")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert "context" in env["next"]["command"]


def test_start_requires_explicit_user_intent(agent):
    env = agent.run("start", "--intent", "", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "intent_required"
    assert "--intent" in env["next"]["command"]


def test_start_persists_intent_in_context_and_status(agent):
    intent = "add a loud greeting without changing the default response"
    started = agent.run("start", "--intent", intent)
    assert started["data"]["intent"] == intent
    assert agent.run("context")["data"]["intent"] == intent
    assert agent.run("status")["data"]["intent"] == intent


def test_start_records_fresh_sync_metadata(agent):
    env = agent.run("start")
    assert env["data"]["sync"]["base_sha"]
    assert env["data"]["sync"]["head_after"] == env["data"]["head_sha"]


def test_start_reports_an_absolute_worktree_path(agent):
    env = agent.run("start")
    assert env["data"]["worktree_path"].startswith("/")


def test_start_uses_the_current_checkout_by_default(agent, feature_repo):
    env = agent.run("start")
    worktree_path = Path(env["data"]["worktree_path"])
    assert worktree_path == feature_repo
    assert env["data"]["worktree_mode"] == "in_place"


def test_start_leaves_the_users_tree_on_its_own_branch(agent, feature_repo):
    agent.run("start")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=feature_repo) == "feature/x"
    assert git("status", "--porcelain", cwd=feature_repo) == ""


def test_start_refuses_a_dirty_tree(agent, feature_repo):
    write(feature_repo, "src/app.py", "uncommitted edit\n")
    env = agent.run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "dirty_tree"


def test_start_stops_when_the_setup_command_fails(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nsetup_command = 'exit 7'\n")
    commit_all(feature_repo, "configure failing setup")

    env = agent.run("start", expect=ExitCode.STAGE_FAILED)

    assert env["error"]["code"] == "setup_failed"
    assert env["stage"] == "setup"
    assert env["data"]["setup"]["command"] == "exit 7"
    assert env["data"]["setup"]["exit_code"] == 7
    assert env["next"]["command"] == "agentic-preflight abort --force"
    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_start_refuses_a_second_lease_while_a_run_is_active(agent):
    first = agent.run("start")

    env = agent.run("start", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "wrong_state"
    assert env["run_id"] == first["run_id"]
    assert env["next"]["command"] == "agentic-preflight status"


def test_start_refuses_when_the_branch_has_no_commits_over_base(tmp_repo):
    agent = ScriptedAgent(tmp_repo)
    env = agent.run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "empty_diff"


# -- context ----------------------------------------------------------------


def test_context_returns_the_diff_and_changed_files(agent):
    agent.run("start")
    env = agent.run("context")
    assert env["data"]["changed_files"] == ["src/app.py"]
    assert "loud=False" in env["data"]["diff"]
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"


def test_context_does_not_advance_the_state(agent):
    agent.run("start")
    first = agent.run("context")
    second = agent.run("context")
    assert first["state"] == second["state"] == "REVIEW_AWAITING_FINDINGS"


def test_context_refuses_before_a_run_exists(feature_repo):
    agent = ScriptedAgent(feature_repo)
    env = agent.run("context", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_run"


def test_context_trips_the_budget_rather_than_truncating(agent, feature_repo):
    write(feature_repo, "src/big.py", "x = 1\n" * 5000)
    commit_all(feature_repo, "add a big file")
    write(feature_repo, ".agentic-preflight.toml", "[diff]\nmax_bytes = 500\n")
    commit_all(feature_repo, "tighten the diff budget")

    agent.run("start")
    env = agent.run("context", expect=ExitCode.STAGE_FAILED)
    assert env["data"]["mode"] == "diff_too_large"
    assert env["data"]["by_file"][0][0] == "src/big.py"
    assert "exclude" in env["next"]["instruction"]


def test_context_exclusions_bring_an_oversized_diff_back_under_budget(agent, feature_repo):
    write(feature_repo, "uv.lock", "generated\n" * 5000)
    commit_all(feature_repo, "add lockfile")
    agent.run("start")
    env = agent.run("context")
    assert "uv.lock" not in env["data"]["changed_files"]
    assert "uv.lock" in env["data"]["excluded_files"]


# -- submit-findings --------------------------------------------------------


def test_a_clean_review_goes_straight_to_green(agent, tmp_path):
    agent.run("start")
    context = agent.run("context")
    assert context["data"]["review_coverage"]["total_units"] == 1
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "REVIEW_GREEN"
    assert env["blocking"] == []
    assert env["data"]["coverage"]["total_units"] == 1
    assert env["data"]["coverage"]["clean_count"] == 1
    assert env["data"]["coverage"]["cited_count"] == 0


def test_a_blocking_finding_holds_the_run_for_responses(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "line": 1,
                "severity": "high",
                "action": "auto_fix",
                "title": "loud flag is never used",
            }
        ],
    )
    env = agent.run("submit-findings", "--file", path)
    assert env["state"] == "REVIEW_BLOCKED"
    assert (
        env["next"]["command"]
        == "agentic-preflight respond --id F001 --action fixed --commit <sha>"
    )
    assert [f["id"] for f in env["blocking"]] == ["F001"]
    assert env["data"]["accepted"][0]["unit"] == "U0001"
    assert env["data"]["coverage"]["cited_count"] == 1
    assert env["data"]["coverage"]["clean_count"] == 0


def test_review_coverage_accounts_for_cited_and_clean_hunks(tmp_repo, tmp_path):
    write(tmp_repo, "src/long.py", "\n".join(f"line {i}" for i in range(40)) + "\n")
    commit_all(tmp_repo, "add long source")
    git("switch", "-c", "feature/two-hunks", cwd=tmp_repo)
    lines = (tmp_repo / "src" / "long.py").read_text().splitlines()
    lines[1] = "changed near start"
    lines[37] = "changed near end"
    write(tmp_repo, "src/long.py", "\n".join(lines) + "\n")
    commit_all(tmp_repo, "change distant lines")
    local_agent = ScriptedAgent(tmp_repo)

    local_agent.run("start")
    context = local_agent.run("context")
    assert context["data"]["review_coverage"]["total_units"] == 2
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/long.py",
                "line": 2,
                "severity": "low",
                "action": "no_op",
                "title": "Review the first change",
                "detail": "The first hunk needs follow-up; the other hunk was examined clean.",
            }
        ],
    )

    env = local_agent.run("submit-findings", "--file", path)

    assert env["data"]["coverage"]["total_units"] == 2
    assert env["data"]["coverage"]["cited_count"] == 1
    assert env["data"]["coverage"]["clean_count"] == 1


def test_a_non_blocking_finding_still_reaches_green(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "line": 1,
                "severity": "low",
                "action": "no_op",
                "title": "nit: naming",
            }
        ],
    )
    env = agent.run("submit-findings", "--file", path)
    assert env["state"] == "REVIEW_GREEN"


def test_an_agent_supplied_id_is_a_hard_error(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "id": "F001",
                "path": "src/app.py",
                "severity": "high",
                "action": "auto_fix",
                "title": "invented an id",
            }
        ],
    )
    env = agent.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "id" in env["error"]["message"]


def test_review_rejects_agent_supplied_code_ownership(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "code_owned": True,
                "path": "src/app.py",
                "severity": "high",
                "action": "auto_fix",
                "title": "spoofed mechanical requirement",
            }
        ],
    )
    env = agent.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "code_owned" in env["error"]["message"]


def test_a_finding_against_an_untouched_file_is_rejected(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "README.md",
                "severity": "high",
                "action": "auto_fix",
                "title": "not in the diff",
            }
        ],
    )
    env = agent.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"


def test_submit_findings_is_illegal_before_start(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "no_run"


def test_submitting_twice_is_a_wrong_state_error_naming_the_next_move(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(tmp_path, [])
    agent.run("submit-findings", "--file", path)
    env = agent.run("submit-findings", "--file", path, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"
    assert env["next"]["command"]


def test_review_findings_reject_a_bare_list_without_coverage(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([]))
    env = agent.run("submit-findings", "--file", str(path), expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "ReviewSubmission" in env["error"]["message"]


def test_review_rejects_a_manifest_that_does_not_match_the_current_diff(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = tmp_path / "wrong-coverage.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "0" * 64, "examined": "all"}, "findings": []})
    )
    env = agent.run("submit-findings", "--file", str(path), expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "does not match" in env["error"]["message"]


# -- verify -----------------------------------------------------------------


def test_verify_reports_the_outstanding_blocking_set(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "severity": "critical",
                "action": "ask_user",
                "title": "should this change the public API?",
            }
        ],
    )
    agent.run("submit-findings", "--file", path)
    env = agent.run("verify", expect=ExitCode.STAGE_FAILED)
    assert [f["id"] for f in env["blocking"]] == ["F001"]
    assert env["state"] == "REVIEW_BLOCKED"


def test_verify_on_a_green_review_confirms_and_moves_on(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("verify")
    assert env["state"] == "REVIEW_GREEN"


# -- status -----------------------------------------------------------------


def test_status_is_legal_before_any_run_exists(feature_repo):
    agent = ScriptedAgent(feature_repo)
    env = agent.run("status")
    assert env["ok"] is True
    assert env["data"]["has_run"] is False
    assert "start" in env["next"]["command"]
    assert "--intent" in env["next"]["command"]


def test_status_reports_state_and_findings_summary(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    path = findings_json(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "severity": "high",
                "action": "auto_fix",
                "title": "x",
            }
        ],
    )
    agent.run("submit-findings", "--file", path)

    env = agent.run("status")
    assert env["state"] == "REVIEW_BLOCKED"
    assert env["data"]["findings_summary"]["open"] == 1
    assert env["data"]["findings"][0]["id"] == "F001"
    assert env["data"]["review_coverage"]["cited_count"] == 1


def test_status_is_legal_in_every_state_reached_by_the_happy_path(agent, tmp_path):
    agent.run("start")
    agent.run("status")
    agent.run("context")
    agent.run("status")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("status")
    assert env["state"] == "REVIEW_GREEN"


# -- staleness --------------------------------------------------------------


def test_a_moved_head_marks_the_run_stale_and_refuses_to_continue(agent, feature_repo, tmp_path):
    agent.run("start")
    agent.run("context")
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return 'changed'\n")
    commit_all(feature_repo, "amend the work after review started")

    env = agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, []),
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "stale_run"
    assert env["next"]["command"] == "agentic-preflight abort --force"

    aborted = agent.run("abort", "--force")
    assert "start" in aborted["next"]["command"]
    assert "--intent" in aborted["next"]["command"]
    assert "exercise the requested behavior safely" in aborted["next"]["command"]


def test_status_still_works_on_a_stale_run(agent, feature_repo):
    agent.run("start")
    write(feature_repo, "src/app.py", "moved\n")
    commit_all(feature_repo, "move the head")
    env = agent.run("status")
    assert env["data"]["stale"] is True
    assert env["next"]["command"] == "agentic-preflight abort --force"


def test_status_uses_the_snapshot_when_working_copy_config_breaks(agent, feature_repo):
    started = agent.run("start")
    write(feature_repo, ".agentic-preflight.toml", "[broken\n")
    env = agent.run("status")
    assert env["run_id"] == started["run_id"]
    assert env["data"]["config_digest"]


# -- the contract itself ----------------------------------------------------


def test_every_command_emits_exactly_one_json_object(agent, tmp_path):
    """Asserted by the driver on every step; this test makes it explicit."""
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("status")
    assert len(agent.steps) == 4


def test_the_contract_holds_over_a_real_subprocess(feature_repo, tmp_path):
    """Some paths only exist as subprocesses; the envelope must survive that."""
    agent = ScriptedAgent(feature_repo, transport="subprocess")
    env = agent.run("start")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    env = agent.run("status")
    assert env["ok"] is True
