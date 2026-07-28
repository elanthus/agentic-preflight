import os
import stat
from pathlib import Path

import pytest

from agentic_preflight.publish import github


def test_pr_health_classifies_pending_failed_and_passed_checks():
    pending = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": "OPEN",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {"name": "tests", "status": "IN_PROGRESS", "conclusion": ""}
            ],
        }
    )
    assert pending.outcome == "pending"

    failed = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": "OPEN",
            "mergeStateStatus": "UNSTABLE",
            "statusCheckRollup": [
                {
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/o/r/actions/runs/42/job/9",
                }
            ],
        }
    )
    assert failed.outcome == "failed"
    assert failed.failed_checks[0].run_id == "42"

    passed = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
        }
    )
    assert passed.outcome == "checks_passed"

    passed_with_skipped_job = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {
                    "name": "publish from main",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                },
            ],
        }
    )
    assert passed_with_skipped_job.outcome == "checks_passed"
    assert passed_with_skipped_job.failed_checks == []

    legacy_context = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"context": "lint", "state": "SUCCESS"}],
        }
    )
    assert legacy_context.outcome == "checks_passed"


@pytest.mark.parametrize(
    "state,outcome",
    [("MERGED", "merged"), ("CLOSED", "closed")],
)
def test_pr_health_classifies_terminal_pr_states(state: str, outcome: str):
    health = github.parse_pr_health(
        {
            "url": "https://github.com/o/r/pull/1",
            "state": state,
            "mergeStateStatus": "UNKNOWN",
            "statusCheckRollup": [],
        }
    )
    assert health.outcome == outcome


def test_failed_check_logs_are_fetched_via_gh(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.log"
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "auth" ]; then exit 0; fi\n'
        'if [ "$1" = "run" ]; then echo "test failure details"; exit 0; fi\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    check = github.CheckResult(
        name="tests",
        status="COMPLETED",
        conclusion="FAILURE",
        details_url="https://github.com/o/r/actions/runs/42/job/9",
        run_id="42",
    )

    logs = github.failed_check_logs(tmp_path, [check])

    assert logs == {"42": "test failure details"}
    assert "run view 42 --log-failed" in log.read_text()


def test_pull_request_health_rejects_invalid_json(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(github, "gh_available", lambda: True)
    monkeypatch.setattr(github, "gh_authenticated", lambda cwd: True)
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda *args, **kwargs: github.subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )
    with pytest.raises(github.GhUnavailable):
        github.pull_request_health(tmp_path, "https://github.com/o/r/pull/1")


def test_revalidated_repair_updates_the_existing_pr(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []
    monkeypatch.setattr(github, "gh_available", lambda: True)
    monkeypatch.setattr(github, "gh_authenticated", lambda cwd: True)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return github.subprocess.CompletedProcess(
                argv, 0, '[{"url":"https://github.com/o/r/pull/1"}]', ""
            )
        return github.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    result = github.create_or_update_pull_request(
        tmp_path,
        base="main",
        head="feature/x",
        title="repair CI",
        body="fresh validation passed",
    )

    assert result.created is False
    assert result.url.endswith("/pull/1")
    assert any(argv[:3] == ["gh", "pr", "edit"] for argv in calls)
