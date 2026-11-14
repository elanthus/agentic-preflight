"""M5 hook: init, hook-check as a pure predicate, and real git push tests."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation, gitx, hook
from agentic_preflight.envelope import ExitCode
from agentic_preflight.stages import command as command_plan
from tests.conftest import HOOK_LOG_TIMEOUT_SECONDS, git, write
from tests.driver import ScriptedAgent, green_run

#: Runs the CLI's ``hook-check`` as ``python -m agentic_preflight hook-check`` does, with
#: the hook log's time bound set to ``sys.argv[1]`` seconds first.
_HOOK_CHECK = """\
import sys
from agentic_preflight import hooklog
hooklog.TIMEOUT_SECONDS = float(sys.argv[1])
from agentic_preflight.cli import main
main(["hook-check"], prog_name="agentic-preflight")
"""


def hook_check(repo, stdin_text, extra_env=None, *, log_timeout=HOOK_LOG_TIMEOUT_SECONDS):
    """Invoke hook-check as the hook does: a real subprocess with stdin.

    The one difference is the hook log's time bound, ``log_timeout`` seconds, so that a
    loaded machine cannot turn a test's expected log line into a ``timed_out`` write.
    """
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-c", _HOOK_CHECK, str(log_timeout)],
        cwd=repo,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )


def bound_hook_log_on_path(directory, monkeypatch, *, log_timeout=HOOK_LOG_TIMEOUT_SECONDS):
    """Put an ``agentic-preflight`` first on ``PATH`` that runs hook-check like ``hook_check``.

    A real ``git push`` runs the installed hook, which finds ``agentic-preflight`` on
    ``PATH``. Git prepends only its own exec path, which holds no such program, so this
    shim runs instead of the installed CLI and sets the same hook-log time bound. The
    shim is a ``sh`` script, so on Windows the push uses the installed CLI and the
    product's bound.
    """
    if sys.platform == "win32":
        return
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "hook_check.py"
    script.write_text(_HOOK_CHECK, encoding="utf-8")
    shim = directory / "agentic-preflight"
    shim.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {log_timeout} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(directory), os.environ["PATH"])))


ZERO = "0" * 40
#: Git's zero object ID in a repository that uses the SHA-256 object format.
ZERO_SHA256 = "0" * 64


# -- init -------------------------------------------------------------------


def test_init_installs_a_pre_push_hook(feature_repo):
    agent = ScriptedAgent(feature_repo)
    env = agent.run("init")
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert "agentic-preflight hook-check" in hook.read_text(encoding="utf-8")
    assert env["data"]["hook_installed"] is True


def test_init_writes_a_config_file_if_absent(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    config = (Path(feature_repo) / ".agentic-preflight.toml").read_text(encoding="utf-8")
    assert "[pr]" in config
    assert 'mode = "auto"' in config
    assert "automated_cleanup = false" in config


def test_init_does_not_clobber_an_existing_config(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[general]\nbase_ref = 'develop'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    assert "develop" in (Path(feature_repo) / ".agentic-preflight.toml").read_text(encoding="utf-8")


def test_init_refuses_to_overwrite_a_foreign_hook(feature_repo):
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho someone elses hook\n")
    agent = ScriptedAgent(feature_repo)
    env = agent.run("init", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "hook_exists"
    assert "someone elses hook" in hook.read_text(encoding="utf-8")


def test_init_force_replaces_a_foreign_hook(feature_repo):
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho someone elses hook\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("init", "--force")
    assert "agentic-preflight hook-check" in hook.read_text(encoding="utf-8")


def test_init_is_idempotent(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    env = agent.run("init")
    assert env["ok"] is True


def test_init_installs_at_a_relative_core_hooks_path_and_blocks_a_real_push(
    feature_repo, bare_remote
):
    git("config", "core.hooksPath", ".githooks", cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("init")

    path = Path(feature_repo) / ".githooks" / "pre-push"
    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "agentic-preflight: push blocked" in result.stderr
    assert path.exists()
    assert not (Path(feature_repo) / ".git" / "hooks" / "pre-push").exists()
    assert env["data"]["hook_installed"] is True
    assert env["data"]["hook_path"] == str(path.resolve())
    assert env["data"]["hooks_path_overridden"] is True


def test_init_refuses_a_foreign_hook_at_the_effective_hooks_path(feature_repo):
    git("config", "core.hooksPath", ".githooks", cwd=feature_repo)
    path = write(feature_repo, ".githooks/pre-push", "#!/bin/sh\necho foreign\n")

    env = ScriptedAgent(feature_repo).run("init", expect=ExitCode.PRECONDITION)

    resolved = str(path.resolve())
    assert env["error"]["code"] == "hook_exists"
    assert resolved in env["error"]["message"]
    assert env["data"]["hook_path"] == resolved
    assert resolved in env["next"]["instruction"]
    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"
    assert not (Path(feature_repo) / ".git" / "hooks" / "pre-push").exists()


def test_status_reports_when_the_effective_hook_is_bypassed(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("init")

    active = agent.run("status")["data"]["hook"]
    assert active == {
        "path": str((Path(feature_repo) / ".git" / "hooks" / "pre-push").resolve()),
        "active": True,
    }

    git("config", "core.hooksPath", ".githooks", cwd=feature_repo)
    bypassed = agent.run("status")["data"]["hook"]
    assert bypassed == {
        "path": str((Path(feature_repo) / ".githooks" / "pre-push").resolve()),
        "active": False,
    }


def test_hook_installer_directly_refuses_and_then_replaces_a_foreign_hook(feature_repo):
    path = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\necho existing\n")

    with pytest.raises(FileExistsError):
        hook.install(feature_repo)

    installed, written = hook.install(feature_repo, force=True)
    assert installed == path
    assert written is True
    assert "agentic-preflight hook-check" in path.read_text(encoding="utf-8")

    installed, written = hook.install(feature_repo)
    assert installed == path
    assert written is False


def test_hook_installer_rejects_a_non_repository_directory(tmp_path):
    with pytest.raises(gitx.GitError):
        hook.install(tmp_path)


def test_a_foreign_hook_that_is_not_utf8_is_still_refused(feature_repo):
    """Someone else's hook is arbitrary bytes, and refusing it must not depend
    on those bytes decoding as text."""
    path = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"#!/bin/sh\n# \xff\xfe not valid utf-8\necho existing\n")

    with pytest.raises(FileExistsError):
        hook.install(feature_repo)

    assert b"echo existing" in path.read_bytes()


def test_init_reports_in_place_default(feature_repo):
    env = ScriptedAgent(feature_repo).run("init", "--no-hook")
    assert env["data"]["worktree_mode"] == "in_place"
    assert env["data"]["worktree_root"] is None
    assert env["data"]["hook_path"] == str(
        (Path(feature_repo) / ".git" / "hooks" / "pre-push").resolve()
    )
    assert env["data"]["hook_installed"] is False
    assert env["data"]["hooks_path_overridden"] is False


# -- hook-check as a pure predicate -----------------------------------------


def test_parse_stdin_ignores_malformed_lines_and_recognizes_deletions():
    updates = hook.parse_stdin(f"malformed\nrefs/heads/x {ZERO} refs/heads/x {'a' * 40}\n")
    assert len(updates) == 1
    assert updates[0].is_deletion is True


def test_evaluate_directly_allows_green_and_explains_a_changed_tip():
    green = "a" * 40
    changed = "d" * 40
    green_update = hook.RefUpdate("refs/heads/feature/x", green, "refs/heads/x", ZERO)
    changed_update = hook.RefUpdate("refs/heads/feature/x", changed, "refs/heads/x", ZERO)

    def has_attestation(sha):
        return sha == green

    assert hook.evaluate(
        [green_update], is_ancestor=lambda *_: True, has_attestation=has_attestation
    ).allowed
    blocked = hook.evaluate(
        [changed_update], is_ancestor=lambda *_: True, has_attestation=has_attestation
    )
    assert blocked.allowed is False
    assert "no valid attestation note" in blocked.message


def test_evaluate_directly_blocks_a_non_fast_forward_green_tip():
    green = "a" * 40
    update = hook.RefUpdate("refs/heads/feature/x", green, "refs/heads/feature/x", "e" * 40)

    blocked = hook.evaluate([update], is_ancestor=lambda *_: False, has_attestation=lambda _: True)
    assert blocked.allowed is False
    assert blocked.reason == hook.FORCE_PUSH_REFUSED
    assert "rewrites history" in blocked.message


def test_a_deletion_is_always_allowed(feature_repo):
    """All-zero local sha means a branch deletion; there is nothing to verify."""
    result = hook_check(feature_repo, f"refs/heads/x {ZERO} refs/heads/x abc123\n")
    assert result.returncode == 0


def test_an_unverified_commit_is_blocked(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == ExitCode.HOOK_BLOCK


def test_the_block_message_goes_to_stderr_and_names_the_skill(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert "agentic-preflight: push blocked" in result.stderr
    assert "/agentic-preflight" in result.stderr
    assert "$agentic-preflight" in result.stderr
    assert sha[:7] in result.stderr
    assert "--no-verify" in result.stderr


def test_the_block_message_explains_an_amend(feature_repo, tmp_path):
    """The most common cause deserves the most specific message."""
    green_run(feature_repo, tmp_path)

    write(feature_repo, "src/app.py", "def greet(n):\n    return 'amended'\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--amend", "-m", "amended", cwd=feature_repo)
    new_sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(
        feature_repo, f"refs/heads/feature/x {new_sha} refs/heads/feature/x {ZERO}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert new_sha[:7] in result.stderr
    assert "no valid attestation note" in result.stderr


def test_a_green_commit_is_allowed(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == 0, result.stderr


def test_attestation_verification_rejects_a_note_for_another_tree(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    value = attestation.verify(feature_repo, sha)
    payload = value.model_dump(mode="json")
    payload["tree_sha"] = "0" * 40
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-f",
        "-m",
        json.dumps(payload),
        sha,
        cwd=feature_repo,
    )

    with pytest.raises(attestation.InvalidAttestation, match="does not match commit tree"):
        attestation.verify(feature_repo, sha)


def test_the_predicate_does_not_mutate_anything(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before
    assert git("status", "--porcelain", cwd=feature_repo) == ""


def test_multiple_refs_block_if_any_is_unverified(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    green = git("rev-parse", "HEAD", cwd=feature_repo)
    stdin = (
        f"refs/heads/a {green} refs/heads/a {ZERO}\nrefs/heads/b {'b' * 40} refs/heads/b {ZERO}\n"
    )
    result = hook_check(feature_repo, stdin)
    assert result.returncode == ExitCode.HOOK_BLOCK


def test_empty_stdin_is_allowed(feature_repo):
    assert hook_check(feature_repo, "").returncode == 0


# -- a broken tool must not brick the repo ----------------------------------


def _posix_shell() -> tuple[str, str]:
    """A POSIX shell, and a PATH containing it but not ``agentic-preflight``.

    The hook is a ``/bin/sh`` script on every platform because git runs it with
    its own bundled shell, so this must find that same interpreter rather than
    whatever ``sh`` the ambient PATH resolves to — which on Windows is nothing
    at all outside the Git installation.
    """
    shell = command_plan.find_shell()
    assert shell is not None, "git ships a shell on every supported platform"

    interpreter = Path(shell[0])
    if not interpreter.is_absolute():
        # POSIX resolves the shell through PATH, but this test *replaces* PATH,
        # so the interpreter has to be named by its full path.
        resolved = shutil.which(interpreter.name)
        assert resolved is not None, f"{interpreter.name} is not on PATH"
        interpreter = Path(resolved)

    plain_sh = interpreter.with_name("sh" + interpreter.suffix)
    if plain_sh.exists():
        interpreter = plain_sh
    return str(interpreter), str(interpreter.parent)


def test_the_installed_hook_allows_and_warns_when_the_tool_is_missing(feature_repo):
    """Deliberate: a repo you cannot push from is worse than a skipped check."""
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"

    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    shell, shell_dir = _posix_shell()
    result = subprocess.run(
        [shell, str(hook), "origin", "ssh://example/repo"],
        cwd=feature_repo,
        input=f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        # A real shell is present, but agentic-preflight (which lives in .venv/bin)
        # is not — exactly the state of a fresh clone by someone who has not
        # installed the tool. On Windows the shell is Git's own, which is also
        # the interpreter git uses to run the hook for real.
        env={"PATH": shell_dir, "HOME": os.environ.get("HOME", "")},
    )
    assert result.returncode == 0
    assert "not found" in result.stderr.lower()


# -- force-push guard -------------------------------------------------------


def test_a_force_push_is_blocked_even_when_green(feature_repo, tmp_path):
    """Non-fast-forward rewrites history the remote already has."""
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    # remote_sha that is not an ancestor of local_sha would be a force push;
    # here we use a sha the local tip does not descend from.
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert "force" in result.stderr.lower()


def test_a_force_push_block_does_not_claim_the_commit_is_unverified(feature_repo, tmp_path):
    """The header must not contradict the reason.

    A green commit blocked for being a force push is not 'unverified', and an
    agent reading that would re-run the gate instead of addressing the rewrite.
    """
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert "force push" in result.stderr
    assert "no green run recorded" not in result.stderr


def test_an_unverified_block_still_says_so(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert "no green run recorded" in result.stderr


def test_allow_force_push_config_permits_it(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[hook]\nallow_force_push = true\n",
    )
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    # The tip is still green, and force is now permitted.
    assert result.returncode == 0, result.stderr


# -- the real thing: an actual git push -------------------------------------


def test_a_real_push_is_blocked_for_an_unverified_commit(feature_repo, bare_remote, tmp_path):
    ScriptedAgent(feature_repo).run("init")
    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "push blocked" in result.stderr


def test_a_real_push_succeeds_after_a_green_run(feature_repo, bare_remote, tmp_path):
    green_run(feature_repo, tmp_path)
    ScriptedAgent(feature_repo).run("init")
    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_amending_after_green_blocks_the_next_push(feature_repo, bare_remote, tmp_path):
    """The scenario the SHA-bound attestation exists to catch."""
    green_run(feature_repo, tmp_path)
    ScriptedAgent(feature_repo).run("init")

    write(feature_repo, "src/app.py", "def greet(n):\n    return 'sneaky change'\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--amend", "-m", "sneaky", cwd=feature_repo)

    result = subprocess.run(
        ["git", "push", "--force", "origin", "feature/x"],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "push blocked" in result.stderr


# -- constraints ------------------------------------------------------------


def test_hook_check_reads_only_the_attestation_note(feature_repo, tmp_path):
    """No run state or network — just the commit's Git note.

    This stable behavioral contract replaces a misleading wall-clock assertion:
    interpreter startup and host load made that check unrelated to the hook's work.
    """
    green_run(feature_repo, tmp_path)
    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )
    import shutil

    shutil.rmtree(state_root / "runs")
    (state_root / "current").unlink(missing_ok=True)

    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == 0, result.stderr


def test_attestation_note_ref_updates_are_not_treated_as_commit_pushes():
    update = hook.RefUpdate(attestation.NOTES_REF, "a" * 40, attestation.NOTES_REF, ZERO)
    decision = hook.evaluate(
        [update],
        is_ancestor=lambda *_: False,
        has_attestation=lambda _: False,
    )
    assert decision.allowed is True


def test_a_tag_push_needs_no_attestation():
    update = hook.RefUpdate("refs/tags/v1.0.0", "a" * 40, "refs/tags/v1.0.0", ZERO)
    decision = hook.evaluate(
        [update], is_ancestor=lambda *_: False, has_attestation=lambda _: False
    )
    assert decision.allowed is True


@pytest.mark.parametrize(
    ("local_sha", "remote_sha"), [("a" * 40, ZERO), ("b" * 40, "a" * 40), (ZERO, "a" * 40)]
)
def test_evidence_destination_updates_need_no_git_or_attestation_checks(local_sha, remote_sha):
    def unexpected_check(*_):
        pytest.fail("Evidence destinations must be skipped before commit checks")

    decision = hook.evaluate(
        [
            hook.RefUpdate(
                "refs/heads/x", local_sha, "refs/agentic-preflight/evidence/" + "a" * 40, remote_sha
            )
        ],
        is_ancestor=unexpected_check,
        has_attestation=unexpected_check,
    )
    assert decision.allowed is True


def test_pushing_a_commit_the_remote_already_has_needs_no_attestation():
    """A permitted rewind to a commit the remote tip already contains publishes nothing new."""
    local = "a" * 40
    remote = "b" * 40
    update = hook.RefUpdate("refs/heads/main", local, "refs/heads/main", remote)
    decision = hook.evaluate(
        [update],
        is_ancestor=lambda a, b: (a, b) == (local, remote),
        has_attestation=lambda _: False,
        allow_force_push=True,
    )
    assert decision.allowed is True


def test_a_new_branch_commit_without_attestation_is_still_blocked():
    update = hook.RefUpdate("refs/heads/feature/x", "a" * 40, "refs/heads/feature/x", "b" * 40)
    decision = hook.evaluate(
        [update],
        is_ancestor=lambda a, b: (a, b) == ("b" * 40, "a" * 40),
        has_attestation=lambda _: False,
    )
    assert decision.allowed is False
    assert decision.reason == hook.MISSING_ATTESTATION


# -- SHA-256 repositories -----------------------------------------------------


@pytest.mark.parametrize("value", [ZERO, ZERO_SHA256])
def test_the_zero_id_of_either_object_format_is_recognized(value):
    assert hook.is_zero_id(value) is True


@pytest.mark.parametrize("value", ["a" * 64, "0" * 39, "0" * 63, "0" * 65, ""])
def test_other_values_are_not_the_zero_id(value):
    assert hook.is_zero_id(value) is False


@pytest.mark.parametrize(
    ("local_sha", "expected"), [(ZERO, True), (ZERO_SHA256, True), ("0" * 7, False)]
)
def test_a_deletion_is_the_zero_id_of_either_object_format(local_sha, expected):
    update = hook.RefUpdate("(delete)", local_sha, "refs/heads/x", "a" * 40)
    assert update.is_deletion is expected


def test_a_new_branch_in_a_sha256_repository_is_not_reported_as_a_force_push():
    """Git sends a 64-character zero ID for a remote ref that does not exist yet."""
    update = hook.RefUpdate("refs/heads/feature/x", "a" * 64, "refs/heads/feature/x", ZERO_SHA256)

    def unexpected_ancestry(*_):
        pytest.fail("A new branch has no remote tip to compare")

    decision = hook.evaluate(
        [update], is_ancestor=unexpected_ancestry, has_attestation=lambda _: False
    )
    assert decision.allowed is False
    assert decision.reason == hook.MISSING_ATTESTATION
    assert decision.updates[0].force is False
    assert "force push" not in decision.message
    assert "SHA-256 repositories are not supported" in decision.message


def test_a_rewriting_push_in_a_sha256_repository_says_it_is_unsupported():
    """The force-push fix cannot help in a SHA-256 repository, so that message comes first."""
    update = hook.RefUpdate("refs/heads/feature/x", "a" * 64, "refs/heads/feature/x", "b" * 64)
    decision = hook.evaluate(
        [update], is_ancestor=lambda *_: False, has_attestation=lambda _: False
    )
    assert decision.allowed is False
    assert decision.reason == hook.MISSING_ATTESTATION
    assert decision.updates[0].force is True
    assert "SHA-256 repositories are not supported" in decision.message
    assert "force push" not in decision.message


def test_a_commit_the_remote_already_has_in_a_sha256_repository_is_allowed():
    local, remote = "a" * 64, "b" * 64
    update = hook.RefUpdate("refs/heads/main", local, "refs/heads/main", remote)
    decision = hook.evaluate(
        [update],
        is_ancestor=lambda a, b: (a, b) == (local, remote),
        has_attestation=lambda _: False,
        allow_force_push=True,
    )
    assert decision.allowed is True
    assert decision.reason == hook.ALREADY_ON_REMOTE


def test_a_deletion_in_a_sha256_repository_is_allowed():
    update = hook.RefUpdate("(delete)", ZERO_SHA256, "refs/heads/feature/x", "a" * 64)
    decision = hook.evaluate(
        [update], is_ancestor=lambda *_: False, has_attestation=lambda _: False
    )
    assert decision.allowed is True
    assert decision.reason == hook.DELETION


def _sha256_repo_with_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", "--object-format=sha256", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", "--object-format=sha256", cwd=repo)
    write(repo, "README.md", "# demo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "origin", "main", "main:refs/heads/stale", cwd=repo)
    return repo


def test_a_real_push_of_a_new_branch_in_a_sha256_repository_says_it_is_unsupported(tmp_path):
    repo = _sha256_repo_with_remote(tmp_path)
    ScriptedAgent(repo).run("init")
    git("switch", "-c", "feature/x", cwd=repo)
    write(repo, "src/app.py", "x = 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "feature", cwd=repo)

    result = subprocess.run(
        ["git", "push", "origin", "feature/x"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "push blocked" in result.stderr
    assert "SHA-256 repositories are not supported" in result.stderr
    assert "force push" not in result.stderr


def test_a_real_branch_deletion_in_a_sha256_repository_is_allowed(tmp_path):
    repo = _sha256_repo_with_remote(tmp_path)
    ScriptedAgent(repo).run("init")
    result = subprocess.run(
        ["git", "push", "origin", "--delete", "stale"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
