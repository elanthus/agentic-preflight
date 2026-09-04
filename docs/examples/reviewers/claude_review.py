#!/usr/bin/env python3
"""Adapt `claude -p` output to Agentic Preflight's strict review submission."""

from __future__ import annotations

import os

from _reviewer_common import (
    ReviewerError,
    emit_submission,
    extract_findings,
    read_context,
    report_error,
    reviewer_prompt,
    run_cli,
)


def main() -> int:
    try:
        context = read_context()
        executable = os.environ.get("AP_CLAUDE_BIN", "claude")
        model = os.environ.get("AP_REVIEWER_MODEL", "sonnet")
        result = run_cli(
            [
                executable,
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "plan",
                "--model",
                model,
            ],
            reviewer_prompt(context),
        )
        findings = extract_findings(result.stdout)
        emit_submission(context, findings)
    except ReviewerError as exc:
        return report_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
