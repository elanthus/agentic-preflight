"""Path matching for GitHub CODEOWNERS, independent of diff exclusion globs."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _pattern_regex(pattern: str) -> re.Pattern[str] | None:
    # GitHub does not support negation or bracket character classes.
    if not pattern or pattern.startswith(("!", "\\#")) or "[" in pattern or "]" in pattern:
        return None
    anchored = pattern.startswith("/")
    directory = pattern.endswith("/")
    pattern = pattern.lstrip("/").rstrip("/")
    prefix = "^" if anchored or "/" in pattern else r"(?:^|/)"
    pieces: list[str] = []
    segments = pattern.split("/")
    for index, segment in enumerate(segments):
        if segment == "**":
            pieces.append(".*" if index == len(segments) - 1 else "(?:[^/]+/)*")
            continue
        pieces.append(
            "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in segment)
        )
        if index < len(segments) - 1:
            pieces.append("/")
    # A matched directory owns its descendants, except the explicit `dir/*`
    # form, which GitHub documents as matching only immediate children.
    suffix = "$" if segments[-1] == "*" and len(segments) > 1 and not directory else r"(?:/|$)"
    return re.compile(prefix + "".join(pieces) + suffix)


def matches(path: str, pattern: str) -> bool:
    compiled = _pattern_regex(pattern)
    return compiled is not None and compiled.search(path) is not None
