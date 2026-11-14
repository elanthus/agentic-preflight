"""Shared fixtures.

Git fixtures drive a real ``git`` binary. The product under test *is* git
semantics — worktrees, merge-bases, cherry-picks, hook stdin protocol — so
mocking git would mean testing our idea of git rather than git. Everything is
made deterministic with fixed identity and timestamp environment instead.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from agentic_preflight import hooklog
from agentic_preflight.machine import State
from agentic_preflight.models import RunDoc
from tests import telemetry_guard

#: The environment and home directory before any test changes them. The telemetry
#: guard reads the user's real files from these, not from a test's override.
_REAL_ENVIRON = dict(os.environ)
_REAL_HOME = Path.home()

# Property tests explore a fixed, derived set of examples so that every run, local or CI,
# checks the same inputs and no failing example is replayed from a local database.
settings.register_profile("deterministic", derandomize=True, database=None, deadline=None)
settings.load_profile("deterministic")

DETERMINISTIC_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


@pytest.fixture(scope="session", autouse=True)
def user_telemetry_guard(tmp_path_factory):
    """Fail the session if a temporary repository's row reached the user's real telemetry.

    It reads the real ``runs.jsonl`` and ``hook-decisions.jsonl`` themselves, so it
    covers rows that a CLI subprocess wrote as well as in-process ones. Rows from other
    repositories are ignored. See :mod:`tests.telemetry_guard`.
    """
    directories = telemetry_guard.real_telemetry_dirs(_REAL_ENVIRON, _REAL_HOME)
    before = telemetry_guard.snapshot(directories)
    yield
    leaked = telemetry_guard.check(
        before,
        basetemp=telemetry_guard.session_basetemp(tmp_path_factory.getbasetemp()),
        roots=telemetry_guard.temporary_roots(_REAL_ENVIRON),
    )
    if leaked:
        pytest.fail(telemetry_guard.failure_message(leaked), pytrace=False)


@pytest.fixture(autouse=True)
def deterministic_git_env(monkeypatch, tmp_path_factory):
    for key, value in DETERMINISTIC_ENV.items():
        monkeypatch.setenv(key, value)
    # Isolated runs default to a worktree under the user cache. Point it at a
    # per-test directory so the suite never writes into the developer's cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
    # Finished runs append to the user-level telemetry mirror. Keep it per test too.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    # The user configuration lives under the home directory. A developer's
    # ``[telemetry] path`` there is an explicit opt-in that the product honours even
    # for repositories without a network remote, so no test may read it.
    set_home(monkeypatch, tmp_path_factory.mktemp("home"))
    # Real-push tests invoke the installed hook, which resolves
    # ``agentic-preflight`` through PATH. Pin that lookup to the same environment
    # running pytest so an unrelated user installation cannot test older code.
    python_bin = str(Path(sys.executable).parent)
    current_path = os.environ.get("PATH", "")
    parts = (python_bin, _developer_git_dir(), current_path)
    monkeypatch.setenv("PATH", os.pathsep.join(part for part in parts if part))


@functools.cache
def _developer_git_dir() -> str | None:
    """The directory of the Git that macOS's ``/usr/bin/git`` stub would run, if any.

    On macOS ``/usr/bin/git`` is a stub that asks ``xcrun`` for the selected
    developer directory on every call and then runs the Git found there. The
    lookup costs more than twice what Git itself takes to start, and nearly every
    test starts Git dozens or hundreds of times. Putting the selected Git's own
    directory on ``PATH`` runs the very binary the stub would have run, without
    the lookup. It holds only Apple's developer tools, whose ``/usr/bin`` entries
    are the same kind of stub, and it comes after the test environment's own
    ``bin`` so that environment's ``python`` still wins. Elsewhere, and when
    ``git`` on ``PATH`` is not the stub, this is ``None`` and ``PATH`` is unchanged.
    """
    if sys.platform != "darwin" or shutil.which("git") != "/usr/bin/git":
        return None
    try:
        found = subprocess.run(
            ["xcrun", "--find", "git"], capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(Path(found).parent) if found and Path(found).is_file() else None


#: The hook log's time bound in tests. The product's two seconds bound a push on an
#: ordinary machine, but a loaded test machine can take longer for the log's few Git
#: calls, and the write then returns ``timed_out`` instead of its result. Tests whose
#: subject is the bound pass their own short ``timeout`` and a step held open on purpose.
HOOK_LOG_TIMEOUT_SECONDS = 60.0
#: The product's bound, read before any test replaces it.
PRODUCT_HOOK_LOG_TIMEOUT_SECONDS = hooklog.TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def generous_hook_log_timeout(monkeypatch):
    """Give in-process hook-log writes :data:`HOOK_LOG_TIMEOUT_SECONDS`.

    ``hook-check`` subprocesses get the same bound from ``tests.test_hook.hook_check``.
    """
    monkeypatch.setattr(hooklog, "TIMEOUT_SECONDS", HOOK_LOG_TIMEOUT_SECONDS)


@pytest.fixture
def telemetry_opt_in(deterministic_git_env) -> Path:
    """Opt this test's fixture repositories in to telemetry, and return the directory.

    No fixture repository has a network remote, so by default the product neither
    mirrors its runs nor logs its hook decisions. An explicit ``[telemetry] path`` is
    an opt-in. This writes one into the test's own user
    configuration, naming the per-test directory that ``XDG_DATA_HOME`` already selects,
    so ``telemetry.mirror_path()`` and ``hooklog.log_path()`` still find the files.
    """
    return opt_in_telemetry(Path(os.environ["XDG_DATA_HOME"]) / "agentic-preflight" / "telemetry")


def opt_in_telemetry(directory: Path, *, home: Path | None = None) -> Path:
    """Write ``[telemetry] path = directory`` into the user configuration file of ``home``.

    ``home`` defaults to the test's own home directory.
    """
    config = (home or Path.home()) / ".config" / "agentic-preflight" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"[telemetry]\npath = {json.dumps(str(directory))}\n", encoding="utf-8")
    return directory


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def write(repo: Path | str, relpath: str, content: str) -> Path:
    path = Path(repo) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_run(run_id: str = "r_abc123", branch: str = "feature/x") -> RunDoc:
    """A minimal valid run document, shared by the store-level test modules."""
    return RunDoc(
        schema_version=2,
        run_id=run_id,
        state=State.CREATED,
        branch=branch,
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
        source_head_sha="b" * 40,
        intent_source="user",
        source_worktree_id="source-worktree",
        source_worktree_path="/repos/source",
        config_snapshot={"worktree": {"mode": "in_place"}},
        config_digest="c" * 64,
        created_at="2026-01-01T00:00:00+00:00",
    )


def home_env(path: Path | str) -> dict[str, str]:
    """Environment that redirects ``Path.home()`` at a temporary directory.

    ``HOME`` alone is not enough. ``Path.home()`` consults ``USERPROFILE`` on
    Windows, so a test that sets only ``HOME`` there does not redirect anything
    — it reads and writes the developer's real home directory, installing
    skills into it and asserting against whatever happens to be there already.
    """
    return {"HOME": str(path), "USERPROFILE": str(path)}


def set_home(monkeypatch, path: Path | str) -> None:
    """The :func:`home_env` redirection, applied to this process."""
    for name, value in home_env(path).items():
        monkeypatch.setenv(name, value)


def access_entries(path: Path) -> list[str]:
    """A Windows file's DACL entries, one per granted principal.

    Every ``icacls`` entry has the shape ``PRINCIPAL:(rights)``, so ``:(`` is
    what identifies one. Splitting on the first colon instead counts the drive
    letter of the path that ``icacls`` prints on its own first line.
    """
    listing = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if ":(" in line]


def assert_owner_only(path: Path) -> None:
    """Assert the platform's expression of "readable by the owner and nobody else".

    On Windows the mode bits carry no permissions, so the DACL is read back:
    exactly one entry may remain, and none of it may be inherited.
    """
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    entries = access_entries(path)
    assert len(entries) == 1, f"expected a single access entry, got: {entries}"
    assert "(I)" not in entries[0], f"inherited access survived: {entries}"


def recorded_mode(repo: Path, ref: str, relpath: str) -> str:
    """The file mode git stored for ``relpath`` in ``ref``.

    ``120000`` is a symlink, ``100644`` a plain file. Worth asking directly:
    whether git records a symlink *as* one, and whether it notices a change
    between the two, both vary by platform and configuration.
    """
    entry = git("ls-tree", ref, "--", relpath, cwd=repo)
    return entry.split(maxsplit=1)[0] if entry else ""


def require_type_change(repo: Path, base: str, head: str, relpath: str) -> None:
    """Skip unless git recorded ``relpath`` changing file type between the two refs.

    Checked as an invariant rather than inferred from a platform, because more
    than one thing decides it. Creating a symlink can be permitted while git is
    configured not to record it, and a recorded symlink can still be replaced
    by a plain file without git registering the mode change — Windows defaults
    to ignoring file modes. Either way there is no type change for a diff test
    to describe, and asserting on the patch count would report a confusing
    number instead of the reason.
    """
    before = recorded_mode(repo, base, relpath)
    after = recorded_mode(repo, head, relpath)
    if before == after:
        pytest.skip(f"git recorded no type change for {relpath}: mode {before} on both sides")


@functools.cache
def _symlinks_available() -> bool:
    """Whether this process may create symlinks.

    Probed rather than assumed from the platform: Windows permits it under
    Developer Mode or elevation and refuses otherwise, so the answer is a
    property of the machine and not of ``sys.platform``.
    """
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "probe"
        try:
            probe.symlink_to(directory)
        except (OSError, NotImplementedError):
            return False
    return True


@functools.cache
def _git_records_symlinks() -> bool:
    """Whether git stores a symlink *as* a symlink in this environment.

    A separate question from whether the filesystem allows one. Git for Windows
    sets ``core.symlinks=false`` unless the installer enabled them, and then
    commits a symlink as an ordinary file holding its target path — so a test
    about symlink-to-file *type changes* sees no type change at all. GitHub's
    Windows runners are exactly this case: elevated enough to create symlinks,
    configured not to record them.

    Probed under the same config isolation the suite runs with, since that is
    what decides the answer.
    """
    if not _symlinks_available():
        return False

    env = {**os.environ, **DETERMINISTIC_ENV}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            subprocess.run(
                ["git", "init", "-q", str(root)], capture_output=True, check=True, env=env
            )
            (root / "target.txt").write_text("target\n", encoding="utf-8")
            (root / "link.txt").symlink_to("target.txt")
            subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True, env=env)
            staged = subprocess.run(
                ["git", "ls-files", "-s", "link.txt"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return False
    # Mode 120000 is git's symlink mode; 100644 means it stored a plain file.
    return staged.startswith("120000")


def __getattr__(name: str):
    """Build the probe-backed names on first use rather than at module import.

    Both probes spawn subprocesses and temporary directories, and only the test
    modules that import these marks need the answers — a run that collects no
    symlink test should not pay for a git init/add/ls-files before collection.
    The probe results themselves are cached, so the cost is paid at most once.
    """
    if name == "SYMLINKS_AVAILABLE":
        return _symlinks_available()
    if name == "GIT_RECORDS_SYMLINKS":
        return _git_records_symlinks()
    if name == "requires_symlinks":
        return pytest.mark.skipif(
            not _symlinks_available(),
            reason="creating symlinks requires Developer Mode or elevation on Windows",
        )
    if name == "requires_git_symlinks":
        return pytest.mark.skipif(
            not _git_records_symlinks(),
            reason="git is not configured to record symlinks as symlinks",
        )
    raise AttributeError(name)


requires_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32",
    reason="making a file unreadable by mode bits has no effect on Windows",
)

# The POSIX kill path cannot be forced on Windows the way other platform
# branches can: ``signal.SIGKILL`` does not exist there, so exercising it would
# mean inventing the constant and testing a fiction. The Windows branch has its
# own tests instead, so both paths stay covered.
requires_posix_signals = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="process groups and SIGKILL are POSIX-only",
)

# Its counterpart. The two kill paths are defined under a ``sys.platform``
# guard, so neither can be forced on the other platform — each is covered by
# the CI leg that actually runs it.
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="taskkill and process-creation flags are Windows-only",
)


def commit_all(repo: Path, message: str) -> str:
    git("add", "-A", cwd=repo)
    git("commit", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def configure_base(repo: Path, config: str) -> None:
    """Commit ``config`` on ``main`` and rebase the current branch onto it.

    Verification reads policy from the protected base, so a fixture whose policy
    should govern the run must land it there, and on ``origin`` when one exists.
    """
    branch = git("branch", "--show-current", cwd=repo)
    git("switch", "main", cwd=repo)
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, "configure agentic-preflight")
    if "origin" in git("remote", cwd=repo).split():
        git("push", "origin", "main", cwd=repo)
    if branch != "main":
        git("switch", branch, cwd=repo)
        git("rebase", "main", cwd=repo)


def build_base_repo(repo: Path) -> None:
    """Create the ``tmp_repo`` layout: ``main`` with one base commit, clean."""
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    write(repo, "README.md", "# demo\n\nA demo project.\n")
    write(repo, "src/app.py", "def greet(name):\n    return f'hi {name}'\n")
    # Committed, not just written: worktrees are checked out at a commit, so an
    # uncommitted ignore rule is invisible where it matters.
    write(repo, ".gitignore", ".env\n")
    commit_all(repo, "base commit")


def add_feature_branch(repo: Path) -> None:
    """Add the ``feature_repo`` layout: ``feature/x`` one commit ahead of ``main``."""
    git("switch", "-c", "feature/x", cwd=repo)
    write(repo, "src/app.py", "def greet(name, loud=False):\n    return f'hi {name}'\n")
    commit_all(repo, "add loud flag")


@pytest.fixture(scope="session")
def repo_templates(tmp_path_factory) -> Iterator[dict[str, Path]]:
    """The ``tmp_repo`` and ``feature_repo`` repositories, built once per worker.

    Every test gets its own copy (see :func:`copy_repo_template`), never these
    directories. The repositories are built by real Git under the same
    deterministic identity and dates the per-test environment sets, so a copy
    holds exactly the objects, refs and reflog a per-test build would.
    """
    root = tmp_path_factory.mktemp("repo-templates")
    with pytest.MonkeyPatch.context() as patch:
        for key, value in DETERMINISTIC_ENV.items():
            patch.setenv(key, value)
        base = root / "base"
        build_base_repo(base)
        feature = root / "feature"
        shutil.copytree(base, feature, symlinks=True)
        add_feature_branch(feature)
        templates = {"base": base, "feature": feature}
        before = {name: _repo_snapshot(path) for name, path in templates.items()}
    yield templates
    # No test may reach a template: each gets a copy. Fail the session if one did.
    with pytest.MonkeyPatch.context() as patch:
        for key, value in DETERMINISTIC_ENV.items():
            patch.setenv(key, value)
        changed = [name for name, path in templates.items() if _repo_snapshot(path) != before[name]]
    if changed:
        pytest.fail(f"a test changed the shared repository template(s): {changed}", pytrace=False)


def _repo_snapshot(repo: Path) -> tuple[str, str, str]:
    """The refs, checked-out branch and working-tree status of a repository."""
    return (
        git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo),
        git("symbolic-ref", "HEAD", cwd=repo),
        git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=repo),
    )


def copy_repo_template(template: Path, repo: Path) -> Path:
    """Copy a template repository to ``repo`` and refresh its index.

    The copy has fresh file times and inodes, so the index's cached stat data
    no longer matches the working tree. ``update-index --refresh`` re-reads the
    files and records their new stat data, which leaves the index as a fresh
    checkout would: plumbing that compares stat data (``diff-index``,
    ``diff-files``) sees a clean tree, exactly as it did after a per-test build.
    """
    shutil.copytree(template, repo, symlinks=True, copy_function=shutil.copy)
    git("update-index", "-q", "--refresh", cwd=repo)
    return repo


@pytest.fixture
def tmp_repo(request, tmp_path: Path, repo_templates: dict[str, Path]) -> Path:
    """A repo on ``main`` with one base commit and no working-tree changes.

    When the test also asks for ``feature_repo`` the copy is taken from the
    feature template directly, so the two fixtures still name one repository.
    """
    template = "feature" if "feature_repo" in request.fixturenames else "base"
    return copy_repo_template(repo_templates[template], tmp_path / "repo")


@pytest.fixture
def feature_repo(tmp_repo: Path) -> Path:
    """``tmp_repo`` with a ``feature/x`` branch holding one commit ahead of main."""
    return tmp_repo


@pytest.fixture
def bare_remote(tmp_path: Path, tmp_repo: Path) -> Path:
    """A bare remote wired up as ``origin`` so push and hook paths run for real."""
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=tmp_repo)
    git("push", "-u", "origin", "main", cwd=tmp_repo)
    return remote


def unreadable_run_bytes(run: RunDoc, kind: str) -> bytes:
    """Synthetic mixed-version records; never rely on a real field staying unknown."""
    import json

    raw = run.model_dump(mode="json")
    if kind == "unknown_field":
        raw["review_coverage"] = {
            "manifest": "a" * 64,
            "head_sha": "b" * 40,
            "total_units": 0,
            "future_coverage_revision": "DO_NOT_ECHO_RECORD_VALUES",
        }
    elif kind == "invalid_value":
        raw["state"] = ["DO_NOT_ECHO_RECORD_VALUES"]
    elif kind == "malformed_json":
        return b'{"DO_NOT_ECHO_RECORD_VALUES": '
    elif kind == "invalid_record":
        return b'["DO_NOT_ECHO_RECORD_VALUES"]'
    elif kind == "invalid_encoding":
        return b"\xffDO_NOT_ECHO_RECORD_VALUES"
    return json.dumps(raw).encode()


@pytest.fixture
def agent(feature_repo):
    from tests.driver import ScriptedAgent

    return ScriptedAgent(feature_repo)
