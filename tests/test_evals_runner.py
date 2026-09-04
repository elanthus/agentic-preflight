"""The public regression corpus exercises the real command-review product path."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evals import run as eval_run

ROOT = Path(__file__).parent.parent
CASES = ROOT / "evals" / "cases"


def test_dry_run_scores_scripted_misses_and_false_positives(tmp_path):
    summary = eval_run.run_evaluation(
        mode="dry",
        executor=None,
        grounding=("on", "off"),
        out=tmp_path / "results",
        case_ids=("unguarded-division", "off-by-one-page"),
    )

    for setting in ("on", "off"):
        result = summary["grounding"][setting]
        assert result["catch_rate"] == 0.5
        assert result["fixed_false_positive_rate"] == 0.5
        assert result["unresolved"] == 0
        assert result["cases"]["unguarded-division"]["catch"] is True
        assert result["cases"]["unguarded-division"]["fixed_false_positive"] is True
        assert result["cases"]["off-by-one-page"]["catch"] is False


def test_summary_json_is_byte_identical_across_runs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    kwargs = {
        "mode": "dry",
        "executor": None,
        "grounding": ("on", "off"),
        "case_ids": ("unguarded-division",),
    }

    eval_run.run_evaluation(out=first, **kwargs)
    eval_run.run_evaluation(out=second, **kwargs)

    assert (first / "summary.json").read_bytes() == (second / "summary.json").read_bytes()


def test_real_mode_requires_authorization_and_reports_projected_calls(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("AP_EVAL_AUTHORIZED", raising=False)

    exit_code = eval_run.main(
        ["--mode", "real", "--executor", "codex", "--out", str(tmp_path / "results")]
    )

    captured = capsys.readouterr()
    expected_calls = len(tuple(eval_run.discover_cases())) * 2 * 2
    assert exit_code == 2
    assert f"{expected_calls} model calls" in captured.err
    assert "AP_EVAL_AUTHORIZED=1" in captured.err
    assert not (tmp_path / "results").exists()


def test_gold_leakage_inside_snapshot_is_rejected(tmp_path):
    copied = tmp_path / "unguarded-division"
    shutil.copytree(CASES / "unguarded-division", copied)
    (copied / "vulnerable" / "gold.json").write_text("planted scorer evidence\n")
    case = eval_run.load_case(copied)

    with pytest.raises(eval_run.LeakageError, match=r"gold\.json"):
        eval_run.run_case_snapshot(
            case,
            snapshot="vulnerable",
            mode="dry",
            executor=None,
            grounding="on",
            workspace=tmp_path / "workspace",
        )


def test_corpus_is_balanced_and_scripts_exercise_nontrivial_rates():
    cases = [eval_run.load_case(path) for path in eval_run.discover_cases()]
    category_counts = {
        category: sum(case.metadata["category"] == category for case in cases)
        for category in eval_run.CATEGORIES
    }
    vulnerable_misses = sum(
        not json.loads(
            (case.directory / "scripted" / "vulnerable.json").read_text(encoding="utf-8")
        )["findings"]
        for case in cases
    )
    fixed_false_positives = sum(
        bool(
            json.loads((case.directory / "scripted" / "fixed.json").read_text(encoding="utf-8"))[
                "findings"
            ]
        )
        for case in cases
    )

    assert len(cases) >= 12
    assert all(count >= 3 for count in category_counts.values())
    assert vulnerable_misses >= 2
    assert fixed_false_positives >= 3


@pytest.mark.parametrize("case_dir", tuple(eval_run.discover_cases()), ids=lambda path: path.name)
def test_eval_case_is_well_formed(case_dir):
    case = eval_run.load_case(case_dir)

    assert case.metadata["id"] == case_dir.name
    assert case.metadata["category"] in eval_run.CATEGORIES
    assert set(case.metadata["snapshots"]) == {"base", "vulnerable", "fixed"}
    for snapshot in ("base", "vulnerable", "fixed"):
        tree = case_dir / snapshot
        assert tree.is_dir()
        files = [path for path in tree.rglob("*") if path.is_file()]
        assert 3 <= len(files) <= 6
        assert not any(path.name == "gold.json" for path in files)

    gold = case.gold
    vulnerable_path = case_dir / "vulnerable" / gold["path"]
    assert vulnerable_path.is_file()
    line_count = len(vulnerable_path.read_text(encoding="utf-8").splitlines())
    assert 1 <= gold["lines"][0] <= gold["lines"][1] <= line_count
    assert gold["category"] == case.metadata["category"]
    assert gold["fixed_expectation"] == "absent"
    assert len(gold["mechanism"].splitlines()) == 1
    assert len(gold["severity"]) == 2

    for snapshot in ("vulnerable", "fixed"):
        script = json.loads(
            (case_dir / "scripted" / f"{snapshot}.json").read_text(encoding="utf-8")
        )
        eval_run.validate_script(script)
