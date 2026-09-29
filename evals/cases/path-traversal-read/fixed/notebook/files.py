from pathlib import Path


def read_note(root: Path, name: str) -> str:
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("note is outside storage root")
    return target.read_text(encoding="utf-8")
