"""Verification takes its merge base and policy from a trusted base, not from the note."""

import json

import pytest

from agentic_preflight import attestation
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json, green_run


def _rewrite_note(repo, sha, **changes):
    payload = attestation.read(repo, sha).model_dump(mode="json")
    payload.update(changes)
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-f",
        "-m",
        json.dumps(payload),
        sha,
        cwd=repo,
    )


def _advance_main(repo, config):
    """Commit a new protected policy on main after the run attested its head."""
    git("switch", "main", cwd=repo)
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, "change protected policy")
    git("switch", "feature/x", cwd=repo)


def test_a_note_cannot_choose_its_own_merge_base(feature_repo, tmp_path):
    write(feature_repo, "src/extra.py", "EXTRA = 1\n")
    commit_all(feature_repo, "add extra")
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    # HEAD^ is a branch commit, so this base would hide the earlier branch change.
    _rewrite_note(feature_repo, sha, merge_base_sha=git("rev-parse", "HEAD^", cwd=feature_repo))

    with pytest.raises(attestation.InvalidAttestation) as caught:
        attestation.verify(feature_repo, sha)
    assert caught.value.reason == "base_mismatch"


@pytest.mark.parametrize("base_ref", ["main~1", "HEAD", "feature/x", "0" * 40])
def test_a_note_base_ref_must_name_the_branch_it_merges_into(feature_repo, tmp_path, base_ref):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    _rewrite_note(feature_repo, sha, base_ref=base_ref)

    with pytest.raises(attestation.InvalidAttestation) as caught:
        attestation.verify(feature_repo, sha)
    assert caught.value.reason == "base_mismatch"
    # The caller's protected base settles the question regardless of the note.
    assert attestation.verify(feature_repo, sha, base="main").sha == sha


def test_a_branch_cannot_relax_the_policy_that_judges_it(feature_repo, tmp_path):
    configure_base(feature_repo, "[commands]\nlint = 'true'\ntest = 'true'\n")
    write(feature_repo, ".agentic-preflight.toml", PROTECTED_CONFIG)
    commit_all(feature_repo, "disable docs on the branch")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    assert attestation.read(feature_repo, sha).config_snapshot["docs"]["enabled"] is False
    with pytest.raises(attestation.InvalidAttestation, match="protected policy enables it"):
        attestation.verify(feature_repo, sha)


def test_attested_commands_must_match_the_protected_policy(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    _advance_main(feature_repo, PROTECTED_CONFIG.replace("lint = 'true'", "lint = 'ruff check'"))

    with pytest.raises(attestation.InvalidAttestation, match="lint command does not match"):
        attestation.verify(feature_repo, sha)


def test_verify_accepts_a_protected_base_that_advanced_past_the_merge_base(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    _advance_main(feature_repo, PROTECTED_CONFIG + "\n[stage]\ntimeout_seconds = 900\n")
    base = git("rev-parse", "main", cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("verify", sha, "--base", base)

    assert env["data"]["verified"] is True


def test_without_base_the_note_names_the_branch_that_judges_it(feature_repo, tmp_path):
    # Documented limit: push and the hook pass no --base, so the note picks the branch.
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    value = attestation.read(feature_repo, sha).model_copy(update={"base_ref": "feature/x"})

    assert attestation.trusted_base(feature_repo, value, None) == sha
    assert attestation.trusted_base(feature_repo, value, "main") == git(
        "rev-parse", "main", cwd=feature_repo
    )
