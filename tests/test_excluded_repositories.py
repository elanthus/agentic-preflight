"""Repositories without a network remote stay out of the default telemetry location.

A preflight run gates a push, so a repository that cannot push is a test fixture,
wherever it lives. Every fixture repository of this suite has no network remote until
a test adds one, so this module does not use ``telemetry_opt_in``: the product's
default applies to each test. ``OUTSIDE`` stands for a worktree outside every
temporary directory; the rule never looks at the path, so it need not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_preflight import hook, hooklog, hosted, telemetry
from agentic_preflight.config import Config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.store import Store
from tests.conftest import git
from tests.driver import ScriptedAgent
from tests.test_hook import hook_check
from tests.test_telemetry import _schema_2_record, _state_root, _store_record, _summary

EXCLUDED = telemetry.NO_NETWORK_REMOTE
ZERO = "0" * 40
GITHUB = "https://github.com/owner/widget.git"
LOOPBACK = "http://127.0.0.1:8080/owner/widget.git"
#: A worktree outside every system temporary directory.
OUTSIDE = "/home/dev/widget"


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
        ("fixtures/bare.git", False),
        ("bare.git", False),
        ("C:\\repos\\widget.git", False),
    ],
)
def test_normalized_remotes_are_classified_by_where_they_live(url, network):
    assert telemetry.has_network_remote(telemetry.normalize_remote(url)) is network


@pytest.mark.parametrize("repository", [OUTSIDE, None], ids=["outside_temp", "unknown"])
def test_the_rule_is_the_remote_wherever_the_repository_lives(tmp_path, repository):
    for path in (repository, tmp_path / "repository"):
        assert telemetry.exclusion_reason(Config(), path, None) == EXCLUDED
        assert telemetry.exclusion_reason(None, path, None) == EXCLUDED
        assert telemetry.exclusion_reason(Config(), path, str(tmp_path / "remote")) == EXCLUDED
        assert telemetry.exclusion_reason(Config(), path, "127.0.0.1/owner/widget") == EXCLUDED
        assert telemetry.exclusion_reason(Config(), path, "github.com/owner/widget") is None
        mirror_off = Config(telemetry={"mirror": False})
        assert telemetry.exclusion_reason(mirror_off, path, None) == EXCLUDED


def test_an_explicit_path_opts_a_repository_without_a_remote_in(tmp_path):
    repository = tmp_path / "repository"
    opted_in = Config(telemetry={"path": str(tmp_path / "telemetry")})

    assert telemetry.exclusion_reason(opted_in, repository, None) is None
    # Even the default location, named explicitly, is an opt-in.
    named = Config(telemetry={"path": str(telemetry.telemetry_dir())})
    assert telemetry.exclusion_reason(named, repository, None) is None


# -- run summaries ------------------------------------------------------------------


@pytest.mark.parametrize("source", [OUTSIDE, "{tmp}/repository"], ids=["outside", "temporary"])
def test_write_summary_keeps_a_repository_without_a_remote_out_of_the_mirror(tmp_path, source):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    summary = _summary_for(source.format(tmp=tmp_path))

    status = telemetry.write_summary(run_dir, summary, Config())

    assert status == {
        "run_id": "r_0000000002",
        "summary_path": str(run_dir / "summary.json"),
        "mirrored": False,
        "mirror_path": None,
        "reason": EXCLUDED,
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
    ordinary = telemetry.write_summary(
        run_dir, _summary_for(OUTSIDE, "github.com/owner/widget"), Config()
    )

    assert (opted_in["mirrored"], opted_in["reason"]) == (True, None)
    assert opted_in["mirror_path"] == str(target / "runs.jsonl")
    assert len((target / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (off["mirrored"], off["reason"], off["mirror_path"]) == (False, None, None)
    assert (ordinary["mirrored"], ordinary["reason"]) == (True, None)
    assert ordinary["mirror_path"] == str(telemetry.mirror_path())


def test_write_summary_judges_every_clone_by_its_remote(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    hosted = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "pr-clone", "github.com/owner/widget"), Config()
    )
    local = telemetry.write_summary(
        run_dir, _summary_for(tmp_path / "fixture", str(tmp_path / "remote")), Config()
    )
    loopback = telemetry.write_summary(
        run_dir, _summary_for(OUTSIDE, "127.0.0.1/owner/widget"), Config()
    )

    assert (hosted["mirrored"], hosted["reason"]) == (True, None)
    assert (local["mirrored"], local["reason"]) == (False, EXCLUDED)
    assert (loopback["mirrored"], loopback["reason"]) == (False, EXCLUDED)
    rows = telemetry.mirror_path().read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["repo"]["remote"] for row in rows] == ["github.com/owner/widget"]


def test_a_clone_with_a_github_remote_keeps_its_telemetry(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    # Added after start, which would otherwise fetch from GitHub. The summary reads
    # the remote when the run ends.
    git("remote", "add", "origin", GITHUB, cwd=feature_repo)

    report = agent.run("abort", "--force")["data"]["telemetry"]

    assert (report["mirrored"], report["reason"]) == (True, None)
    (row,) = [json.loads(line) for line in telemetry.mirror_path().read_text().splitlines()]
    assert (row["run_id"], row["repo"]["remote"]) == (run_id, "github.com/owner/widget")


def test_a_clone_with_a_local_bare_remote_is_excluded(feature_repo, bare_remote):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    report = agent.run("abort", "--force")["data"]["telemetry"]

    assert (report["mirrored"], report["reason"]) == (False, EXCLUDED)
    assert not telemetry.telemetry_dir().exists()


def test_a_run_that_ends_without_a_network_remote_reports_the_reason(agent, feature_repo):
    run_id = agent.run("start")["run_id"]

    aborted = agent.run("abort", "--force")

    report = aborted["data"]["telemetry"]
    assert report["run_id"] == run_id
    assert (report["mirrored"], report["mirror_path"]) == (False, None)
    assert (report["reason"], report["error"]) == (EXCLUDED, None)
    assert _summary(feature_repo, run_id)["state"] == "ABORTED"
    (event,) = [
        item for item in _events(feature_repo, run_id) if item["event"] == "summary_written"
    ]
    assert (event["mirrored"], event["reason"]) == (False, EXCLUDED)
    assert not telemetry.telemetry_dir().exists()


def test_record_run_end_reports_the_reason_and_records_it_in_the_event(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    agent.run("abort")
    store = _store(feature_repo)
    run = store.load_run(run_id)

    status = telemetry.record_run_end(store, feature_repo, run)

    assert (status["mirrored"], status["reason"], status["error"]) == (False, EXCLUDED, None)
    assert telemetry.take_report(run_id) == status
    assert _events(feature_repo, run_id)[-1]["reason"] == EXCLUDED
    assert not telemetry.telemetry_dir().exists()


# -- rebuild and sync-hosted -----------------------------------------------------------


def test_rebuild_in_a_clone_without_a_remote_writes_summaries_and_leaves_the_mirror_alone(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")

    assert result["data"]["summarized"] == 1
    assert result["data"]["mirror"] == {
        "enabled": True,
        "path": None,
        "rows": None,
        "preserved_rows": None,
        "excluded_runs": 1,
        "reason": EXCLUDED,
        "error": None,
    }
    assert _summary(tmp_repo, "r_0000000002")["state"] == "ABORTED"
    assert not telemetry.telemetry_dir().exists()


def test_rebuild_in_a_clone_without_a_remote_does_not_touch_an_existing_mirror(tmp_repo):
    _store_record(tmp_repo, _schema_2_record())
    mirror = telemetry.mirror_path()
    mirror.parent.mkdir(parents=True)
    original = b'{"run_id": "r_0000000009", "repo": {"root_commit": null}}\n'
    mirror.write_bytes(original)

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    assert result["mirror"]["reason"] == EXCLUDED
    assert result["mirror"]["excluded_runs"] == 1
    assert mirror.read_bytes() == original


def test_rebuild_in_a_hosted_clone_leaves_out_runs_recorded_without_a_remote(tmp_repo, monkeypatch):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000005"})
    # A rebuilt summary carries the clone's current remote, so the rule cannot tell
    # runs apart by remote on its own; this stands a different remote in for one
    # run to show that each summary is judged by its own ``repo.remote``.
    original = telemetry.summarize

    def without_a_remote(record, *args, **kwargs):
        summary = original(record, *args, **kwargs)
        if summary.run_id == "r_0000000005":
            summary.repo.remote = None
        return summary

    monkeypatch.setattr(telemetry, "summarize", without_a_remote)

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    assert result["summarized"] == 2
    mirror = result["mirror"]
    assert (mirror["rows"], mirror["excluded_runs"], mirror["reason"]) == (1, 1, None)
    rows = [json.loads(line) for line in telemetry.mirror_path().read_text().splitlines()]
    assert [row["run_id"] for row in rows] == ["r_0000000002"]
    assert _summary(tmp_repo, "r_0000000005")["state"] == "ABORTED"


def test_rebuild_in_a_clone_with_a_network_remote_mirrors_it(tmp_repo):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config())

    mirror = result["mirror"]
    assert (mirror["rows"], mirror["excluded_runs"], mirror["reason"]) == (1, 0, None)
    assert mirror["path"] == str(telemetry.mirror_path())


def test_rebuild_with_an_explicit_path_mirrors_a_clone_without_a_remote(tmp_repo, tmp_path):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    target = tmp_path / "opted-in"

    result = telemetry.rebuild(_store(tmp_repo), tmp_repo, Config(telemetry={"path": str(target)}))

    assert result["mirror"]["path"] == str(target / "runs.jsonl")
    assert (result["mirror"]["rows"], result["mirror"]["excluded_runs"]) == (1, 0)
    assert result["mirror"]["reason"] is None


def test_sync_hosted_summaries_in_a_clone_without_a_remote_leave_the_mirror_alone(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    identity = telemetry.repo_identity(tmp_repo)

    result = hosted.resummarize(_store(tmp_repo), identity, Config(), ["r_0000000002"])

    assert result["regenerated"] == 1
    assert (result["mirror"]["reason"], result["mirror"]["path"]) == (EXCLUDED, None)
    assert not telemetry.telemetry_dir().exists()


def test_sync_hosted_does_not_rewrite_the_mirror_for_excluded_runs_only(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": OUTSIDE})
    identity = telemetry.repo_identity(tmp_repo)

    result = hosted.resummarize(_store(tmp_repo), identity, Config(), ["r_0000000002"])

    assert (result["mirror"]["excluded_runs"], result["mirror"]["path"]) == (1, None)
    assert not telemetry.telemetry_dir().exists()


# -- the hook decision log ----------------------------------------------------------------


def test_hook_check_without_a_remote_logs_nothing(feature_repo):
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


def test_hook_check_with_a_github_remote_logs(feature_repo):
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

        assert outcome == hooklog.EXCLUDED
    assert not hooklog.log_path().parent.exists()


def test_an_explicit_path_logs_the_decisions_of_a_repository_without_a_remote(
    feature_repo, telemetry_opt_in
):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")

    lines = (telemetry_opt_in / hooklog.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["reason"] for line in lines] == [hook.MISSING_ATTESTATION]


@pytest.mark.parametrize("config", [Config(), None], ids=["loaded", "fallback"])
def test_the_log_writer_skips_a_repository_without_a_remote(tmp_repo, config):
    decision = hook.unevaluated([], hook.NO_UPDATES)

    outcome = hooklog.record(decision, repo_root=tmp_repo, config=config, remote="origin")

    assert outcome == hooklog.EXCLUDED == EXCLUDED
    assert not hooklog.log_path().parent.exists()


def test_the_log_writer_honours_an_explicit_path_for_a_repository_without_a_remote(
    tmp_repo, tmp_path
):
    decision = hook.unevaluated([], hook.NO_UPDATES)
    config = Config(telemetry={"path": str(tmp_path / "opted-in")})

    outcome = hooklog.record(decision, repo_root=tmp_repo, config=config, remote="origin")

    assert outcome == hooklog.WRITTEN
    assert (tmp_path / "opted-in" / hooklog.LOG_NAME).exists()


# -- the report ----------------------------------------------------------------------


def test_the_report_in_a_clone_without_a_remote_does_not_point_at_a_rebuild(tmp_repo):
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["source"]["used"] == "clone_records"
    assert (clone["summarized"], clone["excluded_runs"]) == (1, 1)
    instruction = result["next"]["instruction"]
    assert result["next"]["command"] is None
    assert "from a repository without a network remote" in instruction
    assert "Run `agentic-preflight telemetry rebuild`" not in instruction


def test_the_report_counts_runs_that_lost_their_remote_apart(tmp_repo):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})
    agent = ScriptedAgent(tmp_repo)
    agent.run("telemetry", "rebuild")
    _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000005"})
    git("remote", "remove", "origin", cwd=tmp_repo)

    result = agent.run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["source"]["used"] == "mirror"
    assert (clone["unmirrored_terminal_runs"], clone["excluded_runs"]) == (0, 1)
    assert result["next"]["command"] is None


def test_the_report_in_a_clone_with_a_network_remote_points_at_a_rebuild(tmp_repo):
    git("remote", "add", "origin", GITHUB, cwd=tmp_repo)
    _store_record(tmp_repo, {**_schema_2_record(), "source_worktree_path": str(tmp_repo)})

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert (clone["summarized"], clone["excluded_runs"]) == (1, 0)
    assert result["next"]["command"] == "agentic-preflight telemetry rebuild"
