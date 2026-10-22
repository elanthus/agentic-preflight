"""Wall-clock state timing from ``state_transition`` events.

The timing is derived output with the eval's null discipline: a broken chain is
incomplete and keeps only what it can prove, and no transitions means no timing.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentic_preflight.timing import state_timing


def _transition(source: str | None, target: str, at: str | None, **extra: Any) -> dict:
    event = {"event": "state_transition", "from": source, "to": target, **extra}
    if at is not None:
        event["at"] = f"2026-10-21T10:{at}+00:00"
    return event


#: A run that restarted review once, then aborted after the shell stages.
CHAIN = [
    _transition(None, "CREATED", "00:00"),
    _transition("CREATED", "SYNC_GREEN", "00:05"),
    _transition("SYNC_GREEN", "REVIEW_AWAITING_FINDINGS", "00:05"),
    _transition("REVIEW_AWAITING_FINDINGS", "REVIEW_BLOCKED", "01:05"),
    _transition("REVIEW_BLOCKED", "REVIEW_AWAITING_FINDINGS", "02:05"),
    _transition("REVIEW_AWAITING_FINDINGS", "REVIEW_GREEN", "02:35"),
    _transition("REVIEW_GREEN", "DOCS_GREEN", "02:40"),
    _transition("DOCS_GREEN", "LINT_RUNNING", "02:45"),
    _transition("LINT_RUNNING", "LINT_GREEN", "03:00"),
    _transition("LINT_GREEN", "TEST_RUNNING", "03:00"),
    _transition("TEST_RUNNING", "TEST_GREEN", "05:00"),
    _transition("TEST_GREEN", "ABORTED", "05:30"),
]


def test_a_complete_chain_times_every_state_but_the_terminal_one():
    events = [{"event": "run_created"}, *CHAIN, {"event": "aborted", "forced": False}]

    timing = state_timing(events, "ABORTED")

    assert timing is not None
    assert timing["complete"] is True
    states = timing["states"]
    assert "ABORTED" not in states
    assert states["CREATED"] == {"visits": 1, "seconds": 5.0}
    # Entered and left within one second: zero, which is measured, not unknown.
    assert states["SYNC_GREEN"] == {"visits": 1, "seconds": 0.0}
    assert states["REVIEW_AWAITING_FINDINGS"] == {"visits": 2, "seconds": 90.0}
    assert states["TEST_GREEN"] == {"visits": 1, "seconds": 30.0}
    # Review and docs states: 90 + 60 + 5 + 5. Lint and test states: 15 + 0 + 120 + 30.
    assert timing["review_wall_seconds"] == 160.0
    assert timing["shell_wall_seconds"] == 165.0
    assert sum(entry["seconds"] for entry in states.values()) == 330.0


def test_a_broken_chain_keeps_the_visits_before_the_break():
    # The fifth transition claims to leave a state the run never entered.
    events = [*CHAIN[:4], _transition("REVIEW_GREEN", "DOCS_GREEN", "02:40"), *CHAIN[7:]]

    timing = state_timing(events, "ABORTED")

    assert timing is not None
    assert timing["complete"] is False
    assert timing["states"] == {
        "CREATED": {"visits": 1, "seconds": 5.0},
        "SYNC_GREEN": {"visits": 1, "seconds": 0.0},
        "REVIEW_AWAITING_FINDINGS": {"visits": 1, "seconds": 60.0},
        # When the run left this state is unknown.
        "REVIEW_BLOCKED": {"visits": 1, "seconds": None},
    }
    assert timing["review_wall_seconds"] is None
    assert timing["shell_wall_seconds"] is None


def test_an_unknown_visit_makes_its_state_total_unknown():
    events = [*CHAIN[:5], _transition("REVIEW_GREEN", "DOCS_GREEN", "02:40")]

    timing = state_timing(events, "ABORTED")

    assert timing is not None
    assert timing["states"]["REVIEW_AWAITING_FINDINGS"] == {"visits": 2, "seconds": None}


@pytest.mark.parametrize(
    ("events", "final_state"),
    [
        pytest.param(CHAIN[:-1], "ABORTED", id="ends-before-the-terminal-state"),
        pytest.param(CHAIN, "DONE", id="ends-in-another-terminal-state"),
        pytest.param(CHAIN[1:], "ABORTED", id="starts-after-creation"),
        pytest.param(
            [*CHAIN[:3], _transition("REVIEW_AWAITING_FINDINGS", "REVIEW_BLOCKED", "00:04")],
            "ABORTED",
            id="time-runs-backwards",
        ),
        pytest.param(
            [*CHAIN[:3], _transition("REVIEW_AWAITING_FINDINGS", "REVIEW_BLOCKED", None)],
            "ABORTED",
            id="a-transition-without-a-time",
        ),
        pytest.param(
            [*CHAIN[:3], {**CHAIN[3], "at": "2026-10-21T10:01:05"}],
            "ABORTED",
            id="a-time-without-a-zone",
        ),
        pytest.param(
            [*CHAIN[:3], {**CHAIN[3], "to": None}],
            "ABORTED",
            id="a-transition-without-a-target",
        ),
    ],
)
def test_an_incomplete_chain_is_marked_and_has_no_totals(events, final_state):
    timing = state_timing(events, final_state)

    assert timing is not None
    assert timing["complete"] is False
    assert timing["review_wall_seconds"] is None
    assert timing["shell_wall_seconds"] is None


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(None, id="no-event-log"),
        pytest.param([], id="empty-log"),
        pytest.param([{"event": "run_created"}, "not an object", 7], id="no-transitions"),
    ],
)
def test_without_transitions_there_is_no_timing(events):
    assert state_timing(events, "DONE") is None


def test_monotonic_nanoseconds_are_never_used():
    # Another boot restarts the monotonic clock; the wall-clock chain is still valid.
    events = [
        {**event, "monotonic_ns": 10**15 - index * 10**12} for index, event in enumerate(CHAIN)
    ]

    timing = state_timing(events, "ABORTED")

    assert timing is not None
    assert timing["complete"] is True
    assert timing["review_wall_seconds"] == 160.0
