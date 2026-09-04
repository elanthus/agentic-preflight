from pathlib import Path


def read_note(root: Path, name: str) -> str:
    target = (root / name).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("note is outside storage root")
    return target.read_text(encoding="utf-8")
