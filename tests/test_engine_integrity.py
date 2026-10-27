"""Engine guards: fix-commit provenance, branch preconditions, runner release, hook exemption."""

import json
from pathlib import Path

from agentic_preflight import hook
from agentic_preflight.envelope import ExitCode
from agentic_preflight.hook import NOTES_REF
from agentic_preflight.runs import publish
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent, findings_json, green_run

ZERO = "0" * 40


def test_isolated_fix_cannot_cite_an_already_reviewed_commit(feature_repo, tmp_path):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[worktree]\nmode = 'strict'\n\n[docs]\nenabled = false\n\n"
        "[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure")
    original = git("log", "--format=%H", "-1", "--", "src/app.py", cwd=feature_repo).strip()
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    submitted = agent.run(
        "submit-findings",
        "--file",
        findings_json(
            tmp_path,
            [
                {
                    "path": "src/app.py",
                    "severity": "high",
                    "action": "auto_fix",
                    "title": "loud flag is ignored",
                    "detail": "the loud parameter is never used",
                }
            ],
        ),
    )
    assert submitted["state"] == "REVIEW_BLOCKED"

    refused = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        "--commit",
        original,
        expect=ExitCode.PRECONDITION,
    )
    assert refused["error"]["code"] == "invalid_response"
    assert "reviewed head" in refused["error"]["message"]

    agent.run("verify", expect=ExitCode.STAGE_FAILED)
    status = agent.run("status")
    assert status["state"] == "REVIEW_BLOCKED"
    notes = git("notes", "--ref=refs/notes/agentic-preflight", "list", cwd=feature_repo)
    assert notes.strip() == ""


def test_start_rejects_a_detached_head(feature_repo):
    git("switch", "--detach", cwd=feature_repo)
    env = ScriptedAgent(feature_repo).run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_branch"
    assert "detached" in env["error"]["message"]


def test_start_rejects_an_unborn_branch(feature_repo):
    git("switch", "--orphan", "fresh", cwd=feature_repo)
    env = ScriptedAgent(feature_repo).run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_branch"
    assert "no commits" in env["error"]["message"]


def test_push_refspec_names_the_attested_sha_and_a_full_branch_ref():
    sha = "a" * 40
    assert publish._push_refspecs(sha, "feature/x") == [
        f"{sha}:refs/heads/feature/x",
        f"{NOTES_REF}:{NOTES_REF}",
    ]


def test_gate_refspec_pushes_the_attested_head(feature_repo, bare_remote, tmp_path):
    agent = green_run(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo).strip()
    gate = agent.run("gate")
    assert f"{head}:refs/heads/feature/x" in gate["data"]["refspec"]


def test_reusable_restart_after_source_moves_releases_the_runner(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "reusable")
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start")
    write(feature_repo, "src/extra.py", "v = 1\n")
    commit_all(feature_repo, "advance")

    second = agent.run("start")

    assert second["run_id"] != first["run_id"]
    assert second["data"]["worktree_path"] == first["data"]["worktree_path"]


def test_notes_ref_pushed_onto_a_branch_is_not_exempt():
    update = hook.RefUpdate(NOTES_REF, "a" * 40, "refs/heads/main", ZERO)
    assert update.is_attestation_note is False
    blocked = hook.evaluate([update], is_ancestor=lambda *_: True, has_attestation=lambda _: False)
    assert blocked.allowed is False


def test_notes_ref_pushed_to_the_notes_ref_is_exempt():
    update = hook.RefUpdate(NOTES_REF, "a" * 40, NOTES_REF, ZERO)
    assert update.is_attestation_note is True


def _state_root(repo) -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo).strip()
    return Path(common) / "agentic-preflight"


def _reusable_run(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "reusable")
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start")
    write(feature_repo, "src/extra.py", "v = 1\n")
    commit_all(feature_repo, "advance")
    return agent, Path(first["data"]["worktree_path"])


def test_orphaned_run_with_a_dirty_runner_keeps_its_lease(feature_repo):
    agent, runner = _reusable_run(feature_repo)
    write(runner, "src/app.py", "uncommitted repair\n")

    env = agent.run("start", expect=ExitCode.NEEDS_HUMAN)

    assert "still leased" in env["error"]["message"]
    assert (runner / "src/app.py").read_text() == "uncommitted repair\n"


def test_orphaned_run_with_an_advanced_runner_keeps_its_lease(feature_repo):
    agent, runner = _reusable_run(feature_repo)
    write(runner, "src/app.py", "unrecorded repair\n")
    commit_all(runner, "unrecorded repair")
    advanced = git("rev-parse", "HEAD", cwd=runner).strip()

    env = agent.run("start", expect=ExitCode.NEEDS_HUMAN)

    assert "still leased" in env["error"]["message"]
    assert git("rev-parse", "HEAD", cwd=runner).strip() == advanced


def test_runner_release_failure_still_orphans_and_reports_the_lease(feature_repo, monkeypatch):
    import importlib

    startmod = importlib.import_module("agentic_preflight.runs.start")

    agent, _runner = _reusable_run(feature_repo)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(startmod, "_release_run_worktree", boom)
    env = agent.run("start", expect=ExitCode.NEEDS_HUMAN)

    assert "still leased" in env["error"]["message"]
    events = "".join(
        p.read_text() for p in (_state_root(feature_repo) / "runs").rglob("*") if p.is_file()
    )
    assert "runner_release_failed" in events


def test_conflict_retry_refuses_fixes_that_never_landed(feature_repo, tmp_path):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[worktree]\nmode = 'strict'\n\n[docs]\nenabled = false\n\n"
        "[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    status = agent.run("stage", "run", "test")
    run_path = _state_root(feature_repo) / "runs" / status["run_id"] / "run.json"
    doc = json.loads(run_path.read_text())
    doc["state"] = "MERGEBACK_CONFLICT"
    doc["fix_commits"] = [doc["head_sha"]]
    run_path.write_text(json.dumps(doc))

    env = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "fixes_not_landed"
    notes = git("notes", "--ref=refs/notes/agentic-preflight", "list", cwd=feature_repo)
    assert notes.strip() == ""
