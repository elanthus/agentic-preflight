"""Telemetry must not turn missing, truncated, or overlapping usage into exact totals."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agentic_preflight.machine import Action, State
from agentic_preflight.runs._session import _apply
from agentic_preflight.store import Store
from evals import metrics
from evals import run as eval_run
from tests.conftest import make_run


def stream(tmp_path, events, *, tail=""):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events) + tail, encoding="utf-8")
    return path


def codex_event(**overrides):
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 70,
            "output_tokens": 20,
            "reasoning_output_tokens": 10,
            **overrides,
        },
    }


def claude_result(**overrides):
    return {
        "type": "result",
        "subtype": "success",
        "result": '{"findings":[]}',
        "modelUsage": {
            "model-a": {
                "inputTokens": 10,
                "cacheReadInputTokens": 70,
                "cacheCreationInputTokens": 20,
                "outputTokens": 30,
            },
            "model-b": {
                "inputTokens": 5,
                "cacheReadInputTokens": 10,
                "cacheCreationInputTokens": 0,
                "outputTokens": 5,
            },
        },
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 20,
            "output_tokens": 30,
        },
        **overrides,
    }


def assistant(identity="message-1", **overrides):
    return {
        "type": "assistant",
        "message": {
            "id": identity,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 20,
                "output_tokens": 1,
                **overrides,
            },
        },
    }


def test_codex_usage_counts_cache_and_reasoning_only_once(tmp_path):
    record = metrics.read_usage(stream(tmp_path, [codex_event()]), "codex")
    assert record["status"] == "complete"
    assert record["tokens"]["total_tokens"] == 120
    assert record["tokens"]["uncached_input_tokens"] == 30
    assert record["tokens"]["cache_creation_input_tokens"] is None


@pytest.mark.parametrize("bad", [None, True, -1, 3.5, "20"])
def test_invalid_token_counts_are_unknown_not_zero(tmp_path, bad):
    record = metrics.read_usage(stream(tmp_path, [codex_event(output_tokens=bad)]), "codex")
    assert record["status"] == "partial"
    assert record["tokens"]["input_tokens"] == 100
    assert record["tokens"]["output_tokens"] is None
    assert record["tokens"]["total_tokens"] is None


@pytest.mark.parametrize("tail", ['{"type":', "not json\n", "null\n"])
def test_truncated_or_malformed_stream_preserves_prior_usage(tmp_path, tail):
    record = metrics.read_usage(stream(tmp_path, [codex_event()], tail=tail), "codex")
    assert record["status"] == "partial"
    assert record["tokens"]["total_tokens"] == 120
    combined = metrics.aggregate_usage([record])
    assert combined["totals"]["total_tokens"] is None
    assert combined["observed"]["total_tokens"] == 120


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_no_usage_is_unknown(tmp_path, executor):
    for path in (tmp_path / "missing", stream(tmp_path, [])):
        record = metrics.read_usage(path, executor)
        assert record["status"] == "unknown"
        assert all(value is None for value in record["tokens"].values())


def test_failed_codex_stream_retains_completed_turns_as_partial(tmp_path):
    record = metrics.read_usage(stream(tmp_path, [codex_event(), {"type": "turn.failed"}]), "codex")
    assert record["status"] == "partial"
    assert record["tokens"]["total_tokens"] == 120


def test_claude_uses_model_totals_including_subagents_without_adding_main_loop(tmp_path):
    record = metrics.read_usage(
        stream(tmp_path, [assistant(), assistant(), claude_result()]), "claude"
    )
    assert record["status"] == "complete"
    assert record["source"] == "claude.modelUsage"
    assert record["tokens"]["input_tokens"] == 115
    assert record["tokens"]["output_tokens"] == 35
    assert record["tokens"]["total_tokens"] == 150
    assert record["tokens"]["cached_input_tokens"] == 80
    assert record["tokens"]["cache_creation_input_tokens"] == 20
    assert record["tokens"]["reasoning_output_tokens"] is None


def test_claude_main_loop_only_usage_is_partial(tmp_path):
    record = metrics.read_usage(stream(tmp_path, [claude_result(modelUsage=None)]), "claude")
    assert record["status"] == "partial"
    assert record["tokens"]["total_tokens"] == 130


def test_claude_crash_zeros_do_not_erase_unique_message_input_usage(tmp_path):
    crash = claude_result(
        subtype="error_during_execution",
        modelUsage={},
        usage=dict.fromkeys(claude_result()["usage"], 0),
    )
    record = metrics.read_usage(
        stream(tmp_path, [assistant(), assistant(), assistant("message-2"), crash]), "claude"
    )
    assert record["status"] == "partial"
    assert record["tokens"]["input_tokens"] == 200
    assert record["tokens"]["output_tokens"] is None
    assert record["tokens"]["total_tokens"] is None
    unknown = metrics.read_usage(stream(tmp_path, [crash]), "claude")
    assert unknown["status"] == "unknown"


def test_claude_error_result_usage_is_kept_without_adding_overlapping_messages(tmp_path):
    record = metrics.read_usage(
        stream(tmp_path, [assistant(), claude_result(subtype="error_max_turns")]), "claude"
    )
    assert record["status"] == "partial"
    assert record["tokens"]["input_tokens"] == 115
    assert record["tokens"]["output_tokens"] == 35


def test_missing_cache_category_preserves_uncached_observation(tmp_path):
    event = assistant(cache_read_input_tokens=None)
    record = metrics.read_usage(stream(tmp_path, [event]), "claude")
    assert record["tokens"]["uncached_input_tokens"] == 10
    assert record["tokens"]["cached_input_tokens"] is None
    assert record["tokens"]["input_tokens"] is None


def test_rollup_never_presents_known_subset_as_exact_total(tmp_path):
    complete = metrics.read_usage(stream(tmp_path, [codex_event()]), "codex")
    partial = {**complete, "status": "partial"}
    unknown = metrics.usage_record({}, complete=False, source="missing")
    for records, expected in (([complete, partial], 240), ([complete, unknown], 120)):
        result = metrics.aggregate_usage(records)
        assert result["status"] == "partial"
        assert result["totals"]["total_tokens"] is None
        assert result["observed"]["total_tokens"] == expected
    assert metrics.aggregate_usage([unknown])["status"] == "unknown"
    assert metrics.aggregate_usage([metrics.zero_usage("dry")])["totals"]["total_tokens"] == 0


def test_optional_category_missing_in_one_turn_is_observed_but_not_an_exact_sum(tmp_path):
    record = metrics.read_usage(
        stream(tmp_path, [codex_event(), codex_event(reasoning_output_tokens=None)]), "codex"
    )
    assert record["status"] == "complete"
    rollup = metrics.aggregate_usage([record])
    assert rollup["totals"]["total_tokens"] == 240
    assert rollup["totals"]["reasoning_output_tokens"] is None
    assert rollup["observed"]["reasoning_output_tokens"] == 10


def test_committed_state_trace_retains_intermediate_states_and_ignores_rollback(tmp_path):
    store = Store(tmp_path)
    run = store.create_run(make_run())
    with store.transaction(run.run_id) as doc:
        _apply(doc, Action.CREATE_WORKTREE)
        _apply(doc, Action.BEGIN_SYNC)
        _apply(doc, Action.SYNC_PASSED)
        _apply(doc, Action.BEGIN_REVIEW)
    before = store.load_events(run.run_id)

    def rolled_back_transition():
        with store.transaction(run.run_id) as doc:
            _apply(doc, Action.RUN_REVIEW_COMMAND)
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        rolled_back_transition()
    assert store.load_events(run.run_id) == before
    assert [event["to"] for event in before] == [
        "CREATED",
        "WORKTREE_READY",
        "SYNC_RUNNING",
        "SYNC_GREEN",
        "REVIEW_AWAITING_FINDINGS",
    ]
    assert store.load_run(run.run_id).state is State.REVIEW_AWAITING_FINDINGS
    assert "_state_events" not in store.run_path(run.run_id).read_text()


def test_workflow_duration_accounts_for_repeated_visits_and_final_observation(tmp_path):
    states = ["CREATED", "RUNNING", "RED", "RUNNING", "GREEN"]
    events = [
        {
            "event": "state_transition",
            "from": states[i - 1] if i else None,
            "to": state,
            "monotonic_ns": i * 1_000_000_000,
        }
        for i, state in enumerate(states)
    ]
    stream(tmp_path, events)
    (tmp_path / "run.json").write_text(json.dumps({"state": "GREEN"}))
    result = metrics.workflow_metrics(tmp_path, 6_000_000_000)
    assert result["complete"]
    assert result["states"]["RUNNING"] == {
        "visits": 2,
        "duration_seconds": 2.0,
        "observed_duration_seconds": 2.0,
    }
    assert result["states"]["GREEN"]["duration_seconds"] == 2.0
    assert sum(visit["duration_seconds"] for visit in result["visits"]) == 6.0


def test_truncated_state_history_keeps_known_intervals_without_inventing_tail_time(tmp_path):
    events = [
        {"event": "state_transition", "from": None, "to": "CREATED", "monotonic_ns": 0},
        {
            "event": "state_transition",
            "from": "CREATED",
            "to": "RUNNING",
            "monotonic_ns": 1_000_000_000,
        },
    ]
    stream(tmp_path, events, tail='{"event":')
    (tmp_path / "run.json").write_text(json.dumps({"state": "GREEN"}))
    result = metrics.workflow_metrics(tmp_path, 6_000_000_000)
    assert not result["complete"]
    assert result["states"]["CREATED"]["duration_seconds"] == 1.0
    assert result["states"]["RUNNING"]["duration_seconds"] is None
    assert result["states"]["RUNNING"]["observed_duration_seconds"] == 0.0


def install_provider(tmp_path, executor, events, ending):
    path = tmp_path / f"fake_{executor}.py"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        f"events = {events!r}\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
        "if '-o' in sys.argv:\n"
        "    for event in events:\n"
        "        if 'findings' in event:\n"
        "            open(sys.argv[sys.argv.index('-o') + 1], 'w').write(json.dumps(event))\n"
        + ending,
        encoding="utf-8",
    )
    path.chmod(0o755)
    if sys.platform == "win32":
        shim = path.with_suffix(".cmd")
        shim.write_text(f'@"{sys.executable}" "{path}" %*\n', encoding="utf-8")
        return shim
    return path


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("ending", ["success", "exit", "timeout", "bad_submission"])
def test_real_eval_collects_metrics_across_cli_and_submission_failures(
    tmp_path, monkeypatch, executor, ending
):
    if executor == "codex":
        events = [codex_event()]
        if ending != "bad_submission":
            events.insert(0, {"findings": []})
    else:
        events = [claude_result()]
        if ending == "bad_submission":
            events[0]["result"] = "invalid findings"
        if ending == "timeout":
            events = [assistant(), assistant()]
    tail = "time.sleep(5)\n" if ending == "timeout" else "sys.exit(2)\n" if ending == "exit" else ""
    provider = install_provider(tmp_path, executor, events, tail)
    monkeypatch.setenv(f"AP_{executor.upper()}_BIN", str(provider))
    monkeypatch.setenv("AP_REVIEWER_TIMEOUT", "1")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    case = eval_run.load_case(eval_run.CASES_ROOT / "unguarded-division")
    result = eval_run.run_case_snapshot(
        case,
        snapshot="vulnerable",
        mode="real",
        executor=executor,
        grounding="off",
        workspace=tmp_path / "workspace",
    )
    assert result["status"] == ("resolved" if ending == "success" else "unresolved")
    assert result["runtime_seconds"] > 0
    assert result["reviewer_invocations"] == 1
    assert result["workflow"]["complete"]
    assert result["workflow"]["states"]["REVIEW_COMMAND_RUNNING"]["duration_seconds"] > 0
    assert result["usage"]["status"] == ("partial" if ending in {"exit", "timeout"} else "complete")
    if ending != "success":
        assert result["finding_count"] is None
        assert result["failure"]["envelope"]["data"]


def test_invalid_cli_output_is_recorded_and_other_snapshots_continue(tmp_path, monkeypatch):
    real_cli = eval_run._cli

    def broken_cli(repo, env, *argv):
        if argv[0] == "start" and "vulnerable" in env.get("AP_EVAL_SCRIPT", ""):
            raise eval_run.EvaluationError("invalid JSON")
        return real_cli(repo, env, *argv)

    monkeypatch.setattr(eval_run, "_cli", broken_cli)
    summary = eval_run.run_evaluation(
        mode="dry",
        executor=None,
        grounding=("off",),
        out=tmp_path,
        case_ids=("unguarded-division",),
    )
    assert summary["unresolved"] == 1
    assert summary["finding_count"] is None
    assert summary["observed_finding_count"] == 1
    assert not summary["workflow"]["complete"]
    assert summary["runtime_seconds"] > 0
    assert json.loads((tmp_path / "summary.json").read_text())["unresolved"] == 1


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_outer_review_timeout_preserves_flushed_usage(tmp_path, monkeypatch, executor):
    events = [codex_event()] if executor == "codex" else [assistant(), assistant()]
    provider = install_provider(tmp_path, executor, events, "time.sleep(5)\n")
    monkeypatch.setenv(f"AP_{executor.upper()}_BIN", str(provider))
    monkeypatch.setenv("AP_REVIEWER_TIMEOUT", "10")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    config_text = eval_run._config_text

    def short_timeout(**kwargs):
        return config_text(**kwargs).replace("timeout_seconds = 660", "timeout_seconds = 1")

    monkeypatch.setattr(eval_run, "_config_text", short_timeout)
    result = eval_run.run_case_snapshot(
        eval_run.load_case(eval_run.CASES_ROOT / "unguarded-division"),
        snapshot="vulnerable",
        mode="real",
        executor=executor,
        grounding="off",
        workspace=tmp_path / "workspace",
    )
    assert result["status"] == "unresolved"
    assert result["failure"]["envelope"]["data"]["timed_out"]
    assert result["usage"]["status"] == "partial"
    assert result["usage"]["tokens"]["input_tokens"] == 100
    assert result["workflow"]["final_state"] == "REVIEW_COMMAND_RED"
