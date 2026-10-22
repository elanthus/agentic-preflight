"""The pre-push hook decision log: reason codes, the writer, and the report section.

The reason codes are tested on :func:`hook.evaluate` directly. The writer is tested
through a real ``hook-check`` subprocess, as Git runs it, with the telemetry
directory in a per-test data directory. A broken or slow log must never change the
exit code or the message, so those tests compare against a run with the log off.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agentic_preflight import filelock, gitx, hook, hooklog, report, telemetry
from agentic_preflight.config import Config, TelemetrySection
from agentic_preflight.envelope import ExitCode
from agentic_preflight.initcmd import DEFAULT_CONFIG
from tests.conftest import git, home_env, opt_in_telemetry, set_home, write
from tests.driver import ScriptedAgent, green_run
from tests.test_hook import hook_check
from tests.test_report import ROOT_A, ROOT_B

#: Every fixture repository is under the system temporary directory, which the product
#: keeps out of the default telemetry location. These tests are about what the files
#: record, so they opt in with an explicit ``[telemetry] path``.
pytestmark = pytest.mark.usefixtures("telemetry_opt_in")

ZERO = "0" * 40
GREEN = "a" * 40
OTHER = "b" * 40
PROJECT_ROOT = Path(__file__).parent.parent
LINE_KEYS = {"at", "repo", "remote", "allowed", "reason", "updates"}
UPDATE_KEYS = {"local_ref", "remote_ref", "sha", "force"}


def _decide(updates, *, ancestors=(), attested=(GREEN,), allow_force_push=False):
    """Evaluate with an injected ancestry relation and set of attested commits."""
    return hook.evaluate(
        updates,
        is_ancestor=lambda a, b: (a, b) in ancestors,
        has_attestation=lambda sha: sha in attested,
        allow_force_push=allow_force_push,
    )


def _branch(local: str, remote: str = ZERO, name: str = "refs/heads/feature/x"):
    return hook.RefUpdate(name, local, name, remote)


def _outcomes(decision: hook.Decision) -> list[tuple[str, bool | None]]:
    return [(item.outcome, item.force) for item in decision.updates]


# -- reason codes ----------------------------------------------------------------


def test_an_attested_new_branch_is_allowed_as_attested():
    decision = _decide([_branch(GREEN)])

    assert (decision.allowed, decision.reason, decision.message) == (True, hook.ATTESTED, "")
    assert _outcomes(decision) == [(hook.ATTESTED, False)]


def test_a_missing_attestation_blocks_with_its_code_and_the_unchanged_message():
    decision = _decide([_branch(OTHER)])

    assert (decision.allowed, decision.reason) == (False, hook.MISSING_ATTESTATION)
    assert _outcomes(decision) == [(hook.MISSING_ATTESTATION, False)]
    assert decision.message == "\n".join(
        [
            "agentic-preflight: push blocked.",
            "  commit: bbbbbbb (no green run recorded for this exact SHA)",
            "  reason: no valid attestation note is attached to this exact SHA",
            "  fix:    invoke the skill (/agentic-preflight in Claude Code, "
            "$agentic-preflight in Codex)",
            "  bypass: git push --no-verify   (documented escape hatch)",
        ]
    )


def test_a_force_push_is_refused_and_later_updates_are_not_evaluated():
    forced = _branch(GREEN, remote="e" * 40)
    later = _branch(OTHER, name="refs/heads/later")

    decision = _decide([forced, later])

    assert (decision.allowed, decision.reason) == (False, hook.FORCE_PUSH_REFUSED)
    assert _outcomes(decision) == [(hook.FORCE_PUSH_REFUSED, True), (hook.NOT_EVALUATED, None)]
    assert decision.message.startswith("agentic-preflight: push blocked.\n  commit: aaaaaaa (force")


def test_a_permitted_force_push_of_an_attested_tip_records_the_force():
    decision = _decide([_branch(GREEN, remote="e" * 40)], allow_force_push=True)

    assert (decision.allowed, decision.reason) == (True, hook.ATTESTED)
    assert _outcomes(decision) == [(hook.ATTESTED, True)]


def test_a_permitted_rewind_to_a_commit_the_remote_has_is_already_on_remote():
    rewind = _branch(OTHER, remote=GREEN)

    decision = _decide([rewind], ancestors={(OTHER, GREEN)}, attested=(), allow_force_push=True)

    assert (decision.allowed, decision.reason) == (True, hook.ALREADY_ON_REMOTE)
    assert _outcomes(decision) == [(hook.ALREADY_ON_REMOTE, True)]


def test_deletions_alone_are_allowed_as_deletion():
    decision = _decide([_branch(ZERO, remote=GREEN), _branch(ZERO, name="refs/tags/v1")])

    assert (decision.allowed, decision.reason) == (True, hook.DELETION)
    assert _outcomes(decision) == [(hook.DELETION, None), (hook.DELETION, None)]


def test_tags_notes_and_evidence_refs_are_allowed_as_exempt():
    updates = [
        hook.RefUpdate("refs/tags/v1", OTHER, "refs/tags/v1", ZERO),
        hook.RefUpdate(hook.NOTES_REF, OTHER, hook.NOTES_REF, ZERO),
        hook.RefUpdate("refs/heads/x", OTHER, "refs/agentic-preflight/evidence/" + OTHER, ZERO),
        _branch(ZERO, remote=GREEN),
    ]

    decision = _decide(updates, attested=())

    assert (decision.allowed, decision.reason) == (True, hook.EXEMPT)
    assert [item.outcome for item in decision.updates] == [
        hook.TAG,
        hook.ATTESTATION_NOTE,
        hook.EVIDENCE_REF,
        hook.DELETION,
    ]
    assert {item.force for item in decision.updates} == {None}


def test_an_attested_update_outranks_the_exempt_ones():
    decision = _decide(
        [hook.RefUpdate("refs/tags/v1", OTHER, "refs/tags/v1", ZERO), _branch(GREEN)]
    )

    assert decision.reason == hook.ATTESTED


def test_no_updates_and_unevaluated_decisions_have_their_own_codes():
    nothing = _decide([])
    unavailable = hook.unevaluated([_branch(OTHER)], hook.CONFIG_UNAVAILABLE)

    assert (nothing.allowed, nothing.reason, nothing.updates) == (True, hook.NO_UPDATES, [])
    assert (unavailable.allowed, unavailable.reason) == (True, hook.CONFIG_UNAVAILABLE)
    assert _outcomes(unavailable) == [(hook.NOT_EVALUATED, None)]


def test_the_reason_codes_are_a_closed_partition():
    assert set(hook.ALLOWED_REASONS).isdisjoint(hook.BLOCKED_REASONS)
    assert set(hook.REASONS) == set(hook.ALLOWED_REASONS) | set(hook.BLOCKED_REASONS)
    assert set(hook.REASONS) - {hook.NO_UPDATES, hook.CONFIG_UNAVAILABLE, hook.EXEMPT} <= set(
        hook.UPDATE_OUTCOMES
    )


# -- the log line ------------------------------------------------------------------


def _log_lines(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or hooklog.log_path()
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]


def _remote_env(name: str = "origin") -> dict[str, str]:
    return {hook.REMOTE_ENV: name}


def test_a_blocked_decision_is_logged_with_the_contract_shape(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n", _remote_env()
    )

    assert result.returncode == ExitCode.HOOK_BLOCK
    [line] = _log_lines()
    assert set(line) == LINE_KEYS
    assert line["allowed"] is False
    assert line["reason"] == hook.MISSING_ATTESTATION
    assert line["remote"] == "origin"
    root = gitx.repo_root(feature_repo)
    assert line["repo"] == telemetry.repo_identity(root).model_dump(mode="json")
    assert line["updates"] == [
        {
            "local_ref": "refs/heads/feature/x",
            "remote_ref": "refs/heads/feature/x",
            "sha": sha[:7],
            "force": False,
        }
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", line["at"])
    # Sorted keys, one line, nothing else in the file.
    raw = hooklog.log_path().read_text(encoding="utf-8")
    assert raw == json.dumps(line, sort_keys=True) + "\n"


def test_an_allowed_decision_is_logged_as_attested(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")

    assert result.returncode == 0, result.stderr
    [line] = _log_lines()
    assert (line["allowed"], line["reason"], line["remote"]) == (True, hook.ATTESTED, None)
    assert line["updates"][0]["sha"] == sha[:7]


def test_an_up_to_date_push_and_a_deletion_are_logged(feature_repo):
    assert hook_check(feature_repo, "").returncode == 0
    deleted = hook_check(feature_repo, f"(delete) {ZERO} refs/heads/old {'c' * 40}\n")

    assert deleted.returncode == 0
    assert deleted.stderr == ""
    first, second = _log_lines()
    assert (first["reason"], first["updates"]) == (hook.NO_UPDATES, [])
    assert second["reason"] == hook.DELETION
    assert second["updates"] == [
        {"local_ref": "(delete)", "remote_ref": "refs/heads/old", "sha": "0000000", "force": None}
    ]


def test_a_url_remote_is_logged_without_its_credentials(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    hook_check(
        feature_repo,
        f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        _remote_env("https://user:secret-token@github.com:443/owner/widget.git"),
    )

    [line] = _log_lines()
    assert line["remote"] == "github.com/owner/widget"
    assert "secret-token" not in hooklog.log_path().read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("origin", "origin"),
        ("team/upstream", "team/upstream"),
        ("git@github.com:owner/widget.git", "github.com/owner/widget"),
        ("ssh://git:pw@example.com:2222/a/b", "example.com/a/b"),
        ("", None),
        (None, None),
        ("x" * 500, "x" * 200),
    ],
)
def test_remote_labels_keep_names_and_strip_credentials_from_urls(value, label):
    assert hooklog.remote_label(value) == label


def _set_telemetry(repo: Path, body: str) -> None:
    """Add a ``[telemetry]`` section to the working-tree config, keeping the rest."""
    path = repo / ".agentic-preflight.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + "\n[telemetry]\n" + body, encoding="utf-8")


def test_hook_log_false_writes_nothing(feature_repo):
    _set_telemetry(feature_repo, "hook_log = false\n")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    blocked = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    empty = hook_check(feature_repo, "")

    assert (blocked.returncode, empty.returncode) == (ExitCode.HOOK_BLOCK, 0)
    assert not hooklog.log_path().exists()
    assert not hooklog.log_path().parent.exists()


def test_a_configured_path_moves_the_log(feature_repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _set_telemetry(feature_repo, f"path = {json.dumps(str(elsewhere))}\n")

    hook_check(feature_repo, "")

    assert len(_log_lines(elsewhere / hooklog.LOG_NAME)) == 1
    assert not hooklog.log_path().exists()


@pytest.mark.parametrize(
    ("green", "remote_sha", "expected"),
    [
        (False, ZERO, ExitCode.HOOK_BLOCK),
        (True, "c" * 40, ExitCode.HOOK_BLOCK),
        (True, ZERO, ExitCode.OK),
    ],
    ids=["missing-attestation", "force-push", "allowed"],
)
def test_an_unwritable_log_leaves_the_exit_code_and_message_unchanged(
    feature_repo, tmp_path, green, remote_sha, expected
):
    if green:
        green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    stdin = f"refs/heads/feature/x {sha} refs/heads/feature/x {remote_sha}\n"
    config = feature_repo / ".agentic-preflight.toml"
    original = config.read_text(encoding="utf-8") if config.exists() else ""

    _set_telemetry(feature_repo, "hook_log = false\n")
    baseline = hook_check(feature_repo, stdin)

    # A regular file where the telemetry directory should be: mkdir fails on every platform.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    config.write_text(original, encoding="utf-8")
    _set_telemetry(feature_repo, f"path = {json.dumps(str(blocker / 'telemetry'))}\n")
    broken = hook_check(feature_repo, stdin)

    assert baseline.returncode == expected, baseline.stderr
    assert (broken.returncode, broken.stdout, broken.stderr) == (
        baseline.returncode,
        baseline.stdout,
        baseline.stderr,
    )
    assert bool(broken.stderr) is (expected == ExitCode.HOOK_BLOCK)
    # The failed write did not fall back to another directory.
    assert not hooklog.log_path().exists()


def test_an_unavailable_configuration_still_logs_config_unavailable(
    feature_repo, tmp_path, telemetry_opt_in
):
    write(feature_repo, ".agentic-preflight.toml", "[general]\nbogus = 1\n")
    opt_in_telemetry(telemetry_opt_in, home=tmp_path / "home")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(
        feature_repo,
        f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        {**home_env(tmp_path / "home"), **_remote_env()},
    )

    assert result.returncode == 0
    assert "hook check unavailable" in result.stderr
    assert result.stderr.endswith("; allowing push\n")
    [line] = _log_lines()
    assert (line["allowed"], line["reason"]) == (True, hook.CONFIG_UNAVAILABLE)
    assert line["updates"][0]["force"] is None
    assert line["repo"]["source_worktree_path"] == str(gitx.repo_root(feature_repo))


@pytest.mark.parametrize(
    "repo_config",
    [
        "[general]\nbogus = 1\n\n[telemetry]\nhook_log = false\n",
        "[telemetry]\nhook_log = 'sometimes'\n",
        "this is not toml = = =\n",
    ],
    ids=["opted-out", "invalid-section", "invalid-toml"],
)
def test_an_unknown_or_opted_out_preference_stays_silent(feature_repo, tmp_path, repo_config):
    write(feature_repo, ".agentic-preflight.toml", repo_config)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(
        feature_repo,
        f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        home_env(tmp_path / "home"),
    )

    assert result.returncode == 0
    assert "hook check unavailable" in result.stderr
    assert not hooklog.log_path().exists()


def test_the_fallback_reads_each_telemetry_section_and_the_repository_wins(tmp_path):
    user_dir = tmp_path / "user"
    repo = tmp_path / "repo"
    repo.mkdir()
    write(user_dir, "config.toml", "[telemetry]\nhook_log = false\n")

    assert hooklog.fallback_section(repo, user_config_dir=user_dir) == TelemetrySection(
        hook_log=False
    )
    write(repo, ".agentic-preflight.toml", "[general]\nbogus = 1\n\n[telemetry]\nmirror = false\n")
    assert hooklog.fallback_section(repo, user_config_dir=user_dir) == TelemetrySection(
        mirror=False
    )
    write(user_dir, "config.toml", "[[broken")
    assert hooklog.fallback_section(repo, user_config_dir=user_dir) is None
    assert hooklog.fallback_section(repo, user_config_dir=tmp_path / "none") == TelemetrySection(
        mirror=False
    )


def test_the_installed_hook_passes_the_remote_name_on_a_real_push(feature_repo, bare_remote):
    ScriptedAgent(feature_repo).run("init")

    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "push blocked" in result.stderr
    [line] = _log_lines()
    assert (line["remote"], line["reason"]) == ("origin", hook.MISSING_ATTESTATION)
    assert line["updates"][0]["remote_ref"] == "refs/heads/feature/x"


def test_init_replaces_only_an_exact_earlier_hook(feature_repo):
    path = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    [earlier] = hook.PREVIOUS_HOOK_SCRIPTS
    path.write_bytes(earlier)

    installed, written = hook.install(feature_repo)

    assert (installed, written) == (path, True)
    assert path.read_text(encoding="utf-8") == hook.HOOK_SCRIPT
    assert hook.REMOTE_ENV in hook.HOOK_SCRIPT
    assert "exec agentic-preflight hook-check" in hook.HOOK_SCRIPT

    edited = earlier + b"echo also mine\n"
    path.write_bytes(edited)
    assert hook.install(feature_repo) == (path, False)
    assert path.read_bytes() == edited


# -- bounded and best effort -----------------------------------------------------------


def _config(directory: Path) -> Config:
    return Config(telemetry=TelemetrySection(path=str(directory)))


def _worker_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == hooklog._THREAD_NAME]


def test_a_slow_identity_is_abandoned_at_the_deadline_and_writes_nothing(
    tmp_repo, tmp_path, monkeypatch
):
    release = threading.Event()
    real_identity = telemetry.repo_identity

    def slow_identity(*args, **kwargs):
        release.wait(30)
        return real_identity(*args, **kwargs)

    monkeypatch.setattr(telemetry, "repo_identity", slow_identity)
    directory = tmp_path / "telemetry"

    started = time.monotonic()
    status = hooklog.record(
        hook.evaluate([], is_ancestor=None, has_attestation=None),
        repo_root=tmp_repo,
        config=_config(directory),
        remote="origin",
        timeout=0.2,
    )
    elapsed = time.monotonic() - started

    assert status == hooklog.TIMED_OUT
    assert elapsed < 5
    release.set()
    for thread in _worker_threads():
        thread.join(10)
    assert not (directory / hooklog.LOG_NAME).exists()


def test_a_held_lock_is_waited_for_only_until_the_deadline(tmp_repo, tmp_path):
    directory = tmp_path / "telemetry"
    decision = hook.evaluate([], is_ancestor=None, has_attestation=None)

    with filelock.exclusive(directory / "hook-decisions.jsonl.lock"):
        held = hooklog.record(
            decision, repo_root=tmp_repo, config=_config(directory), remote=None, timeout=0.5
        )
    # The abandoned write must not append once the lock is free: its deadline has passed.
    for thread in _worker_threads():
        thread.join(10)
    assert not (directory / hooklog.LOG_NAME).exists()
    free = hooklog.record(
        decision, repo_root=tmp_repo, config=_config(directory), remote=None, timeout=30
    )

    assert (held, free) == (hooklog.TIMED_OUT, hooklog.WRITTEN)
    assert len(_log_lines(directory / hooklog.LOG_NAME)) == 1


def test_a_hung_log_write_does_not_change_the_exit_code_or_output(tmp_repo, tmp_path):
    """The abandoned daemon thread must not hold the process open or print anything."""
    script = f"""
import sys, threading
from pathlib import Path
from agentic_preflight import hook, hooklog, telemetry
from agentic_preflight.config import Config, TelemetrySection
telemetry.repo_identity = lambda *a, **k: threading.Event().wait(60)
config = Config(telemetry=TelemetrySection(path={str(tmp_path / "telemetry")!r}))
decision = hook.evaluate([], is_ancestor=None, has_attestation=None)
hooklog.record(decision, repo_root=Path({str(tmp_repo)!r}), config=config, remote=None,
               timeout=0.2)
sys.exit(10)
"""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )

    assert (result.returncode, result.stdout, result.stderr) == (10, "", "")
    assert time.monotonic() - started < 20


def test_an_interrupted_line_is_ended_before_the_next_one(tmp_repo, tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir()
    (directory / hooklog.LOG_NAME).write_bytes(b'{"truncated": ')

    status = hooklog.record(
        hook.evaluate([], is_ancestor=None, has_attestation=None),
        repo_root=tmp_repo,
        config=_config(directory),
        remote=None,
    )

    read = report.read_hook_log(directory / hooklog.LOG_NAME)
    assert status == hooklog.WRITTEN
    assert (read.lines, len(read.decisions), len(read.malformed)) == (2, 1, 1)


def test_settings_for_an_up_to_date_push_use_a_bounded_git_call(tmp_repo, tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    write(tmp_repo, ".agentic-preflight.toml", "[telemetry]\nhook_log = false\n")
    timeouts: list[float | None] = []
    real_run = gitx.run

    def recording_run(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gitx, "run", recording_run)
    monkeypatch.chdir(tmp_repo)
    root, config = hooklog.load_settings()

    assert root == gitx.repo_root(tmp_repo)
    assert config is not None
    assert config.telemetry.hook_log is False
    assert timeouts[0] == hooklog.TIMEOUT_SECONDS

    def hung_git(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git"], kwargs.get("timeout") or 0)

    monkeypatch.setattr(gitx, "run", hung_git)
    assert hooklog.load_settings() == (None, None)


def test_no_logging_starts_when_it_is_off_or_unknown(tmp_repo, tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    off = Config(telemetry=TelemetrySection(hook_log=False, path=str(tmp_path / "t")))
    decision = hook.evaluate([], is_ancestor=None, has_attestation=None)

    assert hooklog.record(decision, repo_root=tmp_repo, config=off, remote=None) == (
        hooklog.DISABLED
    )
    write(tmp_repo, ".agentic-preflight.toml", "[telemetry]\nhook_log = 3\n")
    assert hooklog.record(decision, repo_root=tmp_repo, config=None, remote=None) == (
        hooklog.UNRESOLVED
    )
    assert not (tmp_path / "t").exists()


# -- the report section ------------------------------------------------------------------

REPO_A = {
    "root_commit": ROOT_A,
    "remote": "github.com/owner/widget",
    "name": "widget",
    "source_worktree_path": "/repos/widget",
}
REPO_B = {
    "root_commit": ROOT_B,
    "remote": "gitlab.example/team/gadget",
    "name": "gadget",
    "source_worktree_path": "/repos/gadget",
}


def _decision_line(at: str, allowed: bool, reason: str, repo=REPO_A) -> dict[str, Any]:
    line = {
        "at": at,
        "repo": repo,
        "remote": "origin",
        "allowed": allowed,
        "reason": reason,
        "updates": [
            {
                "local_ref": "refs/heads/x",
                "remote_ref": "refs/heads/x",
                "sha": "1234567",
                "force": False,
            }
        ],
    }
    return hooklog.HookDecisionLine.model_validate(line).model_dump(mode="json")


def _hook_lines() -> list[dict[str, Any]]:
    return [
        _decision_line("2026-10-01T09:00:00+00:00", False, hook.MISSING_ATTESTATION),
        _decision_line("2026-10-01T09:05:00+00:00", True, hook.ATTESTED),
        _decision_line("2026-10-02T09:00:00+00:00", False, hook.FORCE_PUSH_REFUSED),
        _decision_line("2026-10-03T09:00:00+00:00", True, hook.NO_UPDATES, repo=REPO_B),
        _decision_line("not a time", False, hook.MISSING_ATTESTATION, repo=REPO_B),
    ]


def _payload(lines: list[dict[str, Any]], *extra: bytes) -> bytes:
    rows = [json.dumps(line, sort_keys=True).encode("utf-8") for line in lines]
    return b"\n".join([*rows, *extra]) + b"\n"


def test_malformed_hook_log_lines_are_counted_by_reason(tmp_path):
    good = _hook_lines()[0]
    payload = _payload(
        [good],
        b'{"truncated": ',
        b"[1, 2]",
        json.dumps({**good, "allowed": "yes"}).encode(),
        json.dumps({**good, "schema_version": 2}).encode(),
        b"\xff\xfe",
        b"   ",
    )

    read = report.parse_hook_log(payload, tmp_path / hooklog.LOG_NAME)

    described = read.describe()
    assert (described["lines"], described["rows"], described["malformed"]) == (6, 1, 5)
    assert described["malformed_by_reason"] == {
        "invalid_decision": 2,
        "invalid_json": 1,
        "not_a_decision": 1,
        "undecodable": 1,
    }
    assert [item["line"] for item in described["malformed_lines"]] == [2, 3, 4, 5, 6]


def test_the_section_counts_allowed_and_blocked_overall_and_per_repository(tmp_path):
    read = report.parse_hook_log(_payload(_hook_lines()), tmp_path / hooklog.LOG_NAME)

    section = report.hook_decisions(read, None, all_repos=True, filters=report.Filters())

    overall = section["overall"]
    assert (overall["decisions"], overall["allowed"], overall["blocked"]) == (5, 2, 3)
    assert overall["blocked_share"] == {
        "value": 0.6,
        "numerator": 3,
        "denominator": 5,
        "denominator_label": "hook decisions",
    }
    assert overall["blocked_by_reason"] == {"force_push_refused": 1, "missing_attestation": 2}
    assert overall["allowed_by_reason"] == {"attested": 1, "no_updates": 1}
    assert [(group["label"], group["metrics"]["decisions"]) for group in section["by_repo"]] == [
        ("widget", 3),
        ("gadget", 2),
    ]
    assert section["scope"] == "all"
    assert section["filtered_out"]["total"] == 0


def test_the_section_applies_the_time_and_repository_filters(tmp_path):
    read = report.parse_hook_log(_payload(_hook_lines()), tmp_path / hooklog.LOG_NAME)
    filters = report.Filters(
        since=report.parse_timestamp("2026-10-01T09:05:00"),
        until=report.parse_timestamp("2026-10-03"),
        states=("DONE",),
    )

    timed = report.hook_decisions(read, None, all_repos=True, filters=filters)
    gadget = report.hook_decisions(
        read, None, all_repos=True, filters=report.Filters(repo="gitlab.example/team/gadget.git")
    )

    assert timed["overall"]["decisions"] == 2
    assert timed["filtered_out"] == {
        "scope": 0,
        "repo": 0,
        "since": 1,
        "until": 1,
        "time_unknown": 1,
        "total": 3,
    }
    # --state selects runs, not decisions.
    assert timed["overall"]["blocked_by_reason"] == {"force_push_refused": 1}
    assert gadget["overall"]["decisions"] == 2
    assert gadget["filtered_out"]["repo"] == 3


def test_the_clone_scope_keeps_this_repositorys_decisions(tmp_repo, tmp_path):
    clone = report.Clone(
        store=None,  # type: ignore[arg-type]
        identity=telemetry.RepoIdentity(
            root_commit=ROOT_B, remote=None, name="gadget", source_worktree_path=str(tmp_repo)
        ),
        worktree_paths=frozenset({str(tmp_repo)}),
    )
    path_only = {**REPO_A, "root_commit": None, "source_worktree_path": str(tmp_repo)}
    lines = [
        *_hook_lines(),
        _decision_line("2026-10-04T00:00:00+00:00", True, "attested", path_only),
    ]
    read = report.parse_hook_log(_payload(lines), tmp_path / hooklog.LOG_NAME)

    section = report.hook_decisions(read, clone, all_repos=False, filters=report.Filters())
    everything = report.hook_decisions(read, clone, all_repos=True, filters=report.Filters())

    assert section["scope"] == "clone"
    assert section["overall"]["decisions"] == 3
    assert section["filtered_out"]["scope"] == 3
    assert everything["overall"]["decisions"] == 6


def test_a_missing_log_counts_zero_and_an_unreadable_one_counts_null(tmp_path):
    missing = report.hook_decisions(
        report.read_hook_log(tmp_path / "absent.jsonl"),
        None,
        all_repos=True,
        filters=report.Filters(),
    )
    (tmp_path / "dir.jsonl").mkdir()
    unreadable = report.hook_decisions(
        report.read_hook_log(tmp_path / "dir.jsonl"), None, all_repos=True, filters=report.Filters()
    )

    assert missing["source"]["exists"] is False
    assert missing["overall"]["decisions"] == 0
    assert missing["overall"]["blocked_share"]["value"] is None
    assert unreadable["source"]["error"]
    assert unreadable["overall"] is None
    assert unreadable["by_repo"] == []


def _markdown_with_hooks(section: dict[str, Any] | None) -> str:
    return report.build_report(
        [],
        source={
            "used": "mirror",
            "scope": "all",
            "mirror": report.MirrorRead(path="/data/runs.jsonl", exists=True).describe(),
            "clone": None,
        },
        filters=report.Filters(),
        filtered_out=report.apply_filters([], report.Filters())[1],
        output_format="markdown",
        hook_decisions=section,
    )["markdown"]


def test_the_markdown_section_comes_just_before_the_limits(tmp_path):
    read = report.parse_hook_log(
        _payload(_hook_lines(), b"broken"), tmp_path / "with | pipe" / hooklog.LOG_NAME
    )
    section = report.hook_decisions(read, None, all_repos=True, filters=report.Filters())

    text = _markdown_with_hooks({**section, "source": {**section["source"], "enabled": True}})
    without = _markdown_with_hooks(None)

    headings = re.findall(r"^#{2,3} (.+)$", text, re.MULTILINE)
    assert headings[-2:] == ["Hook decisions", "Limits"]
    assert "Hook decisions" not in without
    assert "| Malformed hook log lines | 1 |" in text
    assert "| Blocked share | 60.0% (3 of 5 hook decisions) |" in text
    assert "| blocked | missing_attestation | 2 |" in text
    assert "| widget (github.com/owner/widget) | 3 | 1 | 2 | 66.7% |" in text
    hook_part = text.split("### Hook decisions\n", 1)[1].split("### Limits", 1)[0]
    prose = [line for line in hook_part.splitlines() if line and not line.startswith("|")]
    assert prose == [report.HOOK_NOTE]
    assert not re.search(r"\d", report.HOOK_NOTE)
    for block in re.split(r"\n\n+", hook_part):
        lines = block.splitlines()
        if lines and lines[0].startswith("|"):
            assert len({len(re.findall(r"(?<!\\)\|", line)) for line in lines}) == 1, block


def test_the_report_command_reads_the_hook_log_with_its_filters(tmp_repo):
    path = hooklog.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_payload(_hook_lines(), b'{"truncated": '))

    result = ScriptedAgent(tmp_repo).run(
        "report", "--all-repos", "--since", "2026-10-02", "--format", "markdown"
    )

    hooks = result["data"]["hook_decisions"]
    assert hooks["source"]["path"] == str(path)
    assert hooks["source"]["enabled"] is True
    assert hooks["source"]["malformed"] == 1
    assert hooks["overall"]["decisions"] == 2
    assert hooks["filtered_out"]["since"] == 2
    assert "### Hook decisions" in result["data"]["markdown"]
    assert "hook decision log lines are not readable" in result["next"]["instruction"]


def test_the_report_in_a_clone_counts_only_its_own_decisions(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    with hooklog.log_path().open("ab") as handle:
        handle.write(_payload(_hook_lines()))

    result = ScriptedAgent(feature_repo).run("report")

    hooks = result["data"]["hook_decisions"]
    assert hooks["scope"] == "clone"
    assert hooks["overall"]["decisions"] == 1
    assert hooks["overall"]["blocked_by_reason"] == {"missing_attestation": 1}
    assert hooks["filtered_out"]["scope"] == 5


# -- docs ---------------------------------------------------------------------------------


def _doc(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_every_reason_code_is_documented():
    telemetry_doc = _doc("docs/local-telemetry.md")
    commands = _doc("skill/reference/commands.md")
    entry = commands.split("### `agentic-preflight hook-check`", 1)[1].split("\n### ", 1)[0]
    for code in hook.REASONS:
        assert f"| `{code}` |" in telemetry_doc, code
        assert f"`{code}`" in entry, code


def test_the_log_and_its_switch_are_documented_where_readers_look():
    telemetry_doc = _doc("docs/local-telemetry.md")
    configuration = _doc("docs/configuration.md")
    pre_push = _doc("docs/pre-push-hook.md")
    compatibility = _doc("COMPATIBILITY.md")

    assert "## Hook decisions" in telemetry_doc
    assert hooklog.LOG_NAME in telemetry_doc
    assert hooklog.LOG_NAME in pre_push
    assert "`[telemetry] hook_log = false`" in telemetry_doc
    assert re.search(r"^hook_log = true\s+#", configuration, re.MULTILINE)
    assert "`hook_log = false`" in configuration
    assert "# hook_log = true" in DEFAULT_CONFIG
    assert hook.REMOTE_ENV in pre_push
    assert hook.REMOTE_ENV in hook.HOOK_SCRIPT
    assert "## Hook decision log" in compatibility
    assert f"{hooklog.TIMEOUT_SECONDS:g} seconds" in telemetry_doc
    for field_name in LINE_KEYS | UPDATE_KEYS:
        assert f"`{field_name}`" in telemetry_doc, field_name


def test_the_limits_name_the_decisions_the_log_cannot_see():
    limits = " ".join(report.LIMITS)
    assert "`git push --no-verify`" in limits
    assert "not on `PATH`" in limits
