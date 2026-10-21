#!/usr/bin/env python3
"""Adapt `codex exec` output to Agentic Preflight's strict review submission."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from _reviewer_common import (
    ReviewerError,
    emit_review_output,
    read_context,
    read_optional,
    report_error,
    reviewer_effort,
    reviewer_prompt,
    run_cli,
)


def main() -> int:
    try:
        context = read_context()
        executable = os.environ.get("AP_CODEX_BIN", "codex")
        # These flags were verified against Codex CLI 0.153.0. The model and
        # medium-effort default preserve a balanced example configuration.
        model = os.environ.get("AP_REVIEWER_MODEL", "gpt-5.3-codex")
        effort = reviewer_effort("medium")
        with tempfile.TemporaryDirectory(prefix="ap-codex-review-") as directory:
            output_path = Path(directory) / "final.txt"
            argv = [
                executable,
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{effort}"',
                "-C",
                str(context.get("worktree_path", ".")),
                "-o",
                str(output_path),
                "-",
            ]
            result = run_cli(argv, reviewer_prompt(context))
            emit_review_output(context, result.stdout, read_optional(output_path))
    except ReviewerError as exc:
        return report_error(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
