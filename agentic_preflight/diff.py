"""Assembling the diff that `context` hands to the agent.

Two jobs live here. The first is mechanical: collect the branch diff against the
merge-base, both whole and split per file. The second is a judgment call —
deciding what to do when the diff is larger than the agent can usefully hold in
one turn. See ``plan_delivery``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from . import gitx

#: Sensible defaults for the noise that makes most large diffs large. These are
#: not reviewable code: excluding them is almost always the right first move
#: when the budget trips, so it is the default rather than a discovery.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    "*.lock",
    "*-lock.json",
    "vendor/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/__snapshots__/**",
    "**/*.pb.go",
    "**/*_pb2.py",
)


def path_matches(path: str, pattern: str) -> bool:
    """Gitignore-flavoured glob matching for exclusion patterns.

    ``fnmatch``'s ``*`` crosses directory separators, which is what we want:
    ``*.lock`` should catch a nested ``sub/dir/uv.lock`` the same way git does.
    The one gap is a leading ``**/``, which under plain fnmatch would demand at
    least one separator — so ``**/*.min.js`` would miss a top-level
    ``app.min.js``. Retrying without the prefix closes that.
    """
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    return False


def is_excluded(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(path_matches(path, pattern) for pattern in patterns)


@dataclass
class DiffBundle:
    """The branch diff, whole and per file, after exclusions."""

    base: str
    head: str
    files: list[str] = field(default_factory=list)
    per_file: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The concatenation of the *included* per-file diffs.

        Derived rather than taken from a whole-diff call so that
        ``total_bytes == sum(file_bytes)`` holds unconditionally. With
        exclusions in play a raw ``git diff`` would carry bytes the agent never
        sees, and the budget check would disagree with the per-file report.
        """
        return "".join(self.per_file[path] for path in self.files)

    @property
    def total_bytes(self) -> int:
        return len(self.text.encode())

    def file_bytes(self, path: str) -> int:
        return len(self.per_file.get(path, "").encode())


def build_bundle(
    repo: Path | str,
    base: str,
    head: str = "HEAD",
    *,
    exclude: list[str] | tuple[str, ...] | None = None,
) -> DiffBundle:
    patterns = list(exclude) if exclude is not None else []
    all_files = gitx.changed_files(repo, base, head)
    kept = [p for p in all_files if not is_excluded(p, patterns)]
    dropped = [p for p in all_files if is_excluded(p, patterns)]
    return DiffBundle(
        base=base,
        head=head,
        files=kept,
        per_file={p: gitx.diff_text_for_path(repo, base, head, p) for p in kept},
        excluded=dropped,
    )


@dataclass
class BudgetReport:
    """The verdict on whether a bundle fits the agent's review budget.

    Over budget is not a truncation — it is a refusal. `context` exits 2 with
    ``mode="diff_too_large"`` and hands back ``by_file`` so the agent can narrow
    with ``--exclude`` or the user can raise the limit. The property being
    protected is that the agent never reviews part of a diff while believing it
    saw all of it; a loud stop protects that far more cheaply than chunking.
    """

    over_budget: bool
    total_bytes: int
    max_bytes: int
    by_file: list[tuple[str, int]] = field(default_factory=list)

    @property
    def overage(self) -> int:
        return max(0, self.total_bytes - self.max_bytes)


def check_budget(bundle: DiffBundle, max_bytes: int) -> BudgetReport:
    """Compare a bundle against the byte budget, largest files reported first."""
    by_file = sorted(
        ((path, bundle.file_bytes(path)) for path in bundle.files),
        key=lambda item: (-item[1], item[0]),
    )
    return BudgetReport(
        over_budget=bundle.total_bytes > max_bytes,
        total_bytes=bundle.total_bytes,
        max_bytes=max_bytes,
        by_file=by_file,
    )
