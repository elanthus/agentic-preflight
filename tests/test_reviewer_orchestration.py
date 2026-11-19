"""All orchestration calls execute local fake CLIs; never a model or network."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from agentic_preflight.cli import main
from agentic_preflight.reviewers import common, orchestration
from agentic_preflight.stages import shellstage
from evals.metrics import read_usage
from tests.liveness import HeldFifo

# Seconds for a call, or a whole review, that a test expects to finish. A fake starts in
# well under a second on most machines, but a loaded Windows runner has needed more.
GENEROUS = "60"

FINDING = {
    "unit": "U0001",
    "path": "app.py",
    "line": 1,
    "severity": "high",
    "action": "auto_fix",
    "title": "Candidate",
}
CONTEXT = {
    "diff": "+ vulnerable()",
    "review_coverage": {"manifest": "a" * 64, "units": [{"id": "U0001", "path": "app.py"}]},
}
SCRIPT = r"""
import json, os, sys, time
from pathlib import Path
prompt = sys.stdin.read()
# The orchestrator logs each call before it starts the provider, so this count names the
# call even when an earlier fake was stopped before it could write anything.
events = Path(os.environ["AP_REVIEWER_EVENTS_FILE"]).read_text().splitlines()
started = sum(json.loads(line)["type"] == "reviewer_call_started" for line in events)
steps = json.loads(os.environ["FAKE_STEPS"])
step = steps[min(started, len(steps))-1]
with open(os.environ["FAKE_PROMPTS"], "a") as out:
    out.write(json.dumps(prompt) + "\n")
if step.get("sleep"):
    time.sleep(step["sleep"])
if step.get("exit"):
    sys.exit(step["exit"])
if step.get("error_result"):
    print(json.dumps({"type": "result", "is_error": True, "subtype": "provider_failure"}))
    sys.exit(0)
answer = step.get("output", "")
if "-o" in sys.argv and step.get("final"):
    Path(sys.argv[sys.argv.index("-o")+1]).write_text(answer)
elif os.environ["FAKE_PROVIDER"] == "claude":
    print(json.dumps({"type":"result", "subtype":"success", "result":answer,
        "modelUsage":{"fake":{"inputTokens":10,"cacheReadInputTokens":2,"cacheCreationInputTokens":3,"outputTokens":4}}}))
else:
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":15,"output_tokens":4}}))
    print(answer)
"""


def run_fake(tmp_path, provider, steps, *flags, context=None):
    """Run the reviewer against a fake CLI that plays one step per call; the last repeats.

    A step that sleeps is a call the test expects to time out, so that call gets one
    second. Every other call gets GENEROUS seconds: how fast a fake starts is not under test.
    """
    fake = tmp_path / "provider.py"
    fake.write_text(f"#!{sys.executable} -S\n" + SCRIPT)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    executable = fake
    if sys.platform == "win32":
        executable = tmp_path / "provider.cmd"
        executable.write_text(f'@"{sys.executable}" -S "{fake}" %*\n')
    events = tmp_path / "events.jsonl"

    def call_timeout():
        # The orchestrator asks before it logs the call, so the count is that call's index.
        started = sum(
            json.loads(line)["type"] == "reviewer_call_started"
            for line in events.read_text().splitlines()
        )
        return 1 if steps[min(started, len(steps) - 1)].get("sleep") else int(GENEROUS)

    # AP_REVIEWER_TIMEOUT is one value for every call. Only a test with a call that must
    # time out needs a different one per call, so only that test replaces the lookup.
    hangs = any(step.get("sleep") for step in steps)
    lookup = call_timeout if hangs else common.timeout_seconds
    with mock.patch.object(common, "timeout_seconds", lookup):
        result = CliRunner().invoke(
            main,
            ["reviewer", "run", "--provider", provider, *flags],
            input=json.dumps(CONTEXT if context is None else context),
            env={
                f"AP_{provider.upper()}_BIN": str(executable),
                "AP_REVIEWER_EVENTS_FILE": str(events),
                "AP_REVIEWER_TIMEOUT": GENEROUS,
                "FAKE_STEPS": json.dumps(steps),
                "FAKE_PROVIDER": provider,
                "FAKE_PROMPTS": str(tmp_path / "prompts"),
            },
        )
    return result, [json.loads(line) for line in events.read_text().splitlines()], events


def output(findings):
    return {"output": json.dumps({"findings": findings})}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_malformed_output_retries_once_with_fresh_output_and_accounts_each_call(tmp_path, provider):
    result, events, path = run_fake(
        tmp_path,
        provider,
        [{"output": "malformed"}, output([])],
        "--retries",
        "1",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["findings"] == []
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert [call["outcome"] for call in calls] == ["malformed", "success"]
    assert [call["requests"] for call in calls] == [1, 1]
    assert all(call["wall_seconds"] > 0 for call in calls)
    assert all(call["tokens"]["total_tokens"] == 19 for call in calls)
    assert read_usage(path, provider)["tokens"]["total_tokens"] is not None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_timeout_can_retry_with_remaining_total_budget(tmp_path, provider):
    result, events, _ = run_fake(
        tmp_path,
        provider,
        [{"sleep": 30}, output([])],
        "--retries",
        "1",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 0, result.output
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert [call["outcome"] for call in calls] == ["timeout", "success"]
    assert calls[0]["tokens"]["total_tokens"] is None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_budget_exhaustion_never_returns_candidates_from_an_earlier_call(tmp_path, provider):
    started = time.monotonic()
    # This budget is real wall time and has to cover the first call, so that call must
    # start and answer in under four seconds. Two seconds ran out on a slow runner before
    # verification could start. The test takes as long as the budget, so it stays small.
    result, events, path = run_fake(
        tmp_path,
        provider,
        [output([FINDING]), {"sleep": 30}],
        "--verify",
        "--retries",
        "5",
        "--budget-seconds",
        "4",
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert time.monotonic() - started < 20
    assert not json.loads(path.with_name(path.name + ".status.json").read_text())["completed"]
    # The budget ended the review, not the retries: each of the six verification
    # attempts the retries allow would time out on its own after a second.
    assert "reviewer total budget exhausted" in result.stderr
    verification = [
        event
        for event in events
        if event["type"] == "reviewer_call" and event.get("phase") == "verification"
    ]
    assert 0 < len(verification) < 6


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_verification_keeps_only_confirmed_candidates_and_logs_rejections(tmp_path, provider):
    decisions = {
        "decisions": [
            {"candidate": 1, "verdict": "reject", "reason": "The guard handles this."},
            {"candidate": 0, "verdict": "confirm", "reason": "The branch is reachable."},
        ]
    }
    result, events, _ = run_fake(
        tmp_path,
        provider,
        [
            output([FINDING, {**FINDING, "title": "Rejected candidate"}]),
            {"output": json.dumps(decisions)},
        ],
        "--verify",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 0, result.output
    submitted = json.loads(result.stdout)
    assert submitted["findings"] == [FINDING]
    assert submitted["coverage"] == {"manifest": "a" * 64, "examined": "all"}
    rejected = [event for event in events if event["type"] == "candidate_rejected"]
    assert rejected[0]["candidate"]["title"] == "Rejected candidate"
    prompts = [json.loads(line) for line in (tmp_path / "prompts").read_text().splitlines()]
    assert CONTEXT["diff"] in prompts[1]
    assert "U0001" in prompts[1]
    assert "Return one JSON object with only a findings array" not in prompts[1]


@pytest.mark.parametrize(
    "decisions",
    [
        [],
        [
            {"candidate": 0, "verdict": "confirm", "reason": "yes"},
            {"candidate": 0, "verdict": "reject", "reason": "no"},
        ],
        [{"candidate": True, "verdict": "confirm", "reason": "yes"}],
    ],
)
def test_verification_rejects_missing_duplicate_and_boolean_indices(tmp_path, decisions):
    result, _, _ = run_fake(
        tmp_path,
        "claude",
        [output([FINDING]), {"output": json.dumps({"decisions": decisions})}],
        "--verify",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 1
    assert result.stdout == ""


def test_verification_retry_does_not_repeat_the_initial_review(tmp_path):
    result, events, _ = run_fake(
        tmp_path,
        "codex",
        [
            output([FINDING]),
            {"output": "bad"},
            {
                "output": json.dumps(
                    {"decisions": [{"candidate": 0, "verdict": "confirm", "reason": "confirmed"}]}
                )
            },
        ],
        "--verify",
        "--retries",
        "1",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 0, result.output
    assert [event["phase"] for event in events if event["type"] == "reviewer_call"] == [
        "review",
        "verification",
        "verification",
    ]


def test_undelivered_unit_is_malformed_and_retried(tmp_path):
    result, events, _ = run_fake(
        tmp_path,
        "claude",
        [output([{**FINDING, "unit": "U9999"}]), output([])],
        "--retries",
        "1",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 0
    assert [event["outcome"] for event in events if event["type"] == "reviewer_call"] == [
        "malformed",
        "success",
    ]


def test_provider_exit_is_not_retried(tmp_path):
    result, events, _ = run_fake(
        tmp_path,
        "claude",
        [{"exit": 7}, output([])],
        "--retries",
        "5",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 1
    assert len([event for event in events if event["type"] == "reviewer_call"]) == 1


def test_codex_retry_cannot_reuse_a_previous_final_output_file(tmp_path):
    result, _, _ = run_fake(
        tmp_path,
        "codex",
        [
            {"output": '{"findings":[{"title":"invalid"}]}', "final": True},
            {"output": "no new findings object"},
        ],
        "--retries",
        "1",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 1
    assert result.stdout == ""


@pytest.mark.parametrize("budget", ["inf", "nan"])
def test_nonfinite_total_budgets_fail_before_calling_a_provider(tmp_path, budget):
    result = CliRunner().invoke(
        main,
        ["reviewer", "run", "--provider", "claude", "--budget-seconds", budget],
        input=json.dumps(CONTEXT),
    )
    assert result.exit_code in (1, 2)
    assert result.stdout == ""


def test_failed_verification_preserves_known_discovery_usage_as_partial(tmp_path):
    result, _, path = run_fake(
        tmp_path,
        "claude",
        [output([FINDING]), {"sleep": 30}],
        "--verify",
        "--budget-seconds",
        GENEROUS,
    )
    assert result.exit_code == 1
    usage = read_usage(path, "claude", require_completion=True)
    assert usage["status"] == "partial"
    assert usage["tokens"]["total_tokens"] == 19


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("section", ["review", "contracts"])
def test_explicit_provider_error_result_is_not_retried(tmp_path, provider, section):
    result, events, _ = run_fake(
        tmp_path,
        provider,
        [{"error_result": True}, output([])],
        "--retries",
        "5",
        "--budget-seconds",
        GENEROUS,
        context={**CONTEXT, "section": section},
    )
    assert result.exit_code == 1
    assert "error result" in result.stderr
    assert result.stdout == ""
    assert len([event for event in events if event["type"] == "reviewer_call"]) == 1


@pytest.mark.parametrize("invalid", ["context", "timeout"])
def test_invalid_setup_clears_previous_invocation_events(tmp_path, invalid):
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"reviewer_call","requests":1}\n')
    status = events.with_name(events.name + ".status.json")
    status.write_text('{"completed":true}\n')
    result = CliRunner().invoke(
        main,
        ["reviewer", "run", "--provider", "claude"],
        input="not JSON" if invalid == "context" else json.dumps(CONTEXT),
        env={
            "AP_REVIEWER_EVENTS_FILE": str(events),
            "AP_REVIEWER_TIMEOUT": "bad" if invalid == "timeout" else "1",
        },
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert events.read_text() == ""
    assert json.loads(status.read_text()) == {"completed": False}


def test_parsed_response_past_budget_fails_without_losing_known_call_usage(tmp_path, monkeypatch):
    from agentic_preflight.reviewers import orchestration

    original_call = orchestration.Orchestrator.call

    def expire_after_parsing(self, prompt, phase, parse):
        def expired(*outputs):
            result = parse(*outputs)
            self.deadline = 0
            return result

        return original_call(self, prompt, phase, expired)

    monkeypatch.setattr(orchestration.Orchestrator, "call", expire_after_parsing)
    result, events, path = run_fake(
        tmp_path, "claude", [output([])], "--retries", "1", "--budget-seconds", GENEROUS
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "budget exhausted" in result.stderr
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert len(calls) == 1
    assert calls[0]["outcome"] == "budget_exhausted"
    assert calls[0]["tokens"]["total_tokens"] == 19
    # The provider response is measured, but the overall review still failed.
    usage = read_usage(path, "claude", require_completion=True)
    assert usage["status"] == "partial"
    assert usage["tokens"]["total_tokens"] == 19


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_stdout_delivery_failure_never_records_completed_status(tmp_path, monkeypatch, failure):
    from agentic_preflight.reviewers import orchestration

    class FailingOutput(io.StringIO):
        def write(self, value):
            if failure == "write":
                raise OSError("output delivery failed")
            return super().write(value)

        def flush(self):
            if failure == "flush":
                raise OSError("output delivery failed")
            return super().flush()

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("AP_REVIEWER_EVENTS_FILE", str(events))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(CONTEXT)))
    monkeypatch.setattr(sys, "stdout", FailingOutput())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(
        orchestration.Orchestrator, "review", lambda self, verify: print('{"findings":[]}')
    )
    assert orchestration.main("claude", 0, 5, False) == 1
    assert "output delivery failed" in sys.stderr.getvalue()
    assert json.loads(Path(str(events) + ".status.json").read_text()) == {"completed": False}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX argument encoding")
def test_unencodable_codex_path_reports_failure_without_traceback(tmp_path):
    result, events, path = run_fake(
        tmp_path,
        "codex",
        [output([])],
        "--retries",
        "1",
        context={**CONTEXT, "worktree_path": "\ud800"},
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("independent reviewer failed:")
    assert "Traceback" not in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert not (tmp_path / "prompts").exists()
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert len(calls) == 1
    assert calls[0]["requests"] == 0
    assert json.loads(Path(str(path) + ".status.json").read_text()) == {"completed": False}


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and process groups")
# The command a project configures as its reviewer, as ``run_stage`` receives it.
REVIEWER_STAGE = (
    f"{shlex.quote(sys.executable)} -m agentic_preflight reviewer run --provider claude"
)


class SleepingProvider:
    """A provider CLI that records its pid and then waits to be stopped."""

    def __init__(self, tmp_path: Path) -> None:
        self.pid_file = tmp_path / "provider.pid"
        self.held = HeldFifo(tmp_path / "provider.alive")
        self.executable = tmp_path / "provider"
        # The FIFO is opened before the pid is written, and ``sleep`` inherits it.
        self.executable.write_text(
            f'#!/bin/sh\nexec 3> "{self.held.path}"\necho $$ > "{self.pid_file}"\nexec sleep 120\n'
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)

    def _recorded_pid(self) -> int | None:
        text = self.pid_file.read_text().strip() if self.pid_file.exists() else ""
        return int(text) if text else None

    def wait_until_started(self, starter: subprocess.Popen | None = None) -> None:
        deadline = time.monotonic() + 30
        while self._recorded_pid() is None:
            assert time.monotonic() < deadline, "the provider never started"
            if starter is not None:
                assert starter.poll() is None, "the process exited before the provider started"
            time.sleep(0.05)

    def assert_stopped(self) -> None:
        assert self._recorded_pid() is not None, "the provider never started"
        assert self.held.closed_by_all(), "the provider is still running"

    def cleanup(self) -> None:
        # Ending the provider also ends a reviewer that a failed test left waiting for it.
        pid = self._recorded_pid()
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        self.held.close()


@pytest.fixture
def sleeping_provider(tmp_path, monkeypatch):
    provider = SleepingProvider(tmp_path)
    monkeypatch.setenv("AP_CLAUDE_BIN", str(provider.executable))
    monkeypatch.setenv("AP_REVIEWER_TIMEOUT", "60")
    try:
        yield provider
    finally:
        provider.cleanup()


def start_reviewer(**popen_kwargs) -> subprocess.Popen:
    reviewer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from agentic_preflight.cli import main; main()",
            *("reviewer", "run", "--provider", "claude"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        cwd=Path(__file__).parent.parent,
        **popen_kwargs,
    )
    assert reviewer.stdin is not None
    reviewer.stdin.write(json.dumps(CONTEXT).encode())
    reviewer.stdin.close()
    return reviewer


@posix_only
@pytest.mark.parametrize("signum", [signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)])
def test_a_signal_to_the_reviewer_stops_the_provider_it_started(sleeping_provider, signum):
    # The provider CLI runs in its own session, so nothing else forwards the signal.
    reviewer = start_reviewer()
    try:
        sleeping_provider.wait_until_started(reviewer)
        reviewer.send_signal(signum)
        reviewer.wait(timeout=30)
        assert reviewer.returncode == -signum
        sleeping_provider.assert_stopped()
    finally:
        if reviewer.poll() is None:
            reviewer.kill()


@posix_only
def test_killing_the_reviewers_process_group_stops_the_provider_it_started(sleeping_provider):
    # What a stage timeout does to the reviewer. SIGKILL cannot be handled, and the
    # provider is in another process group, so the reviewer cannot forward this one.
    reviewer = start_reviewer(start_new_session=True)
    try:
        sleeping_provider.wait_until_started(reviewer)
        os.killpg(reviewer.pid, signal.SIGKILL)
        reviewer.wait(timeout=30)
        sleeping_provider.assert_stopped()
    finally:
        if reviewer.poll() is None:
            reviewer.kill()


@posix_only
def test_terminating_the_tool_stops_the_provider_its_reviewer_started(tmp_path, sleeping_provider):
    driver = tmp_path / "driver.py"
    driver.write_text(
        "from agentic_preflight.stages import shellstage\n"
        f"shellstage.run_stage({str(tmp_path)!r}, {REVIEWER_STAGE!r}, "
        f"timeout_seconds=60, stdin_text={json.dumps(CONTEXT)!r})\n",
        encoding="utf-8",
    )
    tool = subprocess.Popen([sys.executable, str(driver)], cwd=Path(__file__).parent.parent)
    try:
        sleeping_provider.wait_until_started(tool)
        tool.send_signal(signal.SIGTERM)
        tool.wait(timeout=30)
        assert tool.returncode == -signal.SIGTERM
        sleeping_provider.assert_stopped()
    finally:
        if tool.poll() is None:
            tool.kill()


@posix_only
def test_a_stage_timeout_stops_the_provider_the_reviewer_started(tmp_path, sleeping_provider):
    # Long enough for the reviewer to start its provider, which takes under a second.
    result = shellstage.run_stage(
        tmp_path, REVIEWER_STAGE, timeout_seconds=5, stdin_text=json.dumps(CONTEXT)
    )
    assert result.timed_out
    sleeping_provider.assert_stopped()


def test_the_termination_guard_is_given_the_provider_before_the_parent_death_watcher(
    tmp_path, monkeypatch
):
    # On Windows the termination guard stops only a process it has been given, and
    # an interrupt can arrive during whatever runs before it is.
    bound: list[str] = []

    def recording(name, real):
        @contextlib.contextmanager
        def guard_block():
            with real() as guard:
                real_bind = guard.bind

                def bind(process):
                    bound.append(name)
                    real_bind(process)

                guard.bind = bind
                yield guard

        return guard_block

    for name in ("kill_on_termination", "kill_when_parent_dies"):
        monkeypatch.setattr(orchestration, name, recording(name, getattr(orchestration, name)))
    result, _events, _path = run_fake(tmp_path, "claude", [output([])])
    assert result.exit_code == 0, result.output
    assert bound == ["kill_on_termination", "kill_when_parent_dies"]
