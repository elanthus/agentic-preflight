"""Guessing what `lint` and `test` mean in this repo.

Deliberately a *suggestion* mechanism, not an inference: detection never picks a
command and runs it. It returns candidates, exits, and waits for the agent to
choose and re-invoke with ``--command``, because a guessed command may do
something the repository never intended.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Tool names that end in a stage word but are still that stage, unlike "latest".
_STAGE_TOOL_WORDS = {
    "test": ("pytest", "unittest"),
    "lint": ("eslint", "ruff", "flake8", "pylint", "biome"),
}


def _names_stage(name: str, stage: str) -> bool:
    """Whether a word of ``name`` is the stage (optionally plural) or a known stage tool."""
    words = {re.escape(stage) + "s?", *map(re.escape, _STAGE_TOOL_WORDS.get(stage, ()))}
    pattern = rf"(?<![a-z0-9])(?:{'|'.join(sorted(words))})(?![a-z0-9])"
    return re.search(pattern, name.lower()) is not None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@dataclass
class Candidate:
    command: str
    source: str

    def as_dict(self) -> dict:
        return {"command": self.command, "source": self.source}


def _from_package_json(root: Path, stage: str) -> list[Candidate]:
    path = root / "package.json"
    if not path.exists():
        return []
    text = _read_text(path)
    if text is None:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [
        Candidate(f"npm run {name}", "package.json scripts")
        for name in scripts
        if _names_stage(name, stage)
    ]


def _from_makefile(root: Path, stage: str) -> list[Candidate]:
    path = root / "Makefile"
    if not path.exists():
        return []
    text = _read_text(path)
    if text is None:
        return []
    targets = re.findall(r"^([a-zA-Z0-9_.-]+):", text, re.MULTILINE)
    return [Candidate(f"make {t}", "Makefile") for t in targets if _names_stage(t, stage)]


def _from_justfile(root: Path, stage: str) -> list[Candidate]:
    path = root / "justfile"
    if not path.exists():
        path = root / "Justfile"
    if not path.exists():
        return []
    text = _read_text(path)
    if text is None:
        return []
    targets = re.findall(r"^([a-zA-Z0-9_-]+):", text, re.MULTILINE)
    return [Candidate(f"just {t}", "justfile") for t in targets if _names_stage(t, stage)]


def _from_pyproject(root: Path, stage: str) -> list[Candidate]:
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    text = _read_text(path)
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []

    candidates: list[Candidate] = []
    tools = data.get("tool", {})
    if not isinstance(tools, dict):
        tools = {}
    if stage == "test" and ("pytest" in tools or (root / "tests").exists()):
        candidates.append(Candidate("pytest", "pyproject.toml"))
    if stage == "lint":
        if "ruff" in tools:
            candidates.append(Candidate("ruff check .", "pyproject.toml [tool.ruff]"))
        if "black" in tools:
            candidates.append(Candidate("black --check .", "pyproject.toml [tool.black]"))
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
    ):
        found.extend(source(root, stage))

    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in found:
        if candidate.command not in seen:
            seen.add(candidate.command)
            unique.append(candidate)
    return unique
