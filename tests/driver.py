"""A scripted agent driver.

The design asks for agent scenarios to be replayed as ``(argv, expected_exit)``
scripts through two transports: ``CliRunner`` for speed, and a real
``subprocess`` because some paths (notably the pre-push hook) only exist as
subprocesses and a click-internal invocation would not exercise them.

Both transports assert the same contract on every step: **stdout is exactly one
JSON object**. That is the promise the agent relies on, so it is checked
everywhere rather than in one dedicated test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from click.testing import CliRunner

from agentic_preflight.cli import main


@dataclass
class Step:
    argv: list[str]
    expected_exit: int = 0
    envelope: dict = field(default_factory=dict)


def _parse_single_object(stdout: str, argv: list[str]) -> dict:
    stripped = stdout.strip()
    assert stripped, f"no stdout for {argv}"
    lines = stripped.splitlines()
    assert len(lines) == 1, (
        f"stdout for {argv} must be exactly one JSON object, got {len(lines)} lines:\n{stdout}"
    )
    return json.loads(lines[0])


class ScriptedAgent:
    """Runs argv scripts in a repo and returns the parsed envelopes."""

    def __init__(self, repo: Path, transport: str = "cli_runner") -> None:
        self.repo = Path(repo)
        self.transport = transport
        self.steps: list[Step] = []

    def run(self, *argv: str, expect: int = 0) -> dict:
        # Intent is a production precondition. Test scenarios use one stable,
        # explicit intent unless a test supplies its own value.
        if argv and argv[0] == "start" and "--intent" not in argv:
            argv = (*argv, "--intent", "exercise the requested behavior safely")
        if self.transport == "subprocess":
            payload, code = self._run_subprocess(list(argv))
        else:
            payload, code = self._run_click(list(argv))

        assert code == expect, (
            f"`agentic-preflight {' '.join(argv)}` exited {code}, expected {expect}; "
            f"envelope: {json.dumps(payload, indent=2)}"
        )
        self.steps.append(Step(list(argv), code, payload))
        return payload

    def script(self, steps: list[tuple[list[str], int]]) -> list[dict]:
        return [self.run(*argv, expect=code) for argv, code in steps]

    # -- transports ---------------------------------------------------------

    def _run_click(self, argv: list[str]) -> tuple[dict, int]:
        runner = CliRunner()
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            result = runner.invoke(main, argv, catch_exceptions=False)
        finally:
            os.chdir(cwd)
        return _parse_single_object(result.stdout, argv), result.exit_code

    def _run_subprocess(self, argv: list[str]) -> tuple[dict, int]:
        result = subprocess.run(
            [sys.executable, "-m", "agentic_preflight", *argv],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        return _parse_single_object(result.stdout, argv), result.returncode
