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
from pathlib import Path

from click.testing import CliRunner

from agentic_preflight.cli import main


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
        self.review_manifest: str | None = None

    def run(self, *argv: str, expect: int = 0) -> dict:
        # Intent is a production precondition. Test scenarios use one stable,
        # explicit intent unless a test supplies its own value.
        if argv and argv[0] == "start" and "--intent" not in argv:
            argv = (*argv, "--intent", "exercise the requested behavior safely")
        self._materialize_review_coverage(argv)
        if self.transport == "subprocess":
            payload, code = self._run_subprocess(list(argv))
        else:
            payload, code = self._run_click(list(argv))

        assert code == expect, (
            f"`agentic-preflight {' '.join(argv)}` exited {code}, expected {expect}; "
            f"envelope: {json.dumps(payload, indent=2)}"
        )
        coverage = payload.get("data", {}).get("review_coverage", {})
        if isinstance(coverage, dict) and isinstance(coverage.get("manifest"), str):
            self.review_manifest = coverage["manifest"]
        return payload

    def _materialize_review_coverage(self, argv: tuple[str, ...]) -> None:
        """Replace the test fixture's context sentinel with the delivered manifest."""
        if not argv or argv[0] != "submit-findings" or "--file" not in argv:
            return
        file_path = argv[argv.index("--file") + 1]
        if file_path == "-":
            return
        path = Path(file_path)
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        coverage = payload.get("coverage") if isinstance(payload, dict) else None
        if not isinstance(coverage, dict) or coverage.get("manifest") != "$context":
            return
        if self.review_manifest is None:
            return
        coverage["manifest"] = self.review_manifest
        path.write_text(json.dumps(payload))

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


def findings_json(tmp_path: Path, items: list) -> str:
    """Write review findings with coverage supplied by the next context delivery."""
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": items}),
        encoding="utf-8",
    )
    return str(path)


def green_run(repo: Path, tmp_path: Path) -> ScriptedAgent:
    """Drive a full run to a green attestation note."""
    from tests.conftest import commit_all, write

    write(
        repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(repo, "configure agentic-preflight")
    agent = ScriptedAgent(repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    return agent
