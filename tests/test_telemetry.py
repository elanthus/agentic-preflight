"""Local run summaries, the user-level mirror, and ``telemetry rebuild``.

The summary is derived output: code-derived facts only, written once when a run
reaches a terminal state, and never allowed to fail the command that ended it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentic_preflight import telemetry
from agentic_preflight.config import Config, config_digest, load_config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import State
from agentic_preflight.models import RunDoc
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, make_run, set_home, write
from tests.driver import ScriptedAgent, findings_json, green_run

PRIVATE_INTENT = "ship the widget; secret acceptance criteria 7f3a"


def _state_root(repo: Path) -> Path:
    return (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
        / "agentic-preflight"
    )


def _events(repo: Path, run_id: str) -> list[dict]:
    path = _state_root(repo) / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _summary(repo: Path, run_id: str) -> dict:
    path = _state_root(repo) / "runs" / run_id / "summary.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _mirror_rows() -> list[dict]:
    path = telemetry.mirror_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _identity(root_commit: str | None = "a" * 40) -> telemetry.RepoIdentity:
    return telemetry.RepoIdentity(
        root_commit=root_commit,
        remote="github.com/owner/widget",
        name="widget",
        source_worktree_path="/repos/widget",
    )


# -- remote normalization and identity --------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Owner/Widget.git", "github.com/Owner/Widget"),
        ("ssh://git@github.com/owner/widget.git", "github.com/owner/widget"),
        ("ssh://git@Git.Example.com:2222/owner/widget.git", "git.example.com/owner/widget"),
        ("https://user:s3cr3t-token@github.com/owner/widget.git", "github.com/owner/widget"),
        ("https://github.com/owner/widget/", "github.com/owner/widget"),
        ("https://github.com/owner/widget.git/", "github.com/owner/widget"),
        ("git@host.example:/srv/git/widget.git", "host.example/srv/git/widget"),
        ("file:///srv/git/widget.git", "/srv/git/widget"),
        ("/srv/git/widget.git", "/srv/git/widget"),
        ("C:/repos/widget.git", "C:/repos/widget"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_remote_urls_normalize_to_host_and_path_without_credentials(url, expected):
    assert telemetry.normalize_remote(url) == expected


def test_identity_without_a_remote_uses_the_root_commit_and_directory_name(tmp_repo):
    identity = telemetry.repo_identity(tmp_repo)

    assert identity.remote is None
    assert identity.name == "repo"
    assert identity.root_commit == git("rev-list", "--max-parents=0", "HEAD", cwd=tmp_repo)
    assert identity.source_worktree_path == str(tmp_repo)


def test_identity_names_the_remote_and_never_keeps_its_credentials(tmp_repo):
    git("remote", "add", "origin", "https://user:s3cr3t@example.com/Owner/Widget.git", cwd=tmp_repo)

    identity = telemetry.repo_identity(tmp_repo, "/elsewhere/worktree")

    assert identity.remote == "example.com/Owner/Widget"
    assert identity.name == "Widget"
    assert identity.source_worktree_path == "/elsewhere/worktree"
    assert "s3cr3t" not in identity.model_dump_json()


def test_identity_picks_the_smallest_of_several_root_commits(tmp_repo):
    first = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "--orphan", "other", cwd=tmp_repo)
    write(tmp_repo, "other.txt", "unrelated history\n")
    second = commit_all(tmp_repo, "second root")
    git("switch", "main", cwd=tmp_repo)
    git("merge", "--allow-unrelated-histories", "-m", "join", "other", cwd=tmp_repo)

    assert telemetry.repo_identity(tmp_repo).root_commit == min(first, second)


def test_a_linked_worktree_is_named_after_its_clone(tmp_repo, tmp_path):
    linked = tmp_path / "linked-checkout"
    git("worktree", "add", "-b", "side", str(linked), cwd=tmp_repo)

    identity = telemetry.repo_identity(linked)

    assert identity.name == "repo"
    assert identity.root_commit == telemetry.repo_identity(tmp_repo).root_commit


# -- the telemetry directory -------------------------------------------------


def test_an_absolute_xdg_data_home_holds_the_mirror(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert telemetry.telemetry_dir() == tmp_path / "data" / "agentic-preflight" / "telemetry"
    assert telemetry.mirror_path().name == "runs.jsonl"


def test_a_relative_xdg_data_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    set_home(monkeypatch, tmp_path)

    expected = Path.home() / ".local" / "share" / "agentic-preflight" / "telemetry"
    assert telemetry.telemetry_dir() == expected


def test_the_configured_path_wins_and_expands_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    set_home(monkeypatch, tmp_path)
    config = Config.model_validate({"telemetry": {"path": "~/custom-telemetry"}})

    assert telemetry.telemetry_dir(config) == Path.home() / "custom-telemetry"


def test_a_relative_configured_path_is_rejected():
    with pytest.raises(ValueError, match="absolute or start with ~"):
        Config.model_validate({"telemetry": {"path": "relative/telemetry"}})


def test_telemetry_settings_never_reach_the_snapshot_or_its_digest(tmp_path):
    write(tmp_path, ".agentic-preflight.toml", "[telemetry]\nmirror = false\npath = '~/t'\n")

    cfg = load_config(tmp_path, user_config_dir=tmp_path / "no-user-config")

    assert cfg.telemetry.mirror is False
    assert "telemetry" not in cfg.model_dump(mode="json")
    default = Config().model_dump(mode="json")
    assert config_digest(cfg.model_dump(mode="json")) == config_digest(default)


# -- summarize ---------------------------------------------------------------


def _schema_1_record() -> dict:
    """Shaped like a historical schema-1 record: hosted CI fields, no reuse or restarts."""
    return {
        "schema_version": 1,
        "run_id": "r_0000000001",
        "seq": 21,
        "state": "DONE",
        "branch": "docs/readme",
        "base_ref": "main",
        "merge_base_sha": "1" * 40,
        "head_sha": "2" * 40,
        "source_head_sha": "2" * 40,
        "sync_base_sha": "1" * 40,
        "sync_base_ref": "refs/remotes/origin/main",
        "sync_remote": "origin",
        "intent": PRIVATE_INTENT,
        "intent_source": "user",
        "worktree_path": "/scratch/readme",
        "worktree_branch": "docs/readme",
        "worktree_released": True,
        "copied_files": [],
        "config_snapshot": {
            "general": {"base_ref": "main"},
            "review": {"blocking_severities": ["critical", "high"], "max_findings": 50},
            "worktree": {"mode": "in_place", "ttl_hours": 48},
            "publish": {"provider": "auto"},
        },
        "config_digest": "f" * 64,
        "fix_commits": [],
        "stages": {
            "test": {
                "status": "skipped",
                "attempts": 0,
                "command": None,
                "reason": "changes are limited to documentation and CI configuration",
                "exit_code": None,
                "finished_at": "2026-07-29T03:54:23+00:00",
            },
            "lint": {
                "status": "green",
                "attempts": 0,
                "command": "ruff check .",
                "reason": None,
                "exit_code": 0,
                "finished_at": "2026-07-29T03:54:41+00:00",
            },
        },
        "stale": False,
        "gate_token": "f4808a6a1b970cf6",
        "pushed_sha": "2" * 40,
        "pr_url": "https://github.com/owner/widget/pull/17",
        "ci_status": "merged",
        "created_at": "2026-07-29T03:54:13+00:00",
        "updated_at": "2026-07-29T05:24:25+00:00",
    }


def _schema_2_record() -> dict:
    """Shaped like a current schema-2 record that reused review and was aborted."""
    return {
        "schema_version": 2,
        "run_id": "r_0000000002",
        "seq": 9,
        "state": "ABORTED",
        "branch": "feature/widget",
        "base_ref": "main",
        "merge_base_sha": "3" * 40,
        "head_sha": "4" * 40,
        "source_head_sha": "4" * 40,
        "intent": PRIVATE_INTENT,
        "intent_source": "user",
        "source_worktree_id": "owner",
        "source_worktree_path": "/repos/widget-linked",
        "owner_ids": ["owner"],
        "config_snapshot": {
            "review": {"blocking_severities": ["critical"]},
            "docs": {"blocking_severities": ["high", "medium"]},
            "worktree": {"mode": "reusable"},
            "pr": {"mode": "manual", "automated_cleanup": False},
        },
        "config_digest": "e" * 64,
        "changed_files": ["src/app.py"],
        "review_coverage": {
            "manifest": "5" * 64,
            "head_sha": "4" * 40,
            "total_units": 3,
            "cited_units": ["U0001"],
            "clean_units": ["U0002", "U0003"],
            "excluded_files": ["uv.lock"],
        },
        "risk": {
            "level": "high",
            "verdict": "needs_human",
            "requires_human_review": True,
            "reasons": [{"kind": "human_review_path", "level": "high", "path": "x"}],
        },
        "fix_commits": ["6" * 40, "7" * 40],
        "validation_restarts": 1,
        "stages": {
            "lint": {
                "status": "red",
                "attempts": 2,
                "reason": "exit 1: QUOTED COMMAND OUTPUT",
                "finished_at": "2026-10-11T19:57:28+00:00",
            },
            "review": {
                "status": "green",
                "attempts": 0,
                "executor": "command",
                "finished_at": "2026-10-11T19:57:19+00:00",
            },
        },
        "evidence": {
            "review": {"version": 1, "derivation_reason": "equivalent_inputs"},
        },
        "orphaned_reason": None,
        "pushed_sha": None,
        "created_at": "2026-10-11T19:50:00+00:00",
        "updated_at": "2026-10-11T20:10:00+00:00",
    }


def _findings() -> list[dict]:
    base = {"path": "src/app.py", "title": "t", "detail": "SECRET FINDING DETAIL"}
    return [
        {
            **base,
            "id": "F001",
            "stage": "review",
            "severity": "high",
            "status": "fixed",
            "action": "auto_fix",
        },
        {
            **base,
            "id": "F002",
            "stage": "review",
            "severity": "critical",
            "status": "open",
            "action": "ask_user",
        },
        {
            **base,
            "id": "F003",
            "stage": "docs",
            "severity": "medium",
            "status": "accepted",
            "action": "no_op",
        },
        {
            **base,
            "id": "F004",
            "stage": "docs",
            "severity": "low",
            "status": "dismissed",
            "action": "no_op",
        },
    ]


def test_a_schema_1_record_summarizes_with_unknown_values_left_null():
    events = [
        {"event": "run_created", "intent": PRIVATE_INTENT, "at": "2026-07-29T03:54:13+00:00"},
        {"event": "lint_finished"},
        {"event": "pushed", "sha": "2" * 40},
        {"event": "finished"},
        {"event": "pr_updated", "url": "https://github.com/owner/widget/pull/17"},
        {"event": "ci_checked", "outcome": "pending", "intent": PRIVATE_INTENT},
        {"event": "ci_checked", "outcome": "merged", "intent": PRIVATE_INTENT},
    ]

    summary = telemetry.summarize(_schema_1_record(), None, events, _identity())
    row = summary.model_dump(mode="json")

    assert row["kind"] == "agentic-preflight-run-summary"
    assert row["schema_version"] == 1
    assert row["record_schema_version"] == 1
    assert row["state"] == "DONE"
    assert row["finished_at"] == "2026-07-29T05:24:25+00:00"
    assert row["duration_seconds"] == 5412.0
    assert row["validation_restarts"] is None
    assert row["fix_commits"] == 0
    assert row["producer"] is None
    assert row["risk"] is None
    assert row["review"] is None
    assert row["pr_mode"] is None
    assert row["worktree_mode"] == "in_place"
    assert row["findings"] == dict.fromkeys(
        ("total", "by_stage", "by_severity", "by_status", "by_action", "blocking")
    )
    assert row["stages"]["test"] == {
        "status": "skipped",
        "attempts": 0,
        "finished_at": "2026-07-29T03:54:23+00:00",
        "reused": None,
        "reason": "changes are limited to documentation and CI configuration",
    }
    assert list(row["stages"]) == ["lint", "test"]
    assert row["hosted"] == {"last_ci_outcome": "merged", "pr_url_recorded": True}
    assert row["event_counts"] == {
        "ci_checked": 2,
        "lint_finished": 1,
        "pr_updated": 1,
        "pushed": 1,
        "run_created": 1,
    }
    assert row["intent_sha256"] is not None
    assert PRIVATE_INTENT not in summary.model_dump_json()


def test_a_schema_2_record_summarizes_reuse_review_findings_and_terminal_facts():
    events = [
        {"event": "state_transition", "to": "REVIEW_AWAITING_FINDINGS", "seq": 3},
        {"event": "stage_reused", "stage": "review"},
        {"event": "state_transition", "to": "ABORTED", "at": "2026-10-11T20:05:00+00:00", "seq": 8},
        {"event": "aborted", "forced": True, "seq": 8},
        {"event": "summary_written", "summary_path": "x"},
    ]

    row = telemetry.summarize(_schema_2_record(), _findings(), events, _identity()).model_dump()

    assert row["record_schema_version"] == 2
    assert row["repo"]["source_worktree_path"] == "/repos/widget"
    # A later write moved updated_at; the terminal transition keeps the end time.
    assert row["finished_at"] == "2026-10-11T20:05:00+00:00"
    assert row["duration_seconds"] == 900.0
    assert row["stages"]["review"]["reused"] is True
    assert row["stages"]["lint"]["reused"] is False
    assert row["stages"]["lint"]["reason"] is None
    assert list(row["stages"]) == ["review", "lint"]
    assert row["review"] == {
        "total_units": 3,
        "cited_count": 1,
        "clean_count": 2,
        "excluded_count": 1,
        "executor": "command",
    }
    assert row["risk"] == {"level": "high", "verdict": "needs_human", "requires_human_review": True}
    assert row["findings"] == {
        "total": 4,
        "by_stage": {"docs": 2, "review": 2},
        "by_severity": {"critical": 1, "high": 1, "low": 1, "medium": 1},
        "by_status": {"accepted": 1, "dismissed": 1, "fixed": 1, "open": 1},
        "by_action": {"ask_user": 1, "auto_fix": 1, "no_op": 2},
        # review blocks only critical here, docs blocks high and medium.
        "blocking": 2,
    }
    assert row["fix_commits"] == 2
    assert row["validation_restarts"] == 1
    assert row["terminal"] == {"aborted_forced": True, "orphaned_reason": None}
    assert row["hosted"] == {"last_ci_outcome": None, "pr_url_recorded": False}
    assert row["event_counts"] == {"stage_reused": 1, "state_transition": 2}
    assert row["worktree_mode"] == "reusable"
    assert row["pr_mode"] == "manual"
    text = json.dumps(row)
    assert PRIVATE_INTENT not in text
    assert "QUOTED COMMAND OUTPUT" not in text
    assert "SECRET FINDING DETAIL" not in text


def test_unavailable_events_are_null_rather_than_zero():
    row = telemetry.summarize(_schema_2_record(), [], None, _identity()).model_dump()

    assert row["event_counts"] is None
    assert row["terminal"]["aborted_forced"] is None
    assert row["hosted"] == {"last_ci_outcome": None, "pr_url_recorded": None}
    assert row["findings"]["total"] == 0
    assert row["finished_at"] == "2026-10-11T20:10:00+00:00"


def test_a_retired_delegated_record_summarizes_as_the_orphan_the_tool_reads():
    record = {**_schema_2_record(), "state": "TEST_DELEGATED", "test_delegation": {}}

    row = telemetry.summarize(record, None, [], _identity()).model_dump()

    assert row["state"] == "ORPHANED"
    assert "delegation was removed" in row["terminal"]["orphaned_reason"]


def test_a_record_missing_required_identity_fields_is_invalid():
    record = _schema_2_record()
    del record["branch"]

    with pytest.raises(ValueError, match="branch"):
        telemetry.summarize(record, None, [], _identity())


class _Producer(BaseModel):
    tool_version: str
    client: str | None = None


class _RunWithProducer(RunDoc):
    producer: _Producer | None = None


def test_a_loaded_run_document_and_its_producer_block_are_summarized():
    base = make_run().model_dump()
    base.update(
        state=State.DONE, pushed_sha="b" * 40, updated_at="2026-01-01T00:02:00+00:00", seq=4
    )
    run = _RunWithProducer.model_validate(
        {**base, "producer": {"tool_version": "9.9.9", "client": "claude-code"}}
    )

    row = telemetry.summarize(run, [], [], _identity()).model_dump()

    assert row["producer"] == {"tool_version": "9.9.9", "client": "claude-code"}
    assert row["validation_restarts"] == 0
    assert row["duration_seconds"] == 120.0
    plain = make_run().model_copy(update={"updated_at": "2026-01-01T00:01:00+00:00"})
    assert telemetry.summarize(plain, [], [], _identity()).producer is None


# -- the store hook ----------------------------------------------------------


def test_the_store_hook_fires_once_after_the_terminal_commit(tmp_path):
    store = Store(tmp_path / "agentic-preflight")
    store.create_run(make_run())
    seen: list[tuple[str, str]] = []
    store.on_terminal = lambda run: seen.append(
        (run.state.value, json.loads(store.run_path(run.run_id).read_text())["state"])
    )

    def rolled_back() -> None:
        with store.transaction("r_abc123") as doc:
            doc.state = State.ABORTED
            raise RuntimeError("rolled back")

    with store.transaction("r_abc123") as doc:
        doc.stale = True
    with pytest.raises(RuntimeError, match="rolled back"):
        rolled_back()
    with store.transaction("r_abc123", events=[{"event": "aborted", "forced": False}]) as doc:
        doc.state = State.ABORTED
    with store.transaction("r_abc123") as doc:
        doc.worktree_released = True

    # Once, and only after the terminal record is durably on disk.
    assert seen == [("ABORTED", "ABORTED")]
    marker = [e for e in store.load_events("r_abc123") if e["event"] == "aborted"]
    assert marker == [{"at": marker[0]["at"], "event": "aborted", "forced": False, "seq": 2}]


def test_a_failing_hook_never_undoes_the_commit(tmp_path, capsys):
    store = Store(tmp_path / "agentic-preflight")
    store.create_run(make_run())

    def broken(run):
        raise RuntimeError("telemetry exploded")

    store.on_terminal = broken
    with store.transaction("r_abc123") as doc:
        doc.state = State.ABORTED

    assert store.load_run("r_abc123").state is State.ABORTED
    assert "telemetry exploded" in capsys.readouterr().err


# -- every terminal path writes once ------------------------------------------


def _summary_events(repo: Path, run_id: str) -> list[dict]:
    return [
        event
        for event in _events(repo, run_id)
        if event["event"] in {"summary_written", "summary_failed"}
    ]


def test_abort_writes_one_summary_and_mirror_row_without_the_intent(agent, feature_repo):
    run_id = agent.run("start", "--intent", PRIVATE_INTENT)["run_id"]

    aborted = agent.run("abort", "--force")
    agent.run("status")
    agent.run("gc")

    report = aborted["data"]["telemetry"]
    assert report["run_id"] == run_id
    assert report["error"] is None
    assert report["mirrored"] is True
    assert report["mirror_path"] == str(telemetry.mirror_path())
    summary = _summary(feature_repo, run_id)
    assert report["summary_path"].endswith("summary.json")
    assert summary["state"] == "ABORTED"
    assert summary["terminal"]["aborted_forced"] is True
    assert summary["repo"]["root_commit"] == telemetry.repo_identity(feature_repo).root_commit
    assert [event["event"] for event in _summary_events(feature_repo, run_id)] == [
        "summary_written"
    ]
    rows = _mirror_rows()
    assert [row["run_id"] for row in rows] == [run_id]
    assert rows[0] == summary
    mirror_text = telemetry.mirror_path().read_text(encoding="utf-8")
    assert PRIVATE_INTENT not in mirror_text
    assert PRIVATE_INTENT not in json.dumps(summary)


def test_finish_writes_one_summary_and_reports_it(feature_repo, bare_remote, tmp_path):
    agent = green_run(feature_repo, tmp_path)
    token = agent.run("gate")["data"]["token"]
    run_id = agent.run("push", "--confirm", token)["run_id"]

    finished = agent.run("finish")
    agent.run("gc")

    assert finished["data"]["telemetry"]["run_id"] == run_id
    assert finished["data"]["telemetry"]["mirrored"] is True
    summary = _summary(feature_repo, run_id)
    assert summary["state"] == "DONE"
    assert summary["pushed_sha"] == git("rev-parse", "HEAD", cwd=feature_repo)
    assert summary["stages"]["review"]["status"] == "green"
    assert summary["review"]["executor"] == "in_harness"
    assert summary["findings"]["total"] == 0
    assert summary["event_counts"]["pushed"] == 1
    assert "finished" not in summary["event_counts"]
    assert summary["repo"]["name"] == "remote"
    assert len(_summary_events(feature_repo, run_id)) == 1
    assert [row["run_id"] for row in _mirror_rows()] == [run_id]


def test_start_replace_orphans_the_old_run_with_one_summary(agent, feature_repo):
    first = agent.run("start")["run_id"]

    second = agent.run("start", "--replace", "--intent", "a different objective")["run_id"]

    summary = _summary(feature_repo, first)
    assert summary["state"] == "ORPHANED"
    assert summary["terminal"]["orphaned_reason"] == "replaced by a new start"
    assert len(_summary_events(feature_repo, first)) == 1
    assert not (_state_root(feature_repo) / "runs" / second / "summary.json").exists()
    assert [row["run_id"] for row in _mirror_rows()] == [first]


def test_gc_orphaning_writes_one_summary_and_lists_it(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    for pointer in (_state_root(feature_repo) / "active").glob("*.run"):
        if pointer.read_text(encoding="utf-8").strip() == run_id:
            pointer.unlink()

    collected = agent.run("gc")
    again = agent.run("gc")

    assert [report["run_id"] for report in collected["data"]["telemetry"]] == [run_id]
    assert again["data"]["telemetry"] == []
    summary = _summary(feature_repo, run_id)
    assert summary["state"] == "ORPHANED"
    assert summary["terminal"]["orphaned_reason"] == "source worktree lease disappeared"
    assert len(_summary_events(feature_repo, run_id)) == 1


def test_mirror_off_keeps_the_summary_in_the_run_directory(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[telemetry]\nmirror = false\n")
    commit_all(feature_repo, "keep summaries local")
    run_id = agent.run("start")["run_id"]

    report = agent.run("abort")["data"]["telemetry"]

    assert report["mirrored"] is False
    assert report["mirror_path"] is None
    assert report["error"] is None
    assert _summary(feature_repo, run_id)["state"] == "ABORTED"
    assert not telemetry.mirror_path().exists()


def test_an_unwritable_mirror_never_fails_the_command(agent, feature_repo, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    run_id = agent.run("start")["run_id"]

    aborted = agent.run("abort")

    assert aborted["ok"] is True
    assert aborted["state"] == "ABORTED"
    report = aborted["data"]["telemetry"]
    assert report["mirrored"] is False
    assert report["error"].startswith("mirror: ")
    assert _summary(feature_repo, run_id)["state"] == "ABORTED"
    (event,) = _summary_events(feature_repo, run_id)
    assert event["event"] == "summary_written"
    assert event["mirrored"] is False
    assert event["error"] == report["error"]


def test_an_unexpected_summary_failure_is_recorded_and_the_command_succeeds(
    agent, feature_repo, monkeypatch
):
    run_id = agent.run("start")["run_id"]

    def broken(*args, **kwargs):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(telemetry, "summarize", broken)
    aborted = agent.run("abort")

    assert aborted["state"] == "ABORTED"
    assert aborted["data"]["telemetry"]["summary_path"] is None
    assert "summary exploded" in aborted["data"]["telemetry"]["error"]
    (event,) = _summary_events(feature_repo, run_id)
    assert event["event"] == "summary_failed"
    assert not (_state_root(feature_repo) / "runs" / run_id / "summary.json").exists()


# -- rebuild -----------------------------------------------------------------


def _store_record(repo: Path, record: dict, *, findings=None, events=None) -> Path:
    run_dir = _state_root(repo) / "runs" / record["run_id"]
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    if findings is not None:
        (run_dir / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    lines = "".join(json.dumps(event) + "\n" for event in events or [])
    (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")
    return run_dir


def _row(run_id: str, root: str, written_at: str, **extra) -> str:
    repo = {"root_commit": root, "source_worktree_path": "/x"}
    return json.dumps({"run_id": run_id, "repo": repo, "written_at": written_at, **extra})


def test_rebuild_regenerates_summaries_and_rewrites_only_this_repositorys_rows(tmp_repo):
    root = git("rev-list", "--max-parents=0", "HEAD", cwd=tmp_repo)
    _store_record(tmp_repo, _schema_1_record())
    _store_record(tmp_repo, _schema_2_record(), findings=_findings())
    active = {**_schema_2_record(), "run_id": "r_0000000003", "state": "REVIEW_GREEN"}
    _store_record(tmp_repo, active)
    broken = _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000004"})
    (broken / "run.json").write_text('{"truncated": ', encoding="utf-8")

    other_root = "9" * 40
    other_repo = (
        f'{{"run_id":"r_0000000001","repo":{{"root_commit":"{other_root}"}},"written_at":"x"}}'
    )
    mirror = telemetry.mirror_path()
    mirror.parent.mkdir(parents=True)
    mirror.write_text(
        "\n".join(
            [
                other_repo,
                "not a summary row",
                _row("r_0000000001", root, "2020-01-01T00:00:00+00:00", stale=True),
                _row("r_0000000004", root, "2020-01-01T00:00:00+00:00", copy="old"),
                _row("r_0000000004", root, "2021-01-01T00:00:00+00:00", copy="new"),
                _row("r_000000000f", root, "2021-01-01T00:00:00+00:00", clone="other"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")
    again = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")

    data = result["data"]
    assert (data["summarized"], data["skipped"], data["unreadable"]) == (2, 1, 1)
    assert data["unreadable_runs"][0]["run_id"] == "r_0000000004"
    assert data["unreadable_runs"][0]["reason"] == "invalid"
    assert data["repo"]["root_commit"] == root
    assert data["mirror"] == {
        "enabled": True,
        "path": str(mirror),
        "rows": 4,
        "preserved_rows": 2,
        "error": None,
    }
    assert result["next"]["instruction"].startswith("Some run records could not be read")
    assert again["data"]["mirror"]["rows"] == 4

    lines = mirror.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == [other_repo, "not a summary row"]
    rows = [json.loads(line) for line in lines[2:]]
    by_run = {row["run_id"]: row for row in rows}
    assert sorted(by_run) == ["r_0000000001", "r_0000000002", "r_0000000004", "r_000000000f"]
    assert len(rows) == 4
    assert by_run["r_0000000004"]["copy"] == "new"
    assert by_run["r_000000000f"]["clone"] == "other"
    assert "stale" not in by_run["r_0000000001"]
    assert by_run["r_0000000001"]["record_schema_version"] == 1
    assert by_run["r_0000000001"]["repo"]["source_worktree_path"] == str(tmp_repo)
    assert by_run["r_0000000002"]["findings"]["total"] == 4
    assert by_run["r_0000000002"]["repo"]["source_worktree_path"] == "/repos/widget-linked"

    for run_id in ("r_0000000001", "r_0000000002"):
        assert _summary(tmp_repo, run_id) == by_run[run_id]
    assert not (_state_root(tmp_repo) / "runs" / "r_0000000003" / "summary.json").exists()
    assert PRIVATE_INTENT not in mirror.read_text(encoding="utf-8")
    # The rebuild changes no run record and appends no events.
    assert json.loads((broken.parent / "r_0000000001" / "run.json").read_text()) == (
        _schema_1_record()
    )
    assert len(_events(tmp_repo, "r_0000000002")) == 0


def test_rebuild_with_the_mirror_off_writes_summaries_only(tmp_repo):
    write(tmp_repo, ".agentic-preflight.toml", "[telemetry]\nmirror = false\n")
    _store_record(tmp_repo, _schema_2_record())

    result = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")

    assert result["data"]["summarized"] == 1
    assert result["data"]["mirror"]["enabled"] is False
    assert result["next"] is None
    assert _summary(tmp_repo, "r_0000000002")["state"] == "ABORTED"
    assert not telemetry.mirror_path().exists()


def test_rebuild_reports_a_mirror_it_cannot_write(tmp_repo, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    _store_record(tmp_repo, _schema_2_record())

    failed = ScriptedAgent(tmp_repo).run("telemetry", "rebuild", expect=ExitCode.USAGE)

    assert failed["error"]["code"] == "telemetry_write_failed"
    assert failed["data"]["summarized"] == 1
    assert failed["data"]["mirror"]["error"]
    assert _summary(tmp_repo, "r_0000000002")["state"] == "ABORTED"


def test_rebuild_matches_the_summary_written_when_the_run_ended(agent, feature_repo, tmp_path):
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    run_id = agent.run("abort", "--force")["run_id"]
    original = _summary(feature_repo, run_id)

    agent.run("telemetry", "rebuild")

    rebuilt = _summary(feature_repo, run_id)
    assert {**rebuilt, "written_at": None} == {**original, "written_at": None}
    assert [row["run_id"] for row in _mirror_rows()] == [run_id]


def test_a_start_that_aborts_its_own_run_still_writes_one_summary(feature_repo, bare_remote):
    git("push", "origin", "HEAD:main", cwd=feature_repo)

    refused = ScriptedAgent(feature_repo).run("start", expect=ExitCode.PRECONDITION)

    assert refused["error"]["code"] == "empty_diff"
    (run_dir,) = (_state_root(feature_repo) / "runs").iterdir()
    summary = _summary(feature_repo, run_dir.name)
    assert summary["state"] == "ABORTED"
    assert summary["terminal"]["aborted_forced"] is None
    assert len(_summary_events(feature_repo, run_dir.name)) == 1
    assert [row["run_id"] for row in _mirror_rows()] == [run_dir.name]


def test_rebuild_leaves_an_undecodable_mirror_unchanged(tmp_repo):
    _store_record(tmp_repo, _schema_2_record())
    mirror = telemetry.mirror_path()
    mirror.parent.mkdir(parents=True)
    original = b'{"run_id": "r_0000000002"}\n\xff\xfe not text\n'
    mirror.write_bytes(original)

    failed = ScriptedAgent(tmp_repo).run("telemetry", "rebuild", expect=ExitCode.USAGE)

    assert "UnicodeDecodeError" in failed["data"]["mirror"]["error"]
    assert mirror.read_bytes() == original
