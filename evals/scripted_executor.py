#!/usr/bin/env python3
"""Emit a deterministic strict review submission for the public smoke corpus."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _unit_for(finding: dict[str, Any], units: list[dict[str, Any]]) -> str:
    path = finding.get("path")
    line = finding.get("line")
    path_units = [unit for unit in units if unit.get("path") == path]
    if isinstance(line, int):
        for unit in path_units:
            start = unit.get("new_start")
            count = unit.get("new_count")
            if isinstance(start, int) and isinstance(count, int):
                last = start + max(count, 1) - 1
                if start <= line <= last:
                    return str(unit["id"])
        raise ValueError(f"scripted finding line is not in a review unit: {path}:{line}")
    if path_units:
        return str(path_units[0]["id"])
    raise ValueError(f"scripted finding path is not in review coverage: {path!r}")


def main() -> int:
    script_path = os.environ.get("AP_EVAL_SCRIPT")
    if not script_path:
        print("AP_EVAL_SCRIPT is required", file=sys.stderr)
        return 2
    try:
        bundle = json.load(sys.stdin)
        scripted = json.loads(Path(script_path).read_text(encoding="utf-8"))
        coverage = bundle["review_coverage"]
        findings = []
        for raw in scripted["findings"]:
            finding = dict(raw)
            finding["unit"] = _unit_for(finding, coverage["units"])
            findings.append(finding)
        submission = {
            "coverage": {"manifest": coverage["manifest"], "examined": "all"},
            "findings": findings,
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"scripted evaluation failed: {exc}", file=sys.stderr)
        return 1
    json.dump(submission, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
