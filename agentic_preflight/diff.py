"""Assembling the diff that `context` hands to the agent.

Two jobs live here. The first is mechanical: collect the branch diff against the
merge-base, both whole and split per file. The second is a judgment call:
deciding what to do when the diff is larger than the agent can usefully hold in
one turn. See ``plan_delivery``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import cached_property
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
    least one separator, so ``**/*.min.js`` would miss a top-level
    ``app.min.js``. Retrying without the prefix closes that.

    A ``**`` segment between two others has the same gap as a leading one: Git
    lets ``src/**/secrets.py`` match ``src/secrets.py``, where fnmatch needs a
    directory in between. Such a segment matches zero or more directories.

    A bare (unanchored) directory pattern like ``src/`` has the opposite gap:
    appending ``**`` alone only matches at the root, so ``nested/src/app.py``
    needs the same pattern retried with a ``**/`` prefix to match at any depth.
    """
    return any(_matches_pieces(path, pieces) for pieces in _fnmatch_attempts(pattern))


def _fnmatch_attempts(pattern: str) -> tuple[tuple[str, ...], ...]:
    """The attempts ``path_matches`` tries, in order, as ``fnmatchcase`` pieces.

    Each attempt is the pattern split at its inner ``**`` segments; see
    :func:`_matches_pieces`. There are at most two attempts, whatever the pattern.
    """
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    bare_directory = not anchored and pattern.endswith("/")
    pieces = _split_at_inner_globstars(pattern)
    if pattern.endswith("/"):
        pieces = (*pieces[:-1], f"{pieces[-1]}**")
    if bare_directory:
        return (pieces, (f"**/{pieces[0]}", *pieces[1:]))
    if not anchored and pieces[0].startswith("**/"):
        return (pieces, (pieces[0][3:], *pieces[1:]))
    return (pieces,)


def _split_at_inner_globstars(pattern: str) -> tuple[str, ...]:
    """Split ``pattern`` at each inner ``**`` segment.

    A run of ``**`` segments means the same as one, so it is collapsed first. An
    inner segment then has a non-empty segment on both sides, so a leading ``**/``
    (handled by its own retry) and a trailing ``/**`` or ``/**/`` are left alone:
    Git's ``a/**`` and ``a/**/`` never match ``a`` itself.
    """
    segments: list[str] = []
    for segment in pattern.split("/"):
        if not (segment == "**" and segments and segments[-1] == "**"):
            segments.append(segment)
    pieces: list[str] = []
    current: list[str] = []
    for index, segment in enumerate(segments):
        inner = 0 < index < len(segments) - 1 and segments[index + 1] != ""
        if segment == "**" and inner:
            pieces.append("/".join(current))
            current = []
        else:
            current.append(segment)
    pieces.append("/".join(current))
    return tuple(pieces)


def _matches_pieces(path: str, pieces: tuple[str, ...]) -> bool:
    """Whether ``path`` reads as the pieces joined by ``/``, zero or more directories, ``/``.

    That is what each inner ``**`` segment stands for. The pieces are matched in
    order against the text between separators, so the work grows with the number
    of pieces times the square of the path's depth, never with the number of ways
    to keep or drop each ``**``.
    """
    if len(pieces) == 1:
        return fnmatch.fnmatchcase(path, pieces[0])
    separators = [index for index, char in enumerate(path) if char == "/"]
    starts = [0]
    for piece in pieces[:-1]:
        # The piece ends at a separator. The earliest such end leaves the most
        # room: the next piece may start after it or after any later separator.
        first_end = next(
            (
                end
                for end in separators
                if any(
                    start <= end and fnmatch.fnmatchcase(path[start:end], piece) for start in starts
                )
            ),
            None,
        )
        if first_end is None:
            return False
        starts = [end + 1 for end in separators if end >= first_end]
    return any(fnmatch.fnmatchcase(path[start:], pieces[-1]) for start in starts)


_WILDCARD = re.compile(r"[*?\[]")


def literal_scope(pattern: str) -> str | None:
    """Return a path that every path accepted by ``pattern`` equals or lies beneath.

    ``fnmatchcase`` treats only ``*``, ``?`` and ``[`` as special, so the text
    before the first of them must begin every match. A wildcard-free pattern is
    its own scope; otherwise the scope stops at the last ``/`` before the
    wildcard. ``None`` means a match can lie anywhere, or the scope is not a
    plain ASCII repository path that Git takes unchanged as a literal pathspec. Callers use
    the scope to narrow a Git listing and must still match each listed path.
    """
    # Git on macOS may rewrite a non-ASCII argument to its precomposed Unicode
    # form, which would no longer name a decomposed path in the tree.
    if not pattern or "\\" in pattern or not pattern.isascii():
        return None
    wildcard = _WILDCARD.search(pattern)
    scope = pattern if wildcard is None else pattern[: wildcard.start()].rpartition("/")[0]
    if not scope or any(part in {"", ".", ".."} for part in scope.split("/")):
        return None
    return scope


def match_scope(pattern: str) -> str | None:
    """Return the literal scope of every path :func:`path_matches` accepts for ``pattern``."""
    attempts = _fnmatch_attempts(pattern)
    # A bare directory or ``**/`` pattern gets a second attempt because it can
    # match at any depth, so it has no scope. Every match of a split attempt
    # begins with a match of its first piece, which ends at a separator.
    return literal_scope(attempts[0][0]) if len(attempts) == 1 else None


def is_excluded(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """Return whether a path matches any exclude pattern."""
    return any(path_matches(path, pattern) for pattern in patterns)


@dataclass
class DiffBundle:
    """The branch diff, whole and per file, after exclusions."""

    base: str
    head: str
    files: list[str] = field(default_factory=list)
    per_file: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        missing = [path for path in self.files if path not in self.per_file]
        if missing:
            raise ValueError("missing patches for changed files: " + ", ".join(missing))

    @cached_property
    def text(self) -> str:
        """The concatenation of the *included* per-file diffs.

        Derived rather than taken from a whole-diff call so that
        ``total_bytes == sum(file_bytes)`` holds unconditionally. With
        exclusions in play a raw ``git diff`` would carry bytes the agent never
        sees, and the budget check would disagree with the per-file report.
        Computed once: a bundle is not modified after it is built.
        """
        return "".join(self.per_file[path] for path in self.files)

    @cached_property
    def total_bytes(self) -> int:
        return len(self.text.encode())

    def file_bytes(self, path: str) -> int:
        return len(self.per_file.get(path, "").encode())


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


@dataclass(frozen=True)
class ReviewUnit:
    """One mechanically identifiable part of a delivered review diff."""

    id: str
    path: str
    kind: str
    digest: str
    old_start: int | None = None
    old_count: int | None = None
    new_start: int | None = None
    new_count: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "id": self.id,
            "path": self.path,
            "kind": self.kind,
            "digest": self.digest,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
        }


@dataclass(frozen=True)
class ReviewManifest:
    """Snapshot-bound inventory used to prove review-unit accounting."""

    manifest: str
    base_sha: str
    head_sha: str
    diff_sha256: str
    units: tuple[ReviewUnit, ...]
    excluded_files: tuple[str, ...]
    grounding_sha256: str | None = None

    def as_dict(self) -> dict:
        return {
            "manifest": self.manifest,
            "base": self.base_sha,
            "head": self.head_sha,
            "diff_sha256": self.diff_sha256,
            "grounding_sha256": self.grounding_sha256,
            "total_units": len(self.units),
            "units": [unit.as_dict() for unit in self.units],
            "excluded_files": list(self.excluded_files),
        }


def _review_unit_parts(
    path: str, patch: str
) -> list[tuple[str, str, tuple[int, int, int, int] | None]]:
    """Split a per-file patch into hunks, with a file unit for non-hunk diffs."""
    # Split on LF only: a lone CR or form feed in file content is not a Git line break.
    lines = [line + "\n" for line in patch.split("\n")]
    lines[-1] = lines[-1][:-1]
    if not lines[-1]:
        lines.pop()
    starts = [index for index, line in enumerate(lines) if line.startswith("@@ ")]
    if not starts:
        return [(patch, "file", None)] if patch else []

    parts: list[tuple[str, str, tuple[int, int, int, int] | None]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        header = lines[start].rstrip("\n")
        match = _HUNK_HEADER.match(header)
        if match is None:
            # Git produced a hunk marker we cannot safely locate. Keep it reviewable,
            # but do not pretend line-based finding inference is available.
            parts.append(("".join(lines[start:end]), "hunk", None))
            continue
        old_start, old_count, new_start, new_count = match.groups()
        parts.append(
            (
                "".join(lines[start:end]),
                "hunk",
                (
                    int(old_start),
                    int(old_count) if old_count is not None else 1,
                    int(new_start),
                    int(new_count) if new_count is not None else 1,
                ),
            )
        )
    return parts


def build_review_manifest(
    repo: Path | str,
    bundle: DiffBundle,
    grounding_sha256: str | None = None,
) -> ReviewManifest:
    """Derive stable review units and a digest from the exact included diff."""
    units: list[ReviewUnit] = []
    for path in bundle.files:
        patch = bundle.per_file[path]
        for text, kind, location in _review_unit_parts(path, patch):
            old_start = old_count = new_start = new_count = None
            if location is not None:
                old_start, old_count, new_start, new_count = location
            units.append(
                ReviewUnit(
                    id=f"U{len(units) + 1:04d}",
                    path=path,
                    kind=kind,
                    digest=hashlib.sha256(text.encode()).hexdigest(),
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                )
            )

    base_sha = gitx.rev_parse(repo, bundle.base)
    head_sha = gitx.rev_parse(repo, bundle.head)
    diff_sha256 = hashlib.sha256(bundle.text.encode()).hexdigest()
    payload = {
        "version": 2,
        "base": base_sha,
        "head": head_sha,
        "diff_sha256": diff_sha256,
        "grounding_sha256": grounding_sha256,
        "excluded_files": list(bundle.excluded),
        "units": [unit.as_dict() for unit in units],
    }
    manifest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReviewManifest(
        manifest=manifest,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256=diff_sha256,
        grounding_sha256=grounding_sha256,
        units=tuple(units),
        excluded_files=tuple(bundle.excluded),
    )


def _inventory(
    repo: Path | str, base: str, head: str, exclude: list[str] | tuple[str, ...] | None
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return display paths by raw path, the kept raw paths, and the excluded display paths."""
    patterns = list(exclude) if exclude is not None else []
    # Raw paths go back to Git; everything stored, hashed, or shown uses display paths.
    raw_files = gitx.changed_files_raw(repo, base, head)
    display = {raw: gitx.display_path(raw) for raw in raw_files}
    kept_raw = [p for p in raw_files if not is_excluded(display[p], patterns)]
    dropped = [display[p] for p in raw_files if is_excluded(display[p], patterns)]
    return display, kept_raw, dropped


def _bundle(
    base: str,
    head: str,
    display: dict[str, str],
    kept_raw: list[str],
    dropped: list[str],
    raw_patches: dict[str, str],
) -> DiffBundle:
    return DiffBundle(
        base=base,
        head=head,
        files=[display[p] for p in kept_raw],
        per_file={display[p]: raw_patches[p] for p in kept_raw if p in raw_patches},
        excluded=dropped,
    )


def build_bundle(
    repo: Path | str,
    base: str,
    head: str = "HEAD",
    *,
    exclude: list[str] | tuple[str, ...] | None = None,
    legacy: bool = False,
    refuse_hidden: bool = True,
) -> DiffBundle:
    """Build the review diff bundle between two refs, applying excludes.

    ``legacy=True`` builds the patch text of 0.8.1 and earlier releases, and
    ``refuse_hidden`` controls its check for attribute-hidden content; see
    :func:`agentic_preflight.gitx.diff_text_by_path`.
    """
    display, kept_raw, dropped = _inventory(repo, base, head, exclude)
    raw_patches = gitx.diff_text_by_path(
        repo, base, head, kept_raw, legacy=legacy, refuse_hidden=refuse_hidden
    )
    return _bundle(base, head, display, kept_raw, dropped, raw_patches)


#: How far past ``[diff] max_bytes`` a budgeted read keeps measuring before it
#: stops. Up to this multiple the over-budget report is as exact as a full read;
#: past it the read is cut short, so a huge generated file costs a bounded read.
BUDGET_MEASURE_FACTOR = 4


def build_bundle_within_budget(
    repo: Path | str,
    base: str,
    head: str = "HEAD",
    *,
    exclude: list[str] | tuple[str, ...] | None = None,
    max_bytes: int,
) -> tuple[DiffBundle | None, BudgetReport]:
    """Build the review bundle, or stop reading once it is far over ``max_bytes``.

    Reading stops once the patch text passes ``BUDGET_MEASURE_FACTOR`` times
    ``max_bytes``. Until then the result is the bundle :func:`build_bundle`
    returns, byte for byte, with :func:`check_budget`'s report. When the read
    stops, there is no bundle and the report is partial: ``total_bytes`` and
    ``by_file`` are lower bounds that cover the paths read so far, and
    ``unmeasured_files`` lists the paths that were never read.
    """
    display, kept_raw, dropped = _inventory(repo, base, head, exclude)
    read = gitx.diff_text_by_path_bounded(
        repo, base, head, kept_raw, stop_after_bytes=max_bytes * BUDGET_MEASURE_FACTOR
    )
    if read.complete:
        bundle = _bundle(base, head, display, kept_raw, dropped, read.patches)
        return bundle, check_budget(bundle, max_bytes)
    sizes = [(display[p], len(read.patches[p].encode())) for p in kept_raw if p in read.patches]
    partial = None
    if read.partial is not None:
        partial, partial_bytes = read.partial
        sizes.append((display[partial], partial_bytes))
    unmeasured = [display[p] for p in kept_raw if p not in read.patches and p != partial]
    return None, BudgetReport(
        over_budget=True,
        total_bytes=sum(size for _, size in sizes),
        max_bytes=max_bytes,
        by_file=_largest_first(sizes),
        complete=False,
        unmeasured_files=unmeasured,
    )


@dataclass
class BudgetReport:
    """The verdict on whether a bundle fits the agent's review budget.

    Over budget is not a truncation; it is a refusal. `context` exits 2 with
    ``mode="diff_too_large"`` and hands back ``by_file`` so the user can add
    paths to ``[diff] exclude`` or raise ``[diff] max_bytes``. The property being
    protected is that the agent never reviews part of a diff while believing it
    saw all of it; a loud stop protects that far more cheaply than chunking.

    ``complete`` is false when the read stopped far over budget. ``total_bytes``
    and ``by_file`` are then lower bounds over the paths read, and
    ``unmeasured_files`` lists the changed paths that were not read at all.
    """

    over_budget: bool
    total_bytes: int
    max_bytes: int
    by_file: list[tuple[str, int]] = field(default_factory=list)
    complete: bool = True
    unmeasured_files: list[str] = field(default_factory=list)


def over_budget_message(report: BudgetReport) -> str:
    """The error message for a report over budget."""
    size = f"{report.total_bytes}" if report.complete else f"at least {report.total_bytes}"
    return f"the diff is {size} bytes, over the {report.max_bytes} byte budget"


def _largest_first(sizes: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return sorted(sizes, key=lambda item: (-item[1], item[0]))


def check_budget(bundle: DiffBundle, max_bytes: int) -> BudgetReport:
    """Compare a bundle against the byte budget, largest files reported first."""
    return BudgetReport(
        over_budget=bundle.total_bytes > max_bytes,
        total_bytes=bundle.total_bytes,
        max_bytes=max_bytes,
        by_file=_largest_first([(path, bundle.file_bytes(path)) for path in bundle.files]),
    )
