"""The session guard's row classification, on fixture files only.

These tests never touch the developer's telemetry files, and they never make the
real session guard fail: every path they write is under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests import conftest
from tests import telemetry_guard as guard
from tests.conftest import requires_symlinks


def _row(source: str | None, run_id: str = "r_0000000001") -> bytes:
    repo = {"root_commit": "a" * 40, "source_worktree_path": source}
    return json.dumps({"run_id": run_id, "repo": repo}, sort_keys=True).encode()


def _decision(source: str) -> bytes:
    return json.dumps(
        {"allowed": True, "reason": "no_updates", "repo": {"source_worktree_path": source}}
    ).encode()


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A temporary root, this session's base directory inside it, and a work directory."""
    root = tmp_path / "T"
    basetemp = root / "pytest-of-dev" / "pytest-12"
    work = tmp_path / "work"
    for directory in (basetemp, work):
        directory.mkdir(parents=True)
    return root.resolve(), basetemp.resolve(), work.resolve()


def test_rows_are_classified_by_where_their_source_worktree_is(tmp_path):
    root, basetemp, work = _layout(tmp_path)
    roots = [root]

    def where(path: object) -> str | None:
        return guard.classify(path, basetemp=basetemp, roots=roots)

    assert where(str(basetemp / "test_x0" / "repo")) == "basetemp"
    assert where(str(root / "agentic-preflight-eval-1" / "review-1")) == "temporary"
    assert where(str(work / "repository")) is None
    # Another pytest session's base directory belongs to that session.
    assert where(str(root / "pytest-of-dev" / "pytest-11" / "test_y0" / "repo")) is None
    assert where(str(root / "pytest-of-other" / "pytest-3" / "repo")) is None
    # Not a session directory, so still temporary.
    assert where(str(root / "pytest-of-dev" / "scratch")) == "temporary"
    assert where(None) is None
    assert where("") is None
    assert where(42) is None


def test_a_base_directory_outside_the_temporary_roots_still_counts(tmp_path):
    custom = (tmp_path / "custom-basetemp").resolve()

    assert guard.classify(str(custom / "repo"), basetemp=custom, roots=[]) == "basetemp"


def test_an_xdist_worker_reports_the_whole_session_directory(tmp_path):
    session = tmp_path / "pytest-of-dev" / "pytest-12"
    worker = session / "popen-gw3"
    worker.mkdir(parents=True)

    assert guard.session_basetemp(worker) == session.resolve()
    assert guard.session_basetemp(session) == session.resolve()


@requires_symlinks
def test_symlinked_paths_are_compared_by_their_real_paths(tmp_path):
    real = tmp_path / "private" / "T"
    (real / "scratch").mkdir(parents=True)
    link = tmp_path / "T-link"
    link.symlink_to(real, target_is_directory=True)
    # tmp_path is itself in this session's base directory under the real temporary
    # directory, so keep only the root that $TMPDIR named.
    roots = [
        root
        for root in guard.temporary_roots({"TMPDIR": str(link)})
        if root.is_relative_to(tmp_path.resolve())
    ]
    elsewhere = (tmp_path / "elsewhere").resolve()

    assert roots == [real.resolve()]
    assert guard.classify(str(link / "scratch"), basetemp=elsewhere, roots=roots) == "temporary"
    assert guard.classify(str(real / "scratch"), basetemp=elsewhere, roots=roots) == "temporary"


def test_only_lines_added_since_the_snapshot_are_read(tmp_path):
    root, basetemp, work = _layout(tmp_path)
    directory = tmp_path / "real-telemetry"
    directory.mkdir()
    mirror = directory / "runs.jsonl"
    earlier = _row(str(root / "old-scratch"), "r_0000000001")
    mirror.write_bytes(earlier + b"\n")

    before = guard.snapshot([directory])
    leaked = _row(str(basetemp / "test_x0" / "repo"), "r_0000000002")
    legitimate = _row(str(work / "repository"), "r_0000000003")
    mirror.write_bytes(legitimate + b"\n" + earlier + b"\n" + leaked + b"\nnot json\n")
    log = directory / "hook-decisions.jsonl"
    log.write_bytes(_decision(str(root / "agentic-preflight-eval-1" / "review-1")) + b"\n")

    found = guard.check(before, basetemp=basetemp, roots=[root])

    assert [(row.file.name, row.line, row.run_id, row.where) for row in found] == [
        ("runs.jsonl", 3, "r_0000000002", "pytest's basetemp"),
        ("hook-decisions.jsonl", 1, None, "the system temp directory"),
    ]


def test_a_rewritten_mirror_with_the_same_rows_adds_nothing(tmp_path):
    root, basetemp, _ = _layout(tmp_path)
    directory = tmp_path / "real-telemetry"
    directory.mkdir()
    rows = [_row(str(basetemp / f"t{n}"), f"r_000000000{n}") for n in range(3)]
    mirror = directory / "runs.jsonl"
    mirror.write_bytes(b"\n".join(rows) + b"\n")

    before = guard.snapshot([directory])
    mirror.write_bytes(b"\n".join(reversed(rows)) + b"\n")

    assert guard.check(before, basetemp=basetemp, roots=[root]) == []


def test_missing_files_snapshot_as_empty_and_report_every_later_row(tmp_path):
    root, basetemp, _ = _layout(tmp_path)
    directory = tmp_path / "not-yet"

    before = guard.snapshot([directory])
    directory.mkdir()
    (directory / "runs.jsonl").write_bytes(_row(str(basetemp / "t0")) + b"\n")

    assert [row.line for row in guard.check(before, basetemp=basetemp, roots=[root])] == [1]


def test_the_failure_message_names_the_rows_and_the_isolation_rule(tmp_path):
    rows = [
        guard.LeakedRow(
            file=tmp_path / "runs.jsonl",
            line=number,
            source_worktree_path=f"/scratch/pytest-of-dev/pytest-12/t{number}",
            where="pytest's basetemp",
            run_id=f"r_{number:010d}",
        )
        for number in range(1, guard.MESSAGE_LIMIT + 3)
    ]

    message = guard.failure_message(rows)

    assert f"added {len(rows)} row(s)" in message
    assert (
        f"{tmp_path / 'runs.jsonl'}:1 run r_0000000001: /scratch/pytest-of-dev/pytest-12/t1"
        in message
    )
    assert "... and 2 more" in message
    assert "XDG_DATA_HOME" in message
    assert guard.ISOLATION_RULE in message


def test_the_real_directories_come_from_the_environment_before_the_tests(tmp_path):
    home = tmp_path / "home"
    config = home / ".config" / "agentic-preflight" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[telemetry]\npath = "~/custom-telemetry"\n', encoding="utf-8")
    data = tmp_path / "data"

    directories = guard.real_telemetry_dirs({"XDG_DATA_HOME": str(data)}, home)

    default = home / ".local" / "share" / "agentic-preflight" / "telemetry"
    configured = Path("~/custom-telemetry").expanduser()
    assert directories == [
        default.resolve(),
        (data / "agentic-preflight" / "telemetry").resolve(),
        configured.resolve(),
    ]


def test_a_relative_data_home_and_an_unreadable_config_are_ignored(tmp_path):
    home = tmp_path / "home"
    config = home / ".config" / "agentic-preflight" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[telemetry\n", encoding="utf-8")

    directories = guard.real_telemetry_dirs({"XDG_DATA_HOME": "relative"}, home)

    assert directories == [
        (home / ".local" / "share" / "agentic-preflight" / "telemetry").resolve()
    ]


def test_the_session_guard_reads_the_files_named_before_the_test_overrides():
    directories = guard.real_telemetry_dirs(conftest._REAL_ENVIRON, conftest._REAL_HOME)
    per_test = Path(os.environ["XDG_DATA_HOME"]) / "agentic-preflight" / "telemetry"

    assert Path.home() != conftest._REAL_HOME
    assert per_test.resolve() not in directories
    assert all(not path.is_relative_to(Path.home().resolve()) for path in directories)


def test_the_temporary_roots_skip_a_filesystem_root(tmp_path):
    roots = guard.temporary_roots({"TMPDIR": tmp_path.anchor})

    assert Path(tmp_path.anchor) not in roots
    assert roots
