#!/usr/bin/env python3
"""Run the public synthetic regression corpus through the product CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_preflight.models import FindingSubmission

ROOT = Path(__file__).resolve().parent.parent
CASES_ROOT = ROOT / "evals" / "cases"
EXAMPLES_ROOT = ROOT / "docs" / "examples"
CATEGORIES = {
    "correctness",
    "security",
    "evaluation_integrity",
    "documentation_contract",
}
SNAPSHOTS = ("base", "vulnerable", "fixed")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
CATEGORY_WORDS = {
    "correctness": ("bug", "boundary", "crash", "division", "incorrect", "off-by-one"),
    "security": ("credential", "injection", "path", "secret", "security", "traversal"),
    "evaluation_integrity": ("assert", "coverage", "evaluation", "fixture", "golden", "test"),
    "documentation_contract": (
        "changelog",
        "contract",
        "default",
        "documentation",
        "flag",
        "readme",
    ),
}
DETERMINISTIC_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Evaluation Author",
    "GIT_AUTHOR_EMAIL": "evaluation@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Evaluation Committer",
    "GIT_COMMITTER_EMAIL": "evaluation@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


class EvaluationError(RuntimeError):
    """An evaluation failure with a concise command-line diagnostic."""


class LeakageError(EvaluationError):
    """Scorer-only evidence would have entered a reviewer-visible tree or bundle."""


@dataclass(frozen=True)
class EvalCase:
    directory: Path
    metadata: dict[str, Any]
    gold: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.metadata["id"])


def discover_cases(root: Path = CASES_ROOT) -> Iterable[Path]:
    return (
        path
        for path in sorted(root.iterdir())
        if path.is_dir() and not path.name.startswith(".") and (path / "case.json").is_file()
    )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return value


def validate_script(script: dict[str, Any]) -> None:
    findings = script.get("findings")
    if not isinstance(findings, list):
        raise EvaluationError("a scripted submission must contain a findings list")
    for finding in findings:
        if not isinstance(finding, dict):
            raise EvaluationError("each scripted finding must be an object")
        FindingSubmission.model_validate(finding)
        if "unit" in finding:
            raise EvaluationError("scripted units are assigned from the delivered bundle")


def load_case(directory: Path) -> EvalCase:
    metadata = _load_object(directory / "case.json")
    gold = _load_object(directory / "gold.json")
    required = {"id", "category", "title", "intent", "snapshots"}
    if set(metadata) != required:
        raise EvaluationError(f"{directory / 'case.json'} has the wrong fields")
    if metadata["id"] != directory.name:
        raise EvaluationError(f"case id {metadata['id']!r} does not match {directory.name!r}")
    if metadata["category"] not in CATEGORIES:
        raise EvaluationError(f"unknown category {metadata['category']!r}")
    if metadata["snapshots"] != {name: name for name in SNAPSHOTS}:
        raise EvaluationError("snapshots must map base, vulnerable, and fixed to matching trees")
    if not isinstance(metadata["intent"], str) or not metadata["intent"].strip():
        raise EvaluationError("case intent must be non-empty")
    if gold.get("category") != metadata["category"]:
        raise EvaluationError("gold category must match case category")
    if gold.get("fixed_expectation") != "absent":
        raise EvaluationError("fixed_expectation must be absent")
    severity = gold.get("severity")
    if (
        not isinstance(severity, list)
        or len(severity) != 2
        or any(item not in SEVERITY_ORDER for item in severity)
        or SEVERITY_ORDER[severity[0]] > SEVERITY_ORDER[severity[1]]
    ):
        raise EvaluationError("gold severity must be an ordered [min, max] range")
    lines = gold.get("lines")
    if (
        not isinstance(lines, list)
        or len(lines) != 2
        or not all(isinstance(item, int) for item in lines)
        or lines[0] < 1
        or lines[0] > lines[1]
    ):
        raise EvaluationError("gold lines must be an ordered positive [start, end] range")
    for name in SNAPSHOTS:
        tree = directory / name
        if not tree.is_dir():
            raise EvaluationError(f"missing snapshot tree: {tree}")
    target = directory / "vulnerable" / str(gold.get("path", ""))
    if not target.is_file():
        raise EvaluationError(f"gold path does not exist in vulnerable snapshot: {target}")
    if lines[1] > len(target.read_text(encoding="utf-8").splitlines()):
        raise EvaluationError(f"gold line range exceeds {target}")
    for name in ("vulnerable", "fixed"):
        validate_script(_load_object(directory / "scripted" / f"{name}.json"))
    return EvalCase(directory=directory, metadata=metadata, gold=gold)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = _run(["git", *args], cwd=repo, env=env)
    if result.returncode != 0:
        raise EvaluationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _copy_snapshot(source: Path, repo: Path) -> None:
    leaked = [path for path in source.rglob("*") if path.is_file() and path.name == "gold.json"]
    if leaked:
        raise LeakageError(f"snapshot contains scorer-only gold.json: {leaked[0]}")
    for path in list(repo.iterdir()):
        if path.name not in {".git", ".agentic-preflight.toml"}:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    shutil.copytree(source, repo, dirs_exist_ok=True, copy_function=shutil.copyfile)


def _config_text(*, mode: str, executor: str | None, grounding: str) -> str:
    if mode == "dry":
        prefix = """\
[general]
base_ref = "main"

[commands]
lint = "true"
test = "true"

[stage]
timeout_seconds = 660
max_attempts = 2

[review]
blocking_severities = ["critical", "high"]
max_findings = 50
require_fix_commits = true
executor = "command"
command = 'python3 -B "$AP_EVAL_EXECUTOR"'
require_command_for = []

[docs]
enabled = false
"""
    else:
        if executor not in {"codex", "claude"}:
            raise EvaluationError("real mode requires --executor codex or claude")
        prefix = (EXAMPLES_ROOT / f"{executor}-reviewer.toml").read_text(encoding="utf-8")
        prefix = re.sub(r'^lint = ".*"$', 'lint = "true"', prefix, flags=re.MULTILINE)
        prefix = re.sub(r'^test = ".*"$', 'test = "true"', prefix, flags=re.MULTILINE)
        prefix = re.sub(
            r'^command = ".*"$',
            "command = 'python3 -B \"$AP_EVAL_EXECUTOR\"'",
            prefix,
            flags=re.MULTILINE,
        )
        prefix = re.sub(r"(?m)^enabled = true$", "enabled = false", prefix, count=1)
    return prefix.rstrip() + f"\n\n[context]\nenabled = {str(grounding == 'on').lower()}\n"


def _commit(repo: Path, env: dict[str, str], message: str) -> str:
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", message)
    return _git(repo, env, "rev-parse", "HEAD")


def _build_repo(
    case: EvalCase,
    destination: Path,
    *,
    snapshot: str,
    mode: str,
    executor: str | None,
    grounding: str,
) -> tuple[dict[str, str], dict[str, str]]:
    if snapshot not in {"vulnerable", "fixed"}:
        raise EvaluationError("only vulnerable and fixed snapshots are reviewed")
    destination.mkdir(parents=True)
    env = {**os.environ, **DETERMINISTIC_GIT_ENV}
    _git(destination, env, "init", "-q", "--initial-branch", "main")
    _copy_snapshot(case.directory / "base", destination)
    (destination / ".agentic-preflight.toml").write_text(
        _config_text(mode=mode, executor=executor, grounding=grounding), encoding="utf-8"
    )
    base = _commit(destination, env, "Initial snapshot")
    _git(destination, env, "switch", "-q", "-c", "review/change")
    # Never put the unselected snapshot in refs, reflogs, or the object database.
    _copy_snapshot(case.directory / snapshot, destination)
    selected = _commit(destination, env, "Proposed change")
    return {"base": base, snapshot: selected}, env


def _cli(repo: Path, env: dict[str, str], *argv: str) -> tuple[dict[str, Any], int]:
    result = _run([sys.executable, "-m", "agentic_preflight", *argv], cwd=repo, env=env)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            f"agentic-preflight {' '.join(argv)} returned invalid JSON: {result.stdout!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvaluationError(f"agentic-preflight {' '.join(argv)} returned a non-object")
    return payload, result.returncode


def _require_cli_ok(command: str, payload: dict[str, Any], code: int) -> None:
    if code != 0:
        message = payload.get("error", {}).get("message", "unknown failure")
        raise EvaluationError(f"{command} exited {code}: {message}")


def _assert_bundle_is_clean(case: EvalCase, context: dict[str, Any]) -> None:
    data = context.get("data", {})
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if case.id in serialized:
        raise LeakageError("review bundle contains the case identity")
    if any(key in data for key in ("case_id", "snapshot", "gold", "expected_findings")):
        raise LeakageError("review bundle contains scorer-only metadata")
    if "gold.json" in serialized:
        raise LeakageError("review bundle contains gold.json text")
    serialized_gold = json.dumps(case.gold, sort_keys=True, separators=(",", ":"))
    escaped_gold = json.dumps(serialized_gold)[1:-1]
    if serialized_gold in serialized or escaped_gold in serialized:
        raise LeakageError("review bundle contains the serialized gold record")
    if data.get("intent") != case.metadata["intent"]:
        raise LeakageError("review bundle intent differs from case.json intent")


def _recorded_submission(repo: Path, run_id: str) -> dict[str, Any]:
    git_dir_text = _git(
        repo, {**os.environ, **DETERMINISTIC_GIT_ENV}, "rev-parse", "--git-common-dir"
    )
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    return _load_object(
        git_dir / "agentic-preflight" / "runs" / run_id / "review-submission-command.json"
    )


def _unit_overlap(unit: dict[str, Any], gold_lines: list[int]) -> bool:
    start = unit.get("new_start")
    count = unit.get("new_count")
    if not isinstance(start, int) or not isinstance(count, int):
        return False
    end = start + max(count, 1) - 1
    return start <= gold_lines[1] and end >= gold_lines[0]


def _matching_findings(
    findings: list[dict[str, Any]], units: list[dict[str, Any]], gold: dict[str, Any]
) -> list[dict[str, Any]]:
    by_id = {unit["id"]: unit for unit in units}
    matches = []
    for finding in findings:
        if finding.get("path") != gold["path"]:
            continue
        line = finding.get("line")
        line_matches = isinstance(line, int) and gold["lines"][0] <= line <= gold["lines"][1]
        unit = by_id.get(finding.get("unit"))
        if line_matches or (isinstance(unit, dict) and _unit_overlap(unit, gold["lines"])):
            matches.append(finding)
    return matches


def _severity_agrees(finding: dict[str, Any], gold: dict[str, Any]) -> bool:
    value = SEVERITY_ORDER[str(finding["severity"])]
    return SEVERITY_ORDER[gold["severity"][0]] <= value <= SEVERITY_ORDER[gold["severity"][1]]


def _category_agrees(finding: dict[str, Any], category: str) -> bool:
    text = f"{finding.get('title', '')} {finding.get('detail', '')}".lower()
    return any(word in text for word in CATEGORY_WORDS[category])


def run_case_snapshot(
    case: EvalCase,
    *,
    snapshot: str,
    mode: str,
    executor: str | None,
    grounding: str,
    workspace: Path,
) -> dict[str, Any]:
    if snapshot not in {"vulnerable", "fixed"}:
        raise EvaluationError("only vulnerable and fixed snapshots are reviewed")
    repo = workspace / f"review-{uuid.uuid4().hex}"
    shas, env = _build_repo(
        case, repo, snapshot=snapshot, mode=mode, executor=executor, grounding=grounding
    )
    env["AP_EVAL_EXECUTOR"] = str(
        ROOT / "evals" / "scripted_executor.py"
        if mode == "dry"
        else EXAMPLES_ROOT / "reviewers" / f"{executor}_review.py"
    )
    env.pop("AP_EVAL_SCRIPT", None)
    if mode == "dry":
        env["AP_EVAL_SCRIPT"] = str(case.directory / "scripted" / f"{snapshot}.json")

    init, code = _cli(repo, env, "init", "--no-hook")
    _require_cli_ok("init --no-hook", init, code)
    started, code = _cli(repo, env, "start", "--intent", str(case.metadata["intent"]))
    _require_cli_ok("start", started, code)
    context, code = _cli(repo, env, "context")
    _require_cli_ok("context", context, code)
    _assert_bundle_is_clean(case, context)
    reviewed, code = _cli(repo, env, "review", "run")
    if code != 0:
        return {
            "status": "unresolved",
            "exit_code": code,
            "base_sha": shas["base"],
            "head_sha": shas[snapshot],
            "finding_count": 0,
            "matched": None,
            "severity_agreement": None,
            "category_agreement": None,
        }
    run_id = str(reviewed["run_id"])
    submission = _recorded_submission(repo, run_id)
    findings = submission["findings"]
    units = context["data"]["review_coverage"]["units"]
    matches = _matching_findings(findings, units, case.gold)
    first = matches[0] if matches else None
    return {
        "status": "resolved",
        "exit_code": 0,
        "base_sha": shas["base"],
        "head_sha": shas[snapshot],
        "finding_count": len(findings),
        "matched": bool(matches),
        "severity_agreement": _severity_agrees(first, case.gold) if first else None,
        "category_agreement": (
            _category_agrees(first, str(case.metadata["category"])) if first else None
        ),
    }


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _aggregate(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    vulnerable = [result["vulnerable"] for result in cases.values()]
    fixed = [result["fixed"] for result in cases.values()]
    catches = [bool(item["matched"]) for item in vulnerable if item["status"] == "resolved"]
    false_positives = [bool(item["matched"]) for item in fixed if item["status"] == "resolved"]
    severity = [item["severity_agreement"] for item in vulnerable if item["matched"]]
    category = [item["category_agreement"] for item in vulnerable if item["matched"]]
    return {
        "catch_rate": _rate(catches),
        "fixed_false_positive_rate": _rate(false_positives),
        "unresolved": sum(item["status"] == "unresolved" for item in vulnerable + fixed),
        "severity_agreement": _rate([bool(item) for item in severity]),
        "category_agreement": _rate([bool(item) for item in category]),
    }


def _case_summary(vulnerable: dict[str, Any], fixed: dict[str, Any]) -> dict[str, Any]:
    return {
        "catch": vulnerable["matched"],
        "fixed_false_positive": fixed["matched"],
        "severity_agreement": vulnerable["severity_agreement"],
        "category_agreement": vulnerable["category_agreement"],
        "vulnerable": vulnerable,
        "fixed": fixed,
    }


def _markdown(summary: dict[str, Any]) -> str:
    settings = list(summary["grounding"])
    executor = summary["executor"]
    headers = ["Case"]
    for setting in settings:
        headers.extend(
            [
                f"{setting}/{executor} catch",
                f"{setting}/{executor} fixed-FP",
                f"{setting}/{executor} severity",
                f"{setting}/{executor} category",
            ]
        )
    lines = ["# Regression eval summary", "", "| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for case_id in summary["cases"]:
        row = [case_id]
        for setting in settings:
            result = summary["grounding"][setting]["cases"][case_id]
            row.extend(
                str(result[key])
                for key in (
                    "catch",
                    "fixed_false_positive",
                    "severity_agreement",
                    "category_agreement",
                )
            )
        lines.append("| " + " | ".join(row) + " |")
    row = ["**Aggregate**"]
    for setting in settings:
        result = summary["grounding"][setting]
        row.extend(
            str(result[key])
            for key in (
                "catch_rate",
                "fixed_false_positive_rate",
                "severity_agreement",
                "category_agreement",
            )
        )
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(
        f"Unresolved snapshots: {summary['unresolved']}. Model calls: {summary['model_calls']}."
    )
    lines.append("")
    return "\n".join(lines)


def run_evaluation(
    *,
    mode: str,
    executor: str | None,
    grounding: Sequence[str],
    out: Path,
    case_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    all_cases = {path.name: load_case(path) for path in discover_cases()}
    selected_ids = tuple(case_ids) if case_ids is not None else tuple(all_cases)
    unknown = sorted(set(selected_ids) - set(all_cases))
    if unknown:
        raise EvaluationError(f"unknown cases: {', '.join(unknown)}")
    settings = tuple(grounding)
    if not settings or any(item not in {"on", "off"} for item in settings):
        raise EvaluationError("grounding must contain on, off, or both")
    if mode == "real" and os.environ.get("AP_EVAL_AUTHORIZED") != "1":
        calls = len(selected_ids) * 2 * len(settings)
        raise EvaluationError(
            f"real mode would make {calls} model calls; set AP_EVAL_AUTHORIZED=1 to authorize"
        )
    if mode not in {"dry", "real"}:
        raise EvaluationError("mode must be dry or real")

    effective_executor = "scripted" if mode == "dry" else str(executor)
    grounding_results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="agentic-preflight-eval-") as temp:
        workspace = Path(temp)
        for setting in settings:
            per_case = {}
            for case_id in selected_ids:
                case = all_cases[case_id]
                vulnerable = run_case_snapshot(
                    case,
                    snapshot="vulnerable",
                    mode=mode,
                    executor=executor,
                    grounding=setting,
                    workspace=workspace,
                )
                fixed = run_case_snapshot(
                    case,
                    snapshot="fixed",
                    mode=mode,
                    executor=executor,
                    grounding=setting,
                    workspace=workspace,
                )
                per_case[case_id] = _case_summary(vulnerable, fixed)
            grounding_results[setting] = {"executor": effective_executor, "cases": per_case}
            grounding_results[setting].update(_aggregate(per_case))

    aggregates = {
        key: _rate(
            [
                bool(result[key])
                for setting in grounding_results.values()
                for result in setting["cases"].values()
                if result[key] is not None
            ]
        )
        for key in ("catch", "fixed_false_positive", "severity_agreement", "category_agreement")
    }
    summary = {
        "method_version": "public-smoke-v2",
        "mode": mode,
        "executor": effective_executor,
        "cases": list(selected_ids),
        "grounding": grounding_results,
        "catch_rate": aggregates["catch"],
        "fixed_false_positive_rate": aggregates["fixed_false_positive"],
        "unresolved": sum(item["unresolved"] for item in grounding_results.values()),
        "severity_agreement": aggregates["severity_agreement"],
        "category_agreement": aggregates["category_agreement"],
        "model_calls": len(selected_ids) * 2 * len(settings) if mode == "real" else 0,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "summary.md").write_text(_markdown(summary), encoding="utf-8", newline="\n")
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry", "real"), required=True)
    parser.add_argument("--executor", choices=("codex", "claude"))
    parser.add_argument("--grounding", choices=("on", "off", "both"), default="both")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = ("on", "off") if args.grounding == "both" else (args.grounding,)
    try:
        run_evaluation(
            mode=args.mode,
            executor=args.executor,
            grounding=settings,
            out=args.out,
        )
    except EvaluationError as exc:
        print(f"regression eval refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
