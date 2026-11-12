"""Deterministic risk classification over paths and recorded findings.

Diff size is deliberately absent. ``[diff] max_bytes`` is an attention budget:
it decides whether the complete diff fits in review context. Risk instead comes
from repository-owned path policy and the findings the review recorded.

The reviewer is part of that policy. ``[review] command`` and
``[review.contracts] planner_command`` usually run a script committed in the
repository, and they run it from the worktree that holds the change under review.
A branch that edits that script would choose its own reviewer while the command
string stayed the same. So every path that the configured commands name, and every
``[review] trusted_paths`` pattern, counts as a human-review path.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import PureWindowsPath
from typing import Literal

from . import findings as findingsmod
from .config import PolicySection, ReviewSection
from .diff import path_matches
from .models import (
    Finding,
    RiskAssessment,
    RiskLevel,
    RiskReason,
    Severity,
    Stage,
    Verdict,
)
from .stages import command as commandmod

RiskReasonKind = Literal["human_review_path", "high_risk_path", "medium_risk_path", "finding"]

_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_FINDING_RISK = {
    Severity.LOW: RiskLevel.LOW,
    Severity.MEDIUM: RiskLevel.MEDIUM,
    Severity.HIGH: RiskLevel.HIGH,
    Severity.CRITICAL: RiskLevel.HIGH,
}


# Shell control operators and redirections can join words without whitespace, as in
# ``cd tools&&python3 review.py``. Splitting on them keeps the paths on each side.
_SHELL_OPERATORS = re.compile(r"[;&|()<>]+")
# A word with an expansion or a glob does not name one fixed path.
_UNRESOLVED = frozenset("$`~*?[]{}")
# Builtins that change the directory later words are resolved against.
_CHANGE_DIRECTORY = frozenset({"cd", "pushd"})
# ``sh -c 'cd tools && python3 review.py'`` nests a command inside one quoted word.
_MAX_NESTING = 3


def _unique(patterns: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(patterns))


def _runs_a_command_string(flag: str) -> bool:
    """``-c`` and combined short flags such as ``-lc`` or ``-ec`` (``sh``, ``bash``)."""
    return flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]


def _shell_pieces(command: str, depth: int = 0) -> Iterator[str]:
    """Shell words of ``command``, split again on control operators.

    The word after a ``-c`` flag, as in ``sh -c 'python3 tools/review.py'``, is read
    again as a nested command. Any other quoted word stays whole, so a path that
    contains a space is kept.
    """
    words = commandmod.split(command)
    if words is None:
        words = command.split()
    previous = ""
    for word in words:
        if depth < _MAX_NESTING and _runs_a_command_string(previous):
            yield from _shell_pieces(word, depth + 1)
        else:
            yield from (piece for piece in _SHELL_OPERATORS.split(word) if piece)
        previous = word


def _word_value(word: str) -> str | None:
    """The part of one shell word that could be a path."""
    if word.startswith("-"):
        # ``--script=tools/review.py`` names a path; a bare flag does not.
        _, separator, value = word.partition("=")
        return value if separator else None
    name, separator, value = word.partition("=")
    if separator and name.isidentifier():
        # ``VAR=tools/config.json`` before a program, or as an ``env`` argument.
        return value
    return word


def _repo_relative(directory: str, value: str) -> str | None:
    """``value`` resolved against repository ``directory``, if it stays inside."""
    value = value.replace("\\", "/")
    if not value or any(char in _UNRESOLVED for char in value):
        return None
    if value.startswith("/") or PureWindowsPath(value).drive:
        return None
    normalized = posixpath.normpath(posixpath.join(directory, value))
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def _looks_like_path(value: str) -> bool:
    """A word with a directory separator or a dot, which is how a script is named.

    Bare words such as ``python``, ``uv``, or ``run`` are program names or
    subcommands, which the shell finds on ``PATH`` rather than in the worktree.
    """
    return "/" in value or "\\" in value or "." in value


def command_path_patterns(command: str) -> list[str]:
    """Human-review patterns for the repository paths that ``command`` names.

    This is a best-effort reading of shell words, not a shell. A named file is
    protected together with its directory, because interpreters load sibling modules
    from the script's directory (the example wrappers import ``_reviewer_common``).
    A path at the repository root is protected as a file and as a directory. After
    ``cd DIR`` or ``pushd DIR``, a relative path is resolved against both the
    repository root and ``DIR``, so either reading is protected. Paths that the
    command computes, imports by module name, or reads from outside the repository
    are not detected; list those in ``[review] trusted_paths``.
    """
    directories = [""]
    patterns: list[str] = []
    changing_directory = False
    for piece in _shell_pieces(command):
        if changing_directory:
            changing_directory = False
            targets = (_repo_relative(directory, piece) for directory in directories)
            directories = _unique([*directories, *(target for target in targets if target)])
            continue
        if piece in _CHANGE_DIRECTORY:
            changing_directory = True
            continue
        value = _word_value(piece)
        if value is None or not _looks_like_path(value):
            continue
        for directory in directories:
            path = _repo_relative(directory, value)
            if path is None:
                continue
            parent = posixpath.dirname(path)
            if parent:
                patterns.append(f"{parent}/**")
            else:
                patterns.extend((path, f"{path}/**"))
    return _unique(patterns)


def reviewer_patterns(review: ReviewSection) -> list[str]:
    """Patterns whose change could alter the configured reviewer or planner."""
    patterns = list(review.trusted_paths)
    for command in (review.command, review.contracts.planner_command):
        if command:
            patterns.extend(command_path_patterns(command))
    return _unique(patterns)


def _path_reasons(paths: list[str], patterns: list[str], *, kind: RiskReasonKind, level: RiskLevel):
    for path in sorted(set(paths)):
        for pattern in patterns:
            if path_matches(path, pattern):
                yield RiskReason(kind=kind, level=level, path=path, pattern=pattern)


def assess(
    changed_files: list[str],
    findings: list[Finding],
    *,
    policy: PolicySection,
    review: ReviewSection,
    docs_blocking_severities: Sequence[str],
) -> RiskAssessment:
    """Return the explainable policy verdict for one exact reviewed change.

    ``review`` supplies the review blocking severities and the reviewer paths that
    count as human-review paths. Pass the same configuration that ``policy`` comes
    from, so a verifier judges a branch by the reviewer its protected base names.
    """
    reasons = [
        *_path_reasons(
            changed_files,
            _unique([*policy.human_review_paths, *reviewer_patterns(review)]),
            kind="human_review_path",
            level=RiskLevel.HIGH,
        ),
        *_path_reasons(
            changed_files,
            policy.high_risk_paths,
            kind="high_risk_path",
            level=RiskLevel.HIGH,
        ),
        *_path_reasons(
            changed_files,
            policy.medium_risk_paths,
            kind="medium_risk_path",
            level=RiskLevel.MEDIUM,
        ),
    ]

    for finding in sorted(findings, key=lambda item: item.id):
        level = _FINDING_RISK[finding.severity]
        if level is RiskLevel.LOW:
            continue
        reasons.append(
            RiskReason(
                kind="finding",
                level=level,
                path=finding.path,
                finding_id=finding.id,
                severity=finding.severity,
            )
        )

    configured_blocking = {
        Stage.REVIEW: review.blocking_severities,
        Stage.DOCS: docs_blocking_severities,
    }
    has_open_blocker = any(
        findingsmod.blocking(
            [finding for finding in findings if finding.stage is stage],
            blocking_severities=blocking_severities,
        )
        for stage, blocking_severities in configured_blocking.items()
    )

    level = max(
        (reason.level for reason in reasons),
        key=lambda candidate: _RANK[candidate],
        default=RiskLevel.LOW,
    )
    requires_human = level is RiskLevel.HIGH
    verdict = (
        Verdict.CHANGES_REQUIRED
        if has_open_blocker
        else Verdict.NEEDS_HUMAN
        if requires_human
        else Verdict.CLEAR
    )
    return RiskAssessment(
        level=level,
        verdict=verdict,
        requires_human_review=requires_human,
        reasons=reasons,
    )


def include_attested_findings(
    assessment: RiskAssessment, findings_summary: dict[str, int]
) -> RiskAssessment:
    """Preserve finding-derived risk when importing portable green evidence."""
    severity = next(
        (
            severity
            for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
            if findings_summary.get(severity.value, 0) > 0
        ),
        None,
    )
    if severity is None:
        return assessment
    imported_level = _FINDING_RISK[severity]
    if _RANK[assessment.level] >= _RANK[imported_level]:
        return assessment
    requires_human = imported_level is RiskLevel.HIGH
    return RiskAssessment(
        level=imported_level,
        verdict=Verdict.NEEDS_HUMAN if requires_human else Verdict.CLEAR,
        requires_human_review=requires_human,
        reasons=[
            *assessment.reasons,
            RiskReason(kind="finding", level=imported_level, severity=severity),
        ],
    )
