"""M3 shell stages: lint and test, resolved by config or detection."""

import json
import signal
import subprocess
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from agentic_preflight.stages import shellstage
from tests.conftest import (
    commit_all,
    requires_posix_permissions,
    requires_posix_signals,
    requires_windows,
    write,
)
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": items})
    )
    return str(path)


def config(repo, body):
    write(repo, ".agentic-preflight.toml", body)
    commit_all(repo, "configure agentic-preflight")


def complete_reopened_review(agent, tmp_path):
    context = agent.run("context")
    assert context["state"] == "REVIEW_AWAITING_FINDINGS"
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"


@pytest.fixture
def docs_green(feature_repo, tmp_path):
    """A run that has cleared review and docs, ready for lint."""

    def build(config_body="[docs]\nenabled = false\n"):
        config(feature_repo, config_body)
        agent = ScriptedAgent(feature_repo)
        agent.run("start")
        agent.run("context")
        env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
        assert env["state"] == "DOCS_GREEN"
        return agent

    return build


# -- command resolution -----------------------------------------------------


def test_a_configured_command_is_used(docs_green):
    agent = docs_green(
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\n\n[worktree]\nmode = 'reusable'\n"
    )
    env = agent.run("stage", "run", "lint")
    assert env["state"] == "LINT_GREEN"
    assert env["data"]["command"] == "true"


def test_an_explicit_command_flag_overrides_config(docs_green):
    agent = docs_green("[docs]\nenabled = false\n\n[commands]\nlint = 'false'\n")
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"


def test_a_run_keeps_its_config_when_the_main_tree_config_changes(docs_green, feature_repo):
    agent = docs_green(
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\n\n[worktree]\nmode = 'reusable'\n"
    )
    write(
        feature_repo,
        ".agentic-preflight.toml",
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
        "stage",
        "run",
        "lint",
        "--command",
        "echo 'all good!'; exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["state"] == "LINT_RED"
    assert env["data"]["exit_code"] == 1


# -- ordering ---------------------------------------------------------------


def test_docs_must_pass_before_lint_runs(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run(
        "stage", "run", "lint", "--command", "true", "--record", expect=ExitCode.PRECONDITION
    )
    assert env["error"]["code"] == "wrong_state"


def test_documentation_only_changes_skip_software_tests_after_lint(tmp_repo, tmp_path):
    from tests.conftest import git

    git("switch", "-c", "feature/docs", cwd=tmp_repo)
    write(tmp_repo, "README.md", "# demo\n\nUpdated documentation.\n")
    commit_all(tmp_repo, "update docs")
    agent = ScriptedAgent(tmp_repo)
    agent.run("start")
    agent.run("context")

    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")

    assert env["state"] == "TEST_GREEN"
    assert env["next"]["command"] == "agentic-preflight mergeback"
    test_record = agent.run("status")["data"]["stages"]["test"]
    assert test_record["status"] == "skipped"
    assert "documentation and CI configuration" in test_record["reason"]
    assert test_record["command"] is None


def test_ci_configuration_only_changes_skip_software_tests(tmp_repo, tmp_path):
    from tests.conftest import git

    git("switch", "-c", "feature/ci", cwd=tmp_repo)
    write(tmp_repo, ".github/workflows/ci.yml", "name: CI\n")
    commit_all(tmp_repo, "update CI")
    agent = ScriptedAgent(tmp_repo)
    agent.run("start")
    agent.run("context")

    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")

    assert env["state"] == "TEST_GREEN"
    assert agent.run("status")["data"]["stages"]["test"]["status"] == "skipped"


def test_source_changes_still_require_software_tests(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")

    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    assert env["state"] == "REVIEW_GREEN"
    assert env["next"]["command"] == "agentic-preflight context --section docs"


def test_lint_runs_once_docs_are_green_and_points_to_tests(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"
    assert env["next"]["command"] == "agentic-preflight stage run test"


def test_a_red_stage_can_be_retried_after_a_fix(docs_green):
    agent = docs_green()
    agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record", expect=ExitCode.STAGE_FAILED
    )
    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "LINT_GREEN"


def test_a_committed_lint_repair_revalidates_before_the_first_test_run(docs_green, tmp_path):
    agent = docs_green()
    failed = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = failed["data"].get("worktree_path") or agent.run("status")["data"]["worktree_path"]
    write(
        Path(worktree),
        "src/app.py",
        "def greet(name, loud=False):\n    return f'hi {name}'.strip()\n",
    )
    commit_all(Path(worktree), "repair lint failure")

    env = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert env["data"]["validation_restarted"] is True
    assert env["next"]["command"] == "agentic-preflight context"

    complete_reopened_review(agent, tmp_path)

    assert (
        agent.run("stage", "run", "lint", "--command", "true", "--record")["state"] == "LINT_GREEN"
    )
    assert (
        agent.run("stage", "run", "test", "--command", "true", "--record")["state"] == "TEST_GREEN"
    )


def test_a_committed_test_repair_revalidates_docs_and_lint_before_retry(docs_green, tmp_path):
    agent = docs_green()
    agent.run("stage", "run", "lint", "--command", "true", "--record")
    failed = agent.run(
        "stage",
        "run",
        "test",
        "--command",
        "exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = failed["data"].get("worktree_path") or agent.run("status")["data"]["worktree_path"]
    write(
        Path(worktree),
        "src/app.py",
        "def greet(name, loud=False):\n    return f'hi {name}'.strip()\n",
    )
    commit_all(Path(worktree), "repair test failure")

    env = agent.run("stage", "run", "test", "--command", "true", "--record")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert env["data"]["validation_restarted"] is True
    assert env["next"]["command"] == "agentic-preflight context"

    complete_reopened_review(agent, tmp_path)
    agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert (
        agent.run("stage", "run", "test", "--command", "true", "--record")["state"] == "TEST_GREEN"
    )


# -- attempt limits ---------------------------------------------------------


def test_repeated_failures_stop_at_max_attempts(docs_green):
    """Stops an agent looping forever on a stage it cannot fix.

    max_attempts = 2 means two real attempts, then a refusal on the third call.
    """
    agent = docs_green("[docs]\nenabled = false\n\n[stage]\nmax_attempts = 2\n")
    for _ in range(2):
        agent.run(
            "stage", "run", "lint", "--command", "exit 1", "--record", expect=ExitCode.STAGE_FAILED
        )
    env = agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record", expect=ExitCode.NEEDS_HUMAN
    )
    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 2
    assert "logs" in env["next"]["command"]


def test_cross_stage_repairs_preserve_attempt_limits(docs_green, tmp_path):
    agent = docs_green("[docs]\nenabled = false\n\n[stage]\nmax_attempts = 2\n")
    failed = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = Path(
        failed["data"].get("worktree_path") or agent.run("status")["data"]["worktree_path"]
    )
    write(worktree, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit_all(worktree, "repair first lint failure")

    restarted = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert restarted["data"]["validation_restarted"] is True
    complete_reopened_review(agent, tmp_path)
    agent.run("stage", "run", "lint", "--command", "true", "--record")
    failed = agent.run(
        "stage",
        "run",
        "test",
        "--command",
        "exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = Path(
        failed["data"].get("worktree_path") or agent.run("status")["data"]["worktree_path"]
    )
    write(worktree, "src/app.py", "def greet(name):\n    return f'hello, {name}'\n")
    commit_all(worktree, "repair test failure")

    restarted = agent.run("stage", "run", "test", "--command", "true", "--record")
    assert restarted["data"]["validation_restarted"] is True
    complete_reopened_review(agent, tmp_path)
    assert agent.run("status")["data"]["stages"]["lint"] == {
        "attempts": 1,
        "command": None,
        "executor": None,
        "exit_code": None,
        "finished_at": None,
        "head_sha": None,
        "log_path": None,
        "output_sha256": None,
        "reason": None,
        "status": "pending",
    }

    failed = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    worktree = Path(
        failed["data"].get("worktree_path") or agent.run("status")["data"]["worktree_path"]
    )
    write(worktree, "src/app.py", "def greet(name):\n    return f'hi {name}'\n")
    commit_all(worktree, "repair lint failure")

    restarted = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert restarted["data"]["validation_restarted"] is True
    complete_reopened_review(agent, tmp_path)
    assert agent.run("status")["data"]["stages"]["test"]["attempts"] == 1

    env = agent.run(
        "stage", "run", "lint", "--command", "true", "--record", expect=ExitCode.NEEDS_HUMAN
    )
    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 2


# -- log capture ------------------------------------------------------------


def test_full_output_is_written_to_a_log_file(docs_green):
    agent = docs_green()
    env = agent.run("stage", "run", "lint", "--command", "echo hello-from-lint", "--record")
    from pathlib import Path

    log = Path(env["data"]["log_path"])
    assert "hello-from-lint" in log.read_text(encoding="utf-8")


def test_the_envelope_truncates_long_output_and_says_so(docs_green):
    agent = docs_green()
    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "for i in $(seq 1 500); do echo line-$i; done; exit 1",
        "--record",
        expect=ExitCode.STAGE_FAILED,
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
    """``git rev-parse``, not ``pwd``: the latter is a shell builtin that reports
    a POSIX path even on Windows, so it cannot be compared with the worktree
    path the envelope carries."""
    agent = docs_green()
    env = agent.run(
        "stage", "run", "lint", "--command", "git rev-parse --show-toplevel", "--record"
    )
    status = agent.run("status")

    log = Path(env["data"]["log_path"]).read_text(encoding="utf-8")

    assert Path(log.strip()) == Path(status["data"]["worktree_path"])


def test_dotenv_values_are_parsed_including_quotes_multiline_and_short_values(tmp_path):
    write(
        tmp_path,
        ".env",
        (
            "export SECRET=\"hunter2\"\nPIN=123\nSINGLE='quoted value'\n"
            'APOSTROPHE="it\\\'s private"\n'
            'MULTILINE="first line\nsecond line"\n'
        ),
    )

    secrets = shellstage.read_secrets(tmp_path, [".env"])

    assert "hunter2" in secrets
    assert "123" in secrets
    assert "quoted value" in secrets
    assert "it's private" in secrets
    assert "first line\nsecond line" in secrets


def test_exported_dotenv_values_never_reach_stage_stdout_or_stderr(feature_repo, tmp_path):
    """Parsed dotenv values are scrubbed from both captured output streams."""
    write(
        feature_repo,
        ".env",
        'export SECRET="hunter2"\nexport PIN=123\nexport MULTILINE="first line\nsecond line"\n',
    )
    config(feature_repo, "[docs]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        ('. ./.env; printf "%s %s\\n" "$SECRET" "$PIN"; printf "%s\\n" "$MULTILINE" >&2'),
        "--record",
    )
    captured_output = env["data"]["output_head"] + env["data"]["output_tail"]
    log_output = Path(env["data"]["log_path"]).read_text(encoding="utf-8")

    for secret in ("hunter2", "123", "first line", "second line"):
        assert secret not in captured_output
        assert secret not in log_output
    assert log_output == "[redacted] [redacted]\n[redacted]\n"


@requires_posix_signals
def test_timeout_uses_the_known_process_group_when_lookup_is_denied(tmp_path, monkeypatch):
    killed_groups: list[tuple[int, signal.Signals]] = []
    direct_kills: list[bool] = []

    class TimedOutProcess:
        pid = 4242
        returncode = -signal.SIGKILL
        calls = 0

        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("review", 1)
            return "", None

        def kill(self):
            direct_kills.append(True)

    process = TimedOutProcess()
    monkeypatch.setattr(shellstage.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        shellstage.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        shellstage.os,
        "killpg",
        lambda pgid, sig: killed_groups.append((pgid, sig)),
    )

    result = shellstage.run_stage(tmp_path, "ignored", timeout_seconds=1)

    assert result.timed_out is True
    assert killed_groups == [(process.pid, signal.SIGKILL)]
    assert direct_kills == []


@requires_posix_signals
def test_timeout_uses_the_known_process_group_when_the_leader_is_gone(tmp_path, monkeypatch):
    killed_groups: list[tuple[int, signal.Signals]] = []
    direct_kills: list[bool] = []

    class TimedOutProcess:
        pid = 4242
        returncode = -signal.SIGKILL
        calls = 0

        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("review", 1)
            return "", None

        def kill(self):
            direct_kills.append(True)

    process = TimedOutProcess()
    monkeypatch.setattr(shellstage.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        shellstage.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError("leader exited")),
    )
    monkeypatch.setattr(
        shellstage.os,
        "killpg",
        lambda pgid, sig: killed_groups.append((pgid, sig)),
    )

    result = shellstage.run_stage(tmp_path, "ignored", timeout_seconds=1)

    assert result.timed_out is True
    assert killed_groups == [(process.pid, signal.SIGKILL)]
    assert direct_kills == []


@requires_windows
def test_timeout_kills_the_whole_tree_on_windows(tmp_path, monkeypatch):
    """Windows has no process group to signal, so the parent/child tree is walked.

    ``CREATE_NEW_PROCESS_GROUP`` only scopes console control events, which a
    non-console child never receives — killing the direct child alone would
    strand a test runner's workers exactly as it would on POSIX.
    """
    commands: list[list[str]] = []
    direct_kills: list[bool] = []

    class TimedOutProcess:
        pid = 4242
        returncode = 1
        calls = 0

        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("review", 1)
            return "", None

        def kill(self):
            direct_kills.append(True)

    process = TimedOutProcess()
    monkeypatch.setattr(shellstage.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        shellstage.subprocess,
        "run",
        lambda argv, **_kwargs: (
            commands.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")
        ),
    )

    result = shellstage.run_stage(tmp_path, "ignored", timeout_seconds=1)

    assert result.timed_out is True
    assert commands == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert direct_kills == []


def _timed_out_process(direct_kills: list[bool]):
    class TimedOutProcess:
        pid = 4242
        returncode = 1
        calls = 0

        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("review", 1)
            return "", None

        def kill(self):
            direct_kills.append(True)

    return TimedOutProcess()


@requires_windows
@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, b"", b"denied"), "refused"),
        (lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError(2, "not found")), "absent"),
        (
            lambda *_a, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("taskkill", 30)),
            "hung",
        ),
    ],
)
def test_a_failed_taskkill_falls_back_to_killing_the_child(tmp_path, monkeypatch, outcome, label):
    """Losing the tree is bad; leaving the child itself running is worse.

    Every way ``taskkill`` can fail has to reach the same fallback. Two of these
    reach it by raising rather than returning, which previously escaped
    ``run_stage`` and replaced the timed-out result with a crash.
    """
    direct_kills: list[bool] = []
    process = _timed_out_process(direct_kills)
    monkeypatch.setattr(shellstage.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(shellstage.subprocess, "run", outcome)

    result = shellstage.run_stage(tmp_path, "ignored", timeout_seconds=1)

    assert direct_kills == [True], label
    assert result.timed_out is True


@requires_windows
def test_the_taskkill_call_is_bounded_by_its_own_timeout(tmp_path, monkeypatch):
    """It runs on the timeout path, so an unbounded wait here bounds nothing."""
    seen: list[object] = []

    def record(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(
        shellstage.subprocess, "Popen", lambda *_args, **_kwargs: _timed_out_process([])
    )
    monkeypatch.setattr(shellstage.subprocess, "run", record)

    shellstage.run_stage(tmp_path, "ignored", timeout_seconds=1)

    assert seen
    assert all(isinstance(value, int | float) for value in seen)


@requires_posix_permissions
def test_unreadable_copied_file_fails_closed_before_a_stage_runs(feature_repo, tmp_path):
    (feature_repo / ".env").write_bytes(b"SECRET=\xff\n")
    config(feature_repo, "[docs]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "true",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )

    assert env["state"] == "DOCS_GREEN"
    assert env["data"]["copied_file"].endswith("/.env")
    assert "redaction is unavailable" in env["error"]["message"]
    assert "lint" not in agent.run("status")["data"]["stages"]


@requires_posix_permissions
def test_stage_output_is_withheld_if_a_copied_file_becomes_unreadable(feature_repo, tmp_path):
    write(feature_repo, ".env", "SECRET=before-run-secret\n")
    config(feature_repo, "[docs]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "printf '\\377' > .env; printf post-run-secret",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )

    displayed = env["data"]["output_head"] + env["data"]["output_tail"]
    logged = Path(env["data"]["log_path"]).read_text(encoding="utf-8")
    assert env["state"] == "LINT_RED"
    assert "post-run-secret" not in displayed
    assert "post-run-secret" not in logged
    assert displayed == shellstage.REDACTION_FAILURE_OUTPUT
    assert logged == shellstage.REDACTION_FAILURE_OUTPUT
    assert agent.run("status")["data"]["stages"]["lint"]["reason"] == (
        "copied-file redaction became unavailable"
    )


def test_stage_output_is_withheld_if_a_copied_file_is_mutated_then_restored(feature_repo, tmp_path):
    write(feature_repo, ".env", "SECRET=original-secret\n")
    config(feature_repo, "[docs]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    command = (
        "printf 'SECRET=transient-secret\\n' > .env; "
        "printf transient-secret; "
        "printf 'SECRET=original-secret\\n' > .env"
    )
    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        command,
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )

    displayed = env["data"]["output_head"] + env["data"]["output_tail"]
    logged = Path(env["data"]["log_path"]).read_text(encoding="utf-8")
    assert env["state"] == "LINT_RED"
    assert "transient-secret" not in displayed
    assert "transient-secret" not in logged
    assert displayed == shellstage.REDACTION_FAILURE_OUTPUT
    assert logged == displayed
    assert (feature_repo / ".env").read_text(encoding="utf-8") == "SECRET=original-secret\n"


# -- baseline check ---------------------------------------------------------


def test_a_red_baseline_is_reported_rather_than_blamed_on_the_diff(docs_green):
    """If the base commit is already failing, say so instead of blaming the change."""
    agent = docs_green()
    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        "--baseline",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["data"]["baseline_red"] is True
    assert "base" in env["error"]["message"].lower()


def test_a_green_baseline_leaves_the_failure_attributed_to_the_diff(docs_green):
    """Base passes, head fails — so the diff really is responsible and we say so."""
    agent = docs_green()
    # Passes on the base commit (no `loud`), fails at head (the flag was added).
    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "! grep -q loud src/app.py",
        "--record",
        "--baseline",
        expect=ExitCode.STAGE_FAILED,
    )
    assert env["data"]["baseline_red"] is False
    assert "base commit" not in env["error"]["message"]


def test_a_failed_baseline_setup_is_not_reported_as_a_red_base(docs_green):
    agent = docs_green(
        "[docs]\nenabled = false\n\n"
        "[worktree]\n"
        "setup_command = 'case \"$PWD\" in *-baseline) exit 9;; *) exit 0;; esac'\n"
    )

    active_worktree = agent.run("status")["data"]["worktree_path"]
    env = agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        "--baseline",
        expect=ExitCode.STAGE_FAILED,
    )

    assert env["error"]["code"] == "setup_failed"
    assert env["state"] == "LINT_RED"
    assert env["data"]["scope"] == "baseline"
    assert env["data"]["worktree_path"] != active_worktree
    assert env["data"]["worktree_path"].endswith("-baseline")
    assert env["data"]["setup"]["exit_code"] == 9
    assert "baseline_red" not in env["data"]
    assert "--baseline" in env["next"]["command"]
    status = agent.run("status")
    assert status["data"]["stages"]["lint"]["reason"] == "baseline setup command failed"
    assert status["data"]["stages"]["lint"]["attempts"] == 1
    assert status["data"]["stages"]["lint"]["log_path"] is None
    assert status["data"]["setup_failure"]["scope"] == "baseline"
    assert status["data"]["setup_failure"]["stage"] == "lint"
    assert status["data"]["setup_failure"]["command"] == (
        'case "$PWD" in *-baseline) exit 9;; *) exit 0;; esac'
    )
    assert status["data"]["setup_failure"]["exit_code"] == 9
    assert status["data"]["setup_failure"]["worktree_path"] == env["data"]["worktree_path"]
    assert status["next"]["command"] == env["next"]["command"]
    assert "--baseline" in status["next"]["command"]


def test_repeated_baseline_setup_failures_stop_without_a_nonexistent_log(docs_green):
    agent = docs_green(
        "[docs]\nenabled = false\n\n"
        "[stage]\nmax_attempts = 2\n\n"
        "[worktree]\n"
        "setup_command = 'case \"$PWD\" in *-baseline) exit 9;; *) exit 0;; esac'\n"
    )
    command = [
        "stage",
        "run",
        "lint",
        "--command",
        "exit 1",
        "--record",
        "--baseline",
    ]

    for _ in range(2):
        agent.run(*command, expect=ExitCode.STAGE_FAILED)

    status = agent.run("status")
    assert status["data"]["stages"]["lint"]["attempts"] == 2
    assert status["data"]["stages"]["lint"]["log_path"] is None

    env = agent.run(*command, expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 2
    assert env["data"]["setup_failure"]["scope"] == "baseline"
    assert env["next"]["command"] == "agentic-preflight abort --force"
    assert "no stage log" in env["next"]["instruction"]
