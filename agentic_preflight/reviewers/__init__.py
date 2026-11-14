"""Packaged command reviewers for ``[review] executor = "command"``.

Each provider module reads the review context JSON on stdin, shells out to that
provider's CLI, and writes the strict submission JSON to stdout. No module here imports a
model SDK. See docs/adr/0003-ship-a-packaged-reviewer.md.
"""

from __future__ import annotations

from collections.abc import Callable

from . import claude, codex

#: Each provider name mapped to its entry point. ``main`` returns the process exit code.
PROVIDERS: dict[str, Callable[[], int]] = {
    "claude": claude.main,
    "codex": codex.main,
}
