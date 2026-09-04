#!/usr/bin/env python3
"""Adapt `codex exec` output to Agentic Preflight's strict review submission."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from _reviewer_common import (
    ReviewerError,
    emit_submission,
    extract_findings,
    read_context,
    read_optional,
    report_error,
    reviewer_prompt,
    run_cli,
)


def main() -> int:
    try:
        context = read_context()
        executable = os.environ.get("AP_CODEX_BIN", "codex")
        model = os.environ.get("AP_REVIEWER_MODEL", "gpt-5.3-codex")
        with tempfile.TemporaryDirectory(prefix="ap-codex-review-") as directory:
            output_path = Path(directory) / "final.txt"
            argv = [
                executable,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "-C",
                str(context.get("worktree_path", ".")),
                "-o",
                str(output_path),
                "-",
            ]
            result = run_cli(argv, reviewer_prompt(context))
            findings = extract_findings(result.stdout, read_optional(output_path))
        emit_submission(context, findings)
    except ReviewerError as exc:
        return report_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
