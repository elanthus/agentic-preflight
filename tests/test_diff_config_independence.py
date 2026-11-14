"""Review evidence must not depend on the Git configuration of the machine that computes it.

The review digest is a hash of patch text, and a verifier accepts a note only if
it can rebuild that text from the commits. Git shapes patch text from ``core.*``
and ``diff.*`` configuration and from the user's attributes file, so a producer
and a verifier with different settings used to disagree about the same commits.
The rest of the suite cannot see this: it runs with ``GIT_CONFIG_GLOBAL`` and
``GIT_CONFIG_SYSTEM`` pointed at ``/dev/null``. These tests set the configuration
in the repository itself, where that isolation does not reach.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agentic_preflight import attestation, diff, gitx, refresh_validation
from agentic_preflight.models import Stage
from tests.conftest import configure_base, git, write
from tests.driver import PROTECTED_CONFIG, ScriptedAgent, findings_json, green_run

#: Settings that take a plain value. Each one changed the patch text or the file
#: inventory before the options were pinned, or selects a different diff algorithm.
AMBIENT_SETTINGS = [
    ("core.abbrev", "12"),
    ("core.quotePath", "false"),
    ("diff.noprefix", "true"),
    ("diff.mnemonicPrefix", "true"),
    ("diff.srcPrefix", "x/"),
    ("diff.dstPrefix", "y/"),
    ("diff.algorithm", "histogram"),
    ("diff.context", "5"),
    ("diff.interHunkContext", "4"),
    ("diff.indentHeuristic", "false"),
    ("diff.submodule", "log"),
    ("diff.ignoreSubmodules", "all"),
    ("diff.suppressBlankEmpty", "true"),
    ("diff.relative", "true"),
    ("core.bigFileThreshold", "10"),
    ("diff.default.binary", "true"),
]

#: Settings that leave the fixture's text alone when Git runs from the repository
#: root, as every caller does. ``diff.mnemonicPrefix`` changes only diffs against
#: the index or the work tree; ``diff.relative`` is tested from a subdirectory.
_NO_EFFECT_FROM_THE_ROOT = {"diff.mnemonicPrefix", "diff.relative"}

#: Myers and histogram split this change differently.
_REORDER_BEFORE = "a\nb\nc\na\nb\nc\nd\ne\nx\ny\nz\n"
_REORDER_AFTER = "c\na\nb\nd\na\nb\nc\ne\nx\nY\nz\n"
#: The indent heuristic moves the inserted block of this change.
_BRACES_BEFORE = "    y\n{\n}\nx\n    y\n    y\n}\n"
_BRACES_AFTER = "    y\n{\n}\n    return 1\n{\n}\nx\n    y\n    y\n}\n"

#: The command 0.8.1 ran for one path, before any option was pinned.
_EARLIER_DIFF = ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames"]

_FULL_INDEX_LINE = re.compile(r"^index [0-9a-f]{40}\.\.[0-9a-f]{40}(?: [0-7]{6})?$", re.MULTILINE)


def _gitlink(repo: Path, commit: str) -> None:
    """Record a submodule pointer without cloning a submodule.

    The empty directory is what an uninitialized submodule looks like, so the
    checkout stays clean and ``git add -A`` does not stage the pointer's removal.
    """
    (repo / "vendor" / "sub").mkdir(parents=True, exist_ok=True)
    git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/sub", cwd=repo)


@pytest.fixture
def varied_repo(tmp_repo: Path) -> tuple[Path, str]:
    """A branch whose diff has every patch shape the settings above can alter.

    Two hunks in one file, blank context lines, a non-ASCII path, a binary file,
    a submodule pointer, and changes that the diff algorithm and the indent
    heuristic each render differently. Returns the repository and the base commit.
    """
    write(tmp_repo, "docs/reorder.txt", _REORDER_BEFORE)
    write(tmp_repo, "src/braces.c", _BRACES_BEFORE)
    write(tmp_repo, "src/calc.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n" * 3)
    write(tmp_repo, "docs/guide.md", "one\n\ntwo\n\nthree\nfour\nfive\nsix\nseven\neight\nnine\n")
    write(tmp_repo, "café.txt", "before\n")
    (tmp_repo / "logo.bin").write_bytes(b"\x00\x01\x02")
    git("add", "-A", cwd=tmp_repo)
    _gitlink(tmp_repo, "1" * 40)
    git("commit", "-m", "varied base", cwd=tmp_repo)
    base = git("rev-parse", "HEAD", cwd=tmp_repo)

    git("switch", "-c", "feature/varied", cwd=tmp_repo)
    write(tmp_repo, "src/calc.py", "def a():\n    return 10\n\n\ndef b():\n    return 2\n" * 3)
    write(tmp_repo, "docs/guide.md", "one\n\ntwo!\n\nthree\nfour\nfive\nsix\nseven\neight\nnine!\n")
    write(tmp_repo, "café.txt", "after\n")
    write(tmp_repo, "docs/reorder.txt", _REORDER_AFTER)
    write(tmp_repo, "src/braces.c", _BRACES_AFTER)
    (tmp_repo / "logo.bin").write_bytes(b"\x00\x01\x03")
    git("add", "-A", cwd=tmp_repo)
    _gitlink(tmp_repo, "2" * 40)
    git("commit", "-m", "varied change", cwd=tmp_repo)
    return tmp_repo, base


def _snapshot(repo: Path, base: str) -> tuple[list[str], dict[str, str], str]:
    bundle = diff.build_bundle(repo, base, "HEAD")
    manifest = diff.build_review_manifest(repo, bundle)
    return bundle.files, bundle.per_file, manifest.manifest


@pytest.mark.parametrize(("key", "value"), AMBIENT_SETTINGS, ids=[k for k, _ in AMBIENT_SETTINGS])
def test_the_review_snapshot_ignores_a_git_setting(varied_repo, key, value):
    repo, base = varied_repo
    expected = _snapshot(repo, base)

    git("config", "--local", key, value, cwd=repo)

    assert _snapshot(repo, base) == expected


def test_the_review_snapshot_ignores_an_order_file(varied_repo, tmp_path):
    repo, base = varied_repo
    expected = _snapshot(repo, base)
    order = tmp_path / "order.txt"
    order.write_text("vendor/*\nsrc/*\n", encoding="utf-8")

    git("config", "--local", "diff.orderFile", str(order), cwd=repo)

    assert _snapshot(repo, base) == expected


def test_the_review_snapshot_ignores_the_users_attributes_file(varied_repo, tmp_path):
    """A user-level ``diff=python`` rewrites hunk headers, and ``-diff`` hides content."""
    repo, base = varied_repo
    expected = _snapshot(repo, base)
    attributes = tmp_path / "attributes"
    attributes.write_text("*.py diff=python\n*.md -diff\n", encoding="utf-8")

    git("config", "--local", "core.attributesFile", str(attributes), cwd=repo)

    assert _snapshot(repo, base) == expected


def test_review_patches_name_full_object_ids(varied_repo):
    """An abbreviated ID grows with the clone's object count, so it is not reproducible."""
    repo, base = varied_repo
    bundle = diff.build_bundle(repo, base, "HEAD")

    assert bundle.files
    for path in bundle.files:
        assert _FULL_INDEX_LINE.search(bundle.per_file[path]), path


def test_a_submodule_change_stays_in_the_inventory_when_git_is_told_to_ignore_it(varied_repo):
    repo, base = varied_repo
    git("config", "--local", "diff.ignoreSubmodules", "all", cwd=repo)

    assert "vendor/sub" in gitx.changed_files(repo, base, "HEAD")
    assert "vendor/sub" in gitx.classified_files(repo, base, "HEAD")
    assert "vendor/sub" in gitx.non_merge_commit_files(repo, base, "HEAD")


def test_the_inventory_lists_every_path_from_a_subdirectory_under_diff_relative(varied_repo):
    """``diff.relative`` limits a diff to the directory it runs in and shortens its paths."""
    repo, base = varied_repo
    expected = sorted(gitx.changed_files(repo, base, "HEAD"))
    git("config", "--local", "diff.relative", "true", cwd=repo)
    subdirectory = repo / "src"

    assert sorted(gitx.changed_files(subdirectory, base, "HEAD")) == expected
    assert gitx.classified_files(subdirectory, base, "HEAD") == expected
    assert gitx.non_merge_commit_files(subdirectory, base, "HEAD") == expected


def test_the_abbreviated_format_reproduces_the_patches_of_earlier_releases(varied_repo):
    """Notes written before full object IDs hash this text, so it must stay available."""
    repo, base = varied_repo
    legacy = diff.build_bundle(repo, base, "HEAD", full_index=False)

    assert legacy.files == diff.build_bundle(repo, base, "HEAD").files
    for path in legacy.files:
        expected = subprocess.run(
            [*_EARLIER_DIFF, f"{base}...HEAD", "--", f":(literal){path}"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
        assert legacy.per_file[path].encode() == expected, path


def test_a_note_verifies_under_a_different_git_configuration(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    assert attestation.verify(feature_repo, sha).sha == sha

    for key, value in (("diff.noprefix", "true"), ("core.abbrev", "12"), ("diff.context", "9")):
        git("config", "--local", key, value, cwd=feature_repo)
        assert attestation.verify(feature_repo, sha, purpose="publish").sha == sha


def test_a_note_written_with_abbreviated_object_ids_still_verifies(
    feature_repo, tmp_path, monkeypatch
):
    """A release reads the notes of the previous minor release."""
    current = gitx.diff_text_by_path
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(
            gitx,
            "diff_text_by_path",
            lambda cwd, base, head, paths, **_: current(cwd, base, head, paths, full_index=False),
        )
        green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    note = attestation.read(feature_repo, sha)
    assert note is not None
    base = note.merge_base_sha
    canonical = diff.build_review_manifest(feature_repo, diff.build_bundle(feature_repo, base, sha))
    recorded = note.evidence[Stage.REVIEW].fingerprint
    assert recorded.diff_sha256 != canonical.diff_sha256  # the fallback is what is under test

    assert attestation.verify(feature_repo, sha, purpose="publish").sha == sha


def test_a_run_reviewed_by_an_earlier_release_is_reviewed_again_after_an_upgrade(
    feature_repo, tmp_path, monkeypatch
):
    """A review recorded against the abbreviated text does not carry into this release.

    The run is not stranded: the next command reopens review, and the note that
    results binds the full-ID text.
    """
    configure_base(feature_repo, PROTECTED_CONFIG)
    agent = ScriptedAgent(feature_repo)
    current = gitx.diff_text_by_path
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(
            gitx,
            "diff_text_by_path",
            lambda cwd, base, head, paths, **_: current(cwd, base, head, paths, full_index=False),
        )
        agent.run("start")
        agent.run("context")
        agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    reopened = agent.run("stage", "run", "lint")
    assert reopened["state"] == "REVIEW_AWAITING_FINDINGS"

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    note = attestation.verify(feature_repo, sha, purpose="publish")
    canonical = diff.build_review_manifest(
        feature_repo, diff.build_bundle(feature_repo, note.merge_base_sha, sha)
    )
    assert note.evidence[Stage.REVIEW].fingerprint.diff_sha256 == canonical.diff_sha256


def test_a_digest_that_matches_neither_format_is_rejected(feature_repo, tmp_path):
    green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    note = attestation.read(feature_repo, sha)
    assert note is not None
    origin = note.evidence[Stage.REVIEW].origin
    forged = origin.model_copy(
        update={"fingerprint": origin.fingerprint.model_copy(update={"diff_sha256": "0" * 64})}
    )

    with pytest.raises(ValueError, match="review fingerprint does not match the Git diff"):
        refresh_validation._manifest(
            feature_repo, forged, head=origin.head_sha, base=origin.base_sha
        )


def test_the_varied_fixture_covers_every_patch_shape(varied_repo):
    """Guard the fixture: a path missing here would silently weaken every test above."""
    repo, base = varied_repo

    assert gitx.is_clean(repo)
    assert sorted(gitx.changed_files(repo, base, "HEAD")) == [
        "café.txt",
        "docs/guide.md",
        "docs/reorder.txt",
        "logo.bin",
        "src/braces.c",
        "src/calc.py",
        "vendor/sub",
    ]


def _plain_git_diff(repo: Path, base: str, *config: str) -> bytes:
    return subprocess.run(
        ["git", *config, *_EARLIER_DIFF[1:], f"{base}...HEAD"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize(
    ("key", "value"),
    [item for item in AMBIENT_SETTINGS if item[0] not in _NO_EFFECT_FROM_THE_ROOT],
    ids=[k for k, _ in AMBIENT_SETTINGS if k not in _NO_EFFECT_FROM_THE_ROOT],
)
def test_each_setting_changes_an_unpinned_diff_of_the_fixture(varied_repo, key, value):
    """Guard the fixture: a setting it cannot show would pass the test above vacuously."""
    repo, base = varied_repo

    assert _plain_git_diff(repo, base, "-c", f"{key}={value}") != _plain_git_diff(repo, base)


def test_the_review_snapshot_ignores_git_diff_opts(varied_repo, monkeypatch):
    """``GIT_DIFF_OPTS`` sets the context length after the command line is read."""
    repo, base = varied_repo
    expected = _snapshot(repo, base)

    monkeypatch.setenv("GIT_DIFF_OPTS", "-u7")

    assert _snapshot(repo, base) == expected


def test_the_review_snapshot_ignores_replacement_objects(varied_repo):
    """A replacement exists in one clone only and shows content the commit lacks."""
    repo, base = varied_repo
    expected = _snapshot(repo, base)
    # The base side: Git may read the head side from the checkout instead.
    blob = git("rev-parse", f"{base}:src/calc.py", cwd=repo)
    fake = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"def a():\n    return 'replaced'\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git("replace", blob, fake, cwd=repo)
    assert b"replaced" in _plain_git_diff(repo, base)

    assert _snapshot(repo, base) == expected


def test_a_committed_gitmodules_entry_cannot_hide_a_submodule_change(varied_repo):
    """``submodule.<name>.ignore`` in a committed ``.gitmodules`` used to drop the path."""
    repo, base = varied_repo
    write(
        repo,
        ".gitmodules",
        '[submodule "sub"]\n\tpath = vendor/sub\n\turl = ./sub\n\tignore = all\n',
    )
    git("add", ".gitmodules", cwd=repo)
    git("commit", "-m", "ignore the submodule", cwd=repo)
    assert "vendor/sub" not in git("diff", "--name-only", f"{base}...HEAD", cwd=repo)

    assert "vendor/sub" in gitx.changed_files(repo, base, "HEAD")
    assert "vendor/sub" in diff.build_bundle(repo, base, "HEAD").files


def test_a_decomposed_path_builds_under_precompose_unicode(tmp_repo):
    """macOS sets ``core.precomposeUnicode`` at ``git init``; it rewrote the pathspec."""
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    # Off while the tree is written, or Git would store the precomposed name.
    git("config", "--local", "core.precomposeUnicode", "false", cwd=tmp_repo)
    name = "cafe\u0301.txt"  # "e" followed by a combining accent
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_repo,
            input=b"decomposed\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", cwd=tmp_repo)
    git("commit", "-m", "decomposed name", cwd=tmp_repo)
    expected = _snapshot(tmp_repo, base)
    assert expected[0] == [name]

    git("config", "--local", "core.precomposeUnicode", "true", cwd=tmp_repo)

    assert _snapshot(tmp_repo, base) == expected


def test_the_abbreviated_format_ignores_the_verifiers_abbreviation_setting(varied_repo):
    """0.8.1 notes were written with Git's default length; the verifier's own does not apply."""
    repo, base = varied_repo
    expected = diff.build_bundle(repo, base, "HEAD", full_index=False).per_file

    git("config", "--local", "core.abbrev", "12", cwd=repo)

    assert diff.build_bundle(repo, base, "HEAD", full_index=False).per_file == expected
