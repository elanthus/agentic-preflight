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
]

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
    and a submodule pointer. Returns the repository and the base commit.
    """
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
        "logo.bin",
        "src/calc.py",
        "vendor/sub",
    ]
