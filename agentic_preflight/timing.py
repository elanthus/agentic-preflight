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

The tool also times each external command it runs on its own (the names in
:data:`TIMED_COMMANDS`: sync, setup, the review command, the contract planner,
the ``review compare`` shadow reviewer, the baseline check, lint, test, and
merge-back). :func:`timed_command` brackets
one with a ``tool_command_started`` and a ``tool_command_finished`` event; the
second carries ``seconds`` from the monotonic clock, which is valid here because
both ends are read in one process. The sum is ``tool_seconds``:

* ``None`` for a run whose ``run_created`` event lacks the timing marker, because
  an earlier version did not time its commands.
* ``None`` when a start has no finish (the process stopped mid-command) or a
  finish has no valid ``seconds``.
* ``agent_seconds`` is the rest of the run's wall time, only when the chain is
  complete. Transition times are whole seconds, so it is never below zero.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .machine import TERMINAL_STATES

if TYPE_CHECKING:
    from .store import Store

#: States of the stages the agent judges: review, and the docs stage after it.
REVIEW_STATE_PREFIXES = ("REVIEW_", "DOCS_")
#: States of the deterministic shell stages.
SHELL_STATE_PREFIXES = ("LINT_", "TEST_")
_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_STATES)

#: Appended before an external command the tool runs on its own.
COMMAND_STARTED_EVENT = "tool_command_started"
#: Appended after it, with the command's wall time in ``seconds``.
COMMAND_FINISHED_EVENT = "tool_command_finished"
#: Key on the ``run_created`` event of a run whose commands are timed.
COMMAND_TIMING_MARKER = "command_timing"
COMMAND_TIMING_VERSION = 1
#: The commands that are timed, as the ``command`` value of both events.
TIMED_COMMANDS = frozenset(
    {
        "sync",
        "setup",
        "review",
        "contracts",
        "review_compare",
        "baseline",
        "lint",
        "test",
        "mergeback",
    }
)


def _append_quietly(store: Store, run_id: str, event: dict[str, Any]) -> None:
    """Append a timing event. A failure loses the time, never the command."""
    # Timing is telemetry: an unwritable event log must never fail the command.
    with suppress(Exception):
        store.append_event(run_id, event)


@contextmanager
def timed_command(store: Store, run_id: str, command: str) -> Iterator[None]:
    """Record the wall time of one external command in the run's event log.

    The finish event is written even when the command raises, so only a process
    that stops mid-command leaves a start without a finish. A name outside
    :data:`TIMED_COMMANDS` is not timed: telemetry never fails the real command.
    """
    if command not in TIMED_COMMANDS:
        yield
        return
    _append_quietly(store, run_id, {"event": COMMAND_STARTED_EVENT, "command": command})
    started = time.monotonic()
    try:
        yield
    finally:
        seconds = round(time.monotonic() - started, 3)
        _append_quietly(
            store,
            run_id,
            {"event": COMMAND_FINISHED_EVENT, "command": command, "seconds": seconds},
        )


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


def _seconds(value: Any) -> float | None:
    # json.loads accepts NaN and Infinity, which are not times.
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


def tool_seconds(events: Iterable[Any]) -> float | None:
    """Total seconds of the run's timed commands, or ``None`` when any is unknown.

    Only events before the first transition into a terminal state count. A
    command that runs after the run ended, such as ``review compare`` on a
    ``DONE`` run, is not time the tool added to that run, and leaving it out
    keeps a rebuilt summary equal to the one written when the run ended.
    """
    items = [event for event in events if isinstance(event, Mapping)]
    for index, event in enumerate(items):
        target = event.get("to")
        if (
            event.get("event") == "state_transition"
            and isinstance(target, str)
            and target in _TERMINAL_STATES
        ):
            items = items[:index]
            break
    marked = any(
        event.get("event") == "run_created"
        and event.get(COMMAND_TIMING_MARKER) == COMMAND_TIMING_VERSION
        for event in items
    )
    if not marked:
        return None
    started: Counter[Any] = Counter()
    finished: Counter[Any] = Counter()
    total = 0.0
    for event in items:
        name = event.get("event")
        # A malformed command name is still paired, as an unnamed command.
        command = event.get("command") if isinstance(event.get("command"), str) else None
        if name == COMMAND_STARTED_EVENT:
            started[command] += 1
        elif name == COMMAND_FINISHED_EVENT:
            seconds = _seconds(event.get("seconds"))
            if seconds is None:
                return None
            finished[command] += 1
            total += seconds
    if started != finished:
        return None
    return round(total, 3)


def _agent_seconds(states: Mapping[str, Mapping[str, Any]], tool: float | None) -> float | None:
    if tool is None:
        return None
    wall = 0.0
    for entry in states.values():
        if entry["seconds"] is None:
            return None
        wall += entry["seconds"]
    # Transition times are whole seconds, so a short run can measure less wall
    # time than its commands took.
    return round(max(0.0, wall - tool), 3)


def state_timing(events: Iterable[Any] | None, final_state: str | None) -> dict[str, Any] | None:
    """Visits and seconds per state for one run, or ``None`` without transitions.

    ``final_state`` is the run's terminal state, as the summary reports it. The
    terminal visit itself is not listed: the run ends when it enters that state.
    """
    if events is None:
        return None
    events = list(events)
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
        "tool_seconds": (tool := tool_seconds(events)),
        "agent_seconds": _agent_seconds(states, tool) if complete else None,
    }
