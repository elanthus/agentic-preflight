"""Review evidence must not depend on Git attributes.

A ``-diff`` attribute turns a text file's patch into "Binary files ... differ",
which hides its content from review, and ``diff=<driver>`` lets the driver's
configuration rewrite hunk headers. Git reads attributes from the checkout that
runs the command, not from the commits, so a producer and a verifier also
disagreed about the same commits. These tests commit or install the attributes
and check that the review snapshot does not move.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_preflight import attestation, diff, gitx, refresh_validation
from agentic_preflight.models import Stage
from tests.conftest import git, write
from tests.driver import ScriptedAgent, green_run

# Without a driver, Git's hunk header is the nearest earlier line that starts
# with a letter: LABEL. The python driver picks the def, and the custom pattern
# in the tests below picks value_3.
_SOURCE = (
    "def first():\n"
    "LABEL = 'module level'\n"
    + "".join(f"    value_{index} = {index}\n" for index in range(8))
    + "    return value_7\n"
)
_DEFAULT_HEADER = "@@ LABEL = 'module level'\n"
_BINARY = b"\x00\x01\x02 binary payload\n"
_MARKER = "Binary files a/logo.bin and b/logo.bin differ\n"


@pytest.fixture
def change(tmp_repo: Path) -> tuple[Path, str]:
    """A branch that changes a Python function, a text file, and a binary file.

    Returns the repository, checked out at the head, and the base commit.
    """
    write(tmp_repo, "src/calc.py", _SOURCE)
    write(tmp_repo, "notes.txt", "alpha\n")
    (tmp_repo / "logo.bin").write_bytes(_BINARY)
    git("add", "-A", cwd=tmp_repo)
    git("commit", "-m", "attribute base", cwd=tmp_repo)
    base = git("rev-parse", "HEAD", cwd=tmp_repo)

    git("switch", "-c", "feature/attributes", cwd=tmp_repo)
    write(tmp_repo, "src/calc.py", _SOURCE.replace("return value_7", "return value_6"))
    write(tmp_repo, "notes.txt", "alpha\nhidden instruction\n")
    (tmp_repo / "logo.bin").write_bytes(_BINARY + b"\x00more\n")
    git("add", "-A", cwd=tmp_repo)
    git("commit", "-m", "attribute change", cwd=tmp_repo)
    return tmp_repo, base


def _snapshot(repo: Path, base: str, head: str = "HEAD"):
    bundle = diff.build_bundle(repo, base, head)
    manifest = diff.build_review_manifest(repo, bundle)
    return bundle.files, bundle.per_file, manifest.manifest


def _commit_attributes(repo: Path, attributes: str) -> str:
    """Commit a ``.gitattributes`` file on the branch and return the new head."""
    write(repo, ".gitattributes", attributes)
    git("add", ".gitattributes", cwd=repo)
    git("commit", "-m", "attributes", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def _plain_git_diff(repo: Path, base: str, head: str, path: str) -> str:
    """The patch Git prints with the checkout's attributes, as 0.8.1 built it."""
    return subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", f"{base}...{head}", "--", path],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _snapshot_from_base_checkout(repo: Path, base: str, head: str):
    """Build the snapshot where a verifier does: a checkout without the branch's files."""
    git("switch", "--detach", base, cwd=repo)
    try:
        return _snapshot(repo, base, head)
    finally:
        git("switch", "-", cwd=repo)


@pytest.mark.parametrize("attributes", ["* -diff\n", "* binary\n", "*.txt -diff\n*.bin diff\n"])
def test_a_committed_attribute_neither_hides_nor_dumps_content(change, attributes):
    repo, base = change
    head = _commit_attributes(repo, attributes)
    assert "Binary files" in _plain_git_diff(repo, base, head, "notes.txt") or (
        "\x00" in _plain_git_diff(repo, base, head, "logo.bin")
    )

    files, per_file, manifest = _snapshot(repo, base, head)

    assert "+hidden instruction\n" in per_file["notes.txt"]
    assert per_file["logo.bin"].endswith(_MARKER)
    assert "\x00" not in per_file["logo.bin"]
    assert (files, per_file, manifest) == _snapshot_from_base_checkout(repo, base, head)


@pytest.mark.parametrize("attributes", ["* -diff\n", "* binary\n", "*.txt -diff\n*.bin diff\n"])
def test_the_budgeted_read_that_context_uses_ignores_attributes_too(change, attributes):
    """`context` and review build the bundle with a bounded read, not `build_bundle`."""
    repo, base = change
    head = _commit_attributes(repo, attributes)

    bundle, report = diff.build_bundle_within_budget(repo, base, head, max_bytes=1_000_000)

    assert report.complete is True
    assert bundle == diff.build_bundle(repo, base, head)
    assert bundle is not None
    assert "+hidden instruction\n" in bundle.per_file["notes.txt"]
    assert bundle.per_file["logo.bin"].endswith(_MARKER)


def test_a_read_that_stops_among_binary_files_still_checks_the_text_files(change, monkeypatch):
    """Text patches come first; stopping in the binary group must not skip their check."""
    repo, base = change
    paths = gitx.changed_files_raw(repo, base, "HEAD")
    full = gitx.diff_text_by_path(repo, base, "HEAD", paths)
    text_bytes = sum(len(full[path].encode()) for path in paths if path != "logo.bin")
    checked = []
    real = gitx._check_binary_markers

    def recording(path, *args, **kwargs):
        checked.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(gitx, "_check_binary_markers", recording)

    read = gitx.diff_text_by_path_bounded(repo, base, "HEAD", paths, stop_after_bytes=text_bytes)

    assert read.complete is False
    assert read.partial is not None
    assert read.partial[0] == "logo.bin"
    assert sorted(checked) == sorted(path for path in paths if path != "logo.bin")


def test_a_budgeted_read_that_stops_early_still_refuses_an_info_attribute(change):
    repo, base = change
    (Path(gitx.git_dir(repo)) / "info").mkdir(exist_ok=True)
    (Path(gitx.git_dir(repo)) / "info" / "attributes").write_text("*.py diff=python\n")

    with pytest.raises(gitx.ReviewDiffError):
        diff.build_bundle_within_budget(repo, base, "HEAD", max_bytes=1)


def test_an_info_attributes_file_does_not_hide_content(change):
    repo, base = change
    expected = _snapshot(repo, base)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* -diff\n", encoding="utf-8")
    assert "Binary files" in _plain_git_diff(repo, base, "HEAD", "notes.txt")

    assert _snapshot(repo, base) == expected
    assert "+hidden instruction\n" in expected[1]["notes.txt"]


def test_a_committed_diff_driver_changes_neither_text_nor_hunk_headers(change, tmp_path):
    repo, base = change
    textconv = tmp_path / "textconv.sh"
    textconv.write_text("#!/bin/sh\necho DRIVER_OUTPUT\n", encoding="utf-8")
    textconv.chmod(0o755)
    head = _commit_attributes(repo, "*.py diff=custom\n*.txt diff=custom\n")
    expected = _snapshot_from_base_checkout(repo, base, head)

    git("config", "--local", "diff.custom.textconv", str(textconv), cwd=repo)
    git("config", "--local", "diff.custom.xfuncname", "^ *(value_3 .*)$", cwd=repo)
    assert "@@ value_3 = 3" in _plain_git_diff(repo, base, head, "src/calc.py")

    files, per_file, manifest = _snapshot(repo, base, head)
    assert _DEFAULT_HEADER in per_file["src/calc.py"]
    assert "DRIVER_OUTPUT" not in per_file["src/calc.py"]
    assert (files, per_file, manifest) == expected


def test_a_built_in_driver_does_not_change_hunk_headers(change):
    """``diff=python`` is common in committed attribute files."""
    repo, base = change
    head = _commit_attributes(repo, "*.py diff=python\n")
    assert "@@ def first():" in _plain_git_diff(repo, base, head, "src/calc.py")
    expected = _snapshot_from_base_checkout(repo, base, head)

    assert _snapshot(repo, base, head) == expected


def test_attributes_from_attr_tree_or_attr_source_do_not_apply(change, monkeypatch):
    """``attr.tree`` (Git 2.42) and ``GIT_ATTR_SOURCE`` (Git 2.40) name a tree to read."""
    repo, base = change
    head = _commit_attributes(repo, "* -diff\n")
    git("switch", "--detach", base, cwd=repo)
    expected = _snapshot(repo, base, head)
    assert "+hidden instruction\n" in expected[1]["notes.txt"]

    git("config", "--local", "attr.tree", head, cwd=repo)
    assert _snapshot(repo, base, head) == expected
    git("config", "--local", "--unset", "attr.tree", cwd=repo)

    monkeypatch.setenv("GIT_ATTR_SOURCE", head)
    assert _snapshot(repo, base, head) == expected


def test_an_info_attributes_driver_without_a_header_pattern_is_ignored(change, tmp_path):
    """A driver there that only converts text is disabled like any textconv."""
    repo, base = change
    expected = _snapshot(repo, base)
    textconv = tmp_path / "textconv.sh"
    textconv.write_text("#!/bin/sh\necho DRIVER_OUTPUT\n", encoding="utf-8")
    textconv.chmod(0o755)
    git("config", "--local", "diff.convert.textconv", str(textconv), cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* diff=convert\n", encoding="utf-8")

    assert _snapshot(repo, base) == expected


@pytest.mark.parametrize(
    ("attributes", "config"),
    [
        ("*.py diff=custom\n", ("diff.custom.xfuncname", "^ *(value_3 .*)$")),
        ("*.py diff=custom\n", ("diff.custom.funcname", "^ *(value_3 .*)$")),
        ("*.py diff=python\n", None),
        ("*.bin diff\n", None),
        ("*.bin diff=printer\n", ("diff.printer.binary", "false")),
    ],
    ids=["xfuncname", "funcname", "built-in", "binary-as-text", "driver-prints-binary"],
)
def test_an_info_attributes_entry_that_would_change_the_patch_is_refused(
    change, attributes, config
):
    """``$GIT_DIR/info/attributes`` is always read and leaves no trace in the diff."""
    repo, base = change
    if config is not None:
        git("config", "--local", *config, cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(attributes, encoding="utf-8")

    with pytest.raises(gitx.ReviewDiffError, match=r"\$GIT_DIR/info/attributes"):
        diff.build_bundle(repo, base, "HEAD")


def _replace_with_binary(repo: Path, blob: str) -> None:
    """Point a clone-local replace ref from ``blob`` at content Git calls binary."""
    fake = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"\x00replacement\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git("replace", blob, fake, cwd=repo)


def test_a_replacement_object_cannot_make_a_text_file_look_binary(change):
    """With ``-diff`` in the info file, a binary verdict would hide the text."""
    repo, base = change
    _replace_with_binary(repo, git("rev-parse", "HEAD:notes.txt", cwd=repo))
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* -diff\n", encoding="utf-8")

    bundle = diff.build_bundle(repo, base, "HEAD")
    budgeted, _report = diff.build_bundle_within_budget(repo, base, "HEAD", max_bytes=1_000_000)

    assert "+hidden instruction\n" in bundle.per_file["notes.txt"]
    assert budgeted == bundle


def test_a_replacement_object_cannot_pass_the_earlier_format_guard(change):
    repo, base = change
    _replace_with_binary(repo, git("rev-parse", "HEAD:notes.txt", cwd=repo))
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* -diff\n", encoding="utf-8")

    with pytest.raises(gitx.ReviewDiffError, match=r"notes\.txt"):
        diff.build_bundle(repo, base, "HEAD", legacy=True)


def test_a_binary_file_is_reported_as_binary_and_not_dumped(change):
    repo, base = change
    patch = diff.build_bundle(repo, base, "HEAD").per_file["logo.bin"]

    assert patch.endswith(_MARKER)
    assert "\x00" not in patch
    assert "@@" not in patch
    assert patch == _plain_git_diff(repo, base, "HEAD", "logo.bin").replace(
        _index_line(repo, base, "logo.bin", abbreviated=True),
        _index_line(repo, base, "logo.bin", abbreviated=False),
    )


def _index_line(repo: Path, base: str, path: str, *, abbreviated: bool) -> str:
    options = [] if abbreviated else ["--full-index"]
    output = git("diff", *options, f"{base}...HEAD", "--", path, cwd=repo)
    return next(line for line in output.splitlines() if line.startswith("index "))


def test_a_type_change_keeps_a_binary_side_hidden_and_a_symlink_side_visible(change):
    """Git prints a type change as two blocks, and each side is judged on its own blob."""
    repo, base = change
    target = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"pointed/target",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git("update-index", "--cacheinfo", f"120000,{target},logo.bin", cwd=repo)
    git("commit", "-m", "binary becomes a symlink", cwd=repo)

    patch = diff.build_bundle(repo, base, "HEAD").per_file["logo.bin"]

    assert patch.count("diff --git ") == 2
    assert "Binary files a/logo.bin and /dev/null differ\n" in patch
    assert "+pointed/target\n" in patch


def test_the_earlier_format_refuses_an_attribute_that_hides_a_text_file(change):
    """0.8.1 text follows attributes, so a note relying on ``-diff`` must not verify."""
    repo, base = change
    _commit_attributes(repo, "*.txt -diff\n")

    with pytest.raises(gitx.ReviewDiffError, match=r"notes\.txt"):
        diff.build_bundle(repo, base, "HEAD", legacy=True)


def test_a_note_that_an_attribute_hid_content_from_does_not_verify(
    feature_repo, tmp_path, monkeypatch
):
    """A 0.8.1 note whose review saw "Binary files differ" for a text file is rejected."""
    _commit_attributes(feature_repo, "* -diff\n")

    def earlier_release(cwd, base, head, paths, **_):
        blocks = gitx._patches(cwd, base, head, paths)
        return {path: "".join(blocks[path]) for path in paths}

    with monkeypatch.context() as patched:
        patched.setattr(gitx, "diff_text_by_path", earlier_release)
        patched.setattr(
            gitx,
            "diff_text_by_path_bounded",
            lambda cwd, base, head, paths, **_: gitx.BoundedPatches(
                earlier_release(cwd, base, head, paths), complete=True
            ),
        )
        green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    note = attestation.read(feature_repo, sha)
    assert note is not None
    origin = note.evidence[Stage.REVIEW].origin

    with pytest.raises(gitx.ReviewDiffError):
        refresh_validation._manifest(
            feature_repo, origin, head=origin.head_sha, base=origin.base_sha
        )
    with pytest.raises(attestation.InvalidAttestation):
        attestation.verify(feature_repo, sha, purpose="publish")


def test_a_mismatched_digest_is_a_plain_mismatch_despite_a_hiding_attribute(feature_repo, tmp_path):
    """Only a note that the earlier text reproduces can have relied on hidden content."""
    _commit_attributes(feature_repo, "* -diff\n")
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


def test_context_reports_a_refused_info_attribute_as_a_typed_error(feature_repo):
    (feature_repo / ".git" / "info").mkdir(exist_ok=True)
    (feature_repo / ".git" / "info" / "attributes").write_text(
        "*.py diff=python\n", encoding="utf-8"
    )
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    refused = agent.run("context", expect=3)

    assert refused["error"]["code"] == "review_diff_attribute"
    assert "diff=python" in refused["error"]["message"]


@pytest.mark.parametrize("key", ["diff.default.xfuncname", "diff.default.funcname"])
def test_a_hunk_header_pattern_for_the_default_driver_is_refused(change, key):
    """Every path without a ``diff`` attribute uses the ``default`` driver."""
    repo, base = change
    git("config", "--local", key, "^ *(value_3 .*)$", cwd=repo)
    assert _DEFAULT_HEADER not in _plain_git_diff(repo, base, "HEAD", "src/calc.py")

    with pytest.raises(gitx.ReviewDiffError, match=r"diff\.default\.xfuncname"):
        diff.build_bundle(repo, base, "HEAD")
