"""The opt-in records bundle that a schema 8 note pins by commit."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_preflight import attestation as attestationmod
from agentic_preflight import evidence_transport, gitx, records
from agentic_preflight.config import Config, config_digest, load_config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import RecordsPointer, Stage
from agentic_preflight.store import Store
from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

PROTECTED = "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
PUBLISHING = PROTECTED + "\n[records]\npublish = true\n"
USER_MARKER = "user-level-marker-5d1e"
REVIEW_TRANSCRIPT = b"reviewer: looked at every unit\n"


def _store(repo: Path) -> Store:
    return Store(repo / ".git" / "agentic-preflight")


def _user_config(body: str) -> None:
    path = Path.home() / ".config" / "agentic-preflight" / "config.toml"
    write(path.parent, path.name, body)


def _run(agent: ScriptedAgent, repo: Path, tmp_path: Path, *, transcript: bool = True) -> str:
    run_id = agent.run("start", "--client", "claude-code")["run_id"]
    agent.run("context")
    submit = ["submit-findings", "--file", findings_json(tmp_path, [])]
    if transcript:
        notes = tmp_path / "review-notes.txt"
        notes.write_bytes(REVIEW_TRANSCRIPT)
        submit += ["--transcript", str(notes)]
    agent.run(*submit)
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    return run_id


def _head(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo)


def _note(repo: Path) -> attestationmod.Attestation:
    value = attestationmod.read(repo, _head(repo))
    assert value is not None
    return value


def _files(repo: Path, commit: str) -> dict[str, bytes]:
    return {path: gitx.read_blob(repo, oid) for _, _, oid, path in gitx.tree_entries(repo, commit)}


@pytest.fixture
def bundled(feature_repo, tmp_path) -> tuple[Path, str, str]:
    configure_base(feature_repo, PUBLISHING)
    _user_config(f"[context]\nextra_paths = ['{USER_MARKER}/*']\n")
    agent = ScriptedAgent(feature_repo)
    run_id = _run(agent, feature_repo, tmp_path)
    value = _note(feature_repo)
    assert value.records is not None
    return feature_repo, run_id, value.records.commit


# -- configuration ----------------------------------------------------------------


def test_records_publish_is_strict_and_stays_out_of_snapshots(tmp_path):
    assert Config().records.publish is False
    assert "records" not in Config().model_dump()
    on = Config.model_validate({"records": {"publish": True}})
    assert config_digest(on.model_dump()) == config_digest(Config().model_dump())
    write(tmp_path, ".agentic-preflight.toml", "[records]\npublish = 1\n")
    with pytest.raises(Exception, match="publish"):
        load_config(tmp_path, user_config_dir=tmp_path / "none")


def test_without_publish_no_bundle_is_built(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED)
    agent = ScriptedAgent(feature_repo)
    _run(agent, feature_repo, tmp_path)
    value = _note(feature_repo)
    assert value.records is None
    assert evidence_transport.commits(value) == sorted(
        {value.merge_base_sha, *(i.origin.head_sha for i in value.evidence.values())}
        | {i.origin.base_sha for i in value.evidence.values()}
    )
    assert agent.run("verify", _head(feature_repo))["data"]["records"] is None


# -- what the bundle holds ---------------------------------------------------------


def test_the_bundle_holds_what_the_note_digests(bundled):
    repo, run_id, commit = bundled
    value = _note(repo)
    files = _files(repo, commit)
    manifest = json.loads(files["manifest.json"])

    assert manifest["kind"] == "agentic-preflight-records"
    assert manifest["schema_version"] == 1
    assert (manifest["run_id"], manifest["sha"], manifest["tree_sha"]) == (
        run_id,
        value.sha,
        value.tree_sha,
    )
    assert manifest["events"] == value.events.model_dump(mode="json")
    assert manifest["producer"] == value.producer.model_dump(mode="json")  # type: ignore[union-attr]
    assert manifest["events_copy"] == {"redacted": True, "chain_verifiable": False}
    assert set(manifest["files"]) == set(files) - {"manifest.json"}
    assert hashlib.sha256(files["intent.txt"]).hexdigest() == value.intent_sha256
    for stage in ("lint", "test"):
        data = files[f"logs/{stage}.txt"]
        assert hashlib.sha256(data).hexdigest() == value.stages[Stage(stage)].output_sha256
    assert files["logs/review-transcript.txt"] == REVIEW_TRANSCRIPT
    assert manifest["omitted"] == []
    events = [line for line in files["events.jsonl"].split(b"\n") if line]
    assert len(events) == value.events.lines


def test_the_bundle_commit_carries_a_neutral_identity_and_no_parent(bundled):
    repo, _, commit = bundled
    fields = git("log", "-1", "--format=%an|%ae|%cn|%ce|%P|%G?", commit, cwd=repo).split("|")
    assert fields[:4] == [
        "agentic-preflight",
        "records@agentic-preflight.invalid",
        "agentic-preflight",
        "records@agentic-preflight.invalid",
    ]
    assert fields[4] == ""
    assert fields[5] == "N"
    user = git("log", "-1", "--format=%ae", _head(repo), cwd=repo)
    raw = git("cat-file", "commit", commit, cwd=repo)
    assert user not in raw
    green = _note(repo).green_at
    assert green is not None
    assert git("log", "-1", "--format=%cI", commit, cwd=repo).startswith(green[:16])


def test_the_redacted_events_hold_no_path_user_section_or_token(bundled, bare_remote):
    repo, run_id, commit = bundled
    events_text = _files(repo, commit)["events.jsonl"].decode()
    store = _store(repo)
    run = store.load_run(run_id)

    assert USER_MARKER in store.events_path(run_id).read_text()
    assert USER_MARKER not in events_text
    assert str(repo) not in events_text
    assert str(store.root) not in events_text
    assert str(Path.home()) not in events_text
    assert '"/' not in events_text
    assert "log_path" not in events_text
    created = next(
        json.loads(line)
        for line in events_text.splitlines()
        if json.loads(line).get("event") == "run_created"
    )
    assert "context" not in created["config_snapshot"]
    assert "config_digest" not in created
    ready = next(
        json.loads(line)
        for line in events_text.splitlines()
        if json.loads(line).get("event") == "worktree_ready"
    )
    assert "path" not in ready
    assert "remote" not in ready["sync"]
    # The bundle is built at merge-back, before any gate mints a token.
    assert run.gate_token is None


def test_a_gate_token_in_an_event_is_redacted(tmp_path):
    # A run whose gate already minted a token, as on a merge-back retry after a gate.
    # The token is in a key the allowlist publishes, so only the marker can remove it.
    minted = "0123456789abcdef"
    run = _fake_run(tmp_path).model_copy(update={"gate_token": minted})
    store = Store(tmp_path / "store")
    line = json.dumps({"event": "pushed", "sha": minted}).encode()

    published = json.loads(records.redact_events([line], run, _fake_note(), store))

    assert published["sha"] == records.REDACTED_PATH
    assert minted not in json.dumps(published)
    # Without the marker the same value is published, so the marker is what removes it.
    plain = json.loads(records.redact_events([line], _fake_run(tmp_path), _fake_note(), store))
    assert plain["sha"] == minted


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("/var/build/out", False),
        ("~/notes.txt", False),
        ("~", False),
        ("~someone/notes.txt", False),
        ("C:\\Users\\someone", False),
        ("~10% off for ten or more", True),
        ("strict", True),
    ],
)
def test_only_a_path_shaped_value_is_redacted(value, kept):
    assert (records._scrub({"mode": value}, [])["mode"] == value) is kept


def test_the_intent_keeps_text_that_only_starts_like_a_path():
    markers = ["/home/someone"]
    scrubbed = records._scrub(
        {"intent": "/api/users returns 500 for ~10% of calls", "mode": "/api/users"}, markers
    )
    # The bundle publishes the intent as written in intent.txt, so the shape rule
    # does not apply to it. A marker still redacts it.
    assert scrubbed["intent"] == "/api/users returns 500 for ~10% of calls"
    assert scrubbed["mode"] == records.REDACTED_PATH
    assert records._scrub({"intent": "see /home/someone/plan"}, markers)["intent"] == (
        records.REDACTED_PATH
    )


def test_unknown_events_keep_only_their_name_time_and_sequence(tmp_path):
    store = Store(tmp_path / "store")
    store.append_event("r_1", {"event": "mystery", "secret": "/home/someone", "seq": 4})
    raw = store.read_event_bytes("r_1")
    run = _fake_run(tmp_path)
    copy = records.redact_events(
        [line for line in raw.split(b"\n") if line], run, _fake_note(), store
    )
    published = json.loads(copy)
    assert set(published) == {"event", "at", "seq", "prev_sha256"}
    assert published["prev_sha256"] is None


def test_a_torn_line_keeps_its_place_in_the_copy(tmp_path):
    store = Store(tmp_path / "store")
    copy = records.redact_events([b'{"event": "thr'], _fake_run(tmp_path), _fake_note(), store)
    assert json.loads(copy) == {"torn_line": True}


OVERSIZED = b'{"event": "big", "n": ' + b"9" * 5000 + b"}"
NESTED = b'{"event": "deep", "n": ' + b"[" * 100_000 + b"}"


@pytest.mark.parametrize("line", [OVERSIZED, NESTED])
def test_a_line_json_cannot_decode_is_a_torn_line_in_the_copy(tmp_path, line):
    # json.loads raises a plain ValueError or a RecursionError for these, not
    # JSONDecodeError; neither may stop the bundle from being built.
    store = Store(tmp_path / "store")
    copy = records.redact_events([line], _fake_run(tmp_path), _fake_note(), store)
    assert json.loads(copy) == {"torn_line": True}


@pytest.mark.parametrize("line", [OVERSIZED, NESTED])
def test_an_events_copy_json_cannot_decode_is_a_mismatch_not_a_crash(line):
    note = _fake_note()
    note.events.lines = 1
    with pytest.raises(attestationmod.InvalidAttestation) as refused:
        records._check_events_copy(note, line + b"\n")
    assert refused.value.reason == "records_mismatch"


def _fake_run(tmp_path: Path):
    from tests.conftest import make_run

    return make_run()


def _fake_note():
    from tests.attestation_helpers import make_attestation
    from tests.test_models import _attestation_stages

    return make_attestation(stages=_attestation_stages())


def test_a_user_level_command_log_is_left_out_with_its_reason(feature_repo, tmp_path):
    configure_base(feature_repo, "[docs]\nenabled = false\n\n[records]\npublish = true\n")
    _user_config("[commands]\nlint = 'true'\ntest = 'true'\n")
    agent = ScriptedAgent(feature_repo)
    _run(agent, feature_repo, tmp_path, transcript=False)
    value = _note(feature_repo)
    assert value.records is not None
    files = _files(feature_repo, value.records.commit)
    manifest = json.loads(files["manifest.json"])
    assert "logs/lint.txt" not in files
    assert {"path": "logs/lint.txt", "reason": "user_level_command"} in manifest["omitted"]
    assert {"path": "logs/test.txt", "reason": "user_level_command"} in manifest["omitted"]
    assert agent.run("verify", value.sha)["data"]["records"]["commit"] == value.records.commit


# -- verification -----------------------------------------------------------------


def test_verify_reports_what_it_checked(bundled):
    repo, _, commit = bundled
    data = ScriptedAgent(repo).run("verify", _head(repo))["data"]
    assert data["records"] == {
        "commit": commit,
        "required": True,
        "files": sorted(set(_files(repo, commit)) - {"manifest.json"}),
        "omitted": [],
    }


def _forge(repo: Path, commit: str, change: dict[str, bytes | None]) -> str:
    files = _files(repo, commit)
    for path, data in change.items():
        if data is None:
            files.pop(path, None)
        else:
            files[path] = data
    tree = records._write_files(repo, files)
    return gitx.commit_tree(repo, tree, "forged\n", name="x", email="x@invalid", date="@0 +0000")


def _repoint(repo: Path, forged: str) -> None:
    value = _note(repo)
    changed = value.model_copy(update={"records": RecordsPointer(commit=forged)})
    gitx.write_note(repo, attestationmod.NOTES_REF, value.sha, attestationmod.encode(changed))


@pytest.mark.parametrize(
    "path",
    ["intent.txt", "logs/lint.txt", "logs/test.txt", "logs/review-transcript.txt", "events.jsonl"],
)
def test_tampering_with_a_bundle_file_fails_verify(bundled, path):
    repo, _, commit = bundled
    _repoint(repo, _forge(repo, commit, {path: _files(repo, commit)[path] + b"tampered\n"}))

    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_mismatch"


@pytest.mark.parametrize(
    "path",
    ["intent.txt", "logs/lint.txt", "logs/test.txt", "logs/review-transcript.txt", "events.jsonl"],
)
def test_a_bundle_whose_manifest_agrees_with_tampered_files_still_fails(bundled, path):
    repo, _, commit = bundled
    files = _files(repo, commit)
    tampered = files[path] + b"tampered\n"
    if path == "events.jsonl":
        tampered = files[path] + b'{"event": "forged"}\n'
    manifest = json.loads(files["manifest.json"])
    manifest["files"][path] = {
        "sha256": hashlib.sha256(tampered).hexdigest(),
        "bytes": len(tampered),
    }
    forged = _forge(
        repo,
        commit,
        {path: tampered, "manifest.json": json.dumps(manifest).encode()},
    )
    _repoint(repo, forged)

    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_mismatch"


@pytest.mark.parametrize(
    "change",
    [
        {"manifest.json": b"{}"},
        {"manifest.json": None},
        {"logs/lint.txt": None},
        {"extra.txt": b"more"},
    ],
)
def test_a_malformed_bundle_fails_verify(bundled, change):
    repo, _, commit = bundled
    _repoint(repo, _forge(repo, commit, change))
    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_mismatch"


def _omit(repo: Path, commit: str, path: str, reason: str) -> str:
    """A bundle without ``path`` whose manifest lists it as left out for ``reason``."""
    manifest = json.loads(_files(repo, commit)["manifest.json"])
    del manifest["files"][path]
    manifest["omitted"].append({"path": path, "reason": reason})
    return _forge(repo, commit, {path: None, "manifest.json": json.dumps(manifest).encode()})


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("logs/lint.txt", "log_unavailable"),
        ("logs/test.txt", "log_digest_mismatch"),
        ("logs/review-transcript.txt", "transcript_unavailable"),
    ],
)
def test_a_required_bundle_may_not_leave_out_what_the_note_digests(bundled, path, reason):
    # Otherwise a base that requires records is satisfied by a bundle with no logs.
    repo, _, commit = bundled
    _repoint(repo, _omit(repo, commit, path, reason))

    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_required"
    assert path in env["error"]["message"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        # The note publishes the lint command, so it is not a user-level command.
        ("logs/lint.txt", "user_level_command"),
        # This run executed the test stage, so its evidence was not reused.
        ("logs/test.txt", "evidence_reused"),
        # A reason of the other kind of file.
        ("logs/lint.txt", "transcript_unavailable"),
        ("logs/review-transcript.txt", "log_unavailable"),
    ],
)
def test_an_omission_reason_that_the_note_contradicts_fails_verify(bundled, path, reason):
    repo, _, commit = bundled
    _repoint(repo, _omit(repo, commit, path, reason))

    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_mismatch"


def test_a_voluntary_bundle_may_say_that_a_log_was_unavailable(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED)
    write(feature_repo, ".agentic-preflight.toml", PUBLISHING)
    git("commit", "-am", "publish records on this branch", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    _run(agent, feature_repo, tmp_path, transcript=False)
    value = _note(feature_repo)
    assert value.records is not None
    _repoint(
        feature_repo, _omit(feature_repo, value.records.commit, "logs/lint.txt", "log_unavailable")
    )

    data = agent.run("verify", value.sha)["data"]
    assert data["records"]["required"] is False
    assert data["records"]["omitted"] == [{"path": "logs/lint.txt", "reason": "log_unavailable"}]


def test_mergeback_leaves_out_a_log_it_can_no_longer_read(feature_repo, tmp_path):
    configure_base(feature_repo, PUBLISHING)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    (_store(feature_repo).logs_dir(run_id) / "lint.txt").unlink()

    # The producer says what it could not supply; the base's policy is the verifier's.
    assert agent.run("mergeback")["state"] == "VERIFIED"
    env = agent.run("verify", _head(feature_repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_required"
    assert "logs/lint.txt (log_unavailable)" in env["error"]["message"]


def test_a_config_file_broken_mid_run_refuses_mergeback_before_it_changes_anything(
    feature_repo, tmp_path
):
    # The repository file sets no [records], so the user file's section is in force.
    configure_base(feature_repo, PROTECTED)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    head = _head(feature_repo)
    # [records] publish is strict, so 1 is not a boolean and the file no longer loads.
    _user_config("[records]\npublish = 1\n")

    env = agent.run("mergeback", expect=ExitCode.ERROR)

    assert env["error"]["code"] == "config_error"
    assert _head(feature_repo) == head
    assert git("for-each-ref", "refs/notes/agentic-preflight", cwd=feature_repo) == ""

    # Once the file loads again, the same run merges back.
    _user_config("")
    assert agent.run("mergeback")["state"] == "VERIFIED"


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("the manifest failed validation"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        OSError("read-only file system"),
    ],
)
def test_any_bundle_failure_is_a_failed_attestation_not_a_crash(
    feature_repo, tmp_path, monkeypatch, failure
):
    configure_base(feature_repo, PUBLISHING)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    def broken(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(records, "build_bundle", broken)
    env = agent.run("mergeback", expect=ExitCode.STAGE_FAILED)

    assert env["error"]["code"] == "attestation_failed"
    # A decode error has its own `reason` attribute; it must not pass for the tool's.
    assert env["data"]["reason"] == "records_unavailable"
    assert env["next"]["command"] == "agentic-preflight mergeback"


@pytest.mark.parametrize("mode", ["in_place", "reusable"])
def test_a_bundle_failure_after_the_merge_is_reported_and_mergeback_runs_again(
    feature_repo, tmp_path, monkeypatch, mode
):
    configure_base(feature_repo, PUBLISHING + f"\n[worktree]\nmode = '{mode}'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    def broken(*_args, **_kwargs):
        raise gitx.GitError(["commit-tree"], 128, "no space left on device")

    # Only this patch is scoped to the block. ``monkeypatch.undo()`` would also drop
    # the Git identity and the isolated home that the suite's fixtures set.
    with monkeypatch.context() as patch:
        patch.setattr(records, "build_bundle", broken)
        env = agent.run("mergeback", expect=ExitCode.STAGE_FAILED)

    # A mapped failure that says what stands, not a crash: no note, and a retry.
    assert env["error"]["code"] == "attestation_failed"
    assert "records bundle could not be built" in env["error"]["message"]
    assert env["next"]["command"] == "agentic-preflight mergeback"
    assert git("for-each-ref", "refs/notes/agentic-preflight", cwd=feature_repo) == ""

    assert agent.run("mergeback")["state"] == "VERIFIED"
    assert _note(feature_repo).records is not None
    assert agent.run("verify", _head(feature_repo))["data"]["records"]["omitted"] == []


def test_a_manifest_with_another_anchor_fails_verify(bundled):
    repo, _, commit = bundled
    files = _files(repo, commit)
    manifest = json.loads(files["manifest.json"])
    manifest["events"]["lines"] -= 1
    _repoint(repo, _forge(repo, commit, {"manifest.json": json.dumps(manifest).encode()}))
    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_mismatch"


def test_a_missing_bundle_fails_verify(bundled):
    repo, _, _ = bundled
    _repoint(repo, "0" * 40)
    env = ScriptedAgent(repo).run("verify", _head(repo), expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "missing_records"
    assert "evidence" in env["next"]["instruction"]


def test_a_base_that_requires_records_rejects_a_note_without_them(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED)
    agent = ScriptedAgent(feature_repo)
    _run(agent, feature_repo, tmp_path)
    assert _note(feature_repo).records is None
    head = _head(feature_repo)
    configure_base(feature_repo, PUBLISHING)
    git("reset", "--hard", head, cwd=feature_repo)
    main = git("rev-parse", "main", cwd=feature_repo)

    env = agent.run("verify", head, "--base", main, expect=ExitCode.STAGE_FAILED)
    assert env["data"]["reason"] == "records_required"


def test_a_run_may_publish_a_bundle_the_base_does_not_require(feature_repo, tmp_path):
    configure_base(feature_repo, PROTECTED)
    write(feature_repo, ".agentic-preflight.toml", PUBLISHING)
    git("commit", "-am", "publish records on this branch", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    _run(agent, feature_repo, tmp_path, transcript=False)
    value = _note(feature_repo)
    assert value.records is not None
    data = agent.run("verify", value.sha)["data"]
    assert data["records"]["required"] is False


# -- publication ------------------------------------------------------------------


def test_the_bundle_is_pushed_and_verifies_from_a_fresh_clone(feature_repo, bare_remote, tmp_path):
    configure_base(feature_repo, PUBLISHING)
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    _run(agent, feature_repo, tmp_path)
    value = _note(feature_repo)
    assert value.records is not None
    bundle = value.records.commit
    assert bundle in evidence_transport.commits(value)
    assert git("rev-parse", evidence_transport.ref_for(bundle), cwd=feature_repo) == bundle

    gate = agent.run("gate")
    assert evidence_transport.ref_for(bundle) in gate["data"]["push_commands"][0]
    agent.run("push", "--confirm", gate["data"]["token"])
    assert git("rev-parse", evidence_transport.ref_for(bundle), cwd=bare_remote) == bundle

    runner = tmp_path / "fresh"
    git("init", str(runner), cwd=tmp_path)
    git("remote", "add", "origin", str(bare_remote), cwd=runner)
    git(
        "fetch",
        "--no-tags",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/notes/agentic-preflight:refs/notes/agentic-preflight",
        "+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*",
        cwd=runner,
    )
    main = git("rev-parse", "refs/remotes/origin/main", cwd=runner)
    data = ScriptedAgent(runner).run("verify", value.sha, "--base", main)["data"]
    assert data["verified"] is True
    assert data["records"]["commit"] == bundle

    # Without the evidence refs the bundle is absent and verification says so.
    bare = tmp_path / "no-evidence"
    git("init", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare_remote), cwd=bare)
    git(
        "fetch",
        "--no-tags",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/notes/agentic-preflight:refs/notes/agentic-preflight",
        cwd=bare,
    )
    missing = subprocess.run(["git", "cat-file", "-e", bundle], cwd=bare, capture_output=True)
    assert missing.returncode != 0
