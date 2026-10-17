"""Measured eval telemetry; absent evidence is never silently treated as zero."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _sum_known(values: Iterable[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _sum_complete(values: Iterable[int | None]) -> int | None:
    items = list(values)
    return sum(items) if all(item is not None for item in items) else None


def usage_record(
    tokens: dict[str, int | None],
    *,
    complete: bool,
    source: str,
    complete_fields: set[str] | None = None,
) -> dict[str, Any]:
    counts = {field: tokens.get(field) for field in TOKEN_FIELDS}
    counts["total_tokens"] = _sum_complete([counts["input_tokens"], counts["output_tokens"]])
    measured = any(value is not None for value in counts.values())
    status = (
        "complete"
        if complete and counts["total_tokens"] is not None
        else "partial"
        if measured
        else "unknown"
    )
    fields = {
        field
        for field, value in counts.items()
        if status == "complete"
        and value is not None
        and (complete_fields is None or field in complete_fields)
    }
    return {"status": status, "source": source, "tokens": counts, "complete_fields": sorted(fields)}


def zero_usage(source: str) -> dict[str, Any]:
    return usage_record(dict.fromkeys(TOKEN_FIELDS, 0), complete=True, source=source)


def aggregate_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose exact totals separately from observations on incomplete invocations."""
    totals = {
        field: _sum_complete(
            record["tokens"][field]
            if record["status"] == "complete" and field in record["complete_fields"]
            else None
            for record in records
        )
        for field in TOKEN_FIELDS
    }
    observed = {
        field: _sum_known(record["tokens"][field] for record in records) for field in TOKEN_FIELDS
    }
    if not records:
        observed = dict.fromkeys(TOKEN_FIELDS, 0)
    status = (
        "complete"
        if all(record["status"] == "complete" for record in records)
        else "partial"
        if any(value is not None for value in observed.values())
        else "unknown"
    )
    return {
        "status": status,
        "totals": totals,
        "observed": observed,
        "measurements": {
            status: sum(record["status"] == status for record in records)
            for status in ("complete", "partial", "unknown")
        },
    }


def _codex_tokens(raw: dict[str, Any]) -> dict[str, int | None]:
    # Cached input and reasoning output are subsets of input/output respectively.
    tokens = {field: _count(raw.get(field)) for field in TOKEN_FIELDS}
    total, cached = tokens["input_tokens"], tokens["cached_input_tokens"]
    if total is not None and cached is not None and total >= cached:
        tokens["uncached_input_tokens"] = total - cached
    return tokens


def _claude_tokens(raw: dict[str, Any], *, camel: bool = False) -> dict[str, int | None]:
    keys = (
        ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
        if camel
        else (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )
    uncached, cached, created, output = [_count(raw.get(key)) for key in keys]
    # Claude's input count excludes BOTH cache reads and cache creation.
    return {
        "input_tokens": _sum_complete([uncached, cached, created]),
        "uncached_input_tokens": uncached,
        "cached_input_tokens": cached,
        "cache_creation_input_tokens": created,
        "output_tokens": output,
    }


def _combine_tokens(items: list[dict[str, int | None]]) -> dict[str, int | None]:
    return {field: _sum_known(item.get(field) for item in items) for field in TOKEN_FIELDS}


def _complete_counts(items: list[dict[str, int | None]]) -> bool:
    return bool(items) and all(
        item.get("input_tokens") is not None and item.get("output_tokens") is not None
        for item in items
    )


def _complete_fields(items: list[dict[str, int | None]]) -> set[str]:
    fields = {
        field
        for field in TOKEN_FIELDS
        if items and all(item.get(field) is not None for item in items)
    }
    if {"input_tokens", "output_tokens"} <= fields:
        fields.add("total_tokens")
    return fields


def _claude_usage(events: list[dict[str, Any]], intact: bool) -> dict[str, Any]:
    results = [event for event in events if event.get("type") == "result"]
    result = results[-1] if results else {}
    # The per-model aggregate includes subagents; result.usage only covers the
    # main loop. Never add these two overlapping views together.
    models = result.get("modelUsage")
    success = result.get("subtype") == "success" and not result.get("is_error", False)
    if success and isinstance(models, dict) and models:
        tokens = [
            _claude_tokens(value, camel=True)
            for value in models.values()
            if isinstance(value, dict)
        ]
        if tokens:
            return usage_record(
                _combine_tokens(tokens),
                complete=intact and len(tokens) == len(models) and _complete_counts(tokens),
                source="claude.modelUsage",
                complete_fields=_complete_fields(tokens),
            )

    # A crash result can contain zeros even after consuming tokens. Recover input
    # and cache evidence from unique assistant messages; their output is a
    # placeholder, so it must remain unknown without a trustworthy final result.
    messages = {}
    for event in events:
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        identity = message.get("id")
        raw = message.get("usage")
        if isinstance(identity, str) and isinstance(raw, dict):
            tokens = _claude_tokens(raw)
            tokens["output_tokens"] = None
            messages[identity] = tokens
    raw = result.get("usage")
    if success and isinstance(raw, dict):
        return usage_record(_claude_tokens(raw), complete=False, source="claude.main_loop")
    candidates = []
    if messages:
        candidates.append(_combine_tokens(list(messages.values())))
    if isinstance(models, dict) and models and result.get("subtype") != "error_during_execution":
        tokens = [
            _claude_tokens(value, camel=True)
            for value in models.values()
            if isinstance(value, dict)
        ]
        if tokens:
            candidates.append(_combine_tokens(tokens))
    if isinstance(raw, dict) and result.get("subtype") != "error_during_execution":
        candidates.append(_claude_tokens(raw))
    if candidates:
        # Error summaries may undercount. These views overlap, so take the
        # largest observed count per field instead of adding them.
        counts = {
            field: max(known)
            if (known := [item[field] for item in candidates if item.get(field) is not None])
            else None
            for field in TOKEN_FIELDS
        }
        return usage_record(counts, complete=False, source="claude.partial")
    return usage_record({}, complete=False, source="claude")


def read_usage(path: Path, executor: str, *, require_completion: bool = False) -> dict[str, Any]:
    """Read all complete JSONL records, retaining evidence before a truncated tail."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        record = usage_record({}, complete=False, source=executor)
        if not isinstance(exc, FileNotFoundError):
            record["read_error"] = str(exc)
        return record
    events = []
    intact = True
    status_path = Path(str(path) + ".status.json")
    try:
        intact = json.loads(status_path.read_text(encoding="utf-8")).get("completed") is True
    except (OSError, ValueError, AttributeError):
        intact = not require_completion
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            intact = False
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            intact = False
    if executor == "claude":
        return _claude_usage(events, intact)
    completed = [event for event in events if event.get("type") == "turn.completed"]
    tokens = [
        _codex_tokens(event["usage"]) for event in completed if isinstance(event.get("usage"), dict)
    ]
    failed = any(event.get("type") in {"turn.failed", "error"} for event in events)
    if not tokens:
        return usage_record({}, complete=False, source="codex.turn.completed")
    return usage_record(
        _combine_tokens(tokens),
        complete=intact
        and not failed
        and len(tokens) == len(completed)
        and _complete_counts(tokens),
        source="codex.turn.completed",
        complete_fields=_complete_fields(tokens),
    )


def _state_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    events = []
    intact = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            intact = False
            break
        if not isinstance(event, dict):
            intact = False
            break
        if event.get("event") == "state_transition":
            events.append(event)
    return events, intact


def workflow_metrics(run_dir: Path | None, stopped_ns: int) -> dict[str, Any]:
    """Measure recorded state visits through the end of this eval observation."""
    visits: list[dict[str, Any]] = []
    complete = False
    final_state = None
    if run_dir is not None:
        try:
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            final_state = run["state"]
            transitions, intact = _state_events(run_dir / "events.jsonl")
            complete = intact and bool(transitions) and transitions[0].get("from") is None
            previous = None
            for event in transitions:
                instant = event.get("monotonic_ns")
                if type(instant) is not int or instant < 0 or instant > stopped_ns:
                    complete = False
                    break
                if event.get("from") != previous or (visits and instant < visits[-1]["entered_ns"]):
                    complete = False
                    break
                if not isinstance(event.get("to"), str):
                    complete = False
                    break
                visits.append({"state": event["to"], "entered_ns": instant})
                previous = event["to"]
            complete = complete and previous == final_state
        except (OSError, KeyError, TypeError, ValueError):
            complete = False
    states: dict[str, Any] = {}
    for index, visit in enumerate(visits):
        end = visits[index + 1]["entered_ns"] if index + 1 < len(visits) else stopped_ns
        seconds = (end - visit["entered_ns"]) / 1e9 if complete or index + 1 < len(visits) else None
        visit["duration_seconds"] = seconds
        del visit["entered_ns"]
        entry = states.setdefault(
            visit["state"], {"visits": 0, "duration_seconds": 0.0, "observed_duration_seconds": 0.0}
        )
        entry["visits"] += 1
        entry["duration_seconds"] = (
            entry["duration_seconds"] + seconds
            if entry["duration_seconds"] is not None and seconds is not None
            else None
        )
        if seconds is not None:
            entry["observed_duration_seconds"] += seconds
    return {"complete": complete, "final_state": final_state, "visits": visits, "states": states}


def aggregate_states(results: list[dict[str, Any]]) -> dict[str, Any]:
    states: dict[str, Any] = {}
    for result in results:
        for name, observation in result["workflow"]["states"].items():
            entry = states.setdefault(
                name, {"visits": 0, "duration_seconds": 0.0, "observed_duration_seconds": 0.0}
            )
            entry["visits"] += observation["visits"]
            entry["duration_seconds"] = (
                entry["duration_seconds"] + observation["duration_seconds"]
                if entry["duration_seconds"] is not None
                and observation["duration_seconds"] is not None
                else None
            )
            entry["observed_duration_seconds"] += observation["observed_duration_seconds"]
    return {
        "complete": all(result["workflow"]["complete"] for result in results),
        "states": states,
    }
