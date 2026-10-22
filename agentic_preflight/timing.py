"""Wall-clock time in each run state, from the run's ``state_transition`` events.

Every committed state change appends a ``state_transition`` event with ``from``,
``to``, and a UTC ``at`` (see :func:`agentic_preflight.runs._session._apply`).
This module turns that chain into time spent per state for the run summary.

It reads ``at`` only. ``monotonic_ns`` is exact but valid only within one boot of
one host, and a run often spans several commands, sessions, and machines. ``at``
has one-second granularity, so a state left within the same second it was
entered counts as zero seconds.

The rules follow the eval's ``workflow_metrics``: absent evidence is ``None``,
never zero.

* No event log, or no transitions in it: there is no timing at all (``None``).
* The chain is complete when it starts at creation (``from`` is ``None``), each
  ``from`` names the previous ``to``, every ``at`` is a time no earlier than the
  one before, and the last ``to`` is the run's terminal state.
* At the first break the chain stops. Visits before the break keep their
  seconds. The visit whose end is unknown has ``None``, and so does the total
  of its state.
* Review and shell totals are ``None`` unless the chain is complete, because a
  broken chain can hide later visits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .machine import TERMINAL_STATES

#: States of the stages the agent judges: review, and the docs stage after it.
REVIEW_STATE_PREFIXES = ("REVIEW_", "DOCS_")
#: States of the deterministic shell stages.
SHELL_STATE_PREFIXES = ("LINT_", "TEST_")
_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_STATES)


def _parse_time(value: Any) -> datetime | None:
    """An ISO 8601 time with a zone, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _transitions(events: Iterable[Any]) -> list[Mapping[str, Any]]:
    return [
        event
        for event in events
        if isinstance(event, Mapping) and event.get("event") == "state_transition"
    ]


def _visits(transitions: list[Mapping[str, Any]]) -> tuple[list[tuple[str, datetime]], bool]:
    """The states entered, in order, up to the first break, and whether there was none."""
    visits: list[tuple[str, datetime]] = []
    previous: str | None = None
    for event in transitions:
        target = event.get("to")
        instant = _parse_time(event.get("at"))
        if (
            not isinstance(target, str)
            or instant is None
            or event.get("from") != previous
            or (visits and instant < visits[-1][1])
        ):
            return visits, False
        visits.append((target, instant))
        previous = target
    return visits, True


def _total(states: Mapping[str, Mapping[str, Any]], prefixes: tuple[str, ...]) -> float | None:
    total = 0.0
    for name, entry in states.items():
        if name.startswith(prefixes):
            if entry["seconds"] is None:
                return None
            total += entry["seconds"]
    return total


def state_timing(events: Iterable[Any] | None, final_state: str | None) -> dict[str, Any] | None:
    """Visits and seconds per state for one run, or ``None`` without transitions.

    ``final_state`` is the run's terminal state, as the summary reports it. The
    terminal visit itself is not listed: the run ends when it enters that state.
    """
    if events is None:
        return None
    transitions = _transitions(events)
    if not transitions:
        return None
    visits, intact = _visits(transitions)
    complete = (
        intact and bool(visits) and final_state in _TERMINAL_STATES and visits[-1][0] == final_state
    )
    states: dict[str, dict[str, Any]] = {}
    for index, (name, entered) in enumerate(visits):
        last = index + 1 == len(visits)
        if last and complete:
            break
        seconds = None if last else (visits[index + 1][1] - entered).total_seconds()
        entry = states.setdefault(name, {"visits": 0, "seconds": 0.0})
        entry["visits"] += 1
        entry["seconds"] = (
            entry["seconds"] + seconds
            if entry["seconds"] is not None and seconds is not None
            else None
        )
    return {
        "complete": complete,
        "states": states,
        "review_wall_seconds": _total(states, REVIEW_STATE_PREFIXES) if complete else None,
        "shell_wall_seconds": _total(states, SHELL_STATE_PREFIXES) if complete else None,
    }
