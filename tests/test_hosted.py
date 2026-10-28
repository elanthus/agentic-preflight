"""Hosted outcomes joined onto pushed runs: ``agentic-preflight telemetry sync-hosted``.

Every test runs a fake ``gh``. The autouse fixture fails any test that would
start the real one, so the suite never reaches the network.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

import agentic_preflight
from agentic_preflight import hosted, report, telemetry
from agentic_preflight.envelope import ExitCode
from agentic_preflight.store import Store
from tests.conftest import git, write
from tests.driver import ScriptedAgent
from tests.test_report import make_row
from tests.test_telemetry import _schema_2_record, _state_root, _store_record

#: No fixture repository has a network remote, so the product keeps every one out of
#: the default telemetry location. These tests are about what the files record, so
#: they opt in with an explicit ``[telemetry] path``.
pytestmark = pytest.mark.usefixtures("telemetry_opt_in")

PROJECT_ROOT = Path(__file__).parent.parent
SHA_NEW = "a1" * 20
SHA_MID = "b2" * 20
SHA_OLD = "c3" * 20
TOKEN = "gho_" + "Z" * 36
SYNCED_KEYS = ("pr_number", "pr_state", "merged", "checks_conclusion", "fetched_at")
#: The real runner factory, kept before the autouse fixture replaces it.
REAL_GH_RUNNER = hosted.gh_runner


# -- the fake gh ------------------------------------------------------------------


def _ok(payload: Any) -> hosted.GhResult:
    return hosted.GhResult(0, json.dumps(payload), "")


def _http_error(status: int, message: str) -> hosted.GhResult:
    body = json.dumps({"message": message, "status": str(status)})
    return hosted.GhResult(1, body, f"gh: {message} (HTTP {status})\n")


def _pull(
    number: int,
    sha: str,
    *,
    ref: str = "feature/widget",
    state: str = "closed",
    merged_at: str | None = "2026-10-02T00:00:00Z",
) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/widget/pull/{number}",
        "state": state,
        "merged_at": merged_at,
        "head": {"sha": sha, "ref": ref},
    }


def _check(name: str, conclusion: str | None = "success", status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


class FakeGh:
    """Answers ``gh api`` like GitHub would, from a table of commits."""

    def __init__(
        self,
        commits: dict[str, dict],
        overrides: dict[str, Any] | None = None,
        branch_pulls: dict[str, list] | None = None,
    ) -> None:
        self.commits = commits
        self.overrides = overrides or {}
        self.branch_pulls = branch_pulls or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: Any) -> hosted.GhResult:
        args = list(args)
        self.calls.append(args)
        assert args[:5] == ["api", "--method", "GET", "--hostname", "github.com"]
        by_branch = re.fullmatch(
            r"/repos/owner/widget/pulls\?state=all&head=owner%3A([^&]+)&per_page=100", args[-1]
        )
        if by_branch:
            return _ok(self.branch_pulls.get(unquote(by_branch.group(1)), []))
        match = re.fullmatch(
            r"/repos/owner/widget/commits/([0-9a-f]{40})/(pulls|check-runs)\?(.*)", args[-1]
        )
        assert match, args
        sha, kind, _ = match.groups()
        override = self.overrides.get(sha)
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        if sha not in self.commits:
            return _http_error(422, f"No commit found for SHA: {sha}")
        commit = self.commits[sha]
        if kind == "pulls":
            return _ok(commit.get("pulls", []))
        checks = commit.get("checks", [])
        return _ok({"total_count": len(checks), "check_runs": checks})

    def fetched(self) -> list[str]:
        """The commits asked about, in order, one entry per commit."""
        found = (re.search(r"commits/([0-9a-f]{40})/", call[-1]) for call in self.calls)
        return list(dict.fromkeys(match.group(1) for match in found if match))


@pytest.fixture(autouse=True)
def no_real_gh(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("a test reached the real gh")

    monkeypatch.setattr(hosted, "gh_runner", refuse)


def _use(monkeypatch, fake: Any) -> None:
    monkeypatch.setattr(hosted, "gh_runner", lambda *args, **kwargs: fake)


# -- a clone with pushed runs ---------------------------------------------------------


def _done(run_id: str, sha: str | None, created_at: str, **extra: Any) -> dict[str, Any]:
    return {
        **_schema_2_record(),
        "run_id": run_id,
        "state": "DONE",
        "branch": "feature/widget",
        "pushed_sha": sha,
        "created_at": created_at,
        "updated_at": created_at.replace("T10", "T11"),
        **extra,
    }


@pytest.fixture
def github_repo(tmp_repo: Path) -> Path:
    """A clone whose origin is on github.com, with credentials in the URL."""
    url = "https://x-access-token:s3cr3t-remote@github.com/owner/widget.git"
    git("remote", "add", "origin", url, cwd=tmp_repo)
    _store_record(tmp_repo, _done("r_0000000001", SHA_OLD, "2026-10-01T10:00:00+00:00"))
    _store_record(tmp_repo, _done("r_0000000002", SHA_MID, "2026-10-02T10:00:00+00:00"))
    _store_record(tmp_repo, _done("r_0000000003", SHA_NEW, "2026-10-03T10:00:00+00:00"))
    _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000004"})
    _store_record(tmp_repo, _done("r_0000000005", None, "2026-10-04T10:00:00+00:00"))
    return tmp_repo


def _commits() -> dict[str, dict]:
    return {
        SHA_NEW: {
            "pulls": [_pull(9, SHA_NEW, merged_at=None, state="open")],
            "checks": [_check("lint"), _check("test", "failure"), _check("test", "failure")],
        },
        SHA_MID: {"pulls": [], "checks": []},
        SHA_OLD: {
            "pulls": [_pull(3, "f" * 40, ref="other"), _pull(4, SHA_OLD)],
            "checks": [_check("lint"), _check("docs", "skipped"), _check("test", "neutral")],
        },
    }


def _run_dir(repo: Path, run_id: str) -> Path:
    return _state_root(repo) / "runs" / run_id


def _hosted_file(repo: Path, run_id: str) -> dict[str, Any] | None:
    path = _run_dir(repo, run_id) / "hosted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _summary_hosted(repo: Path, run_id: str) -> dict[str, Any]:
    path = _run_dir(repo, run_id) / "summary.json"
    return json.loads(path.read_text(encoding="utf-8"))["hosted"]


def _mirror_rows() -> dict[str, dict]:
    path = telemetry.mirror_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rows = [json.loads(line) for line in lines if line.startswith("{")]
    return {row["run_id"]: row for row in rows if "hosted" in row}


# -- one commit -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], {"conclusion": "none", "total": 0, "failed": []}),
        (
            [_check("a"), _check("b", "skipped"), _check("c", "neutral")],
            {"conclusion": "success", "total": 3, "failed": []},
        ),
        (
            [_check("a"), _check("b", None, "in_progress")],
            {"conclusion": "pending", "total": 2, "failed": []},
        ),
        (
            [_check("z", "timed_out"), _check("b", None, "queued"), _check("a", "cancelled")],
            {"conclusion": "failure", "total": 3, "failed": ["a", "z"]},
        ),
        (
            [_check("t", "failure"), _check("t", "failure"), {"conclusion": "failure"}],
            {"conclusion": "failure", "total": 3, "failed": ["t", "unnamed"]},
        ),
    ],
)
def test_check_runs_reduce_to_one_conclusion(runs, expected):
    assert hosted.reduce_checks(runs) == expected


def test_the_pull_request_whose_head_is_the_commit_wins():
    by_branch = _pull(1, "f" * 40, ref="feature/widget")
    exact = _pull(2, SHA_NEW, ref="elsewhere")
    first = _pull(3, "e" * 40, ref="unrelated")

    assert hosted.choose_pull_request([by_branch, exact], SHA_NEW, "feature/widget")["number"] == 2
    assert hosted.choose_pull_request([first, by_branch], SHA_NEW, "feature/widget")["number"] == 1
    assert hosted.choose_pull_request([first, "junk"], SHA_NEW, None) == {
        "number": 3,
        "url": "https://github.com/owner/widget/pull/3",
        "state": "closed",
        "merged_at": "2026-10-02T00:00:00Z",
    }
    assert hosted.choose_pull_request([], SHA_NEW, "feature/widget") is None
    with pytest.raises(hosted.GhError, match="not a list"):
        hosted.choose_pull_request({"message": "x"}, SHA_NEW, None)


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("github.com/owner/widget", "owner/widget"),
        ("github.com/owner", None),
        ("github.com/owner/widget/extra", None),
        ("github.company.com/owner/widget", None),
        ("gitlab.com/owner/widget", None),
        ("/srv/git/widget", None),
        (None, None),
    ],
)
def test_only_a_github_com_remote_has_a_slug(remote, slug):
    assert hosted.github_slug(remote) == slug


def test_check_runs_are_read_page_by_page_until_the_total():
    pages = {
        1: [_check(f"c{index}") for index in range(100)],
        2: [_check("late", "failure")],
    }
    calls: list[str] = []

    def runner(args):
        calls.append(args[-1])
        page = int(re.search(r"page=(\d+)$", args[-1]).group(1))
        return _ok({"total_count": 101, "check_runs": pages[page]})

    checks = hosted.reduce_checks(hosted._check_runs(runner, "owner/widget", SHA_NEW))

    assert checks == {"conclusion": "failure", "total": 101, "failed": ["late"]}
    assert [call.rsplit("page=", 1)[1] for call in calls] == ["1", "2"]


@pytest.mark.parametrize(
    ("result", "reason", "status"),
    [
        (
            hosted.GhResult(4, "", "To get started with GitHub CLI, please run:  gh auth login"),
            "gh_unavailable",
            None,
        ),
        (_http_error(401, "Bad credentials"), "gh_unavailable", 401),
        (_http_error(403, "API rate limit exceeded for user ID 1."), "rate_limited", 403),
        (_http_error(429, "Too many requests"), "rate_limited", 429),
        (
            _http_error(403, "Resource protected by organization SAML enforcement."),
            "forbidden",
            403,
        ),
        (_http_error(404, "Not Found"), "not_found", 404),
        (_http_error(422, "No commit found for SHA: x"), "commit_not_found", 422),
        (_http_error(502, "Bad Gateway"), "http_error", 502),
        (hosted.GhResult(1, "", "error connecting to api.github.com\n"), "network_error", None),
        (hosted.GhResult(1, "", "something odd\n"), "request_failed", None),
        (hosted.GhResult(0, "<html>", ""), "invalid_response", None),
    ],
)
def test_gh_failures_are_classified_without_copying_gh_output(result, reason, status):
    leaked = hosted.GhResult(
        result.returncode, result.stdout, result.stderr + f"\ntoken {TOKEN} RAW-STDERR\n"
    )

    with pytest.raises(hosted.GhError) as caught:
        hosted._api(lambda args: leaked, "/repos/owner/widget/commits/x/pulls")

    assert (caught.value.reason, caught.value.http_status) == (reason, status)
    assert TOKEN not in caught.value.message
    assert "RAW-STDERR" not in caught.value.message


def test_a_token_in_githubs_own_message_is_redacted():
    result = _http_error(500, f"oops {TOKEN} at https://user:pw@github.com/owner/widget")

    with pytest.raises(hosted.GhError) as caught:
        hosted._api(lambda args: result, "/repos/owner/widget/commits/x/pulls")

    assert TOKEN not in caught.value.message
    assert "pw@" not in caught.value.message
    assert "[redacted]" in caught.value.message


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(subprocess.TimeoutExpired("gh", 60), "network_error"), (OSError("gone"), "gh_unavailable")],
)
def test_a_gh_process_that_cannot_answer_is_a_gh_error(exc, reason):
    def runner(args):
        raise exc

    with pytest.raises(hosted.GhError) as caught:
        hosted._api(runner, "/repos/owner/widget/commits/x/pulls")

    assert caught.value.reason == reason


def test_the_gh_runner_resolves_gh_on_path_and_never_prompts(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(hosted, "resolve_on_path", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(hosted.subprocess, "run", fake_run)

    runner = REAL_GH_RUNNER(timeout=5)
    assert runner is not None
    assert runner(["api", "/x"]) == hosted.GhResult(0, "[]", "")
    assert seen["argv"] == ["/opt/bin/gh", "api", "/x"]
    assert seen["timeout"] == 5
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"
    assert seen["stdin"] is subprocess.DEVNULL

    monkeypatch.setattr(hosted, "resolve_on_path", lambda name: None)
    assert REAL_GH_RUNNER() is None


# -- the command ----------------------------------------------------------------------


def test_sync_writes_hosted_json_and_regenerates_summaries_and_mirror_rows(
    github_repo, monkeypatch
):
    root = git("rev-list", "--max-parents=0", "HEAD", cwd=github_repo)
    mirror = telemetry.mirror_path()
    mirror.parent.mkdir(parents=True)
    other = make_row("r_0000000009", repo={**make_row("x")["repo"], "root_commit": "9" * 40})
    mine_unsynced = make_row(
        "r_0000000008", repo={**make_row("x")["repo"], "root_commit": root, "name": "repo"}
    )
    mirror.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in (other, mine_unsynced)),
        encoding="utf-8",
    )
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted")

    data = result["data"]
    assert data["repo"]["remote"] == "github.com/owner/widget"
    assert (data["eligible"], data["selected"], data["synced"], data["deferred"]) == (3, 3, 3, 0)
    assert data["not_eligible"] == {"not_done": 1, "no_pushed_sha": 1}
    assert data["stopped"] is None
    assert data["gh_found"] is True
    # Newest first.
    assert fake.fetched() == [SHA_NEW, SHA_MID, SHA_OLD]
    assert [run["run_id"] for run in data["runs"]] == [
        "r_0000000003",
        "r_0000000002",
        "r_0000000001",
    ]
    assert result["next"]["command"] == "agentic-preflight report"

    newest = _hosted_file(github_repo, "r_0000000003")
    assert set(newest) == {"fetched_at", "sha", "pr", "checks", "missing", "source"}
    assert newest["sha"] == SHA_NEW
    assert newest["source"] == "gh"
    assert newest["missing"] is False
    assert newest["pr"] == {
        "number": 9,
        "url": "https://github.com/owner/widget/pull/9",
        "state": "open",
        "merged_at": None,
    }
    assert newest["checks"] == {"conclusion": "failure", "total": 3, "failed": ["test"]}
    assert _hosted_file(github_repo, "r_0000000002")["pr"] is None
    assert _hosted_file(github_repo, "r_0000000002")["checks"]["conclusion"] == "none"
    assert _hosted_file(github_repo, "r_0000000001")["pr"]["number"] == 4

    assert _summary_hosted(github_repo, "r_0000000003") == {
        "pr_number": 9,
        "pr_state": "open",
        "merged": False,
        "checks_conclusion": "failure",
        "fetched_at": newest["fetched_at"],
    }
    assert _summary_hosted(github_repo, "r_0000000002")["merged"] is None
    assert _summary_hosted(github_repo, "r_0000000002")["checks_conclusion"] == "none"
    assert _summary_hosted(github_repo, "r_0000000001")["merged"] is True
    assert _summary_hosted(github_repo, "r_0000000001")["checks_conclusion"] == "success"
    assert not (_run_dir(github_repo, "r_0000000004") / "hosted.json").exists()
    assert not (_run_dir(github_repo, "r_0000000004") / "summary.json").exists()

    summaries = data["summaries"]
    assert summaries["regenerated"] == 3
    assert summaries["mirror"]["enabled"] is True
    assert summaries["mirror"]["error"] is None
    rows = _mirror_rows()
    assert sorted(rows) == [
        "r_0000000001",
        "r_0000000002",
        "r_0000000003",
        "r_0000000008",
        "r_0000000009",
    ]
    assert rows["r_0000000003"]["hosted"]["checks_conclusion"] == "failure"
    assert rows["r_0000000003"] == json.loads(
        (_run_dir(github_repo, "r_0000000003") / "summary.json").read_text(encoding="utf-8")
    )
    assert mirror.read_text(encoding="utf-8").splitlines()[0] == json.dumps(other, sort_keys=True)
    # The run records and event logs are unchanged.
    for run_id in ("r_0000000001", "r_0000000002", "r_0000000003"):
        assert (_run_dir(github_repo, run_id) / "events.jsonl").read_text() == ""
    assert "s3cr3t-remote" not in json.dumps(result)


def test_a_second_sync_skips_synced_runs_and_refresh_fetches_them_again(github_repo, monkeypatch):
    commits = _commits()
    _use(monkeypatch, FakeGh(commits))
    agent = ScriptedAgent(github_repo)
    agent.run("telemetry", "sync-hosted")
    first = _hosted_file(github_repo, "r_0000000003")

    fake = FakeGh(commits)
    _use(monkeypatch, fake)
    again = agent.run("telemetry", "sync-hosted")

    assert fake.calls == []
    assert (again["data"]["already_synced"], again["data"]["synced"]) == (3, 0)
    assert again["data"]["summaries"] is None
    assert again["next"] is None

    commits[SHA_NEW]["checks"] = [_check("lint"), _check("test")]
    refreshed = agent.run("telemetry", "sync-hosted", "--refresh", "--limit", "1")

    assert fake.fetched() == [SHA_NEW]
    assert refreshed["data"]["synced"] == 1
    assert refreshed["data"]["deferred"] == 2
    assert "--limit" in refreshed["next"]["instruction"]
    assert _hosted_file(github_repo, "r_0000000003")["checks"]["conclusion"] == "success"
    assert _summary_hosted(github_repo, "r_0000000003")["checks_conclusion"] == "success"
    assert _hosted_file(github_repo, "r_0000000003")["sha"] == first["sha"]


def test_limit_takes_the_newest_runs_and_run_selects_one(github_repo, monkeypatch):
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)
    agent = ScriptedAgent(github_repo)

    limited = agent.run("telemetry", "sync-hosted", "--limit", "2")
    assert fake.fetched() == [SHA_NEW, SHA_MID]
    assert (limited["data"]["selected"], limited["data"]["deferred"]) == (2, 1)

    fake.calls.clear()
    one = agent.run("telemetry", "sync-hosted", "--run", "r_0000000001")
    assert fake.fetched() == [SHA_OLD]
    assert (one["data"]["eligible"], one["data"]["synced"], one["data"]["run"]) == (
        1,
        1,
        "r_0000000001",
    )

    synced = agent.run("--run", "r_0000000001", "telemetry", "sync-hosted")
    assert synced["data"]["run"] == "r_0000000001"
    assert "--refresh" in synced["next"]["instruction"]

    ineligible = agent.run("telemetry", "sync-hosted", "--run", "r_0000000004")
    assert ineligible["data"]["not_eligible"] == {"not_done": 1, "no_pushed_sha": 0}
    assert "not a DONE run" in ineligible["next"]["instruction"]


def test_run_names_a_stored_run_in_the_expected_form(github_repo, monkeypatch):
    _use(monkeypatch, FakeGh({}))
    agent = ScriptedAgent(github_repo)

    missing = agent.run(
        "telemetry", "sync-hosted", "--run", "r_00000000ff", expect=ExitCode.PRECONDITION
    )
    malformed = agent.run("telemetry", "sync-hosted", "--run", "nope", expect=ExitCode.USAGE_ERROR)
    zero = agent.run("telemetry", "sync-hosted", "--limit", "0", expect=ExitCode.USAGE_ERROR)

    assert missing["error"]["code"] == "no_run"
    assert missing["next"]["command"] == "agentic-preflight status --all"
    assert malformed["error"]["code"] == "usage_error"
    assert zero["error"]["code"] == "usage_error"


def test_sync_hosted_tolerates_a_removed_config_key_in_the_repository_file(
    github_repo, monkeypatch
):
    _use(monkeypatch, FakeGh(_commits()))
    monkeypatch.setattr(hosted, "resolve_on_path", lambda name: None)
    (github_repo / ".agentic-preflight.toml").write_text(
        "[hook]\nenabled = true\n", encoding="utf-8"
    )

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", "--dry-run")

    assert result["ok"] is True
    assert result["data"]["dry_run"] is True


def test_dry_run_contacts_nothing_and_writes_nothing(github_repo, monkeypatch):
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)
    monkeypatch.setattr(hosted, "resolve_on_path", lambda name: None)
    before = sorted(path.name for path in _state_root(github_repo).rglob("*"))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", "--dry-run", "--limit", "2")

    assert fake.calls == []
    assert sorted(path.name for path in _state_root(github_repo).rglob("*")) == before
    assert not telemetry.mirror_path().exists()
    data = result["data"]
    assert data["dry_run"] is True
    assert (data["eligible"], data["selected"], data["deferred"], data["synced"]) == (3, 2, 1, 0)
    assert data["gh_found"] is False
    assert [run["outcome"] for run in data["runs"]] == ["planned", "planned"]
    assert result["next"]["command"] is None
    assert "without `--dry-run`" in result["next"]["instruction"]


def test_an_unknown_commit_is_recorded_as_missing_and_not_asked_again(github_repo, monkeypatch):
    commits = _commits()
    del commits[SHA_MID]
    _use(monkeypatch, FakeGh(commits))
    agent = ScriptedAgent(github_repo)

    result = agent.run("telemetry", "sync-hosted")

    assert result["data"]["commit_not_found"] == 1
    assert result["data"]["synced"] == 3
    middle = _hosted_file(github_repo, "r_0000000002")
    assert (middle["missing"], middle["pr"], middle["checks"]) == (True, None, None)
    summary = _summary_hosted(github_repo, "r_0000000002")
    assert summary["fetched_at"] == middle["fetched_at"]
    assert [summary[key] for key in SYNCED_KEYS[:-1]] == [None, None, None, None]

    fake = FakeGh(commits)
    _use(monkeypatch, fake)
    agent.run("telemetry", "sync-hosted")
    assert fake.calls == []


def test_a_rate_limit_stops_the_sync_and_keeps_what_was_synced(github_repo, monkeypatch):
    limited = _http_error(403, "API rate limit exceeded for user ID 1.")
    _use(monkeypatch, FakeGh(_commits(), overrides={SHA_MID: limited}))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["ok"] is False
    assert result["error"]["code"] == "hosted_rate_limited"
    data = result["data"]
    assert data["synced"] == 1
    assert data["stopped"] == {
        "reason": "rate_limited",
        "run_id": "r_0000000002",
        "http_status": 403,
        "message": "GitHub rate limit reached: API rate limit exceeded for user ID 1.",
    }
    assert _hosted_file(github_repo, "r_0000000003") is not None
    assert _hosted_file(github_repo, "r_0000000002") is None
    assert _hosted_file(github_repo, "r_0000000001") is None
    assert _summary_hosted(github_repo, "r_0000000003")["checks_conclusion"] == "failure"
    assert result["next"]["command"] is None
    assert "Wait until the limit resets" in result["next"]["instruction"]


@pytest.mark.parametrize(
    ("override", "code", "reason"),
    [
        (
            hosted.GhResult(4, "", f"token {TOKEN}\nplease run:  gh auth login"),
            "gh_unavailable",
            "gh_unavailable",
        ),
        (
            hosted.GhResult(1, "", "error connecting to api.github.com"),
            "hosted_network_error",
            "network_error",
        ),
        (subprocess.TimeoutExpired("gh", 60), "hosted_network_error", "network_error"),
        (_http_error(404, "Not Found"), "hosted_request_failed", "not_found"),
        (_http_error(403, "Forbidden"), "hosted_request_failed", "forbidden"),
    ],
)
def test_gh_failures_stop_the_sync_as_envelope_data(
    github_repo, monkeypatch, override, code, reason
):
    _use(monkeypatch, FakeGh(_commits(), overrides={SHA_NEW: override}))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == code
    assert result["data"]["stopped"]["reason"] == reason
    assert result["data"]["synced"] == 0
    assert result["data"]["summaries"] is None
    assert TOKEN not in json.dumps(result)
    assert not list(_state_root(github_repo).rglob("hosted.json"))


def test_a_missing_gh_is_reported_before_any_request(github_repo, monkeypatch):
    monkeypatch.setattr(hosted, "gh_runner", lambda *args, **kwargs: None)

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == "gh_unavailable"
    assert result["error"]["message"] == "gh was not found on PATH."
    assert "gh auth login" in result["next"]["instruction"]
    assert result["data"]["gh_found"] is False


@pytest.mark.parametrize("remote", ["git@gitlab.com:owner/widget.git", None])
def test_a_clone_without_a_github_origin_is_unsupported(tmp_repo, monkeypatch, remote):
    if remote:
        git("remote", "add", "origin", remote, cwd=tmp_repo)
    _store_record(tmp_repo, _done("r_0000000001", SHA_OLD, "2026-10-01T10:00:00+00:00"))
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)

    result = ScriptedAgent(tmp_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == "unsupported_remote"
    assert result["data"]["stopped"]["reason"] == "unsupported_remote"
    assert result["data"]["selected"] == 0
    assert fake.calls == []


def test_an_unreadable_mirror_is_reported_after_hosted_json_is_written(github_repo, monkeypatch):
    _use(monkeypatch, FakeGh(_commits()))

    def broken(*args, **kwargs):
        raise OSError("mirror is read-only")

    monkeypatch.setattr(telemetry, "_rewrite_mirror", broken)

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == "telemetry_write_failed"
    assert result["next"]["command"] == "agentic-preflight telemetry rebuild"
    assert result["data"]["synced"] == 3
    assert "read-only" in result["data"]["summaries"]["mirror"]["error"]
    assert _summary_hosted(github_repo, "r_0000000003")["pr_number"] == 9


def test_an_unreadable_record_is_listed_and_the_other_runs_sync(github_repo, monkeypatch):
    (_run_dir(github_repo, "r_0000000001") / "run.json").write_text('{"truncated": ')
    _use(monkeypatch, FakeGh(_commits()))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted")

    assert result["data"]["unreadable"] == 1
    assert result["data"]["unreadable_runs"][0]["run_id"] == "r_0000000001"
    assert result["data"]["synced"] == 2
    assert "data.unreadable_runs" in result["next"]["instruction"]

    one = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", "--run", "r_0000000001")
    assert "data.unreadable_runs" in one["next"]["instruction"]
    assert "not a DONE run" not in one["next"]["instruction"]


def test_a_hosted_json_that_cannot_be_written_stops_the_sync(github_repo, monkeypatch):
    _use(monkeypatch, FakeGh(_commits()))

    def refuse(path, payload):
        raise PermissionError("read-only run directory")

    monkeypatch.setattr(hosted, "_atomic_write", refuse)

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == "telemetry_write_failed"
    assert result["data"]["stopped"]["reason"] == "write_failed"
    assert result["data"]["stopped"]["run_id"] == "r_0000000003"
    assert result["data"]["synced"] == 0


def test_a_pull_request_closed_without_merging_is_found_by_its_branch(github_repo, monkeypatch):
    abandoned = _pull(7, SHA_MID, state="closed", merged_at=None)
    other = _pull(6, "e" * 40, state="closed", merged_at=None)
    fake = FakeGh(_commits(), branch_pulls={"feature/widget": [other, abandoned]})
    _use(monkeypatch, fake)

    ScriptedAgent(github_repo).run("telemetry", "sync-hosted")

    assert _hosted_file(github_repo, "r_0000000002")["pr"]["number"] == 7
    assert _summary_hosted(github_repo, "r_0000000002")["merged"] is False
    branch_calls = [call[-1] for call in fake.calls if "/pulls?state=all" in call[-1]]
    # Only the commit with no associated pull request needs the branch search.
    assert branch_calls == [
        "/repos/owner/widget/pulls?state=all&head=owner%3Afeature%2Fwidget&per_page=100"
    ]


def test_a_commit_that_fails_alone_does_not_block_older_runs(github_repo, monkeypatch):
    broken = _http_error(502, "Server Error")
    _use(monkeypatch, FakeGh(_commits(), overrides={SHA_MID: broken}))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    assert result["error"]["code"] == "hosted_request_failed"
    data = result["data"]
    assert data["stopped"] is None
    assert data["synced"] == 2
    assert data["errors"] == [
        {
            "reason": "http_error",
            "run_id": "r_0000000002",
            "http_status": 502,
            "message": "GitHub returned HTTP 502: Server Error",
        }
    ]
    assert _hosted_file(github_repo, "r_0000000001") is not None
    assert _hosted_file(github_repo, "r_0000000002") is None
    assert data["summaries"]["regenerated"] == 2


def test_an_answer_of_an_unexpected_shape_is_an_error_for_that_commit(github_repo, monkeypatch):
    commits = _commits()
    commits[SHA_NEW]["checks"] = [{"name": "x", "status": "completed", "conclusion": ["odd"]}]
    fake = FakeGh(commits)
    _use(monkeypatch, fake)

    def explode(*args, **kwargs):
        raise KeyError("surprise")

    real_fetch = hosted.fetch
    monkeypatch.setattr(
        hosted,
        "fetch",
        lambda runner, slug, sha, branch: (
            explode() if sha == SHA_OLD else real_fetch(runner, slug, sha, branch)
        ),
    )

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", expect=ExitCode.USAGE)

    # A conclusion that is not text is not a failure, and never crashes the reduction.
    assert _hosted_file(github_repo, "r_0000000003")["checks"]["conclusion"] == "success"
    assert result["data"]["synced"] == 2
    assert result["data"]["errors"][0]["run_id"] == "r_0000000001"
    assert result["data"]["errors"][0]["message"] == (
        "GitHub's answer could not be read (KeyError)."
    )


def test_a_summary_left_behind_by_an_interrupted_sync_is_regenerated_without_requests(
    github_repo, monkeypatch
):
    _use(monkeypatch, FakeGh(_commits()))
    agent = ScriptedAgent(github_repo)
    agent.run("telemetry", "sync-hosted")
    (_run_dir(github_repo, "r_0000000002") / "summary.json").unlink()
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)

    preview = agent.run("telemetry", "sync-hosted", "--dry-run")
    result = agent.run("telemetry", "sync-hosted")

    assert preview["data"]["stale_summaries"] == 1
    assert fake.calls == []
    assert (result["data"]["synced"], result["data"]["stale_summaries"]) == (0, 1)
    assert result["data"]["summaries"]["regenerated"] == 1
    assert _summary_hosted(github_repo, "r_0000000002")["checks_conclusion"] == "none"
    assert result["next"]["command"] == "agentic-preflight report"


def test_with_the_mirror_off_only_the_summaries_are_regenerated(github_repo, monkeypatch):
    write(github_repo, ".agentic-preflight.toml", "[telemetry]\nmirror = false\n")
    _use(monkeypatch, FakeGh(_commits()))

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted")

    assert result["data"]["summaries"]["mirror"]["enabled"] is False
    assert not telemetry.mirror_path().exists()
    assert _summary_hosted(github_repo, "r_0000000001")["pr_number"] == 4


def test_rebuild_and_report_carry_the_synced_outcomes(github_repo, monkeypatch):
    _use(monkeypatch, FakeGh(_commits()))
    agent = ScriptedAgent(github_repo)
    agent.run("telemetry", "sync-hosted")

    agent.run("telemetry", "rebuild")
    result = agent.run("report", "--format", "markdown")

    assert _summary_hosted(github_repo, "r_0000000003")["checks_conclusion"] == "failure"
    synced = result["data"]["overall"]["hosted"]["synced"]
    # r_0000000004 aborted and r_0000000005 pushed nothing: finished, never synced.
    assert synced["runs"] == report.ratio(3, 5, "runs")
    assert synced["green_checks_failed"] == report.ratio(1, 3, "synced runs with settled checks")
    assert synced["merged"] == report.ratio(1, 2, "synced runs with a pull request")
    line = "| Preflight green, hosted checks failed | 33.3% | 1 | synced runs with settled checks: 3 | 0 |"
    assert line in result["data"]["markdown"]


def test_a_stale_or_unreadable_hosted_json_is_not_synced(github_repo, monkeypatch):
    run_dir = _run_dir(github_repo, "r_0000000003")
    (run_dir / "hosted.json").write_text('{"truncated": ', encoding="utf-8")
    other = {"fetched_at": "2026-10-01T00:00:00+00:00", "sha": "d" * 40, "pr": None}
    (_run_dir(github_repo, "r_0000000002") / "hosted.json").write_text(json.dumps(other))
    fake = FakeGh(_commits())
    _use(monkeypatch, fake)

    result = ScriptedAgent(github_repo).run("telemetry", "sync-hosted", "--dry-run")

    assert result["data"]["already_synced"] == 0
    store = Store(_state_root(github_repo))
    identity = telemetry.repo_identity(github_repo)
    row = telemetry._summarize_stored_run(store, "r_0000000002", identity, "now")
    assert {key: row.hosted[key] for key in SYNCED_KEYS} == dict.fromkeys(SYNCED_KEYS)


# -- the report -----------------------------------------------------------------------


def _hosted_row(run_id: str, *, repo=None, state="DONE", **hosted_keys: Any) -> dict[str, Any]:
    keys = {**dict.fromkeys(SYNCED_KEYS), "fetched_at": "2026-10-05T00:00:00+00:00", **hosted_keys}
    extra = {"repo": repo} if repo else {}
    return make_row(
        run_id,
        state=state,
        hosted=dict(keys),
        **extra,
    )


def _report_rows() -> list[dict[str, Any]]:
    gadget = {
        "root_commit": "b" * 40,
        "remote": "github.com/owner/gadget",
        "name": "gadget",
        "source_worktree_path": "/repos/gadget",
    }
    return [
        _hosted_row(
            "r_0000000001", pr_number=1, pr_state="closed", merged=True, checks_conclusion="failure"
        ),
        _hosted_row(
            "r_0000000002",
            pr_number=2,
            pr_state="closed",
            merged=False,
            checks_conclusion="success",
        ),
        _hosted_row("r_0000000003", checks_conclusion="none"),
        _hosted_row("r_0000000004"),
        _hosted_row(
            "r_0000000005",
            repo=gadget,
            pr_number=5,
            pr_state="open",
            merged=False,
            checks_conclusion="failure",
        ),
        make_row("r_0000000006"),
        make_row("r_0000000007", hosted={**_hosted_row("x")["hosted"], "fetched_at": None}),
        _hosted_row("r_0000000008", checks_conclusion="pending"),
    ]


def test_report_counts_green_runs_whose_hosted_checks_failed_over_synced_runs():
    synced = report.aggregate(_report_rows())["hosted"]["synced"]

    assert synced["runs"] == report.ratio(6, 8, "runs")
    # The pending run and the run whose commit GitHub did not know are left out.
    assert synced["green_checks_failed"] == report.ratio(2, 4, "synced runs with settled checks")
    assert synced["checks_excluded_runs"] == 2
    assert synced["pull_request_found"] == report.ratio(
        3, 5, "synced runs whose commit GitHub knew"
    )
    assert synced["commit_unknown_runs"] == 1
    assert synced["merged"] == report.ratio(1, 3, "synced runs with a pull request")
    assert synced["pr_states"] == {"closed": 2, "open": 1}
    assert synced["checks_conclusions"] == {
        "failure": 2,
        "none": 1,
        "pending": 1,
        "success": 1,
        "unknown": 1,
    }
    assert "Only synced runs count" in synced["note"]


def test_report_without_synced_runs_has_null_shares_not_zero():
    synced = report.aggregate([make_row("r_0000000001")])["hosted"]["synced"]

    assert synced["runs"] == report.ratio(0, 1, "runs")
    assert synced["green_checks_failed"]["value"] is None
    assert synced["merged"]["value"] is None
    assert synced["checks_conclusions"] == {}


def test_report_markdown_shows_synced_outcomes_overall_and_per_repository():
    data = report.build_report(
        _report_rows(),
        source={
            "used": "mirror",
            "scope": "all",
            "mirror": report.MirrorRead(path="/data/runs.jsonl", exists=True).describe(),
            "clone": None,
        },
        filters=report.Filters(),
        filtered_out=report.apply_filters([], report.Filters())[1],
        output_format="markdown",
    )
    text = data["markdown"]
    section = text.split("### Hosted outcomes synced\n\n", 1)[1].split("\n### ", 1)[0]

    assert section.startswith(report.SYNC_NOTE)
    assert "| Runs synced | 75.0% | 6 | runs: 8 | n/a |" in section
    assert (
        "| Preflight green, hosted checks failed | 50.0% | 2 "
        "| synced runs with settled checks: 4 | 2 |" in section
    )
    assert (
        "| Pull request found | 60.0% | 3 | synced runs whose commit GitHub knew: 5 | 1 |"
        in section
    )
    assert (
        "| Pull request merged | 33.3% | 1 | synced runs with a pull request: 3 | n/a |" in section
    )
    assert "| failure | 2 |" in section
    assert "| widget (github.com/owner/widget) | 5 | 1 | 33.3% | 1 of 2 | 50.0% |" in section
    assert "| gadget (github.com/owner/gadget) | 1 | 1 | 100.0% | 0 of 1 | 0.0% |" in section
    by_repo = {group["name"]: group["metrics"]["hosted"]["synced"] for group in data["by_repo"]}
    assert by_repo["gadget"]["green_checks_failed"] == report.ratio(
        1, 1, "synced runs with settled checks"
    )


def test_report_limits_name_what_a_hosted_failure_does_not_prove():
    limits = " ".join(report.HOSTED_LIMITS)

    assert "not proof of an escaped defect" in limits
    assert "only the runs that `telemetry sync-hosted` fetched" in limits
    assert not any("hosted" in limit for limit in report.LIMITS)


# -- boundaries and docs ------------------------------------------------------------


def test_only_the_telemetry_cli_imports_the_hosted_sync():
    """No workflow command can reach GitHub through this module."""
    package = Path(agentic_preflight.__file__).parent
    importers = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name.split(".")[-1] == "hosted" for name in names):
                importers.add(path.name)
    assert importers == {"cli_telemetry.py"}


def test_docs_describe_the_sync_and_say_it_only_reads_github():
    telemetry_doc = (PROJECT_ROOT / "docs" / "local-telemetry.md").read_text(encoding="utf-8")
    commands = (PROJECT_ROOT / "skill" / "reference" / "commands.md").read_text(encoding="utf-8")
    playbooks = (PROJECT_ROOT / "skill" / "reference" / "playbooks.md").read_text(encoding="utf-8")
    compatibility = (PROJECT_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")
    fragment = PROJECT_ROOT / "docs" / "CHANGELOG.d" / "hosted-outcome-sync.added.md"

    assert "## Join hosted outcomes" in telemetry_doc
    for text in (telemetry_doc, commands):
        assert "only telemetry command that talks to a network service" in text
        assert "It only reads" in text
    section = commands.split("### `agentic-preflight telemetry sync-hosted", 1)[1].split("\n### ")[
        0
    ]
    assert commands.index("telemetry rebuild`") < commands.index("telemetry sync-hosted")
    for key in ("errors", "stale_summaries", "gh_found"):
        assert f"`{key}`" in section, key
    for code in (
        "gh_unavailable",
        "unsupported_remote",
        "hosted_rate_limited",
        "hosted_network_error",
        "hosted_request_failed",
        "telemetry_write_failed",
        "no_run",
    ):
        assert f"`{code}`" in section, code
    for key in ("fetched_at", "sha", "pr", "checks", "missing", "source", *SYNCED_KEYS):
        assert f"`{key}`" in section, key
    playbook = playbooks.split("## Summarize preflight history", 1)[1].split("\n## ")[0]
    assert "agentic-preflight telemetry sync-hosted" in playbook
    assert "calls GitHub" in playbook
    assert "## Hosted outcome records" in compatibility
    # Release assembly consumes the fragment into CHANGELOG.md; accept either home.
    if fragment.exists():
        assert fragment.read_text(encoding="utf-8").startswith("- ")
    else:
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "telemetry sync-hosted" in changelog
