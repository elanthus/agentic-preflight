"""Guessing what `lint` and `test` mean in this repo.

Deliberately a *suggestion* mechanism, not an inference: detection never picks a
command and runs it. It returns candidates, exits, and waits for the agent to
choose and re-invoke with ``--command``. Running a guessed command against
someone's repo is exactly the kind of confident wrongness this tool exists to
prevent.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class Candidate:
    command: str
    source: str
    trust: Literal["repo_manifest", "untrusted"]

    def as_dict(self) -> dict:
        return {"command": self.command, "source": self.source, "trust": self.trust}


def _from_package_json(root: Path, stage: str) -> list[Candidate]:
    path = root / "package.json"
    if not path.exists():
        return []
    try:
        scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return []
    return [
        Candidate(f"npm run {name}", "package.json scripts", "repo_manifest")
        for name in scripts
        if stage in name.lower()
    ]


def _from_makefile(root: Path, stage: str) -> list[Candidate]:
    path = root / "Makefile"
    if not path.exists():
        return []
    targets = re.findall(r"^([a-zA-Z0-9_.-]+):", path.read_text(encoding="utf-8"), re.MULTILINE)
    return [
        Candidate(f"make {t}", "Makefile", "repo_manifest") for t in targets if stage in t.lower()
    ]


def _from_justfile(root: Path, stage: str) -> list[Candidate]:
    path = root / "justfile"
    if not path.exists():
        path = root / "Justfile"
    if not path.exists():
        return []
    targets = re.findall(r"^([a-zA-Z0-9_-]+):", path.read_text(encoding="utf-8"), re.MULTILINE)
    return [
        Candidate(f"just {t}", "justfile", "repo_manifest") for t in targets if stage in t.lower()
    ]


def _from_pyproject(root: Path, stage: str) -> list[Candidate]:
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []

    candidates: list[Candidate] = []
    tools = data.get("tool", {})
    if stage == "test" and ("pytest" in tools or (root / "tests").exists()):
        candidates.append(Candidate("pytest", "pyproject.toml", "repo_manifest"))
    if stage == "lint":
        if "ruff" in tools:
            candidates.append(
                Candidate("ruff check .", "pyproject.toml [tool.ruff]", "repo_manifest")
            )
        if "black" in tools:
            candidates.append(
                Candidate("black --check .", "pyproject.toml [tool.black]", "repo_manifest")
            )
    return candidates


def _from_workflows(root: Path, stage: str) -> list[Candidate]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    candidates: list[Candidate] = []
    for path in sorted(workflows.glob("*.y*ml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("run:") and stage in stripped.lower():
                command = stripped.removeprefix("run:").strip()
                if command:
                    candidates.append(
                        Candidate(
                            command,
                            f"untrusted:workflow:.github/workflows/{path.name}",
                            "untrusted",
                        )
                    )
    return candidates


def candidates_for(root: Path | str, stage: str) -> list[Candidate]:
    """Every plausible command for ``stage``, de-duplicated, best sources first."""
    root = Path(root)
    found: list[Candidate] = []
    for source in (
        _from_pyproject,
        _from_package_json,
        _from_justfile,
        _from_makefile,
        _from_workflows,
    ):
        found.extend(source(root, stage))

    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in found:
        if candidate.command not in seen:
            seen.add(candidate.command)
            unique.append(candidate)
    return unique
