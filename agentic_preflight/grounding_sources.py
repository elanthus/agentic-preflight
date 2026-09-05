"""Bounded reads of committed grounding sources from one immutable Git tree."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import gitx

MAX_SOURCE_BYTES = 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_SOURCES = 1024
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".woff",
        ".woff2",
        ".mp4",
        ".mp3",
        ".mov",
        ".ttf",
    }
)


def load(
    repo: Path | str, select: Callable[[str], bool]
) -> tuple[dict[str, str], dict[str, int], set[str]]:
    """Select by metadata before one batched content read; report every omitted source.

    Object IDs from ls-tree bind subsequent reads to that tree even if HEAD moves.
    Neither staged files nor filesystem symlink targets enter the source set.
    """
    output = gitx.run(repo, "ls-tree", "-r", "-l", "-z", "HEAD").stdout
    candidates = []
    paths = set()
    omitted: Counter[str] = Counter()
    for record in output.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        if not select(path):
            continue
        paths.add(path)
        mode, kind, oid, size = metadata.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            omitted["non_regular"] += 1
        elif Path(path).suffix.lower() in _BINARY_SUFFIXES:
            omitted["binary"] += 1
        elif int(size) > MAX_SOURCE_BYTES:
            omitted["oversized"] += 1
        else:
            candidates.append((path, oid, int(size)))
    selected: list[tuple[str, str]] = []
    total = 0
    for path, oid, size in sorted(candidates):
        if len(selected) >= MAX_SOURCES or total + size > MAX_READ_BYTES:
            omitted["read_budget"] += 1
            continue
        total += size
        selected.append((path, oid))
    blobs = gitx.read_blobs(repo, [oid for _, oid in selected])
    texts = {}
    for (path, _), blob in zip(selected, blobs, strict=True):
        if b"\0" in blob:
            omitted["binary"] += 1
            continue
        texts[path] = blob.decode("utf-8", errors="backslashreplace")
    return texts, dict(sorted(omitted.items())), paths
