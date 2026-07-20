"""The stdout contract.

Every command prints **exactly one JSON object** to stdout. Human prose goes to
stderr. The agent must be able to ``json.loads(stdout)`` without first checking
whether the run went well, so every key is always present — an envelope with
nothing to say still carries ``data: {}`` and ``blocking: []`` rather than
omitting them and forcing defensive parsing on the other side.

``next`` is the anti-wandering device: after any command the agent is told the
single next legal command. It is ``null`` only when there is genuinely nothing
left to do.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, TextIO


class ExitCode(IntEnum):
    OK = 0
    USAGE = 1
    STAGE_FAILED = 2
    PRECONDITION = 3
    NEEDS_HUMAN = 4
    NEEDS_CONFIRM = 5
    HOOK_BLOCK = 10


@dataclass
class Envelope:
    ok: bool = True
    run_id: str | None = None
    state: str | None = None
    stage: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    blocking: list[Any] = field(default_factory=list)
    next_instruction: str | None = None
    next_command: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_detail: dict[str, Any] | None = None

    #: Prose for a person. Deliberately *not* part of the JSON payload — it is
    #: written to stderr so the machine-readable stream stays machine-readable.
    human: str | None = None

    def to_payload(self) -> dict[str, Any]:
        next_block = None
        if self.next_instruction or self.next_command:
            next_block = {
                "instruction": self.next_instruction,
                "command": self.next_command,
            }

        error_block = None
        if self.error_code:
            error_block = {
                "code": self.error_code,
                "message": self.error_message or "",
            }
            if self.error_detail:
                error_block["detail"] = self.error_detail

        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "state": self.state,
            "stage": self.stage,
            "data": self.data,
            "blocking": self.blocking,
            "next": next_block,
            "error": error_block,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True)


def error_envelope(
    *,
    code: str,
    message: str,
    state: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
    detail: dict[str, Any] | None = None,
    next_instruction: str | None = None,
    next_command: str | None = None,
    data: dict[str, Any] | None = None,
) -> Envelope:
    return Envelope(
        ok=False,
        run_id=run_id,
        state=state,
        stage=stage,
        data=data or {},
        error_code=code,
        error_message=message,
        error_detail=detail,
        next_instruction=next_instruction,
        next_command=next_command,
    )


def emit(
    envelope: Envelope,
    *,
    stream: TextIO | None = None,
    err_stream: TextIO | None = None,
) -> None:
    """Write the envelope as one line of JSON, and any prose to stderr."""
    stream = stream if stream is not None else sys.stdout
    stream.write(envelope.to_json() + "\n")
    stream.flush()

    if envelope.human:
        err = err_stream if err_stream is not None else sys.stderr
        err.write(envelope.human.rstrip("\n") + "\n")
        err.flush()
