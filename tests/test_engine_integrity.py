"""Engine guards: fix-commit provenance, branch preconditions, runner release, hook exemption."""

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
