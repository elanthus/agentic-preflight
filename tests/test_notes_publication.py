"""Publication sends only the pushed commits' attestation notes (GHSA-r5jg-frwr-44qc)."""

import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentic_preflight import gitx
from agentic_preflight.attestation import NOTES_REF
from agentic_preflight.envelope import ExitCode
from agentic_preflight.runs import publish
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

CONFIG = (
    "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
    "\n[worktree]\nmode = 'reusable'\n"
)
MANUAL_CONFIG = CONFIG + "\n[gate]\nmode = 'manual'\n"


def verify(repo: Path, tmp_path: Path) -> ScriptedAgent:
    """Drive the checked-out branch to VERIFIED, which writes its note locally."""
    agent = ScriptedAgent(repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    return agent


def remote_notes(remote: Path) -> set[str]:
    """The commits annotated under the remote notes ref."""
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", NOTES_REF], cwd=remote, capture_output=True
    ).returncode:
        return set()
    return set(gitx.note_blobs(remote, NOTES_REF))


def private_attested_commit(repo: Path, tmp_path: Path, back: str = "feature/x") -> str:
    """Attest a commit on a branch that is aborted and never pushed."""
    git("switch", "-c", "private-wip", "main", cwd=repo)
    write(repo, "src/private.py", "SECRET_PLAN = 'not for the remote'\n")
    private = commit_all(repo, "private work in progress")
    verify(repo, tmp_path).run("abort")
    assert gitx.read_note(repo, NOTES_REF, private) is not None
    git("switch", back, cwd=repo)
    return private


def peer_clone(remote: Path, tmp_path: Path, branch: str) -> tuple[Path, str]:
    """A second clone with one new commit on ``branch``."""
    peer = tmp_path / f"peer-{branch.replace('/', '-')}"
    git("clone", str(remote), str(peer), cwd=tmp_path)
    git("switch", "-c", branch, "origin/main", cwd=peer)
    write(peer, f"src/{branch.replace('/', '_')}.py", "PEER = True\n")
    return peer, commit_all(peer, f"peer work on {branch}")


def test_pushing_one_branch_does_not_publish_an_unpushed_branchs_note(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, CONFIG)
    private = private_attested_commit(feature_repo, tmp_path)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)

    gate = agent.run("gate")
    assert gate["data"]["published_notes"] == [head]
    assert private not in gate["data"]["published_notes"]
    assert f"{NOTES_REF}:{NOTES_REF}" not in gate["data"]["refspec"]
    assert f"{publish.NOTES_COMMIT_PLACEHOLDER}:{NOTES_REF}" in gate["data"]["refspec"]
    dry = agent.run("push", "--confirm", gate["data"]["token"], "--dry-run")
    assert dry["data"]["published_notes"] == [head]

    pushed = agent.run("push", "--confirm", gate["data"]["token"])

    assert pushed["data"]["published_notes"] == [head]
    assert remote_notes(bare_remote) == {head}
    assert gitx.read_note(bare_remote, NOTES_REF, head) == gitx.read_note(
        feature_repo, NOTES_REF, head
    )
    # The unpushed commit's note stays local, and so does the commit.
    assert gitx.read_note(feature_repo, NOTES_REF, private) is not None
    assert subprocess.run(
        ["git", "cat-file", "-e", private], cwd=bare_remote, capture_output=True
    ).returncode


def test_two_clones_pushing_different_branches_keep_both_notes(feature_repo, bare_remote, tmp_path):
    configure_base(feature_repo, CONFIG)
    first_private = private_attested_commit(feature_repo, tmp_path)
    peer, _ = peer_clone(bare_remote, tmp_path, "feature/y")
    second_private = private_attested_commit(peer, tmp_path, back="feature/y")
    first = verify(feature_repo, tmp_path)
    second = verify(peer, tmp_path)
    first_head = git("rev-parse", "HEAD", cwd=feature_repo)
    second_head = git("rev-parse", "HEAD", cwd=peer)

    pushed = second.run("push", "--confirm", second.run("gate")["data"]["token"])
    assert pushed["data"]["published_notes"] == [second_head]
    assert remote_notes(bare_remote) == {second_head}
    pushed = first.run("push", "--confirm", first.run("gate")["data"]["token"])
    assert pushed["data"]["published_notes"] == [first_head]

    # Each clone's unpushed attested commit kept its note local.
    assert remote_notes(bare_remote) == {first_head, second_head}
    assert not {first_private, second_private} & remote_notes(bare_remote)
    # A later synchronization in either clone merges cleanly.
    gitx.fetch_notes(peer, "origin", NOTES_REF)
    assert {first_head, second_head} <= set(gitx.note_blobs(peer, NOTES_REF))


def test_a_concurrent_remote_notes_update_is_kept_not_overwritten(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, CONFIG)
    private = private_attested_commit(feature_repo, tmp_path)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "peer", peer_commit, cwd=peer)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    token = agent.run("gate")["data"]["token"]
    assert remote_notes(bare_remote) == set()

    original = gitx.notes_publication
    bases: list[str | None] = []

    @contextmanager
    def racing(cwd, notes_ref, base, commits):
        bases.append(base)
        if len(bases) == 1:
            # The other clone publishes after this clone fetched the (absent)
            # remote notes ref and before it pushes.
            git("push", "origin", "feature/y", NOTES_REF, cwd=peer)
        with original(cwd, notes_ref, base, commits) as staged:
            yield staged

    monkeypatch.setattr(gitx, "notes_publication", racing)
    pushed = agent.run("push", "--confirm", token)

    assert pushed["state"] == "PUSHED"
    assert bases[0] is None
    assert bases[1] == git("rev-parse", NOTES_REF, cwd=peer)
    assert remote_notes(bare_remote) == {head, peer_commit}
    assert gitx.read_note(bare_remote, NOTES_REF, peer_commit) == "peer"
    assert private not in remote_notes(bare_remote)


def test_publication_stops_after_bounded_retries_without_pushing_the_branch(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, CONFIG)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    git("push", "origin", "feature/y", cwd=peer)
    agent = verify(feature_repo, tmp_path)
    token = agent.run("gate")["data"]["token"]

    original = gitx.notes_publication
    calls = []

    @contextmanager
    def always_racing(cwd, notes_ref, base, commits):
        calls.append(base)
        git(
            "notes",
            f"--ref={NOTES_REF}",
            "add",
            "-f",
            "-m",
            str(len(calls)),
            peer_commit,
            cwd=peer,
        )
        git("push", "origin", NOTES_REF, cwd=peer)
        with original(cwd, notes_ref, base, commits) as staged:
            yield staged

    monkeypatch.setattr(gitx, "notes_publication", always_racing)
    agent.run("push", "--confirm", token, expect=ExitCode.ERROR)

    assert len(calls) == publish._NOTES_PUBLISH_ATTEMPTS
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/feature/x"],
        cwd=bare_remote,
        capture_output=True,
    ).returncode
    assert agent.run("status")["state"] == "AWAITING_PUSH_CONFIRM"


def test_staging_refs_are_removed_after_publication(feature_repo, bare_remote, tmp_path):
    configure_base(feature_repo, CONFIG)
    agent = verify(feature_repo, tmp_path)
    agent.run("push", "--confirm", agent.run("gate")["data"]["token"])

    refs = git("for-each-ref", "--format=%(refname)", "refs/notes/", cwd=feature_repo)
    assert refs.split() == [NOTES_REF]


def test_manual_gate_command_publishes_only_the_pushed_commits_note(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, MANUAL_CONFIG)
    private = private_attested_commit(feature_repo, tmp_path)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)

    env = agent.run("gate", expect=ExitCode.NEEDS_HUMAN)
    command = env["data"]["manual_command"]
    assert env["data"]["published_notes"] == [head]
    assert f"{NOTES_REF}:{NOTES_REF}" not in command
    assert publish.NOTES_COMMIT_PLACEHOLDER not in command
    # The remote had no notes ref when the gate fetched, so the lease requires none.
    assert f"--force-with-lease={NOTES_REF}: " in command

    for part in command.split(" && "):
        subprocess.run(shlex.split(part), cwd=feature_repo, check=True, capture_output=True)

    assert remote_notes(bare_remote) == {head}
    assert private not in remote_notes(bare_remote)


def test_manual_gate_command_is_rejected_once_the_remote_notes_move(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, MANUAL_CONFIG)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    agent = verify(feature_repo, tmp_path)
    env = agent.run("gate", expect=ExitCode.NEEDS_HUMAN)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "peer", peer_commit, cwd=peer)
    git("push", "origin", "feature/y", NOTES_REF, cwd=peer)

    for part in env["data"]["manual_command"].split(" && "):
        result = subprocess.run(shlex.split(part), cwd=feature_repo, capture_output=True)
    assert result.returncode != 0
    assert remote_notes(bare_remote) == {peer_commit}
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/feature/x"],
        cwd=bare_remote,
        capture_output=True,
    ).returncode


def test_manual_gate_rejects_a_fetch_that_changes_the_head_attestation(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, MANUAL_CONFIG)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    git("push", "origin", NOTES_REF, cwd=feature_repo)
    # Another writer replaces the head's note on the remote; the gate's fetch
    # fast-forwards the local notes ref onto it.
    git("notes", f"--ref={NOTES_REF}", "add", "-f", "-m", "replaced", head, cwd=bare_remote)

    env = agent.run("gate", expect=ExitCode.STAGE_FAILED)

    assert env["error"]["code"] == "attestation_failed"
    assert "notes synchronization" in env["error"]["message"]
    assert agent.run("status")["state"] == "VERIFIED"


@pytest.mark.parametrize("remote_has_notes", [False, True])
def test_notes_publication_adds_only_the_named_commits(tmp_repo, tmp_path, remote_has_notes):
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    write(tmp_repo, "a.txt", "a\n")
    pushed = commit_all(tmp_repo, "pushed")
    write(tmp_repo, "b.txt", "b\n")
    local_only = commit_all(tmp_repo, "local only")
    remote_tip = None
    if remote_has_notes:
        git("notes", f"--ref={NOTES_REF}", "add", "-m", "remote", base, cwd=tmp_repo)
        remote_tip = git("rev-parse", NOTES_REF, cwd=tmp_repo)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "pushed", pushed, cwd=tmp_repo)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "private", local_only, cwd=tmp_repo)

    with gitx.notes_publication(tmp_repo, NOTES_REF, remote_tip, [pushed]) as (ref, included):
        assert ref is not None
        assert included == [pushed]
        annotated = set(gitx.note_blobs(tmp_repo, ref))
        if remote_tip is not None:
            assert gitx.is_ancestor(tmp_repo, remote_tip, ref)
    assert annotated == ({base, pushed} if remote_has_notes else {pushed})
    assert local_only not in annotated
    assert git("for-each-ref", "refs/notes/", cwd=tmp_repo).count("\n") == 0


def test_push_refspecs_never_publish_the_local_notes_ref():
    sha = "a" * 40
    notes_commit = "b" * 40
    assert publish._push_refspecs(sha, "feature/x", notes_commit) == [
        f"{sha}:refs/heads/feature/x",
        f"{notes_commit}:{NOTES_REF}",
    ]
    assert publish._push_refspecs(sha, "feature/x", None) == [f"{sha}:refs/heads/feature/x"]
    assert not any(spec.startswith(f"{NOTES_REF}:") for spec in publish._push_refspecs(sha, "x"))


def test_an_attested_intermediate_commit_publishes_with_the_head(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, CONFIG)
    verify(feature_repo, tmp_path).run("abort")
    intermediate = git("rev-parse", "HEAD", cwd=feature_repo)
    write(feature_repo, "src/second.py", "SECOND = True\n")
    head = commit_all(feature_repo, "second change")
    agent = verify(feature_repo, tmp_path)

    gate = agent.run("gate")
    assert gate["data"]["published_notes"] == [intermediate, head]
    pushed = agent.run("push", "--confirm", gate["data"]["token"])

    assert pushed["data"]["published_notes"] == [intermediate, head]
    assert remote_notes(bare_remote) == {intermediate, head}


def test_a_merge_range_publishes_the_merged_commits_notes_only(feature_repo, bare_remote, tmp_path):
    configure_base(feature_repo, CONFIG)
    private = private_attested_commit(feature_repo, tmp_path)
    git("switch", "-c", "side", "main", cwd=feature_repo)
    write(feature_repo, "src/side.py", "SIDE = True\n")
    side = commit_all(feature_repo, "side change")
    verify(feature_repo, tmp_path).run("abort")
    git("switch", "feature/x", cwd=feature_repo)
    git("merge", "--no-ff", "-m", "merge side", "side", cwd=feature_repo)
    merge = git("rev-parse", "HEAD", cwd=feature_repo)
    agent = verify(feature_repo, tmp_path)

    gate = agent.run("gate")
    assert set(gate["data"]["published_notes"]) == {side, merge}
    agent.run("push", "--confirm", gate["data"]["token"])

    assert remote_notes(bare_remote) == {side, merge}
    assert private not in remote_notes(bare_remote)


def test_published_notes_are_folded_so_a_rewritten_note_still_syncs(
    feature_repo, bare_remote, tmp_path
):
    configure_base(feature_repo, CONFIG)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    # After this clone attested, another clone starts the remote notes history,
    # so the published commit and the local notes ref share no common tip.
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "peer", peer_commit, cwd=peer)
    git("push", "origin", "feature/y", NOTES_REF, cwd=peer)
    agent.run("push", "--confirm", agent.run("gate")["data"]["token"])
    remote_tip = git("rev-parse", NOTES_REF, cwd=bare_remote)
    assert gitx.is_ancestor(feature_repo, remote_tip, NOTES_REF)

    # Without the fold, the local and published histories diverge after the
    # peer's commit and disagree on this note, so the next synchronization
    # would fail its three-way notes merge.
    git("notes", f"--ref={NOTES_REF}", "add", "-f", "-m", "rewritten", head, cwd=feature_repo)

    assert gitx.fetch_notes(feature_repo, "origin", NOTES_REF) == remote_tip
    assert gitx.read_note(feature_repo, NOTES_REF, head) == "rewritten"


def test_a_failed_follow_up_query_reports_the_original_push_error(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, CONFIG)
    peer, _ = peer_clone(bare_remote, tmp_path, "feature/x")
    git("push", "origin", "feature/x", cwd=peer)
    agent = verify(feature_repo, tmp_path)
    token = agent.run("gate")["data"]["token"]

    def unreachable(cwd, remote, ref):
        raise gitx.GitError(["ls-remote", remote, ref], 128, "QUERY-FAILED")

    monkeypatch.setattr(gitx, "remote_ref_tip", unreachable)
    env = agent.run("push", "--confirm", token, expect=ExitCode.ERROR)

    message = env["error"]["message"]
    assert message.startswith("git push --atomic")
    assert "QUERY-FAILED" not in message
    assert agent.run("status")["state"] == "AWAITING_PUSH_CONFIRM"


def test_leftover_temporary_notes_refs_are_swept(feature_repo, bare_remote, tmp_path):
    configure_base(feature_repo, CONFIG)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    stale = [
        "refs/notes/agentic-preflight-publish-99999-deadbeef",
        "refs/agentic-preflight/fetched-notes-99999",
    ]
    for ref in stale:
        git("update-ref", ref, head, cwd=feature_repo)
    agent = verify(feature_repo, tmp_path)
    agent.run("push", "--confirm", agent.run("gate")["data"]["token"])
    for ref in stale:
        assert git("for-each-ref", ref, cwd=feature_repo) == ""

    for ref in stale:
        git("update-ref", ref, head, cwd=feature_repo)
    agent.run("finish")
    agent.run("gc")
    for ref in stale:
        assert git("for-each-ref", ref, cwd=feature_repo) == ""
    assert git("show-ref", "--verify", NOTES_REF, cwd=feature_repo)


def test_notes_publication_reports_only_added_or_changed_notes(tmp_repo):
    write(tmp_repo, "a.txt", "a\n")
    same = commit_all(tmp_repo, "same note")
    write(tmp_repo, "b.txt", "b\n")
    rewritten = commit_all(tmp_repo, "rewritten note")
    write(tmp_repo, "c.txt", "c\n")
    new = commit_all(tmp_repo, "new note")
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "same", same, cwd=tmp_repo)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "remote value", rewritten, cwd=tmp_repo)
    remote_tip = git("rev-parse", NOTES_REF, cwd=tmp_repo)
    git("notes", f"--ref={NOTES_REF}", "add", "-f", "-m", "local value", rewritten, cwd=tmp_repo)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "new", new, cwd=tmp_repo)

    commits = [same, rewritten, new]
    with gitx.notes_publication(tmp_repo, NOTES_REF, remote_tip, commits) as (ref, changed):
        assert ref is not None
        assert changed == [rewritten, new]
        assert gitx.read_note(tmp_repo, ref, rewritten) == "local value"
        assert gitx.read_note(tmp_repo, ref, same) == "same"


@contextmanager
def _race(original, bases, interfere):
    """Wrap ``notes_publication`` to change the remote once, after the fetch."""

    @contextmanager
    def racing(cwd, notes_ref, base, commits):
        bases.append(base)
        if len(bases) == 1:
            interfere()
        with original(cwd, notes_ref, base, commits) as staged:
            yield staged

    yield racing


def test_a_remote_notes_ref_deleted_after_the_fetch_is_not_recreated_from_it(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, CONFIG)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "peer", peer_commit, cwd=peer)
    git("push", "origin", "feature/y", NOTES_REF, cwd=peer)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    token = agent.run("gate")["data"]["token"]

    bases: list[str | None] = []
    with _race(
        gitx.notes_publication,
        bases,
        lambda: git("update-ref", "-d", NOTES_REF, cwd=bare_remote),
    ) as racing:
        monkeypatch.setattr(gitx, "notes_publication", racing)
        pushed = agent.run("push", "--confirm", token)

    assert pushed["state"] == "PUSHED"
    assert bases[0] is not None
    assert bases[1] is None
    # The deletion is honoured: the rebuilt ref carries only this push's note.
    assert remote_notes(bare_remote) == {head}


def test_a_remote_notes_ref_rewound_after_the_fetch_is_not_moved_forward_again(
    feature_repo, bare_remote, tmp_path, monkeypatch
):
    configure_base(feature_repo, CONFIG)
    peer, peer_commit = peer_clone(bare_remote, tmp_path, "feature/y")
    main = git("rev-parse", "origin/main", cwd=peer)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "kept", peer_commit, cwd=peer)
    git("push", "origin", "feature/y", NOTES_REF, cwd=peer)
    rewound = git("rev-parse", NOTES_REF, cwd=peer)
    git("notes", f"--ref={NOTES_REF}", "add", "-m", "withdrawn", main, cwd=peer)
    git("push", "origin", NOTES_REF, cwd=peer)
    agent = verify(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    token = agent.run("gate")["data"]["token"]

    bases: list[str | None] = []
    with _race(
        gitx.notes_publication,
        bases,
        lambda: git("update-ref", NOTES_REF, rewound, cwd=bare_remote),
    ) as racing:
        monkeypatch.setattr(gitx, "notes_publication", racing)
        pushed = agent.run("push", "--confirm", token)

    assert pushed["state"] == "PUSHED"
    assert bases[1] == rewound
    assert gitx.is_ancestor(bare_remote, rewound, NOTES_REF)
    # The withdrawn note stays withdrawn; this push adds only its own.
    assert remote_notes(bare_remote) == {peer_commit, head}
