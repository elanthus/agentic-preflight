"""The pre-push hook's fixed cost: how many Git processes and modules ``hook-check`` uses.

``hook-check`` runs on every push, so each Git process it spawns and each module it
imports is paid by every user on every push. These tests count both, and they pin
that the savings never change a decision.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import git
from tests.driver import green_run
from tests.test_hook import ZERO, hook_check

#: The most Git processes ``hook-check`` may spawn for one new branch with a green
#: note, when the remote-tracking base branch exists. 17 when this test was written,
#: down from 38: the Git version check, 13 to evaluate the push, and 3 that the hook
#: decision log runs on a background thread to identify the repository.
MAX_GIT_PROCESSES = 17

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the Git shim is a sh script")


def _git_logging_shim(directory: Path, log: Path) -> dict[str, str]:
    """Put a ``git`` first on ``PATH`` that appends each argument list to ``log``."""
    real = shutil.which("git")
    assert real is not None
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f'exec {shlex.quote(real)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {"PATH": os.pathsep.join((str(directory), os.environ["PATH"]))}


@posix_only
def test_hook_check_for_one_green_ref_spawns_few_git_processes(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    # A clone that pushes has the remote-tracking base the note's base_ref names.
    git("update-ref", "refs/remotes/origin/main", "main", cwd=feature_repo)
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
    assert len(calls) <= MAX_GIT_PROCESSES, "\n".join(calls)


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
