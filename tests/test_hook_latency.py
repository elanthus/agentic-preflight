"""The pre-push hook's fixed cost: how many Git processes and modules ``hook-check`` uses.

``hook-check`` runs on every push, so each Git process it spawns and each module it
imports is paid by every user on every push. These tests count both, and they pin
that the savings never change a decision.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from agentic_preflight import attestation
from agentic_preflight.envelope import ExitCode
from tests.conftest import git
from tests.driver import green_run
from tests.test_hook import ZERO, hook_check

#: The most Git processes ``hook-check`` may spawn for one new branch with a green
#: note, when the remote-tracking base branch exists: 38 before this bound was set.
#: The Git version check and 18 processes evaluate the push. Five of them keep
#: clone-local attributes out of the verified patch text (GHSA-m8wg-p58p-cqmf): the
#: Git directory for the attribute-free work tree, the raw inventory, the binary
#: check, the diff driver configuration, and ``$GIT_DIR/info/attributes``.
MAX_GIT_PROCESSES_TO_DECIDE = 19
#: The hook decision log identifies the repository on a background thread, which
#: overlaps the evaluation: 3 more processes when the log is on.
MAX_GIT_PROCESSES_WITH_LOG = MAX_GIT_PROCESSES_TO_DECIDE + 3

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the Git shim is a sh script")


def _git_logging_shim(directory: Path, log: Path) -> dict[str, str]:
    """Put a ``git`` first on ``PATH`` that appends each argument list to ``log``.

    A ``cat-file --batch-check`` reads its object names from stdin, so its line also
    records them: two batches for different objects are different commands.
    """
    real = shutil.which("git")
    assert real is not None
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "git"
    quoted_log, quoted_git = shlex.quote(str(log)), shlex.quote(real)
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  cat-file\\ --batch-check*)\n"
        "    input=$(cat)\n"
        f"    printf '%s <<< %s\\n' \"$*\" \"$(printf '%s' \"$input\" | tr '\\n' ' ')\" >> {quoted_log}\n"
        f'    printf \'%s\\n\' "$input" | exec {quoted_git} "$@" ;;\n'
        "  *)\n"
        f"    printf '%s\\n' \"$*\" >> {quoted_log}\n"
        f'    exec {quoted_git} "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {"PATH": os.pathsep.join((str(directory), os.environ["PATH"]))}


@posix_only
@pytest.mark.parametrize(
    ("hook_log", "bound"),
    [(False, MAX_GIT_PROCESSES_TO_DECIDE), (True, MAX_GIT_PROCESSES_WITH_LOG)],
)
def test_hook_check_for_one_green_ref_spawns_few_git_processes(
    feature_repo, tmp_path, hook_log, bound
):
    green_run(feature_repo, tmp_path)
    # A clone that pushes has the remote-tracking base the note's base_ref names.
    git("update-ref", "refs/remotes/origin/main", "main", cwd=feature_repo)
    if not hook_log:
        # With the log off no background thread starts, so every Git process
        # counted here decides the push.
        user_config = Path.home() / ".config" / "agentic-preflight" / "config.toml"
        user_config.parent.mkdir(parents=True, exist_ok=True)
        user_config.write_text("[telemetry]\nhook_log = false\n", encoding="utf-8")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    log = tmp_path / "git-calls.log"
    env = _git_logging_shim(tmp_path / "shim", log)

    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n", env
    )

    assert result.returncode == ExitCode.OK, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    repeated = {call: count for call, count in Counter(calls).items() if count > 1}
    assert repeated == {}, f"identical Git commands ran more than once: {repeated}"
    assert len(calls) <= bound, "\n".join(calls)


def _forge_note(repo: Path, sha: str, change) -> None:
    """Rewrite the note on ``sha`` with ``change`` applied to its JSON payload."""
    payload = json.loads(git("notes", f"--ref={attestation.NOTES_REF}", "show", sha, cwd=repo))
    change(payload)
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


def _missing_merge_base(payload: dict) -> None:
    payload["merge_base_sha"] = MISSING_COMMIT


def _missing_origin_base(payload: dict) -> None:
    for item in payload["evidence"].values():
        item["origin"]["base_sha"] = MISSING_COMMIT


#: A commit no fixture repository has.
MISSING_COMMIT = "c" * 40


@pytest.mark.parametrize(
    ("change", "reason"),
    [(_missing_merge_base, "base_mismatch"), (_missing_origin_base, "invalid_evidence")],
)
def test_a_note_naming_a_missing_commit_blocks_as_before(feature_repo, tmp_path, change, reason):
    """A commit the batch cannot peel is looked up again and fails as it did without it.

    The exit code, the message, and the verification reason are those of the release
    before the batch (3cb3527).
    """
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    _forge_note(feature_repo, sha, change)

    with pytest.raises(attestation.InvalidAttestation) as caught:
        attestation.verify(feature_repo, sha, purpose="publish")
    assert caught.value.reason == reason

    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert result.stderr == (
        "agentic-preflight: push blocked.\n"
        f"  commit: {sha[:7]} (no green run recorded for this exact SHA)\n"
        "  reason: no valid attestation note is attached to this exact SHA\n"
        "  fix:    invoke the skill (/agentic-preflight in Claude Code, "
        "$agentic-preflight in Codex)\n"
        "  bypass: git push --no-verify   (documented escape hatch)\n"
    )


@posix_only
def test_a_missing_object_still_blocks_with_the_cache(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    green = git("rev-parse", "HEAD", cwd=feature_repo)
    missing = "b" * 40
    stdin = (
        f"refs/heads/a {green} refs/heads/a {ZERO}\nrefs/heads/b {missing} refs/heads/b {ZERO}\n"
    )

    result = hook_check(feature_repo, stdin)

    assert result.returncode == ExitCode.HOOK_BLOCK
    assert "no valid attestation note" in result.stderr


#: Runs hook-check with ``sys.argv[1]`` made unimportable, the way a broken or
#: partial installation would fail to import a deferred module.
_HOOK_CHECK_WITHOUT_MODULE = """\
import sys
blocked = sys.argv[1]

class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name == blocked:
            raise ImportError(f"refused {name}")
        return None

sys.meta_path.insert(0, Refuse())
from agentic_preflight.cli import main
main(["hook-check"], prog_name="agentic-preflight")
"""


@pytest.mark.parametrize(
    "module",
    [
        "agentic_preflight.attestation",
        "agentic_preflight.refresh_validation",
        "agentic_preflight.cli_policy",
    ],
)
def test_a_deferred_import_failure_never_allows_a_push(feature_repo, tmp_path, module):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = subprocess.run(
        [sys.executable, "-c", _HOOK_CHECK_WITHOUT_MODULE, module],
        cwd=feature_repo,
        input=f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode != ExitCode.OK, result.stderr


_LOADED_FOR_HOOK_CHECK = """\
import sys
from agentic_preflight.cli import main
main.get_command(None, "hook-check")
print("\\n".join(sorted(name for name in sys.modules if name.startswith("agentic_preflight"))))
"""


def test_resolving_hook_check_does_not_import_the_run_commands():
    result = subprocess.run(
        [sys.executable, "-c", _LOADED_FOR_HOOK_CHECK],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(result.stdout.split())
    assert "agentic_preflight.cli_policy" in loaded
    for unused in (
        "agentic_preflight.runs",
        "agentic_preflight.cli_runs",
        "agentic_preflight.cli_report",
        "agentic_preflight.cli_integrations",
        "agentic_preflight.report",
    ):
        assert unused not in loaded


def test_the_lazy_command_map_names_every_command_and_its_module():
    import importlib

    import click

    from agentic_preflight.cli import COMMAND_MODULES, main

    assert set(main.commands) == set(COMMAND_MODULES)
    for name, module in COMMAND_MODULES.items():
        group = click.Group()
        importlib.import_module(f"agentic_preflight.{module}").register(group)
        assert name in group.commands, (name, module)


# -- the object lookup cache --------------------------------------------------


def _count_git(monkeypatch) -> list[tuple[str, ...]]:
    from agentic_preflight import gitx

    calls: list[tuple[str, ...]] = []
    run = gitx.run

    def counted(cwd, *args, **kwargs):
        calls.append(args)
        return run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", counted)
    return calls


def test_lookups_of_a_full_object_id_run_once_inside_the_cache(tmp_repo, monkeypatch):
    from agentic_preflight import gitx

    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    calls = _count_git(monkeypatch)
    with gitx.object_cache():
        trees = {gitx.tree_sha(tmp_repo, head) for _ in range(3)}
        commits = {gitx.rev_parse(tmp_repo, head) for _ in range(3)}
    assert commits == {head}
    assert trees == {git("rev-parse", "HEAD^{tree}", cwd=tmp_repo)}
    assert len(calls) == 2

    # Outside the block nothing is cached.
    gitx.tree_sha(tmp_repo, head)
    gitx.tree_sha(tmp_repo, head)
    assert len(calls) == 4


def test_symbolic_names_are_never_cached(tmp_repo, monkeypatch):
    from agentic_preflight import gitx

    calls = _count_git(monkeypatch)
    with gitx.object_cache():
        first = gitx.rev_parse(tmp_repo, "HEAD")
        git("commit", "--allow-empty", "-m", "move HEAD", cwd=tmp_repo)
        second = gitx.rev_parse(tmp_repo, "HEAD")
    assert first != second
    assert len(calls) == 2


def test_a_failed_lookup_is_not_cached_and_fails_every_time(tmp_repo, monkeypatch):
    from agentic_preflight import gitx

    missing = "b" * 40
    calls = _count_git(monkeypatch)
    with gitx.object_cache():
        gitx.prime_object_lookups(tmp_repo, [missing])
        for _ in range(2):
            with pytest.raises(gitx.GitError):
                gitx.tree_sha(tmp_repo, missing)
            with pytest.raises(gitx.GitError):
                gitx.rev_parse(tmp_repo, missing)
    assert len(calls) == 4


def test_priming_resolves_commits_and_trees_in_one_process(tmp_repo, monkeypatch):
    from agentic_preflight import gitx

    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    tree = git("rev-parse", "HEAD^{tree}", cwd=tmp_repo)
    calls = _count_git(monkeypatch)
    with gitx.object_cache():
        gitx.prime_object_lookups(tmp_repo, [head, "HEAD", "not an id\n" + head])
        assert gitx.rev_parse(tmp_repo, head) == head
        assert gitx.tree_sha(tmp_repo, head) == tree
    # The batch runs through subprocess directly; no lookup reached ``run``.
    assert calls == []


def test_priming_outside_the_cache_does_nothing(tmp_repo, monkeypatch):
    from agentic_preflight import gitx

    ran = []
    monkeypatch.setattr(gitx.subprocess, "run", lambda *a, **k: ran.append(a))
    gitx.prime_object_lookups(tmp_repo, ["a" * 40])
    assert ran == []


# -- committed configuration ---------------------------------------------------


def test_committed_config_defaults_only_when_the_file_is_absent(tmp_repo):
    from agentic_preflight import gitx
    from agentic_preflight.config import Config, committed_config

    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert git("ls-tree", "--name-only", head, "--", ".agentic-preflight.toml", cwd=tmp_repo) == ""
    assert committed_config(tmp_repo, head) == Config()
    with pytest.raises(gitx.GitError):
        committed_config(tmp_repo, "b" * 40)


def test_an_id_in_the_other_object_format_is_never_cached(tmp_repo, monkeypatch):
    """In a SHA-1 repository 64 hexadecimal digits are a ref name, which can move."""
    from agentic_preflight import gitx

    name = "d" * 64
    first = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("update-ref", f"refs/heads/{name}", first, cwd=tmp_repo)
    calls = _count_git(monkeypatch)
    diffs: list[tuple[str, ...]] = []
    run_bytes = gitx._run_bytes

    def counted_bytes(cwd, *args, **kwargs):
        diffs.append(args)
        return run_bytes(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "_run_bytes", counted_bytes)
    with gitx.object_cache():
        # ``first`` is a full ID, so only ``name`` can keep the diff out of the cache.
        gitx.prime_object_lookups(tmp_repo, [first, name])
        assert gitx.rev_parse(tmp_repo, name) == first
        assert gitx.changed_files_raw(tmp_repo, first, name) == []

        (tmp_repo / "added.txt").write_text("added\n", encoding="utf-8")
        git("add", "added.txt", cwd=tmp_repo)
        git("commit", "-m", "move the ref", cwd=tmp_repo)
        second = git("rev-parse", "HEAD", cwd=tmp_repo)
        git("update-ref", f"refs/heads/{name}", second, cwd=tmp_repo)

        assert gitx.rev_parse(tmp_repo, name) == second
        assert gitx.changed_files_raw(tmp_repo, first, name) == ["added.txt"]
        # Both diffs ran: the ref name's result was never cached.
        assert len(diffs) == 2

        # Control: between two IDs already peeled as full IDs, the diff is cached.
        gitx.prime_object_lookups(tmp_repo, [first, second])
        assert gitx.changed_files_raw(tmp_repo, first, second) == ["added.txt"]
        assert gitx.changed_files_raw(tmp_repo, first, second) == ["added.txt"]
        assert len(diffs) == 3
    assert len(calls) == 2
