"""The public regression corpus exercises the real command-review product path."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
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

    assert summary["method_version"] == "public-smoke-v2"
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


def test_build_repo_copies_changed_same_size_file_with_fresh_mtime(tmp_path):
    copied = tmp_path / "wrong-config-default"
    shutil.copytree(CASES / "wrong-config-default", copied)
    # Build a same-size transition in the two-commit base -> selected layout.
    # The production case's base file has a different size from both review trees.
    relative = Path("docs/configuration.md")
    base_file = copied / "base" / relative
    fixed_file = copied / "fixed" / relative
    base_file.write_bytes((copied / "vulnerable" / relative).read_bytes())
    assert base_file.read_bytes() != fixed_file.read_bytes()
    assert base_file.stat().st_size == fixed_file.stat().st_size
    old_timestamp = 1_000_000_000
    for path in (base_file, fixed_file):
        os.utime(path, (old_timestamp, old_timestamp))
    assert base_file.stat().st_mtime_ns == fixed_file.stat().st_mtime_ns
    case = eval_run.load_case(copied)
    repository = tmp_path / "repo"

    copy_started = time.time()
    commits, env = eval_run._build_repo(
        case, repository, snapshot="fixed", mode="dry", executor=None, grounding="on"
    )

    assert len(set(commits.values())) == 2
    fixed_files = eval_run._git(
        repository, env, "show", "--name-only", "--format=", commits["fixed"]
    ).splitlines()
    assert "docs/configuration.md" in fixed_files
    for snapshot, source in (("base", base_file), ("fixed", fixed_file)):
        assert (
            eval_run._git(repository, env, "show", f"{commits[snapshot]}:docs/configuration.md")
            == source.read_text(encoding="utf-8").strip()
        )
    assert (repository / "docs" / "configuration.md").stat().st_mtime >= copy_started


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


def test_discover_cases_ignores_hidden_and_non_case_directories(tmp_path):
    cases_root = tmp_path / "cases"
    copied = cases_root / "unguarded-division"
    shutil.copytree(CASES / "unguarded-division", copied)
    (cases_root / ".ruff_cache").mkdir()
    (cases_root / "notes").mkdir()

    assert list(eval_run.discover_cases(cases_root)) == [copied]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"gold_reference": "gold.json"}, r"gold\.json"),
        ({"gold_record": "$serialized_gold"}, "serialized gold record"),
        ({"intent": "a different intent"}, "intent differs"),
    ],
)
def test_bundle_leakage_is_rejected(data, message):
    case = eval_run.load_case(CASES / "unguarded-division")
    data = {"intent": case.metadata["intent"], **data}
    if data.get("gold_record") == "$serialized_gold":
        data["gold_record"] = json.dumps(case.gold, sort_keys=True, separators=(",", ":"))

    with pytest.raises(eval_run.LeakageError, match=message):
        eval_run._assert_bundle_is_clean(case, {"data": data})


def test_clean_bundle_passes_leakage_check():
    case = eval_run.load_case(CASES / "unguarded-division")

    eval_run._assert_bundle_is_clean(case, {"data": {"intent": case.metadata["intent"]}})


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


@pytest.mark.parametrize("snapshot", ["vulnerable", "fixed"])
@pytest.mark.parametrize("case_dir", tuple(eval_run.discover_cases()), ids=lambda path: path.name)
def test_reviewer_git_history_contains_only_selected_snapshot(tmp_path, snapshot, case_dir):
    case = eval_run.load_case(case_dir)
    repo = tmp_path / "review"
    commits, env = eval_run._build_repo(
        case, repo, snapshot=snapshot, mode="dry", executor=None, grounding="on"
    )
    assert set(commits) == {"base", snapshot}
    assert eval_run._git(repo, env, "rev-list", "--all", "--count") == "2"
    history = eval_run._git(repo, env, "log", "--all", "--format=%s")
    refs = eval_run._git(repo, env, "for-each-ref", "--format=%(refname)")
    assert set(history.splitlines()) == {"Proposed change", "Initial snapshot"}
    for token in (case.id, "vulnerable", "fixed"):
        assert token not in history + refs

    # Hash every source file; identical content shared with an allowed tree is safe.
    def snapshot_oids(name):
        return {
            eval_run._git(repo, env, "hash-object", str(path))
            for path in (case.directory / name).rglob("*")
            if path.is_file()
        }

    other = "fixed" if snapshot == "vulnerable" else "vulnerable"
    allowed_oids = snapshot_oids("base") | snapshot_oids(snapshot)
    unselected_only_oids = snapshot_oids(other) - allowed_oids
    assert unselected_only_oids, "case must exercise exclusion of distinct snapshot content"
    # Include unreachable objects, not just blobs reachable from the two commits.
    objects = eval_run._git(repo, env, "cat-file", "--batch-all-objects", "--batch-check")
    object_oids = {line.split()[0] for line in objects.splitlines()}
    assert unselected_only_oids.isdisjoint(object_oids)


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("snapshot", ["vulnerable", "fixed"])
def test_actual_provider_stdin_excludes_scorer_identity(tmp_path, monkeypatch, executor, snapshot):
    # The fake provider captures bytes after the real CLI and worked wrapper.
    # It is a subprocess boundary test; no model or network is involved.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "provider-input.txt"
    provider = bin_dir / executor
    provider.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "assert 'AP_EVAL_SCRIPT' not in os.environ\n"
        "pathlib.Path(os.environ['CAPTURE_INPUT']).write_text(sys.stdin.read())\n"
        "print(json.dumps({'findings': []}))\n",
        encoding="utf-8",
    )
    provider.chmod(0o755)
    if sys.platform == "win32":
        shim = provider.with_suffix(".cmd")
        shim.write_text(f'@"{sys.executable}" "{provider}" %*\n', encoding="utf-8")
        provider = shim
    monkeypatch.setenv(f"AP_{executor.upper()}_BIN", str(provider))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE_INPUT", str(capture))
    monkeypatch.setenv("AP_EVAL_SCRIPT", "must-not-be-inherited/fixed.json")
    case = eval_run.load_case(CASES / "unguarded-division")
    result = eval_run.run_case_snapshot(
        case,
        snapshot=snapshot,
        mode="real",
        executor=executor,
        grounding="on",
        workspace=tmp_path / "workspace",
    )
    assert result["status"] == "resolved"
    sent = capture.read_text()
    assert "Review this change independently" in sent
    for token in (case.id, "gold.json", '"snapshot"', '"case_id"'):
        assert token not in sent
    assert json.dumps(case.gold, sort_keys=True) not in sent
