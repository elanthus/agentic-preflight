"""What the tool does in large and unusual repository shapes.

Each test sets one shape up in temporary repositories and records what the CLI
does there. ``docs/repository-shapes.md`` states only facts these tests verify.
A test that records a known defect says so; when the defect is fixed, the test
fails, and the page must change with it.

Remotes are local ``file://`` URLs or paths, so no test uses the network.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight import fingerprints
from agentic_preflight.config import Config
from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json

DOCS_CONFIG = "[docs]\nenabled = true\n\n[commands]\nlint = 'true'\ntest = 'true'\n"


def _lfs_available() -> bool:
    try:
        return (
            subprocess.run(["git", "lfs", "version"], capture_output=True, check=False).returncode
            == 0
        )
    except OSError:
        return False


requires_lfs = pytest.mark.skipif(
    not _lfs_available(), reason="git lfs is not installed, so the Git LFS shape is unverified"
)


def _seed_origin(tmp_path: Path, config: str = PROTECTED_CONFIG) -> Path:
    """A bare origin whose ``main`` has five commits and allows partial clones."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-b", "main", cwd=seed)
    write(seed, ".agentic-preflight.toml", config)
    write(seed, "README.md", "# demo\n")
    write(seed, "docs/guide.md", "# Guide\n")
    write(seed, "src/app.py", "x = 1\n")
    commit_all(seed, "c1")
    for number in range(2, 6):
        write(seed, "src/app.py", f"x = {number}\n")
        commit_all(seed, f"c{number}")
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("config", "uploadpack.allowFilter", "true", cwd=origin)
    git("push", str(origin), "main", cwd=seed)
    return origin


def _green_to_verified(agent: ScriptedAgent, tmp_path: Path) -> dict:
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    return agent.run("mergeback")


def _missing_objects(repo: Path) -> set[str]:
    """Objects a partial clone has not fetched. Listing them fetches nothing."""
    listing = git("rev-list", "--objects", "--all", "--missing=print", cwd=repo)
    return {line[1:] for line in listing.splitlines() if line.startswith("?")}


# -- shallow clones ------------------------------------------------------------


def test_shallow_clone_with_the_base_in_history_runs_and_pushes(tmp_path):
    origin = _seed_origin(tmp_path)
    clone = tmp_path / "clone"
    git("clone", "--depth", "1", origin.as_uri(), str(clone), cwd=tmp_path)
    assert git("rev-parse", "--is-shallow-repository", cwd=clone) == "true"
    git("switch", "-c", "feature/x", cwd=clone)
    write(clone, "src/app.py", "x = 6\n")
    commit_all(clone, "feature")

    agent = ScriptedAgent(clone)
    agent.run("init")
    assert _green_to_verified(agent, tmp_path)["state"] == "VERIFIED"
    token = agent.run("gate")["data"]["token"]
    assert agent.run("push", "--confirm", token)["state"] == "PUSHED"
    head = git("rev-parse", "HEAD", cwd=clone)
    assert git("rev-parse", "refs/heads/feature/x", cwd=origin) == head


def test_shallow_clone_without_the_merge_base_cannot_start(tmp_path):
    origin = _seed_origin(tmp_path)
    seed = tmp_path / "seed"
    git("switch", "-c", "feature/x", "HEAD~2", cwd=seed)
    for number in range(1, 4):
        write(seed, "src/feature.py", f"f = {number}\n")
        commit_all(seed, f"f{number}")
    git("push", str(origin), "feature/x", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--depth", "2", "--no-single-branch", origin.as_uri(), str(clone), cwd=tmp_path)
    git("switch", "feature/x", cwd=clone)

    agent = ScriptedAgent(clone)
    env = agent.run("start", expect=ExitCode.PRECONDITION)
    # Known issue (pending), #392: the code says the diff is empty, and nothing suggests
    # deepening the history.
    assert env["error"]["code"] == "empty_diff"
    assert "cannot find a merge base with 'main'" in env["error"]["message"]
    assert env["next"] is None

    git("fetch", "--unshallow", cwd=clone)
    assert agent.run("start")["state"] == "REVIEW_AWAITING_FINDINGS"


# -- partial clones ------------------------------------------------------------


def test_partial_clone_fetches_blobs_lazily_for_the_review_diff(tmp_path):
    origin = _seed_origin(tmp_path)
    seed = tmp_path / "seed"
    git("switch", "-c", "feature/x", cwd=seed)
    write(seed, "src/app.py", "x = 5\ny = 1\n")
    commit_all(seed, "feature")
    git("push", str(origin), "feature/x", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--filter=blob:none", "--no-checkout", origin.as_uri(), str(clone), cwd=tmp_path)
    git("switch", "feature/x", cwd=clone)
    agent = ScriptedAgent(clone)

    missing = _missing_objects(clone)
    assert missing
    agent.run("start")
    # start compares trees, so it fetched no blob.
    assert _missing_objects(clone) == missing

    moved = tmp_path / "moved.git"
    origin.rename(moved)
    env = agent.run("context", expect=ExitCode.ERROR)
    assert env["error"]["code"] == "git_error"
    message = env["error"]["message"]
    assert "from promisor remote" in message
    # The binary check reads the blobs with cat-file before the diff runs.
    assert re.search(r"\b(diff|cat-file)\b", message)

    moved.rename(origin)
    env = agent.run("context")
    assert "+y = 1" in env["data"]["diff"]
    # The merge-base blob of the changed file arrived through a lazy fetch.
    assert _missing_objects(clone) < missing


# -- sparse checkouts ----------------------------------------------------------


@pytest.fixture
def sparse_feature_repo(tmp_repo: Path) -> Path:
    """A branch that changes code and a guide, checked out with only ``src`` present."""
    write(tmp_repo, "docs/guide.md", "# Guide\n")
    write(tmp_repo, ".agentic-preflight.toml", DOCS_CONFIG)
    commit_all(tmp_repo, "add guide and config")
    git("switch", "-c", "feature/x", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name, loud=False):\n    return f'hi {name}'\n")
    write(tmp_repo, "docs/guide.md", "# Guide\n\nPass loud=True to shout.\n")
    commit_all(tmp_repo, "add loud flag")
    git("sparse-checkout", "set", "--cone", "src", cwd=tmp_repo)
    assert not (tmp_repo / "docs").exists()
    return tmp_repo


def test_sparse_checkout_review_covers_paths_outside_the_sparse_set(sparse_feature_repo, tmp_path):
    agent = ScriptedAgent(sparse_feature_repo)
    agent.run("start")
    env = agent.run("context")
    assert env["data"]["changed_files"] == ["docs/guide.md", "src/app.py"]
    assert "Pass loud=True" in env["data"]["diff"]

    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    surface = agent.run("context", "--section", "docs")["data"]["doc_surface"]
    # Known issue (pending), #393: the documentation surface lists only files present in
    # the checkout, so a changed guide outside the sparse set is not on it.
    assert [entry["path"] for entry in surface] == ["README.md"]
    finding = {
        "path": "docs/guide.md",
        "severity": "medium",
        "action": "auto_fix",
        "title": "the guide misdescribes the flag",
    }
    docs_findings = tmp_path / "docs-findings.json"
    docs_findings.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")
    env = agent.run("submit-findings", "--file", str(docs_findings), expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "invalid_findings"
    assert "allowlist" in env["error"]["message"]


def test_isolated_worktree_inherits_the_sparse_checkout(sparse_feature_repo, tmp_path):
    # The lint command passes only when it sees the code and not the guide.
    probe = (
        "import pathlib, sys; "
        "sys.exit(0 if pathlib.Path('src/app.py').is_file() "
        "and not pathlib.Path('docs/guide.md').exists() else 1)"
    )
    lint = json.dumps(f'"{sys.executable}" -c "{probe}"')
    git("switch", "main", cwd=sparse_feature_repo)
    write(
        sparse_feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[worktree]\nmode = 'strict'\n\n"
        f"[commands]\nlint = {lint}\ntest = 'true'\n",
    )
    commit_all(sparse_feature_repo, "use strict mode")
    git("switch", "feature/x", cwd=sparse_feature_repo)
    git("rebase", "main", cwd=sparse_feature_repo)

    agent = ScriptedAgent(sparse_feature_repo)
    env = agent.run("start")
    validation = Path(env["data"]["worktree_path"])
    assert (validation / "src" / "app.py").is_file()
    assert not (validation / "docs").exists()
    assert git("sparse-checkout", "list", cwd=validation) == "src"

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"


def test_docs_fingerprint_depends_on_the_sparse_set(sparse_feature_repo, tmp_path):
    full = tmp_path / "full"
    git("worktree", "add", "--detach", str(full), "feature/x", cwd=sparse_feature_repo)
    git("sparse-checkout", "disable", cwd=full)
    assert (full / "docs" / "guide.md").is_file()

    snapshot = Config().model_dump(mode="json")
    args = {
        "base_sha": git("rev-parse", "main", cwd=full),
        "head_sha": git("rev-parse", "HEAD", cwd=full),
        "changed_files": ["docs/guide.md", "src/app.py"],
        "doc_paths": [],
        "config_snapshot": snapshot,
    }
    sparse = fingerprints.compute_docs_fingerprint(sparse_feature_repo, **args)
    complete = fingerprints.compute_docs_fingerprint(full, **args)
    # Tree identities match; the surface digest reads the checkout and does not.
    assert (sparse.base_tree_sha, sparse.head_tree_sha) == (
        complete.base_tree_sha,
        complete.head_tree_sha,
    )
    assert sparse.doc_surface_sha256 != complete.doc_surface_sha256


# -- submodules ----------------------------------------------------------------


def _submodule_repo(tmp_path: Path, config: str) -> Path:
    """``main`` pins ``lib`` at its first commit; ``feature/x`` moves it to the second."""
    library = tmp_path / "lib"
    library.mkdir()
    git("init", "-b", "main", cwd=library)
    write(library, "lib.py", "v = 1\n")
    write(library, "README.md", "# Library\n")
    commit_all(library, "l1")

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    write(repo, "README.md", "# demo\n")
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, "base")
    git("-c", "protocol.file.allow=always", "submodule", "add", str(library), "lib", cwd=repo)
    commit_all(repo, "add submodule")

    write(library, "lib.py", "v = 2\nSECRET_LIBRARY_CHANGE = True\n")
    commit_all(library, "l2")
    git("switch", "-c", "feature/x", cwd=repo)
    git("-c", "protocol.file.allow=always", "pull", cwd=repo / "lib")
    commit_all(repo, "bump lib")
    return repo


@pytest.mark.parametrize(
    "settings",
    [{}, {"diff.submodule": "log", "submodule.lib.ignore": "all"}],
    ids=["default", "log-and-ignore"],
)
def test_submodule_pointer_change_is_one_review_unit(tmp_path, settings):
    repo = _submodule_repo(tmp_path, DOCS_CONFIG)
    # Git settings that would otherwise change or hide the submodule diff.
    for key, value in settings.items():
        git("config", key, value, cwd=repo)
    agent = ScriptedAgent(repo)
    agent.run("start")
    env = agent.run("context")
    assert env["data"]["changed_files"] == ["lib"]
    assert "+Subproject commit " in env["data"]["diff"]
    assert "SECRET_LIBRARY_CHANGE" not in env["data"]["diff"]
    units = env["data"]["review_coverage"]["units"]
    assert [unit["path"] for unit in units] == ["lib"]

    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    surface = agent.run("context", "--section", "docs")["data"]["doc_surface"]
    assert [entry["path"] for entry in surface] == ["README.md"]


@pytest.mark.parametrize("change", ["modified", "untracked"])
def test_uncommitted_work_inside_a_submodule_blocks_start(tmp_path, change):
    repo = _submodule_repo(tmp_path, DOCS_CONFIG)
    if change == "modified":
        write(repo, "lib/lib.py", "v = 3\n")
    else:
        write(repo, "lib/scratch.txt", "notes\n")
    env = ScriptedAgent(repo).run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "dirty_tree"


@pytest.mark.parametrize("initialize", [False, True])
def test_isolated_worktree_submodules_need_the_setup_command(tmp_path, initialize):
    config = PROTECTED_CONFIG + "\n[worktree]\nmode = 'strict'\n"
    if initialize:
        config += "setup_command = 'git -c protocol.file.allow=always submodule update --init'\n"
    repo = _submodule_repo(tmp_path, config)

    env = ScriptedAgent(repo).run("start")
    library = Path(env["data"]["worktree_path"]) / "lib"
    assert library.is_dir()
    if initialize:
        assert "SECRET_LIBRARY_CHANGE" in (library / "lib.py").read_text(encoding="utf-8")
    else:
        assert list(library.iterdir()) == []


# -- Git LFS -------------------------------------------------------------------


def _lfs_feature_repo(tmp_path: Path, config: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("lfs", "install", "--local", cwd=repo)
    git("lfs", "track", "*.bin", cwd=repo)
    write(repo, "README.md", "# demo\n")
    write(repo, ".agentic-preflight.toml", config)
    commit_all(repo, "base")
    git("switch", "-c", "feature/x", cwd=repo)
    write(repo, "model.bin", "LARGE-BINARY-CONTENT\n")
    commit_all(repo, "add model")
    return repo


@requires_lfs
def test_lfs_review_sees_pointers_and_isolated_worktrees_smudge_content(tmp_path):
    repo = _lfs_feature_repo(tmp_path, PROTECTED_CONFIG + "\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(repo)
    validation = Path(agent.run("start")["data"]["worktree_path"])
    diff = agent.run("context")["data"]["diff"]
    assert "+version https://git-lfs.github.com/spec/v1" in diff
    assert "+oid sha256:" in diff
    assert "LARGE-BINARY-CONTENT" not in diff
    content = (validation / "model.bin").read_text(encoding="utf-8")
    assert content == "LARGE-BINARY-CONTENT\n"


@requires_lfs
def test_init_refuses_to_replace_the_lfs_pre_push_hook(tmp_path):
    repo = _lfs_feature_repo(tmp_path, PROTECTED_CONFIG)
    env = ScriptedAgent(repo).run("init", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "hook_exists"
    assert "git lfs pre-push" in (repo / ".git" / "hooks" / "pre-push").read_text()
    # Known issue (pending): the suggested command replaces the LFS hook.
    assert env["next"]["command"] == "agentic-preflight init --force"


COMPOSED_HOOK = """#!/bin/sh
# Run the gate first so that a blocked push sends nothing, then let Git LFS
# upload objects. Both commands read the ref list on standard input, so each
# gets its own copy.
updates=$(cat)
if command -v agentic-preflight >/dev/null 2>&1; then
  printf '%s\\n' "$updates" | AGENTIC_PREFLIGHT_PUSH_REMOTE="$1" agentic-preflight hook-check || exit $?
else
  echo "agentic-preflight: not found on PATH, skipping the quality gate (warn only)" >&2
fi
command -v git-lfs >/dev/null 2>&1 || { echo "git-lfs was not found on your path" >&2; exit 2; }
printf '%s\\n' "$updates" | git lfs pre-push "$@"
"""


@requires_lfs
@pytest.mark.skipif(sys.platform == "win32", reason="the composed hook is a POSIX shell script")
def test_a_composed_lfs_hook_gates_pushes_and_uploads_objects(tmp_path):
    repo = _lfs_feature_repo(tmp_path, PROTECTED_CONFIG)
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "origin", "main", cwd=repo)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(COMPOSED_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    blocked = subprocess.run(
        ["git", "push", "origin", "feature/x"], cwd=repo, capture_output=True, text=True
    )
    assert blocked.returncode != 0
    assert "feature/x" not in git("ls-remote", str(remote), cwd=repo)
    oid = git("lfs", "ls-files", "--long", cwd=repo).split()[0]
    stored = remote / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    # The gate runs first, so a blocked push uploads no LFS object either.
    assert not stored.exists()

    agent = ScriptedAgent(repo)
    assert agent.run("status")["data"]["hook"]["active"] is True
    _green_to_verified(agent, tmp_path)
    token = agent.run("gate")["data"]["token"]
    assert agent.run("push", "--confirm", token)["state"] == "PUSHED"
    assert stored.is_file()

    # Git LFS rewrites its hooks on a forced update, which drops the gate.
    git("lfs", "update", "--force", cwd=repo)
    assert agent.run("status")["data"]["hook"]["active"] is False


def test_the_documented_lfs_hook_is_the_tested_one():
    page = (Path(__file__).parent.parent / "docs" / "repository-shapes.md").read_text(
        encoding="utf-8"
    )
    assert f"```sh\n{COMPOSED_HOOK}```" in page


# -- monorepos and large trees -------------------------------------------------


def test_a_large_generated_change_is_refused_until_excluded(tmp_repo):
    write(tmp_repo, ".agentic-preflight.toml", PROTECTED_CONFIG + "\n[diff]\nmax_bytes = 4000\n")
    for number in range(200):
        write(tmp_repo, f"packages/p{number:03}/index.py", f"value = {number}\n")
    commit_all(tmp_repo, "a small synthetic monorepo")
    git("switch", "-c", "feature/x", cwd=tmp_repo)
    write(tmp_repo, "packages/p000/index.py", "value = 'changed'\n")
    for number in range(200):
        write(tmp_repo, f"generated/g{number:03}.py", f"generated = {number}\n")
    commit_all(tmp_repo, "change one package and regenerate code")

    agent = ScriptedAgent(tmp_repo)
    agent.run("start")
    env = agent.run("context", expect=ExitCode.STAGE_FAILED)
    assert env["data"]["mode"] == "diff_too_large"
    assert env["data"]["max_bytes"] == 4000
    # The read stops early (#356), so sizes are lower bounds for the files read so
    # far, and the rest are listed as unmeasured.
    assert env["data"]["complete"] is False
    listed = {path for path, _ in env["data"]["by_file"]} | set(env["data"]["unmeasured_files"])
    assert {f"generated/g{number:03}.py" for number in range(200)} <= listed
    agent.run("abort", "--reason", "other")

    write(
        tmp_repo,
        ".agentic-preflight.toml",
        PROTECTED_CONFIG + "\n[diff]\nmax_bytes = 4000\nexclude = ['generated/**']\n",
    )
    commit_all(tmp_repo, "exclude generated code from review")
    agent.run("start")
    env = agent.run("context")
    assert env["data"]["changed_files"] == [".agentic-preflight.toml", "packages/p000/index.py"]
    assert len(env["data"]["excluded_files"]) == 200


# -- SHA-256 object format -----------------------------------------------------


def test_sha256_repository_refuses_to_start(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", "--object-format=sha256", cwd=repo)
    write(repo, ".agentic-preflight.toml", PROTECTED_CONFIG)
    write(repo, "README.md", "# demo\n")
    commit_all(repo, "base")
    git("switch", "-c", "feature/x", cwd=repo)
    write(repo, "README.md", "# demo\n\nMore.\n")
    commit_all(repo, "feature")

    agent = ScriptedAgent(repo)
    env = agent.run("start", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "unsupported_object_format"
    assert "sha256" in env["error"]["message"]
    assert "COMPATIBILITY.md#repository-shapes" in env["error"]["message"]
    assert env["run_id"] is None
    assert not list((repo / ".git/agentic-preflight/runs").glob("r_*"))


@pytest.mark.parametrize("options", [(), ("--force",), ("--no-hook",)])
def test_sha256_init_refuses_before_writing_configuration_or_hook(tmp_path, options):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", "--object-format=sha256", cwd=repo)
    hook = repo / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    env = ScriptedAgent(repo).run("init", *options, expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "unsupported_object_format"
    assert "COMPATIBILITY.md#repository-shapes" in env["error"]["message"]
    assert not (repo / ".agentic-preflight.toml").exists()
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"


# -- worktrees and bare repositories -------------------------------------------


def test_bare_repository_with_a_linked_worktree(tmp_path):
    origin = _seed_origin(tmp_path)
    bare = tmp_path / "bare.git"
    git("clone", "--bare", str(origin), str(bare), cwd=tmp_path)

    for command in ("status", "init", "start"):
        env = ScriptedAgent(bare).run(command, expect=ExitCode.ERROR)
        assert env["error"]["code"] == "git_error"
        assert "must be run in a work tree" in env["error"]["message"]

    linked = tmp_path / "linked"
    git("worktree", "add", "-b", "feature/x", str(linked), "main", cwd=bare)
    write(linked, "src/app.py", "x = 6\n")
    commit_all(linked, "feature")

    agent = ScriptedAgent(linked)
    env = agent.run("init")
    assert Path(env["data"]["hook_path"]) == bare.resolve() / "hooks" / "pre-push"
    assert _green_to_verified(agent, tmp_path)["state"] == "VERIFIED"
    assert (bare / "agentic-preflight" / "runs").is_dir()
