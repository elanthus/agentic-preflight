"""Differential test: ``diff.path_matches`` against Git's own matching.

Git is the oracle. Each example writes generated paths into a fresh index (every
entry points at the empty blob, so nothing touches the worktree) and asks
``git ls-files`` which of them a pattern selects. A pattern is checked against a
``:(glob)`` pathspec. A directory pattern (one that ends in ``/``) is checked against
``.gitignore`` matching instead, through ``git ls-files -ci --exclude``, because a pathspec
with a wildcard and a trailing ``/`` selects no file at all. Every disagreement must fall
into one of the intended differences below, and only when that difference alone explains
it; anything else fails with the pattern and path that disagree.

Intended differences (``path_matches`` is documented as gitignore-flavoured, not as
pathspec matching):

1. ``fnmatch`` wildcards cross ``/``. ``*.lock`` catches ``sub/dir/uv.lock``; the
   ``path_matches`` docstring calls this out for ``*``, and ``?`` and ``[!...]`` follow
   from the same ``fnmatch`` choice. Git's wildcards stop at ``/``.
2. A bare directory pattern such as ``a/b/`` matches that directory at any depth.
   ``.gitignore`` anchors a directory pattern that has a slash before its last one.
3. A wildcard-free pattern names exactly one path. A pathspec ``src`` also selects
   everything under a directory ``src``. ``risk._file_patterns`` relies on this by
   adding ``path/**`` explicitly when it wants a directory protected.

An inner ``**`` segment matches zero or more directories, as in Git: ``src/**/x.py``
matches ``src/x.py``. The generator places ``**`` anywhere in a pattern.

Known mismatches, kept out of the generator and pinned by strict ``xfail`` tests at
the end of this file so they show up when fixed:

- A leading ``**/`` on a directory pattern: ``**/a/`` misses ``a/x``, which
  ``.gitignore`` matches. The leading ``**/`` retry is skipped for directory patterns.
- ``[^...]``: Git negates the class; ``fnmatch`` treats ``^`` as a literal.

Not generated: a leading ``/`` (policy patterns reject it, and a pathspec ``/`` means
the filesystem root), backslashes (an escape for Git, a literal for ``fnmatch``),
and upper-case names (``core.ignorecase`` would make Git fold case on some machines).
"""

from __future__ import annotations

import fnmatch
import itertools
import os
import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_preflight.diff import path_matches
from tests.conftest import DETERMINISTIC_ENV

_EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
_WILDCARDS = frozenset("*?[")

_NAMES = ["a", "b", "src", "x.py", "y.lock", ".h", "ab"]
_TOKENS = [*_NAMES, "*", "?", "*.py", "s*", "?.py", ".*", "*b", "[ab]", "[!a]", "a[!x]b"]


@st.composite
def _paths(draw) -> list[str]:
    """Distinct paths with no file/directory conflict, which an index cannot hold."""
    candidates = draw(
        st.lists(
            st.lists(st.sampled_from(_NAMES), min_size=1, max_size=4).map("/".join),
            min_size=1,
            max_size=12,
            unique=True,
        )
    )
    directories = {
        "/".join(path.split("/")[:depth])
        for path in candidates
        for depth in range(1, path.count("/") + 1)
    }
    return sorted(path for path in candidates if path not in directories)


@st.composite
def _patterns(draw) -> str:
    segments = draw(st.lists(st.sampled_from([*_TOKENS, "**"]), min_size=1, max_size=3))
    if draw(st.booleans()):
        segments = ["**", *segments]
    leading_globstar = segments[0] == "**"
    # A leading ``**/`` directory pattern is a known mismatch; see the module docstring.
    trailing = draw(st.sampled_from(["", "/**"] if leading_globstar else ["", "/", "/**"]))
    return "/".join(segments) + trailing


def _git(repo: Path, *args: str, stdin: str | None = None, index: Path) -> str:
    env = {**os.environ, **DETERMINISTIC_ENV, "GIT_INDEX_FILE": str(index)}
    env.pop("GIT_DIR", None)
    result = subprocess.run(
        ["git", "-c", "core.ignorecase=false", "-c", "core.precomposeunicode=false", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def oracle_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("glob-oracle")
    env = {**os.environ, **DETERMINISTIC_ENV}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    return repo


def _git_matches(
    repo: Path, paths: list[str], patterns: list[str], index: Path
) -> dict[str, set[str]]:
    """The paths Git selects for each pattern: a pathspec, or ``.gitignore`` for a directory."""
    index.unlink(missing_ok=True)
    entries = "".join(f"100644 {_EMPTY_BLOB}\t{path}\n" for path in paths)
    _git(repo, "update-index", "--index-info", stdin=entries, index=index)
    selected = {}
    for pattern in patterns:
        if pattern.endswith("/"):
            args = ["ls-files", "-z", "--cached", "--ignored", f"--exclude={pattern}"]
        else:
            args = ["ls-files", "-z", "--", f":(glob){pattern}"]
        selected[pattern] = set(filter(None, _git(repo, *args, index=index).split("\0")))
    return selected


def _separator_aware(pattern: str) -> re.Pattern[str]:
    """``pattern`` read as ``fnmatch`` reads it, except that ``*``, ``?`` and a negated
    class stop at ``/``. ``**`` still crosses, as it does in Git.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = pattern.index("]", index + 2)
            body = pattern[index + 1 : close]
            out.append(f"[^{re.escape(body[1:])}/]" if body.startswith("!") else f"[{body}]")
            index = close + 1
            continue
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out), re.DOTALL)


def _inner_globstar_variants(pattern: str) -> list[str]:
    """``pattern`` with each subset of its inner ``**`` segments removed.

    An inner segment has a non-empty segment on each side, so ``a/**`` and ``a/**/``
    keep theirs: neither matches ``a`` itself in Git.
    """
    segments = pattern.split("/")
    inner = [
        index
        for index, segment in enumerate(segments)
        if segment == "**" and 0 < index < len(segments) - 1 and segments[index + 1]
    ]
    variants = []
    for dropped in itertools.product([False, True], repeat=len(inner)):
        gone = {index for index, drop in zip(inner, dropped, strict=True) if drop}
        variants.append("/".join(s for index, s in enumerate(segments) if index not in gone))
    return variants


def _documented_attempts(pattern: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ``path_matches`` contract, from its docstring: (root attempts, all attempts).

    An inner ``**`` segment may stand for no directory, so each is also tried removed.
    A bare directory ``d/`` means ``d/**`` at the root, or ``**/d/**`` at any depth.
    A leading ``**/`` is also tried without the prefix, so it can match at the root.
    """
    root: list[str] = []
    every: list[str] = []
    for variant in _inner_globstar_variants(pattern):
        if variant.endswith("/"):
            root.append(f"{variant}**")
            every += [f"{variant}**", f"**/{variant}**"]
        elif variant.startswith("**/"):
            root += [variant, variant[3:]]
            every += [variant, variant[3:]]
        else:
            root.append(variant)
            every.append(variant)
    return tuple(root), tuple(every)


def _intended(path: str, pattern: str, *, git: bool) -> bool:
    """Whether a disagreement is one of the intended differences in the module docstring.

    Each excuse holds only when that difference alone explains the disagreement.
    Otherwise the pattern is held to Git's answer.
    """
    if git:
        # 3. A pathspec selected the contents of a directory a wildcard-free pattern names.
        # Directory patterns use the .gitignore oracle, where this does not apply.
        return (
            not pattern.endswith("/")
            and not (_WILDCARDS & set(pattern))
            and path.startswith(pattern + "/")
        )
    root, every = _documented_attempts(pattern)
    at_root = any(_separator_aware(attempt).fullmatch(path) for attempt in root)
    at_any_depth = any(_separator_aware(attempt).fullmatch(path) for attempt in every)
    # 1. The documented fnmatch reading matches, and only because a single wildcard
    # crosses a separator.
    documented = any(fnmatch.fnmatchcase(path, attempt) for attempt in every)
    crosses = documented and not at_any_depth
    # 2. The match needs a bare directory below the root.
    deeper = pattern.endswith("/") and at_any_depth and not at_root
    return crosses or deeper


@settings(max_examples=120)
@given(paths=_paths(), patterns=st.lists(_patterns(), min_size=1, max_size=4, unique=True))
def test_path_matches_agrees_with_git(oracle_repo, paths, patterns):
    selected = _git_matches(oracle_repo, paths, patterns, oracle_repo / ".git" / "oracle-index")

    unexplained = [
        (pattern, path, "git" if in_git else "path_matches")
        for pattern in patterns
        for path in paths
        if (in_git := path in selected[pattern]) != path_matches(path, pattern)
        and not _intended(path, pattern, git=in_git)
    ]
    assert not unexplained, f"(pattern, path, only matched by): {unexplained}"


@settings(max_examples=120)
@given(paths=_paths(), patterns=st.lists(_patterns(), min_size=1, max_size=4, unique=True))
def test_top_level_files_match_exactly_as_git_does(oracle_repo, paths, patterns):
    """No intended difference applies to a path with no separator."""
    top_level = [path for path in paths if "/" not in path] or ["x.py"]
    selected = _git_matches(oracle_repo, top_level, patterns, oracle_repo / ".git" / "oracle-index")
    for pattern in patterns:
        for path in top_level:
            assert path_matches(path, pattern) == (path in selected[pattern]), (pattern, path)


def test_an_inner_globstar_matches_zero_directories():
    assert path_matches("src/secrets.py", "src/**/secrets.py")
    assert path_matches("src/a/b/secrets.py", "src/**/secrets.py")
    assert path_matches("a/b", "a/**/**/b")
    assert path_matches("src/b/x", "src/**/b/")
    assert not path_matches("srcsecrets.py", "src/**/secrets.py")
    # A trailing ** is not inner: Git's a/** and a/**/ never match a itself.
    assert not path_matches("a", "a/**")
    assert not path_matches("a/x", "a/**/")


# -- known mismatches -----------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="The leading **/ retry is skipped for a directory pattern, unlike .gitignore.",
)
def test_a_leading_globstar_directory_pattern_matches_at_the_root():
    assert path_matches("a/x", "**/a/")


@pytest.mark.xfail(
    strict=True,
    reason="fnmatch reads [^...] as a literal ^, where Git negates the class.",
)
def test_a_caret_class_is_negated():
    assert path_matches("b", "[^a]")
    assert not path_matches("a", "[^a]")
