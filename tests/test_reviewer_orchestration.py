"""All orchestration calls execute local fake CLIs; never a model or network."""

from __future__ import annotations

import io
import json
import stat
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_preflight.cli import main
from evals.metrics import read_usage

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
state = Path(os.environ["FAKE_STATE"])
index = int(state.read_text()) if state.exists() else 0
state.write_text(str(index + 1))
steps = json.loads(os.environ["FAKE_STEPS"])
step = steps[min(index, len(steps)-1)]
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
    fake = tmp_path / "provider.py"
    fake.write_text(f"#!{sys.executable} -S\n" + SCRIPT)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    executable = fake
    if sys.platform == "win32":
        executable = tmp_path / "provider.cmd"
        executable.write_text(f'@"{sys.executable}" -S "{fake}" %*\n')
    events = tmp_path / "events.jsonl"
    result = CliRunner().invoke(
        main,
        ["reviewer", "run", "--provider", provider, *flags],
        input=json.dumps(CONTEXT if context is None else context),
        env={
            f"AP_{provider.upper()}_BIN": str(executable),
            "AP_REVIEWER_EVENTS_FILE": str(events),
            "AP_REVIEWER_TIMEOUT": "1",
            "FAKE_STATE": str(tmp_path / "state"),
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
        "5",
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
        tmp_path, provider, [{"sleep": 2}, output([])], "--retries", "1", "--budget-seconds", "4"
    )
    assert result.exit_code == 0, result.output
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert [call["outcome"] for call in calls] == ["timeout", "success"]
    assert calls[0]["tokens"]["total_tokens"] is None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_budget_exhaustion_never_returns_candidates_from_an_earlier_call(tmp_path, provider):
    started = time.monotonic()
    result, events, path = run_fake(
        tmp_path,
        provider,
        [output([FINDING]), {"sleep": 3}],
        "--verify",
        "--retries",
        "5",
        "--budget-seconds",
        "0.5",
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert time.monotonic() - started < 3
    assert not json.loads(path.with_name(path.name + ".status.json").read_text())["completed"]
    assert any(event.get("phase") == "verification" for event in events)


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
        "5",
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
        "5",
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
        "5",
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
        "5",
    )
    assert result.exit_code == 0
    assert [event["outcome"] for event in events if event["type"] == "reviewer_call"] == [
        "malformed",
        "success",
    ]


def test_provider_exit_is_not_retried(tmp_path):
    result, events, _ = run_fake(
        tmp_path, "claude", [{"exit": 7}, output([])], "--retries", "5", "--budget-seconds", "5"
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
        "5",
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
        tmp_path, "claude", [output([FINDING]), {"sleep": 3}], "--verify", "--budget-seconds", "0.5"
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
        "5",
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
        tmp_path, "claude", [output([])], "--retries", "1", "--budget-seconds", "5"
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
    assert not (tmp_path / "state").exists()
    calls = [event for event in events if event["type"] == "reviewer_call"]
    assert len(calls) == 1
    assert calls[0]["requests"] == 0
    assert json.loads(Path(str(path) + ".status.json").read_text()) == {"completed": False}
