"""Node dependency setup in disposable worktrees."""

from pathlib import Path

from agentic_cli import dependencies, gitx, worktree
from tests.conftest import commit_all, write


def _npm_repo(tmp_repo: Path) -> Path:
    write(tmp_repo, ".gitignore", ".env\nnode_modules/\n")
    write(tmp_repo, "package.json", '{"engines":{"node":"24.x"}}\n')
    write(tmp_repo, "package-lock.json", '{"lockfileVersion":3}\n')
    write(tmp_repo, ".nvmrc", "24\n")
    commit_all(tmp_repo, "add npm project")
    (tmp_repo / "node_modules" / "native-addon").mkdir(parents=True)
    write(tmp_repo, "node_modules/native-addon/index.js", "module.exports = 1\n")
    return tmp_repo


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
    commit_all(tmp_repo, "add pnpm project")
    wt = _worktree(tmp_repo, tmp_path, "pnpm")
    seen = []

    def fake_install(target, command, **kwargs):
        seen.append((target, command, kwargs))
        return 0, {"manager": "none"}

    monkeypatch.setattr(dependencies, "_run_install", fake_install)
    result = dependencies.setup(wt)

    assert result.manager == "pnpm"
    assert result.action == "install"
    assert result.command == "pnpm install --frozen-lockfile"
    assert seen[0][1] == result.command


def test_npm_always_runs_an_isolated_ci_even_when_main_modules_exist(
    tmp_repo, tmp_path, monkeypatch
):
    repo = _npm_repo(tmp_repo)
    wt = _worktree(repo, tmp_path, "npm")
    calls = []
    monkeypatch.setattr(
        dependencies,
        "_run_install",
        lambda target, command, **kwargs: (
            calls.append((target, command, kwargs)) or 0,
            {"manager": "nvm"},
        ),
    )

    result = dependencies.setup(wt)

    assert result.manager == "npm"
    assert result.action == "install"
    assert result.command == "npm ci"
    assert "in isolation" in result.reason
    assert result.node == {"manager": "nvm"}
    assert calls[0][0] == wt
    assert calls[0][1] == "npm ci"
    assert not (wt / "node_modules").is_symlink()
    assert (repo / "node_modules/native-addon/index.js").is_file()


def test_npm_install_exit_code_is_reported(tmp_repo, tmp_path, monkeypatch):
    repo = _npm_repo(tmp_repo)
    wt = _worktree(repo, tmp_path, "npm-failure")
    monkeypatch.setattr(
        dependencies, "_run_install", lambda target, command, **kwargs: (17, {})
    )

    result = dependencies.setup(wt)

    assert result.exit_code == 17


def test_no_node_lockfile_skips_dependency_setup(tmp_repo, tmp_path):
    wt = _worktree(tmp_repo, tmp_path, "no-node-lockfile")

    result = dependencies.setup(wt)

    assert result.manager == "none"
    assert result.action == "skip"
