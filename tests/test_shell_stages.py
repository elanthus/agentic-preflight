"""M3 shell stages: lint and test, resolved by config or detection."""

import json
from pathlib import Path

import pytest

from agentic_cli.envelope import ExitCode
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


def config(repo, body):
    write(repo, ".agentic-cli.toml", body)
    commit_all(repo, "configure agentic-cli")


@pytest.fixture
def docs_green(feature_repo, tmp_path):
    """A run that has cleared review and docs, ready for lint."""

    def build(config_body="[docs]\nenabled = false\n"):
        config(feature_repo, config_body)
        agent = ScriptedAgent(feature_repo)
        agent.run("start")
        agent.run("context")
        agent.run("submit-findings", "--file", findings_json(tmp_path, []))
        env = agent.run("stage", "run", "test", "--command", "true", "--record")
        assert env["state"] == "DOCS_GREEN"
        return agent

    return build


# -- command resolution -----------------------------------------------------


def test_a_configured_command_is_used(docs_green):
    agent = docs_green(
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n"
    )
    env = agent.run("stage", "run", "lint")
    assert env["state"] == "LINT_GREEN"
    assert env["data"]["command"] == "true"


def test_an_explicit_command_flag_overrides_config(docs_green):
    agent = docs_green("[docs]\nenabled = false\n\n[commands]\nlint = 'false'\n")
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"


def test_a_run_keeps_its_config_when_the_main_tree_config_changes(
    docs_green, feature_repo
):
    agent = docs_green(
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n"
    )
    write(
        feature_repo,
        ".agentic-cli.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'false'\n",
    )
    env = agent.run("stage", "run", "lint")
    assert env["state"] == "LINT_GREEN"
    assert env["data"]["command"] == "true"


def test_with_no_command_configured_detection_asks_the_agent_to_choose(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", expect=ExitCode.STAGE_FAILED)
    assert env["data"]["mode"] == "needs_command"
    assert "--command" in env["next"]["command"]


def test_detection_offers_candidates_from_the_repo(docs_green, feature_repo):
    write(feature_repo, "Makefile", "lint:\n\techo linting\n\ntest:\n\techo testing\n")
    commit_all(feature_repo, "add a makefile")
    agent = docs_green()
    env = agent.run("stage", "run", "lint", expect=ExitCode.STAGE_FAILED)
    candidates = json.dumps(env["data"]["candidates"])
    assert "make lint" in candidates


def test_detection_reads_package_json_scripts(docs_green, feature_repo):
    write(feature_repo, "package.json", json.dumps({"scripts": {"lint": "eslint ."}}))
    commit_all(feature_repo, "add package.json")
    agent = docs_green()
    env = agent.run("stage", "run", "lint", expect=ExitCode.STAGE_FAILED)
    assert "npm run lint" in json.dumps(env["data"]["candidates"])


# -- pass/fail is exit code only -------------------------------------------


def test_a_zero_exit_is_green_regardless_of_output(docs_green):
    """Never parse stdout: a tool that prints 'error' but exits 0 has passed."""
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint", "--command", "echo '3 errors found'; exit 0", "--record"
    )
    assert env["state"] == "LINT_GREEN"


def test_a_non_zero_exit_is_red_regardless_of_output(docs_green):
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint", "--command", "echo 'all good!'; exit 1", "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["state"] == "LINT_RED"
    assert env["data"]["exit_code"] == 1


# -- ordering ---------------------------------------------------------------


def test_tests_must_pass_before_lint_runs(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("stage", "run", "lint", "--command", "true", "--record",
                    expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_lint_runs_once_tests_and_docs_are_green(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"
    assert "mergeback" in env["next"]["command"]


def test_a_red_stage_can_be_retried_after_a_fix(docs_green):
    agent = docs_green()
    agent.run("stage", "run", "lint", "--command", "exit 1", "--record",
              expect=ExitCode.STAGE_FAILED)
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"


def test_a_committed_lint_repair_invalidates_tests_and_docs(docs_green):
    agent = docs_green()
    failed = agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = failed["data"].get("worktree_path") or agent.run("status")["data"][
        "worktree_path"
    ]
    write(
        Path(worktree),
        "src/app.py",
        "def greet(name, loud=False):\n    return f'hi {name}'.strip()\n",
    )
    commit_all(Path(worktree), "repair lint failure")

    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "REVIEW_GREEN"
    assert env["data"]["validation_restarted"] is True
    assert env["next"]["command"] == "agentic-cli stage run test"

    assert agent.run("stage", "run", "test", "--command", "true", "--record")[
        "state"
    ] == "DOCS_GREEN"
    assert agent.run("stage", "run", "lint", "--command", "true", "--record")[
        "state"
    ] == "LINT_GREEN"


# -- attempt limits ---------------------------------------------------------


def test_repeated_failures_stop_at_max_attempts(docs_green):
    """Stops an agent looping forever on a stage it cannot fix.

    max_attempts = 2 means two real attempts, then a refusal on the third call.
    """
    agent = docs_green("[docs]\nenabled = false\n\n[stage]\nmax_attempts = 2\n")
    for _ in range(2):
        agent.run("stage", "run", "lint", "--command", "exit 1", "--record",
                  expect=ExitCode.STAGE_FAILED)
    env = agent.run("stage", "run", "lint", "--command", "exit 1", "--record",
                    expect=ExitCode.NEEDS_HUMAN)
    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 2
    assert "logs" in env["next"]["command"]


# -- log capture ------------------------------------------------------------


def test_full_output_is_written_to_a_log_file(docs_green):
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint", "--command", "echo hello-from-lint", "--record"
    )
    from pathlib import Path
    log = Path(env["data"]["log_path"])
    assert "hello-from-lint" in log.read_text()


def test_the_envelope_truncates_long_output_and_says_so(docs_green):
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint",
        "--command", "for i in $(seq 1 500); do echo line-$i; done; exit 1",
        "--record", expect=ExitCode.STAGE_FAILED,
    )
    assert env["data"]["truncated"] is True
    assert "line-1" in env["data"]["output_head"]
    assert "line-500" in env["data"]["output_tail"]


def test_short_output_is_not_marked_truncated(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", "--command", "echo brief", "--record")
    assert env["data"]["truncated"] is False


def test_logs_command_returns_a_stage_log(docs_green):
    agent = docs_green()
    agent.run("stage", "run", "lint", "--command", "echo findable", "--record")
    env = agent.run("logs", "--stage", "lint")
    assert "findable" in env["data"]["output"]


def test_logs_for_a_stage_that_never_ran_is_an_error(docs_green):
    agent = docs_green()
    env = agent.run("logs", "--stage", "lint", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_log"


# -- the worktree is where stages run --------------------------------------


def test_the_command_runs_inside_the_worktree(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", "--command", "pwd", "--record")
    status = agent.run("status")
    from pathlib import Path
    log = Path(env["data"]["log_path"]).read_text()
    assert status["data"]["worktree_path"] in log


def test_copied_file_contents_never_reach_a_stage_log(feature_repo, tmp_path):
    """copy_files paths are in the redaction set for stage logs."""
    write(feature_repo, ".env", "SECRET=hunter2\n")
    config(feature_repo, "[docs]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test", "--command", "true", "--record")

    env = agent.run("stage", "run", "lint", "--command", "cat .env", "--record")
    from pathlib import Path
    assert "hunter2" not in Path(env["data"]["log_path"]).read_text()
    assert "hunter2" not in json.dumps(env)


# -- baseline check ---------------------------------------------------------


def test_a_red_baseline_is_reported_rather_than_blamed_on_the_diff(docs_green):
    """If the base commit is already failing, say so instead of blaming the change."""
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record", "--baseline",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["data"]["baseline_red"] is True
    assert "base" in env["error"]["message"].lower()


def test_a_green_baseline_leaves_the_failure_attributed_to_the_diff(docs_green):
    """Base passes, head fails — so the diff really is responsible and we say so."""
    agent = docs_green()
    # Passes on the base commit (no `loud`), fails at head (the flag was added).
    env = agent.run(
        "stage", "run", "lint",
        "--command", "! grep -q loud src/app.py", "--record", "--baseline",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["data"]["baseline_red"] is False
    assert "base commit" not in env["error"]["message"]
