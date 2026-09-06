"""Deterministic absence retries, strict snapshots, and hosted command policy."""

import json
import subprocess
from typing import ClassVar

import pytest

from agentic_preflight import attestation
from agentic_preflight import note_availability as availability
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Attestation
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent
from tests.test_approval import _stages

HEAD = "a" * 40
BASE = "b" * 40
SNAPSHOTS = [str(i) * 40 for i in range(1, 5)]


def evidence(head=HEAD, tree="c" * 40):
    return Attestation(
        sha=head,
        tree_sha=tree,
        branch="feature",
        base_ref="main",
        merge_base_sha=BASE,
        intent_sha256="d" * 64,
        config_sha256="e" * 64,
        run_id="r_test",
        green_at="2026-01-01T00:00:00+00:00",
        stages=_stages(),
        findings_summary={},
    )


@pytest.fixture
def remote(monkeypatch):
    class FakeRemote:
        timeout = 30.0
        attempts = 0
        observed = HEAD
        available_at = 1
        ref_missing = False
        payload = attestation.encode(evidence())
        notes_read: ClassVar[list] = []
        sleeps: ClassVar[list] = []
        cleanup: ClassVar[list] = []
        fail = None
        fetch_head = HEAD

        def __init__(self, *args):
            pass

        def run(self, *args):
            if args[0] == "remote":
                return "https://user:SECRET@example.com/owner/repo.git?token=SECRET"
            if args[0] == "rev-parse":
                return BASE
            if args[0] == "update-ref":
                self.cleanup.append(args[-1])
            return ""

        def advertised(self, ref):
            if self.fail:
                raise self.fail
            if ref.startswith("refs/heads/"):
                return self.observed
            return None if self.ref_missing else SNAPSHOTS[self.attempts - 1]

        def fetch(self, ref, target):
            if ref.startswith("refs/heads/"):
                type(self).attempts += 1
                return self.fetch_head
            return SNAPSHOTS[self.attempts - 1]

        def note(self, snapshot, head):
            self.notes_read.append((snapshot, head))
            if self.attempts >= self.available_at:
                return "f" * 40, self.payload
            return None

    monkeypatch.setattr(availability, "RemoteNotes", FakeRemote)
    monkeypatch.setattr(attestation.gitx, "tree_sha", lambda *_: "c" * 40)
    return FakeRemote


def check(remote, **kwargs):
    return availability.check(
        ".",
        remote="origin",
        head_ref="refs/heads/feature",
        expected_head=HEAD,
        base_sha=BASE,
        sleeper=remote.sleeps.append,
        clock=lambda: 123.0,
        evaluate=kwargs.pop("evaluate", lambda value: {"sha": value.sha}),
        **kwargs,
    )


@pytest.mark.parametrize("available_at", [1, 2, 4])
def test_only_absence_retries_selected_snapshot(remote, available_at):
    remote.available_at = available_at
    consumed = []
    result = check(remote, evaluate=lambda value: consumed.append(value) or {"verified": True})
    assert len(consumed) == 1
    assert remote.attempts == available_at
    assert remote.sleeps == list(availability.DEFAULT_DELAYS[: available_at - 1])
    assert remote.notes_read == [(SNAPSHOTS[i], HEAD) for i in range(available_at)]
    diag = result["availability"]
    assert diag["attempts"][-1]["notes_commit"] == SNAPSHOTS[available_at - 1]
    assert diag["attempts"][-1]["completion_head"] == HEAD
    assert diag["verifier_revision"] == BASE
    assert "SECRET" not in json.dumps(diag)
    assert len(remote.cleanup) == 2


@pytest.mark.parametrize("missing_ref", [True, False])
def test_exhaustion_is_failure_without_final_sleep(remote, missing_ref):
    remote.available_at = 100
    remote.ref_missing = missing_ref
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote)
    assert caught.value.reason == ("missing_notes_ref" if missing_ref else "missing_note")
    assert remote.attempts == 4
    assert remote.sleeps == [2, 4, 8]
    assert len(caught.value.diagnostics["attempts"]) == 4


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("json", "malformed_payload"),
        ("extra", "incompatible_schema"),
        ("version", "incompatible_schema"),
        ("missing", "invalid_evidence"),
        ("commit", "commit_mismatch"),
        ("tree", "tree_mismatch"),
    ],
)
def test_present_invalid_note_never_retries_or_evaluates(remote, kind, reason):
    payload = json.loads(remote.payload)
    if kind == "extra":
        payload["stages"]["review"]["coverage"]["future_field"] = "SECRET"
    elif kind == "version":
        payload["schema_version"] = 100
    elif kind == "missing":
        del payload["stages"]["test"]
    elif kind == "commit":
        payload["sha"] = "e" * 40
    elif kind == "tree":
        payload["tree_sha"] = "e" * 40
    remote.payload = "SECRET invalid JSON" if kind == "json" else json.dumps(payload)
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote, evaluate=lambda _: pytest.fail("invalid evidence reached policy"))
    assert caught.value.reason == reason
    assert "SECRET" not in str(caught.value)
    assert remote.attempts == 1
    assert remote.sleeps == []


@pytest.mark.parametrize("when", ["initial", "fetch", "backoff", "completion"])
def test_stale_candidate_never_adopts_new_head(remote, when):
    if when == "initial":
        remote.observed = "e" * 40
    if when == "fetch":
        remote.fetch_head = "e" * 40
    if when == "backoff":
        remote.available_at = 2

        class Sleeper(list):
            def append(self, value):
                super().append(value)
                remote.observed = "e" * 40

        remote.sleeps = Sleeper()

    def evaluate(value):
        if when == "completion":
            remote.observed = "e" * 40
        return {"verified": True}

    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote, evaluate=evaluate)
    assert caught.value.reason == "stale_candidate"
    assert caught.value.diagnostics["expected_head"] == HEAD
    assert all(head == HEAD for _, head in remote.notes_read)


@pytest.mark.parametrize("reason", ["git_failure", "git_timeout", "io_failure"])
def test_unclassified_and_transport_failures_do_not_retry(remote, reason):
    remote.fail = attestation.InvalidAttestation("failed", reason=reason)
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote)
    assert caught.value.reason == reason
    assert remote.sleeps == []


@pytest.mark.parametrize("mode", ["verify", "peer_review", "environment", "manual_merge"])
def test_git_snapshot_reads_remote_note_without_replacing_local_note(tmp_repo, tmp_path, mode):
    if mode != "verify":
        write(
            tmp_repo,
            ".agentic-preflight.toml",
            f"[policy]\nhigh_risk_paths = ['src/**']\n[approval]\nmode = '{mode}'\n",
        )
        commit_all(tmp_repo, "protected policy")
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    write(tmp_repo, "src/new.py", "pass\n")
    head = commit_all(tmp_repo, "candidate")
    git("branch", "candidate", head, cwd=tmp_repo)
    valid = evidence(head, git("rev-parse", f"{head}^{{tree}}", cwd=tmp_repo))
    attestation.write(tmp_repo, valid)
    source = tmp_path / "source.git"
    git("clone", "--bare", str(tmp_repo), str(source), cwd=tmp_repo)
    git("push", str(source), attestation.NOTES_REF, cwd=tmp_repo)
    git("remote", "add", "source", str(source), cwd=tmp_repo)
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-f",
        "-m",
        "SECRET bad local note",
        head,
        cwd=tmp_repo,
    )
    git("checkout", "--detach", base, cwd=tmp_repo)
    agent = ScriptedAgent(tmp_repo)
    extra = []
    if mode != "verify":
        reviews_file = tmp_path / "reviews.json"
        reviews_file.write_text("[]")
        extra = ["--mode", "approval", "--reviews-file", str(reviews_file), "--author", "author"]
    result = agent.run(
        "hosted-check",
        head,
        "--base",
        base,
        "--source-remote",
        "source",
        "--head-ref",
        "refs/heads/candidate",
        *extra,
        expect=ExitCode.NEEDS_HUMAN if mode in {"peer_review", "environment"} else ExitCode.OK,
    )
    if mode == "verify":
        assert result["data"]["verified"] is True
    else:
        assert result["data"]["approved"] is (mode == "manual_merge")
        assert result["data"]["manual_merge_required"] is (mode == "manual_merge")
    assert len(result["data"]["availability"]["attempts"]) == 1
    assert result["data"]["availability"]["attempts"][0]["note_present"]
    assert "SECRET" not in json.dumps(result)
    assert (
        git("notes", f"--ref={attestation.NOTES_REF}", "show", head, cwd=tmp_repo)
        == "SECRET bad local note"
    )
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=tmp_repo)


@pytest.mark.parametrize("failure", ["absent", "auth", "timeout", "io"])
def test_git_absence_is_distinct_from_transport(tmp_repo, monkeypatch, failure):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 30
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git SECRET", 30)
        if failure == "io":
            raise PermissionError("SECRET")
        return subprocess.CompletedProcess([], 2 if failure == "absent" else 128, "", "SECRET")

    monkeypatch.setattr(availability.gitx, "run", run)
    source = availability.RemoteNotes(tmp_repo, "origin")
    if failure == "absent":
        assert source.advertised(attestation.NOTES_REF) is None
    else:
        with pytest.raises(attestation.InvalidAttestation) as caught:
            source.advertised(attestation.NOTES_REF)
        assert (
            caught.value.reason
            == {"auth": "git_failure", "timeout": "git_timeout", "io": "io_failure"}[failure]
        )
        assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("command", ["verify", "approval-check"])
def test_local_commands_share_schema_reason_and_stay_offline(
    tmp_repo, tmp_path, monkeypatch, command
):
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    payload = json.loads(attestation.encode(evidence(head)))
    payload["future_field"] = "SECRET"
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-m",
        json.dumps(payload),
        head,
        cwd=tmp_repo,
    )
    monkeypatch.setattr(
        availability.RemoteNotes,
        "advertised",
        lambda *_: pytest.fail("local command accessed remote"),
    )
    args = [command, head]
    if command == "approval-check":
        reviews = tmp_path / "reviews.json"
        reviews.write_text("[]")
        args += ["--base", head, "--reviews-file", str(reviews), "--author", "author"]
    result = ScriptedAgent(tmp_repo).run(*args, expect=ExitCode.STAGE_FAILED)
    assert result["error"]["code"] == "attestation_failed"
    assert result["data"]["reason"] == "incompatible_schema"
    assert "SECRET" not in json.dumps(result)
    assert "compatible" in result["next"]["instruction"]
    assert result["next"]["command"] is None


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("git SECRET", 30), PermissionError("SECRET")]
)
def test_validation_io_is_bounded_and_not_retried(remote, monkeypatch, failure):
    def tree(*args):
        assert availability.gitx._COMMAND_TIMEOUT.get() == 30
        raise failure

    monkeypatch.setattr(attestation.gitx, "tree_sha", tree)
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote)
    assert caught.value.reason in {"git_timeout", "io_failure"}
    assert "SECRET" not in str(caught.value)
    assert remote.attempts == 1
    assert remote.sleeps == []
    assert availability.gitx._COMMAND_TIMEOUT.get() is None


def test_cleanup_failure_preserves_primary_reason_and_diagnostics(remote, monkeypatch):
    original = remote.run

    def run(self, *args):
        if args[0] == "update-ref":
            raise attestation.InvalidAttestation("cleanup failed", reason="git_failure")
        return original(self, *args)

    monkeypatch.setattr(remote, "run", run)
    remote.payload = "bad JSON"
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote)
    assert caught.value.reason == "malformed_payload"
    assert len(caught.value.diagnostics["cleanup_errors"]) == 2


def test_fetch_failure_never_uses_existing_note(remote, monkeypatch):
    original = remote.fetch

    def fetch(self, ref, target):
        if ref.startswith("refs/notes/"):
            raise attestation.InvalidAttestation("fetch failed", reason="git_failure")
        return original(self, ref, target)

    monkeypatch.setattr(remote, "fetch", fetch)
    with pytest.raises(availability.HostedCheckFailed) as caught:
        check(remote)
    assert caught.value.reason == "git_failure"
    assert remote.notes_read == []
    assert remote.sleeps == []


@pytest.mark.parametrize("validator", ["refresh", "declaration"])
def test_nested_git_failure_retains_safe_classification(remote, monkeypatch, validator):
    from agentic_preflight import ci_policy, refresh_validation

    value = evidence().model_copy(update={"schema_version": 5 if validator == "refresh" else 6})

    def fail(*args):
        raise availability.gitx.GitError(["show", "SECRET"], 128, "SECRET transport failure")

    monkeypatch.setattr(
        refresh_validation, "verify_evidence", fail if validator == "refresh" else lambda *_: None
    )
    monkeypatch.setattr(ci_policy, "verify_declaration", fail)
    with pytest.raises(attestation.InvalidAttestation) as caught:
        attestation.verify_value(".", value, HEAD, purpose="local")
    assert caught.value.reason == "git_failure"
    assert "128" in str(caught.value)
    assert "SECRET" not in str(caught.value)
