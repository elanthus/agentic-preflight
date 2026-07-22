"""Node dependency setup in disposable worktrees."""

from pathlib import Path

from agentic_cli import dependencies, gitx, runtime, worktree
from tests.conftest import commit_all, git, write


def _node_probe(major: int) -> runtime.NodeProbe:
    info = runtime.RuntimeInfo(
        manager="none",
        pin_file=".nvmrc",
        requested="24",
        available=True,
        node_project=True,
    )
    return runtime.NodeProbe(
        True,
        f"{major}.1.0",
        major,
        "137" if major == 24 else "127",
        info,
    )


def _npm_repo(tmp_repo: Path) -> tuple[Path, str]:
    write(tmp_repo, ".gitignore", ".env\nnode_modules/\n")
    write(tmp_repo, "package.json", '{"engines":{"node":"24.x"}}\n')
    write(tmp_repo, "package-lock.json", '{"lockfileVersion":3}\n')
    write(tmp_repo, ".nvmrc", "24\n")
    base = commit_all(tmp_repo, "add npm project")
    (tmp_repo / "node_modules" / "native-addon").mkdir(parents=True)
    write(tmp_repo, "node_modules/native-addon/index.js", "module.exports = 1\n")
    return tmp_repo, base


def _worktree(repo: Path, tmp_path: Path, name: str = "deps") -> Path:
    return worktree.create(
        repo,
        path=tmp_path / name,
        branch=f"ac/{name}",
        head_sha=gitx.rev_parse(repo, "HEAD"),
    )


def test_pnpm_uses_a_frozen_install_and_its_shared_store(
    tmp_repo, tmp_path, monkeypatch
):
    write(tmp_repo, "package.json", '{"packageManager":"pnpm@11.0.0"}\n')
    write(tmp_repo, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    base = commit_all(tmp_repo, "add pnpm project")
    wt = _worktree(tmp_repo, tmp_path, "pnpm")
    seen = []

    def fake_install(target, command, **kwargs):
        seen.append((target, command, kwargs))
        return 0, {"manager": "none"}

    monkeypatch.setattr(dependencies, "_run_install", fake_install)
    result = dependencies.setup(tmp_repo, wt, base_sha=base)

    assert result.manager == "pnpm"
    assert result.action == "install"
    assert result.command == "pnpm install --frozen-lockfile"
    assert seen[0][1] == result.command


def test_npm_shares_main_node_modules_when_inputs_and_node_abi_match(
    tmp_repo, tmp_path, monkeypatch
):
    repo, base = _npm_repo(tmp_repo)
    wt = _worktree(repo, tmp_path, "share")
    monkeypatch.setattr(runtime, "probe_node", lambda *args, **kwargs: _node_probe(24))

    result = dependencies.setup(repo, wt, base_sha=base)

    assert result.action == "share"
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules").resolve() == (repo / "node_modules").resolve()


def test_npm_ci_isolated_when_dependency_inputs_changed(
    tmp_repo, tmp_path, monkeypatch
):
    repo, base = _npm_repo(tmp_repo)
    write(repo, "package-lock.json", '{"lockfileVersion":3,"changed":true}\n')
    commit_all(repo, "change dependency graph")
    wt = _worktree(repo, tmp_path, "changed")
    monkeypatch.setattr(runtime, "probe_node", lambda *args, **kwargs: _node_probe(24))
    commands = []
    monkeypatch.setattr(
        dependencies,
        "_run_install",
        lambda target, command, **kwargs: (commands.append(command) or 0, {}),
    )

    result = dependencies.setup(repo, wt, base_sha=base)

    assert result.action == "install"
    assert result.command == "npm ci"
    assert commands == ["npm ci"]
    assert not (wt / "node_modules").exists()


def test_npm_never_shares_modules_built_for_a_different_node_major(
    tmp_repo, tmp_path, monkeypatch
):
    repo, base = _npm_repo(tmp_repo)
    wt = _worktree(repo, tmp_path, "abi")
    monkeypatch.setattr(runtime, "probe_node", lambda *args, **kwargs: _node_probe(22))
    monkeypatch.setattr(
        dependencies, "_run_install", lambda target, command, **kwargs: (0, {})
    )

    result = dependencies.setup(repo, wt, base_sha=base)

    assert result.action == "install"
    assert result.command == "npm ci"
    assert "Node version" in result.reason
    assert not (wt / "node_modules").is_symlink()


def test_worktree_cleanup_removes_only_the_node_modules_symlink(
    tmp_repo, tmp_path, monkeypatch
):
    repo, base = _npm_repo(tmp_repo)
    wt = _worktree(repo, tmp_path, "cleanup")
    monkeypatch.setattr(runtime, "probe_node", lambda *args, **kwargs: _node_probe(24))
    dependencies.setup(repo, wt, base_sha=base)

    worktree.remove(repo, wt, branch="ac/cleanup")

    assert not wt.exists()
    assert (repo / "node_modules/native-addon/index.js").is_file()
    assert git("branch", "--list", "ac/cleanup", cwd=repo) == ""
