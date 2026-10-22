"""Temporary repositories stay out of the default telemetry location.

A repository is temporary when it is under a system temporary directory and has no
network remote. Every fixture repository of this suite is under the system temporary
directory, so this module does not use ``telemetry_opt_in``: the product's default
applies to each test. Tests that need an exact set of temporary directories set them
with :func:`_temporary_dirs`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from agentic_preflight import hook, hooklog, hosted, telemetry
from agentic_preflight.config import Config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.store import Store
from tests.conftest import git, requires_symlinks
from tests.driver import ScriptedAgent
from tests.test_hook import hook_check
from tests.test_telemetry import _schema_2_record, _state_root, _store_record, _summary

TEMPORARY = telemetry.TEMPORARY_REPOSITORY
ZERO = "0" * 40
GITHUB = "https://github.com/owner/widget.git"


def _temporary_dirs(monkeypatch, gettempdir: Path, *, tmpdir: Path | str | None = None) -> None:
    """Make ``gettempdir()`` answer ``gettempdir``, and stand it in for ``/tmp`` as well."""
    monkeypatch.setattr(tempfile, "tempdir", str(gettempdir))
    if tmpdir is None:
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr(telemetry, "_POSIX_TMP", str(gettempdir))


def _summary_for(source: Path | str, remote: str | None = None) -> telemetry.RunSummary:
    identity = telemetry.RepoIdentity(
        root_commit="a" * 40, remote=remote, name="widget", source_worktree_path=str(source)
    )
    return telemetry.summarize(_schema_2_record(), None, None, identity)


def _events(repo: Path, run_id: str) -> list[dict]:
    path = _state_root(repo) / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _store(repo: Path) -> Store:
    return Store(_state_root(repo))


# -- the rule ---------------------------------------------------------------------


def test_a_repository_under_a_temporary_directory_is_temporary(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    _temporary_dirs(monkeypatch, temp)

    assert telemetry.temporary_roots() == [temp.resolve()]
    assert telemetry.is_temporary_repository(temp / "repository")
    assert telemetry.is_temporary_repository(str(temp))
    assert not telemetry.is_temporary_repository(tmp_path / "work" / "repository")
    # A shared name prefix is not containment.
    assert not telemetry.is_temporary_repository(tmp_path / "temp-other" / "repository")
    assert not telemetry.is_temporary_repository(None)
    assert not telemetry.is_temporary_repository("")


def test_the_real_temporary_directory_holds_every_fixture_repository(tmp_repo):
    assert telemetry.is_temporary_repository(tmp_repo)
    assert telemetry.is_temporary_repository(Path(tempfile.gettempdir()) / "scratch")
    if os.name != "nt":
        assert telemetry.is_temporary_repository("/tmp/scratch-clone")  # noqa: S108


@requires_symlinks
def test_symlinked_temporary_paths_are_compared_by_their_real_paths(tmp_path, monkeypatch):
    # Shaped like macOS: gettempdir() names /var/folders/..., a link to /private/var/folders/...
    real = tmp_path / "private" / "T"
    (real / "repository").mkdir(parents=True)
    link = tmp_path / "T-link"
    link.symlink_to(real, target_is_directory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real / "repository", target_is_directory=True)
    outside = tmp_path / "work"
    outside.mkdir()
    escape = real / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    _temporary_dirs(monkeypatch, link)

    assert telemetry.temporary_roots() == [real.resolve()]
    assert telemetry.is_temporary_repository(real / "repository")
    assert telemetry.is_temporary_repository(link / "repository")
    assert telemetry.is_temporary_repository(alias)
    assert not telemetry.is_temporary_repository(escape)

    _temporary_dirs(monkeypatch, real)

    assert telemetry.is_temporary_repository(link / "repository")


def test_tmpdir_counts_even_after_gettempdir_has_answered(tmp_path, monkeypatch):
    cached = tmp_path / "cached"
    custom = tmp_path / "custom"
    cached.mkdir()
    custom.mkdir()

    _temporary_dirs(monkeypatch, cached, tmpdir=custom)

    assert custom.resolve() in telemetry.temporary_roots()
    assert telemetry.is_temporary_repository(custom / "repository")


def test_a_tmpdir_that_would_cover_every_repository_is_ignored(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    home = Path.home()

    for value in (home, Path(tmp_path.anchor), "relative/temp"):
        _temporary_dirs(monkeypatch, temp, tmpdir=value)

        assert telemetry.temporary_roots() == [temp.resolve()]
        assert not telemetry.is_temporary_repository(home / "repository")
        assert not telemetry.is_temporary_repository(tmp_path / "work")


@pytest.mark.parametrize(
    ("remote", "network"),
    [
        ("github.com/owner/widget", True),
        ("git.example.com/group/sub/widget", True),
        # An SSH host alias from ~/.ssh/config has no dot, and is still a host.
        ("github-work/owner/widget", True),
        ("10.0.0.5/srv/widget", True),
        ("/srv/git/widget", False),
        ("/scratch/pytest-of-dev/pytest-1/test0/remote", False),
        ("C:/repos/widget", False),
        ("C:\\repos\\widget", False),
        ("../remote", False),
        ("./remote", False),
        ("~/repos/widget", False),
        ("localhost/srv/widget", False),
        ("127.0.0.1/owner/widget", False),
        ("::1/owner/widget", False),
        ("github.com", False),
        ("remote", False),
        ("", False),
        (None, False),
    ],
)
def test_a_network_remote_names_a_host_and_a_path(remote, network):
    assert telemetry.has_network_remote(remote) is network


@pytest.mark.parametrize(
    ("url", "network"),
    [
        (GITHUB, True),
        ("git@github.com:owner/widget.git", True),
        ("ssh://git@git.example.com:2222/owner/widget.git", True),
        ("/srv/git/widget.git", False),
        ("file:///srv/git/widget.git", False),
        ("file://localhost/srv/git/widget.git", False),
        ("http://127.0.0.1:8080/owner/widget.git", False),
    ],
)
def test_normalized_remotes_are_classified_by_where_they_live(url, network):
    assert telemetry.has_network_remote(telemetry.normalize_remote(url)) is network


def test_the_rule_needs_a_temporary_path_and_no_network_remote(tmp_path):
    repository = tmp_path / "repository"

    assert telemetry.exclusion_reason(Config(), repository, None) == TEMPORARY
    assert telemetry.exclusion_reason(None, repository, None) == TEMPORARY
    assert telemetry.exclusion_reason(Config(), repository, str(tmp_path / "remote")) == TEMPORARY
    assert telemetry.exclusion_reason(Config(), repository, "github.com/owner/widget") is None
    assert telemetry.exclusion_reason(Config(), "/repos/widget", None) is None
    mirror_off = Config(telemetry={"mirror": False})
    assert telemetry.exclusion_reason(mirror_off, repository, None) == TEMPORARY


def test_an_explicit_path_opts_a_temporary_repository_in(tmp_path):
    repository = tmp_path / "repository"
    opted_in = Config(telemetry={"path": str(tmp_path / "telemetry")})

    assert telemetry.exclusion_reason(opted_in, repository, None) is None
    # Even the default location, named explicitly, is an opt-in.
    named = Config(telemetry={"path": str(telemetry.telemetry_dir())})
    assert telemetry.exclusion_reason(named, repository, None) is None


# -- run summaries ------------------------------------------------------------------


def test_write_summary_keeps_a_temporary_repository_out_of_the_mirror(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    summary = _summary_for(tmp_path / "repository")

    status = telemetry.write_summary(run_dir, summary, Config())

    assert status == {
        "run_id": "r_0000000002",
        "summary_path": str(run_dir / "summary.json"),
        "mirrored": False,
        "mirror_path": None,
        "reason": TEMPORARY,
        "error": None,
    }
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["state"] == "ABORTED"
    assert not telemetry.telemetry_dir().exists()


def test_write_summary_mirrors_an_opted_in_or_ordinary_repository(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "opted-in"

    opted_in = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "repository"), Config(telemetry={"path": str(target)})
    )
    off = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "repository"), Config(telemetry={"mirror": False})
    )
    ordinary = telemetry.write_summary(run_dir, _summary_for("/repos/widget"), Config())

    assert (opted_in["mirrored"], opted_in["reason"]) == (True, None)
    assert opted_in["mirror_path"] == str(target / "runs.jsonl")
    assert len((target / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (off["mirrored"], off["reason"], off["mirror_path"]) == (False, None, None)
    assert (ordinary["mirrored"], ordinary["reason"]) == (True, None)
    assert ordinary["mirror_path"] == str(telemetry.mirror_path())


def test_write_summary_mirrors_a_temporary_clone_with_a_network_remote(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    hosted = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "pr-clone", "github.com/owner/widget"), Config()
    )
    local = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "fixture", str(tmp_path / "remote")), Config()
    )

    assert (hosted["mirrored"], hosted["reason"]) == (True, None)
    assert (local["mirrored"], local["reason"]) == (False, TEMPORARY)
    rows = telemetry.mirror_path().read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["repo"]["remote"] for row in rows] == ["github.com/owner/widget"]


def test_a_temporary_clone_with_a_github_remote_keeps_its_telemetry(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    # Added after start, which would otherwise fetch from GitHub. The summary reads
    # the remote when the run ends.
    git("remote", "add", "origin", GITHUB, cwd=feature_repo)

    report = agent.run("abort", "--force")["data"]["telemetry"]

    assert (report["mirrored"], report["reason"]) == (True, None)
    (row,) = [json.loads(line) for line in telemetry.mirror_path().read_text().splitlines()]
    assert (row["run_id"], row["repo"]["remote"]) == (run_id, "github.com/owner/widget")


def test_a_temporary_clone_with_a_local_bare_remote_is_excluded(feature_repo, bare_remote):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    report = agent.run("abort", "--force")["data"]["telemetry"]

    assert (report["mirrored"], report["reason"]) == (False, TEMPORARY)
    assert not telemetry.telemetry_dir().exists()


def test_a_run_that_ends_in_a_temporary_repository_reports_the_reason(agent, feature_repo):
    run_id = agent.run("start")["run_id"]

    aborted = agent.run("abort", "--force")

    report = aborted["data"]["telemetry"]
    assert report["run_id"] == run_id
    assert (report["mirrored"], report["mirror_path"]) == (False, None)
    assert (report["reason"], report["error"]) == (TEMPORARY, None)
    assert _summary(feature_repo, run_id)["state"] == "ABORTED"
    (event,) = [
        item for item in _events(feature_repo, run_id) if item["event"] == "summary_written"
    ]
    assert (event["mirrored"], event["reason"]) == (False, TEMPORARY)
    assert not telemetry.telemetry_dir().exists()


def test_record_run_end_reports_the_reason_and_records_it_in_the_event(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    agent.run("abort")
    store = _store(feature_repo)
    run = store.load_run(run_id)

    status = telemetry.record_run_end(store, feature_repo, run)

    assert (status["mirrored"], status["reason"], status["error"]) == (False, TEMPORARY, None)
    assert telemetry.take_report(run_id) == status
    assert _events(feature_repo, run_id)[-1]["reason"] == TEMPORARY
    assert not telemetry.telemetry_dir().exists()


# -- rebuild and sync-hosted -----------------------------------------------------------


def test_rebuild_in_a_temporary_clone_writes_summaries_and_leaves_the_mirror_alone(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")

    assert result["data"]["summarized"] == 1
    assert result["data"]["mirror"] == {
        "enabled": True,
        "path": None,
        "rows": None,
        "preserved_rows": None,
        "temporary_runs": 1,
        "reason": TEMPORARY,
        "error": None,
    }
    assert _summary(tmp_repo, "r_0000000002")["state"] == "ABORTED"
    assert not telemetry.telemetry_dir().exists()


def test_rebuild_in_a_temporary_clone_does_not_touch_an_existing_mirror(tmp_repo):
    _store_record(tmp_repo, _schema_2_record())
    mirror = telemetry.mirror_path()
    mirror.parent.mkdir(parents=True)
    original = b'{"run_id": "r_0000000009", "repo": {"root_commit": null}}\n'
    mirror.write_bytes(original)

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    assert result["mirror"]["reason"] == TEMPORARY
    assert result["mirror"]["temporary_runs"] == 0
    assert mirror.read_bytes() == original


def test_rebuild_in_another_clone_leaves_out_runs_of_a_temporary_worktree(
    tmp_repo, tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _temporary_dirs(monkeypatch, scratch)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    linked = {
        **_schema_2_record(),
        "run_id": "r_0000000005",
        "source_worktree_path": str(scratch / "linked"),
    }
    _store_record(tmp_repo, linked)

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    assert result["summarized"] == 2
    mirror = result["mirror"]
    assert (mirror["rows"], mirror["temporary_runs"], mirror["reason"]) == (1, 1, None)
    rows = [json.loads(line) for line in telemetry.mirror_path().read_text().splitlines()]
    assert [row["run_id"] for row in rows] == ["r_0000000002"]
    assert _summary(tmp_repo, "r_0000000005")["state"] == "ABORTED"


def test_rebuild_in_a_temporary_clone_with_a_network_remote_mirrors_it(tmp_repo):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    mirror = result["mirror"]
    assert (mirror["rows"], mirror["temporary_runs"], mirror["reason"]) == (1, 0, None)
    assert mirror["path"] == str(telemetry.mirror_path())


def test_rebuild_with_an_explicit_path_mirrors_a_temporary_clone(tmp_repo, tmp_path):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    target = tmp_path / "opted-in"

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config(telemetry={"path": str(target)}))

    assert result["mirror"]["path"] == str(target / "runs.jsonl")
    assert (result["mirror"]["rows"], result["mirror"]["temporary_runs"]) == (1, 0)
    assert result["mirror"]["reason"] is None


def test_sync_hosted_summaries_in_a_temporary_clone_leave_the_mirror_alone(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    identity = telemetry.repo_identity(tmp_repo)

    result = hosted.resummarize(_store(tmp_repo), identity, Config(), ["r_0000000002"])

    assert result["regenerated"] == 1
    assert (result["mirror"]["reason"], result["mirror"]["path"]) == (TEMPORARY, None)
    assert not telemetry.telemetry_dir().exists()


def test_sync_hosted_does_not_rewrite_the_mirror_for_temporary_runs_only(
    tmp_repo, tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _temporary_dirs(monkeypatch, scratch)
    linked = {**_schema_2_record(), "source_worktree_path": str(scratch / "linked")}
    _store_record(tmp_repo, linked)
    identity = telemetry.repo_identity(tmp_repo)

    result = hosted.resummarize(_store(tmp_repo), identity, Config(), ["r_0000000002"])

    assert (result["mirror"]["temporary_runs"], result["mirror"]["path"]) == (1, None)
    assert not telemetry.telemetry_dir().exists()


# -- the hook decision log ----------------------------------------------------------------


def test_hook_check_in_a_temporary_repository_logs_nothing(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    blocked = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    up_to_date = hook_check(feature_repo, "")

    assert blocked.returncode == ExitCode.HOOK_BLOCK
    assert "agentic-preflight: push blocked." in blocked.stderr
    assert (up_to_date.returncode, up_to_date.stderr) == (0, "")
    assert not hooklog.log_path().parent.exists()


def test_hook_check_with_a_local_bare_remote_logs_nothing(feature_repo, bare_remote):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    hook_check(
        feature_repo,
        f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        {hook.REMOTE_ENV: "origin"},
    )

    assert not hooklog.log_path().parent.exists()


def test_hook_check_in_a_temporary_clone_with_a_github_remote_logs(feature_repo):
    git("remote", "add", "origin", GITHUB, cwd=feature_repo)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    hook_check(
        feature_repo,
        f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        {hook.REMOTE_ENV: "origin"},
    )

    (line,) = [json.loads(item) for item in hooklog.log_path().read_text().splitlines()]
    assert (line["reason"], line["repo"]["remote"]) == (
        hook.MISSING_ATTESTATION,
        "github.com/owner/widget",
    )


@pytest.mark.parametrize("push_remote", ["upstream", GITHUB], ids=["name", "url"])
def test_without_origin_the_push_remote_decides(tmp_repo, push_remote):
    git("remote", "add", "upstream", GITHUB, cwd=tmp_repo)
    decision = hook.unevaluated([], hook.NO_UPDATES)

    outcome = hooklog.record(decision, repo_root=tmp_repo, config=Config(), remote=push_remote)

    assert outcome == hooklog.WRITTEN
    assert hooklog.log_path().exists()


def test_without_origin_a_local_push_remote_logs_nothing(tmp_repo, tmp_path):
    git("remote", "add", "upstream", str(tmp_path / "remote.git"), cwd=tmp_repo)
    decision = hook.unevaluated([], hook.NO_UPDATES)

    for push_remote in ("upstream", str(tmp_path / "remote.git"), "missing", None):
        outcome = hooklog.record(decision, repo_root=tmp_repo, config=Config(), remote=push_remote)

        assert outcome == hooklog.TEMPORARY
    assert not hooklog.log_path().parent.exists()


def test_an_explicit_path_logs_a_temporary_repositorys_decisions(feature_repo, telemetry_opt_in):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")

    lines = (telemetry_opt_in / hooklog.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["reason"] for line in lines] == [hook.MISSING_ATTESTATION]


@pytest.mark.parametrize("config", [Config(), None], ids=["loaded", "fallback"])
def test_the_log_writer_skips_a_temporary_repository(tmp_repo, config):
    decision = hook.unevaluated([], hook.NO_UPDATES)

    outcome = hooklog.record(decision, repo_root=tmp_repo, config=config, remote="origin")

    assert outcome == hooklog.TEMPORARY == TEMPORARY
    assert not hooklog.log_path().parent.exists()


def test_the_log_writer_honours_an_explicit_path_for_a_temporary_repository(tmp_repo, tmp_path):
    decision = hook.unevaluated([], hook.NO_UPDATES)
    config = Config(telemetry={"path": str(tmp_path / "opted-in")})

    outcome = hooklog.record(decision, repo_root=tmp_repo, config=config, remote="origin")

    assert outcome == hooklog.WRITTEN
    assert (tmp_path / "opted-in" / hooklog.LOG_NAME).exists()


# -- the report ----------------------------------------------------------------------


def test_the_report_in_a_temporary_clone_does_not_point_at_a_rebuild(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["source"]["used"] == "clone_records"
    assert (clone["summarized"], clone["temporary_runs"]) == (1, 1)
    instruction = result["next"]["instruction"]
    assert result["next"]["command"] is None
    assert "under the system temporary directory without a network remote" in instruction
    assert "Run `agentic-preflight telemetry rebuild`" not in instruction


def test_the_report_counts_runs_of_a_temporary_worktree_apart(tmp_repo, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _temporary_dirs(monkeypatch, scratch)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    agent = ScriptedAgent(tmp_repo)
    agent.run("telemetry", "rebuild")
    linked = {
        **_schema_2_record(),
        "run_id": "r_0000000005",
        "source_worktree_path": str(scratch / "linked"),
    }
    _store_record(tmp_repo, linked)

    result = agent.run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["source"]["used"] == "mirror"
    assert (clone["unmirrored_terminal_runs"], clone["temporary_runs"]) == (0, 1)
    assert result["next"]["command"] is None


def test_the_report_in_a_temporary_clone_with_a_network_remote_points_at_a_rebuild(tmp_repo):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert (clone["summarized"], clone["temporary_runs"]) == (1, 0)
    assert result["next"]["command"] == "agentic-preflight telemetry rebuild"
