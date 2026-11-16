"""The budgeted diff read: identical within budget, bounded far over it."""

import pytest

from agentic_preflight import diff, gitx
from tests.conftest import commit_all, git, requires_git_symlinks, write


def _awkward_change(repo):
    """Commit text that exercises every decoding and splitting rule of the patch reader."""
    write(repo, "keep/plain.txt", "before\n")
    write(repo, "keep/café.txt", "before\n")
    write(repo, "keep/moved.txt", "".join(f"shared {index}\n" for index in range(20)))
    (repo / "keep" / "image.bin").write_bytes(b"before\0")
    commit_all(repo, "add awkward files")
    base = gitx.rev_parse(repo, "HEAD")
    git("switch", "-c", "feature/awkward", cwd=repo)
    write(repo, "keep/plain.txt", "after\n")
    write(repo, "keep/café.txt", "après\n")
    git("mv", "keep/moved.txt", "keep/renamed.txt", cwd=repo)
    (repo / "keep" / "image.bin").write_bytes(b"after\0")
    (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (repo / "lone-cr.txt").write_bytes(b"hello\rdiff --git a/x b/x\rmore\n")
    (repo / "invalid.txt").write_bytes(b"bad \xe2\x82 bytes \xff\n")
    for index in range(5):
        write(repo, f"many/{index}.txt", f"file {index}\n")
    commit_all(repo, "change awkward files")
    return base


@pytest.mark.parametrize("batch_files", [1, 3, 256])
def test_a_bounded_read_within_its_bound_matches_the_unbounded_read(
    tmp_repo, monkeypatch, batch_files
):
    base = _awkward_change(tmp_repo)
    monkeypatch.setattr(gitx, "_DIFF_PATH_BATCH_FILES", batch_files)
    paths = gitx.changed_files_raw(tmp_repo, base, "HEAD")

    bounded = gitx.diff_text_by_path_bounded(
        tmp_repo, base, "HEAD", paths, stop_after_bytes=10_000_000
    )

    assert bounded.complete is True
    assert bounded.partial is None
    assert list(bounded.patches) == paths
    assert bounded.patches == gitx.diff_text_by_path(tmp_repo, base, "HEAD", paths)


@requires_git_symlinks
def test_a_bounded_read_keeps_both_blocks_of_a_type_change(tmp_repo):
    from tests.conftest import require_type_change

    write(tmp_repo, "kind.txt", "before\n")
    commit_all(tmp_repo, "add file")
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/kind", cwd=tmp_repo)
    (tmp_repo / "kind.txt").unlink()
    (tmp_repo / "kind.txt").symlink_to("elsewhere")
    commit_all(tmp_repo, "file to symlink")
    require_type_change(tmp_repo, base, "HEAD", "kind.txt")

    bounded = gitx.diff_text_by_path_bounded(
        tmp_repo, base, "HEAD", ["kind.txt"], stop_after_bytes=10_000_000
    )

    assert bounded.patches == gitx.diff_text_by_path(tmp_repo, base, "HEAD", ["kind.txt"])


def test_a_budgeted_bundle_and_its_digest_match_the_full_bundle(tmp_repo):
    base = _awkward_change(tmp_repo)
    full = diff.build_bundle(tmp_repo, base, "HEAD", exclude=["many/**"])

    bundle, report = diff.build_bundle_within_budget(
        tmp_repo, base, "HEAD", exclude=["many/**"], max_bytes=200_000
    )

    assert bundle == full
    assert report == diff.check_budget(full, 200_000)
    assert diff.build_review_manifest(tmp_repo, bundle) == diff.build_review_manifest(
        tmp_repo, full
    )


def test_a_diff_over_budget_within_the_measuring_margin_is_reported_exactly(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "uv.lock", "generated = true\n" * 200)
    commit_all(feature_repo, "add lockfile")
    full = diff.build_bundle(feature_repo, base, "HEAD")
    max_bytes = full.total_bytes // 2

    bundle, report = diff.build_bundle_within_budget(
        feature_repo, base, "HEAD", max_bytes=max_bytes
    )

    assert bundle == full
    assert report == diff.check_budget(full, max_bytes)
    assert report.over_budget is True
    assert report.complete is True
    assert report.unmeasured_files == []


def test_a_bounded_read_does_not_start_later_batches_once_over(tmp_repo, monkeypatch):
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/batches", cwd=tmp_repo)
    for index in range(6):
        write(tmp_repo, f"part/{index}.txt", f"line {index}\n" * 50)
    commit_all(tmp_repo, "add parts")
    monkeypatch.setattr(gitx, "_DIFF_PATH_BATCH_FILES", 2)
    paths = gitx.changed_files_raw(tmp_repo, base, "HEAD")
    single = len(gitx.diff_text_by_path(tmp_repo, base, "HEAD", paths[:1])[paths[0]].encode())
    started = []
    real = gitx._streamed_diff_text_by_path

    def counted(*args, **kwargs):
        started.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(gitx, "_streamed_diff_text_by_path", counted)

    bundle, report = diff.build_bundle_within_budget(
        tmp_repo, base, "HEAD", max_bytes=single // diff.BUDGET_MEASURE_FACTOR
    )

    assert bundle is None
    assert report.complete is False
    assert len(started) == 1
    assert report.total_bytes > report.max_bytes
    assert len(report.by_file) + len(report.unmeasured_files) == 6
    assert (
        report.unmeasured_files
        == [path for path in paths if path.startswith("part/")][len(report.by_file) :]
    )


def test_the_partially_read_file_reports_a_lower_bound(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    write(feature_repo, "generated.txt", "row\r\n" * 100_000)
    commit_all(feature_repo, "add a generated file")
    exact = diff.build_bundle(feature_repo, base, "HEAD").file_bytes("generated.txt")

    bundle, report = diff.build_bundle_within_budget(feature_repo, base, "HEAD", max_bytes=100)

    assert bundle is None
    sizes = dict(report.by_file)
    assert diff.BUDGET_MEASURE_FACTOR * 100 < sizes["generated.txt"] < exact
    assert diff.over_budget_message(report).startswith("the diff is at least ")


def test_a_bounded_windows_stream_stops_early(tmp_repo, monkeypatch):
    from tests.test_gitx import _commit_raw_paths

    with (tmp_repo / "big.txt").open("wb") as stream:
        stream.write(b"z\n" * 500_000)
    git("add", "big.txt", cwd=tmp_repo)
    commit = _commit_raw_paths(tmp_repo, [b"caf\xe9.txt"])
    git("reset", "-q", "--hard", "main", cwd=tmp_repo)
    paths = gitx.changed_files_raw(tmp_repo, "main", commit)
    assert "big.txt" in paths
    monkeypatch.setattr(gitx, "_WINDOWS_PATH_ARGUMENTS", True)

    bounded = gitx.diff_text_by_path_bounded(
        tmp_repo, "main", commit, paths, stop_after_bytes=1_000
    )

    assert bounded.complete is False
    assert bounded.partial is not None
    assert bounded.partial[0] == "big.txt"
    assert bounded.partial[1] < 500_000
