from pathlib import Path


def read_note(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")
