#!/usr/bin/env python3
"""Adapt `claude -p` output to Agentic Preflight's strict review submission."""

from __future__ import annotations

import os

from _reviewer_common import (
    ReviewerError,
    emit_review_output,
    read_context,
    report_error,
    reviewer_effort,
    reviewer_prompt,
    run_cli,
)


def main() -> int:
    try:
        context = read_context()
        executable = os.environ.get("AP_CLAUDE_BIN", "claude")
        # These flags were verified against Claude Code 2.1.236.
        model = os.environ.get("AP_REVIEWER_MODEL", "claude-sonnet-5")
        effort = reviewer_effort("high")
        result = run_cli(
            [
                executable,
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "plan",
                "--model",
                model,
                "--effort",
                effort,
            ],
            reviewer_prompt(context),
        )
        emit_review_output(context, result.stdout)
    except ReviewerError as exc:
        return report_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
