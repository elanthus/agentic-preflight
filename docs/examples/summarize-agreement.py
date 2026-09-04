#!/usr/bin/env python3
"""Aggregate saved independent-review agreement reports into one table."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def report_paths(argv: list[str]) -> list[Path]:
    root = (
        Path(argv[0]).expanduser()
        if argv
        else Path.home() / ".local" / "share" / "agentic-preflight" / "agreement"
    )
    return sorted(root.glob("*.json"))


def load_reports(paths: list[Path]) -> list[dict[str, Any]]:
    reports = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("findings"), dict):
            print(f"skipping {path}: not an agreement report", file=sys.stderr)
            continue
        reports.append(payload)
    return reports


def main(argv: list[str]) -> int:
    reports = load_reports(report_paths(argv))
    rates = [
        report["agreement_rate"] for report in reports if report.get("agreement_rate") is not None
    ]
    mean = sum(rates) / len(rates) if rates else None
    only_a = sum(len(report["findings"].get("only_a", [])) for report in reports)
    only_b = sum(len(report["findings"].get("only_b", [])) for report in reports)
    disagreements = sum(
        len(report["findings"].get("severity_disagreements", [])) for report in reports
    )
    print("runs  mean_agreement_rate  only_in_harness  only_command  severity_disagreements")
    formatted_mean = f"{mean:.3f}" if mean is not None else "n/a"
    print(
        f"{len(reports):>4}  {formatted_mean:>19}  {only_a:>15}  {only_b:>12}  {disagreements:>22}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
