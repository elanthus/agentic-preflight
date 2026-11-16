"""Time fingerprinting commands in a synthetic repository with a large tree.

Builds a Git repository with ``--files`` committed files (default 120,000), a
documentation tree, and a feature branch one small commit ahead of ``main``. It
drives a run to the docs review and then times ``status``, the docs
``submit-findings``, and ``stage run lint``, each in a fresh process, and counts
the ``git ls-tree`` processes each one starts.

Every CLI subprocess gets ``XDG_DATA_HOME`` and ``XDG_CACHE_HOME`` inside the
workspace, so a benchmark run writes nothing to your telemetry directory or
worktree cache. Run it from a checkout with ``uv run``::

    uv run python scripts/bench_large_tree.py --workspace /tmp/ap-bench

The workspace keeps the generated repository, so a second invocation with the
same ``--workspace`` and ``--files`` skips the slow build and only re-times.
Pass ``--profile`` to write a cProfile dump of ``status`` into the workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

FILES_PER_DIRECTORY = 500
DOC_FILES = 600


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and not key.startswith("AGENTIC_PREFLIGHT")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Bench",
        GIT_AUTHOR_EMAIL="bench@example.invalid",
        GIT_COMMITTER_NAME="Bench",
        GIT_COMMITTER_EMAIL="bench@example.invalid",
    )
    return env


def build_repository(repo: Path, files: int) -> None:
    """Write and commit the synthetic tree, then branch ``feature/x`` from it."""
    env = _git_env()
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main", env=env)
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    (repo / ".agentic-preflight.toml").write_text(
        f'[worktree]\nmode = "in_place"\n[commands]\nlint = {command}\ntest = {command}\n'
    )
    (repo / "README.md").write_text("# Bench\n\nA synthetic repository.\n")
    (repo / "AGENTS.md").write_text("# Agents\n\nKeep src/pkg0000/mod0000.py stable.\n")
    docs = repo / "docs" / "guide"
    docs.mkdir(parents=True)
    for index in range(DOC_FILES):
        (docs / f"page{index:04d}.md").write_text(f"# Page {index}\n\nSee module {index}.\n")
    for index in range(files):
        directory = repo / "src" / f"pkg{index // FILES_PER_DIRECTORY:04d}"
        if index % FILES_PER_DIRECTORY == 0:
            directory.mkdir(parents=True)
        (directory / f"mod{index % FILES_PER_DIRECTORY:04d}.py").write_text(f"VALUE = {index}\n")
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", "synthetic tree", env=env)
    _git(repo, "switch", "-q", "-c", "feature/x", env=env)
    target = repo / "src" / "pkg0000" / "mod0000.py"
    target.write_text("VALUE = -1\n")
    _git(repo, "commit", "-q", "-am", "change one module", env=env)


class Cli:
    """Run the CLI in fresh processes with scratch XDG directories."""

    def __init__(self, repo: Path, workspace: Path, trace: Path) -> None:
        self.repo = repo
        self.trace = trace
        self.env = _git_env()
        self.env.update(
            XDG_DATA_HOME=str(workspace / "xdg-data"),
            XDG_CACHE_HOME=str(workspace / "xdg-cache"),
        )

    def run(
        self, *argv: str, prefix: tuple[str, ...] = (), allowed: tuple[int, ...] = (0,)
    ) -> tuple[float, int, dict]:
        """Return wall seconds, ``git ls-tree`` process count, and the envelope."""
        self.trace.unlink(missing_ok=True)
        env = {**self.env, "GIT_TRACE2_EVENT": str(self.trace)}
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, *prefix, "-m", "agentic_preflight", *argv],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
        )
        elapsed = time.perf_counter() - started
        envelope = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout else {}
        if result.returncode not in allowed:
            raise SystemExit(
                f"{' '.join(argv)} exited {result.returncode}: {result.stdout}{result.stderr}"
            )
        return elapsed, self._ls_tree_count(), envelope

    def _ls_tree_count(self) -> int:
        if not self.trace.exists():
            return 0
        count = 0
        for line in self.trace.read_text(encoding="utf-8", errors="replace").splitlines():
            event = json.loads(line)
            if event.get("event") == "cmd_name" and event.get("name") == "ls-tree":
                count += 1
        return count


def drive_to_docs_green(cli: Cli, workspace: Path) -> None:
    payload = workspace / "findings.json"
    # Exit 3 means there was no run to abort.
    cli.run("abort", "--force", allowed=(0, 3))
    # A fresh intent keeps earlier runs' review and docs evidence from being reused.
    cli.run("start", "--intent", f"benchmark fingerprinting on a large tree ({time.time_ns()})")
    _, _, envelope = cli.run("context")
    manifest = envelope["data"]["review_coverage"]["manifest"]
    payload.write_text(
        json.dumps({"coverage": {"manifest": manifest, "examined": "all"}, "findings": []})
    )
    cli.run("submit-findings", "--file", str(payload))
    cli.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--files", type=int, default=120_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    repo = workspace / f"repo-{args.files}"
    if args.rebuild and repo.exists():
        shutil.rmtree(repo)
    if not repo.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        build_repository(repo, args.files)
        print(f"built {args.files} files in {time.perf_counter() - started:.1f}s")
    cli = Cli(repo, workspace, workspace / "trace2.json")

    commands = ("status", "submit-findings docs", "stage run lint")
    results: dict[str, list[tuple[float, int]]] = {command: [] for command in commands}
    for _ in range(args.repeat):
        drive_to_docs_green(cli, workspace)
        elapsed, ls_tree, _ = cli.run("status")
        results["status"].append((elapsed, ls_tree))
        elapsed, ls_tree, _ = cli.run("submit-findings", "--file", str(workspace / "findings.json"))
        results["submit-findings docs"].append((elapsed, ls_tree))
        elapsed, ls_tree, _ = cli.run("stage", "run", "lint")
        results["stage run lint"].append((elapsed, ls_tree))
    if args.profile:
        profile = workspace / "status.prof"
        cli.run("status", prefix=("-m", "cProfile", "-o", str(profile)))
        print(f"profile written to {profile}")

    print(f"{'command':<22} {'median s':>9} {'min s':>7} {'ls-tree':>8}")
    for command, samples in results.items():
        times = [elapsed for elapsed, _ in samples]
        counts = sorted({count for _, count in samples})
        print(
            f"{command:<22} {statistics.median(times):>9.2f} {min(times):>7.2f} "
            f"{'/'.join(map(str, counts)):>8}"
        )


if __name__ == "__main__":
    main()
