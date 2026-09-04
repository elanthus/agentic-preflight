"""Standard-library helpers shared by the worked reviewer wrappers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class ReviewerError(RuntimeError):
    """A reviewer failure that is safe to show on stderr."""


def read_context() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ReviewerError(f"review context is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewerError("review context must be a JSON object")
    coverage = payload.get("review_coverage")
    if not isinstance(coverage, dict) or not isinstance(coverage.get("manifest"), str):
        raise ReviewerError("review context has no review_coverage.manifest")
    return payload


def reviewer_prompt(context: dict[str, Any]) -> str:
    """Make the evidence explicit while leaving the manifest out of model control."""
    units = context.get("review_coverage", {}).get("units", [])
    grounding = context.get("grounding")
    parts = [
        "Review this change independently. Return one JSON object with only a findings array.\n",
        "Each finding must contain unit, path, optional line, severity, action, title, "
        "optional detail, and optional suggestion. Do not return coverage or a manifest.\n",
        f"Intent:\n{context.get('intent', '')}\n",
        "Changed files:\n" + json.dumps(context.get("changed_files", []), indent=2) + "\n",
        "Review units:\n" + json.dumps(units, indent=2) + "\n",
    ]
    if grounding is not None:
        parts.append("Grounding:\n" + json.dumps(grounding, indent=2) + "\n")
    parts.append("Diff:\n" + str(context.get("diff", "")))
    return "\n".join(parts)


def timeout_seconds() -> int:
    raw = os.environ.get("AP_REVIEWER_TIMEOUT", "600")
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise ReviewerError(f"AP_REVIEWER_TIMEOUT must be an integer, got {raw!r}") from exc
    if timeout < 1:
        raise ReviewerError("AP_REVIEWER_TIMEOUT must be at least 1 second")
    return timeout


def run_cli(argv: list[str], prompt: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReviewerError(f"reviewer CLI not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewerError(
            f"reviewer CLI timed out after {timeout_seconds()} seconds: {argv[0]}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ReviewerError(f"reviewer CLI exited {result.returncode}: {detail}")
    return result


def _last_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, length = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((index + length, -index, value))
    return max(candidates, default=(0, 0, None))[2]


def extract_findings(*outputs: str) -> list[Any]:
    """Use the last model object, including one wrapped by Claude's JSON envelope."""
    candidate = next(
        (
            parsed
            for output in reversed(outputs)
            if (parsed := _last_json_object(output)) is not None
        ),
        None,
    )
    if candidate is not None and isinstance(candidate.get("result"), str):
        candidate = _last_json_object(candidate["result"])
    if candidate is not None and isinstance(candidate.get("findings"), list):
        return candidate["findings"]
    raise ReviewerError("reviewer CLI returned no JSON object containing findings")


def emit_submission(context: dict[str, Any], findings: list[Any]) -> None:
    submission = {
        "coverage": {
            "manifest": context["review_coverage"]["manifest"],
            "examined": "all",
        },
        "findings": findings,
    }
    json.dump(submission, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def report_error(exc: ReviewerError) -> int:
    print(f"independent reviewer failed: {exc}", file=sys.stderr)
    return 1


def read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
