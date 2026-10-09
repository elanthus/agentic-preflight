"""Assemble this repository's changelog at release time (standard library only)."""

from __future__ import annotations

import argparse
import datetime
import re
from pathlib import Path

CATEGORIES = ("added", "changed", "deprecated", "removed", "fixed", "security")


def assemble(root: Path, version: str, date: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must be MAJOR.MINOR.PATCH")
    datetime.date.fromisoformat(date)
    changelog = root / "CHANGELOG.md"
    original = changelog.read_text(encoding="utf-8")
    if f"## [{version}]" in original:
        raise ValueError(f"version {version} is already in the changelog")
    prefix, marker, remainder = original.partition("## [Unreleased]\n")
    if not marker:
        raise ValueError("missing Unreleased heading")
    boundary = re.search(r"^## \[", remainder, re.MULTILINE)
    pending = remainder[: boundary.start()] if boundary else remainder
    history = remainder[boundary.start() :] if boundary else ""
    fragments = sorted((root / "docs/CHANGELOG.d").glob("*.md"))
    grouped: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    legacy = re.split(r"^### ([A-Za-z]+)\n", pending, flags=re.MULTILINE)
    if legacy[0].strip():
        raise ValueError("unexpected prose before Unreleased categories")
    for category, content in zip(legacy[1::2], legacy[2::2], strict=True):
        if category.lower() not in grouped:
            raise ValueError(f"unknown Unreleased category: {category}")
        if content.strip():
            grouped[category.lower()].append(content.strip())
    for fragment in fragments:
        match = re.fullmatch(r"[a-z0-9][a-z0-9-]*\.([a-z]+)\.md", fragment.name)
        if not match or match[1] not in grouped or fragment.is_symlink():
            raise ValueError(f"invalid fragment: {fragment.name}")
        content = fragment.read_text(encoding="utf-8").strip()
        if not content.startswith("- ") or re.search(r"^#", content, re.MULTILINE):
            raise ValueError(f"fragment must contain Markdown bullets: {fragment.name}")
        grouped[match[1]].append(content)
    sections = []
    for category, entries in grouped.items():
        if entries:
            sections.append(f"### {category.title()}\n\n" + "\n\n".join(entries))
    if not sections:
        raise ValueError("no unreleased changes")
    rendered = prefix + marker + f"\n## [{version}] - {date}\n\n"
    rendered += "\n\n".join(sections) + "\n\n" + history
    temporary = changelog.with_suffix(".md.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(changelog)
    for fragment in fragments:
        fragment.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--date", required=True, help="release date, YYYY-MM-DD")
    args = parser.parse_args()
    try:
        assemble(Path(__file__).resolve().parents[1], args.version, args.date)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
