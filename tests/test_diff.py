import pytest

from agentic_cli import diff
from tests.conftest import commit_all, git, write


def test_bundle_captures_the_changed_set_and_full_text(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    assert bundle.files == ["src/app.py"]
    assert "loud=False" in bundle.text
    assert bundle.total_bytes == len(bundle.text.encode())


def test_bundle_splits_per_file(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "README.md", "# demo\n\nNow documented.\n")
    commit_all(feature_repo, "touch readme")

    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    assert sorted(bundle.files) == ["README.md", "src/app.py"]
    assert set(bundle.per_file) == {"README.md", "src/app.py"}
    assert "Now documented" in bundle.per_file["README.md"]
    assert "Now documented" not in bundle.per_file["src/app.py"]


def test_an_empty_diff_yields_an_empty_bundle(tmp_repo):
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    bundle = diff.build_bundle(tmp_repo, head, "HEAD")
    assert bundle.files == []
    assert bundle.text == ""
    assert bundle.total_bytes == 0


def test_total_bytes_always_equals_the_sum_of_file_bytes(feature_repo):
    """The budget check and the per-file report must never disagree."""
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "README.md", "# demo\n\nNow documented.\n")
    commit_all(feature_repo, "touch readme")

    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    assert bundle.total_bytes == sum(bundle.file_bytes(p) for p in bundle.files)


# -- exclusion matching -----------------------------------------------------


@pytest.mark.parametrize(
    "path,pattern",
    [
        ("uv.lock", "*.lock"),
        ("nested/dir/uv.lock", "*.lock"),
        ("package-lock.json", "*-lock.json"),
        ("vendor/rack/lib.rb", "vendor/**"),
        ("static/app.min.js", "**/*.min.js"),
        ("app.min.js", "**/*.min.js"),
        ("src/__snapshots__/a.snap", "**/__snapshots__/**"),
    ],
)
def test_patterns_that_should_match(path, pattern):
    assert diff.path_matches(path, pattern) is True


@pytest.mark.parametrize(
    "path,pattern",
    [
        ("src/app.py", "*.lock"),
        ("src/locking.py", "*.lock"),
        ("src/vendor_notes.md", "vendor/**"),
        ("src/app.js", "**/*.min.js"),
    ],
)
def test_patterns_that_should_not_match(path, pattern):
    assert diff.path_matches(path, pattern) is False


def test_excluded_files_are_dropped_and_recorded(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "uv.lock", "generated = true\n" * 50)
    commit_all(feature_repo, "add lockfile")

    bundle = diff.build_bundle(feature_repo, base, "HEAD", exclude=["*.lock"])
    assert bundle.files == ["src/app.py"]
    assert bundle.excluded == ["uv.lock"]
    assert "generated = true" not in bundle.text


def test_exclusion_shrinks_the_byte_count(feature_repo):
    """Excluding noise is the intended remedy for an over-budget diff."""
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "uv.lock", "generated = true\n" * 500)
    commit_all(feature_repo, "add lockfile")

    unfiltered = diff.build_bundle(feature_repo, base, "HEAD")
    filtered = diff.build_bundle(feature_repo, base, "HEAD", exclude=["*.lock"])
    assert filtered.total_bytes < unfiltered.total_bytes


# -- the budget tripwire ----------------------------------------------------


def test_a_small_diff_is_under_budget(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    report = diff.check_budget(bundle, max_bytes=200_000)
    assert report.over_budget is False
    assert report.overage == 0


def test_an_oversized_diff_trips_the_budget(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    report = diff.check_budget(bundle, max_bytes=10)
    assert report.over_budget is True
    assert report.overage == bundle.total_bytes - 10


def test_budget_report_names_the_biggest_files_first(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "uv.lock", "generated = true\n" * 200)
    commit_all(feature_repo, "add lockfile")

    bundle = diff.build_bundle(feature_repo, base, "HEAD")
    report = diff.check_budget(bundle, max_bytes=10)
    assert report.by_file[0][0] == "uv.lock"
    assert [p for p, _ in report.by_file] == sorted(
        bundle.files, key=lambda p: -bundle.file_bytes(p)
    )
