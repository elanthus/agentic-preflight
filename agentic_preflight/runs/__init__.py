"""Run orchestration: the logic behind each command.

``cli.py`` is argument parsing and envelope emission only. The focused modules
behind this facade each own one phase of the run state machine; entry points take
a :class:`Session` and return an :class:`Envelope` without printing or exiting.
"""

from ._session import STATE_DIR_NAME, Session, open_session
from .lifecycle import abort, events, gc, status
from .mergeback import mergeback
from .publish import cleanup, finish, gate, monitor_ci, pull_request, push
from .resolve import RESPONSE_ACTIONS, respond
from .review import context, submit_findings, verify
from .stages import logs, run_stage
from .start import start

__all__ = [
    "RESPONSE_ACTIONS",
    "STATE_DIR_NAME",
    "Session",
    "abort",
    "cleanup",
    "context",
    "events",
    "finish",
    "gate",
    "gc",
    "logs",
    "mergeback",
    "monitor_ci",
    "open_session",
    "pull_request",
    "push",
    "respond",
    "run_stage",
    "start",
    "status",
    "submit_findings",
    "verify",
]
