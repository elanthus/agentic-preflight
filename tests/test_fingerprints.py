"""Fingerprint contract: review/docs reuse classification (issue #85).

Pure classification is exercised against hand-built fingerprints so every
reason code has a dedicated case. The git-backed tests then prove the two
scenarios the contract exists for: a history-only rebase (new commit SHA, same
trees) stays reusable, and an upstream content change invalidates review even
when the patch text is byte-identical.
"""

from __future__ import annotations

import pytest

from agentic_preflight import config
from agentic_preflight import diff as diffmod
from agentic_preflight import fingerprints as fp
from tests.conftest import commit_all, git, write


def _configure(repo, toml_body: str) -> str:
    write(repo, ".agentic-preflight.toml", toml_body)
    return commit_all(repo, "configure agentic-preflight")


def _review_fingerprint(repo, tmp_path, base: str, head: str) -> fp.ReviewFingerprint:
    bundle = diffmod.build_bundle(repo, base, head)
    manifest = diffmod.build_review_manifest(repo, bundle, grounding_sha256="a" * 64)
    snapshot = config.load_config(repo, user_config_dir=tmp_path / "nowhere").model_dump(
        mode="json"
    )
    return fp.compute_review_fingerprint(
        repo,
        base_sha=base,
        head_sha=head,
        manifest=manifest,
        executor="in_harness",
        intent="exercise the requested behavior safely",
        config_snapshot=snapshot,
    )


def _docs_fingerprint(repo, tmp_path, base: str, head: str) -> fp.DocsFingerprint:
    bundle = diffmod.build_bundle(repo, base, head)
    cfg = config.load_config(repo, user_config_dir=tmp_path / "nowhere")
    return fp.compute_docs_fingerprint(
        repo,
        base_sha=base,
        head_sha=head,
        changed_files=bundle.files,
        doc_paths=cfg.docs.paths,
        config_snapshot=cfg.model_dump(mode="json"),
    )


def _review_kwargs(**overrides) -> dict:
    base = {
        "base_tree_sha": "a" * 40,
        "head_tree_sha": "b" * 40,
        "diff_sha256": "c" * 64,
        "excluded_files": (),
        "grounding_sha256": "d" * 64,
        "intent_sha256": "e" * 64,
        "executor": "in_harness",
        "config_sha256": "f" * 64,
    }
    base.update(overrides)
    return base


def _docs_kwargs(**overrides) -> dict:
    base = {
        "base_tree_sha": "a" * 40,
        "head_tree_sha": "b" * 40,
        "doc_surface_sha256": "c" * 64,
        "config_sha256": "d" * 64,
    }
    base.update(overrides)
    return base


# -- pure classification: review ---------------------------------------------


def test_classify_review_reuses_when_every_input_matches():
    old = fp.ReviewFingerprint(**_review_kwargs())
    new = fp.ReviewFingerprint(**_review_kwargs())
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.REUSABLE
    assert result.reasons == ()


def test_classify_review_is_unknown_with_no_prior_fingerprint():
    new = fp.ReviewFingerprint(**_review_kwargs())
    result = fp.classify_review(None, new)
    assert result.disposition == fp.Disposition.UNKNOWN
    assert result.reasons == (fp.ReasonCode.FINGERPRINT_MISSING,)


def test_classify_review_is_unknown_on_a_fingerprint_version_mismatch():
    old = fp.ReviewFingerprint(**_review_kwargs(version=999))
    new = fp.ReviewFingerprint(**_review_kwargs())
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.UNKNOWN
    assert result.reasons == (fp.ReasonCode.FINGERPRINT_VERSION_MISMATCH,)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("base_tree_sha", "9" * 40, fp.ReasonCode.BASE_TREE_CHANGED),
        ("head_tree_sha", "9" * 40, fp.ReasonCode.HEAD_TREE_CHANGED),
        ("diff_sha256", "9" * 64, fp.ReasonCode.DIFF_CONTENT_CHANGED),
        ("excluded_files", ("newly-excluded.lock",), fp.ReasonCode.EXCLUSIONS_CHANGED),
        ("grounding_sha256", "9" * 64, fp.ReasonCode.GROUNDING_CHANGED),
        ("intent_sha256", "9" * 64, fp.ReasonCode.INTENT_CHANGED),
        ("executor", "command", fp.ReasonCode.EXECUTOR_CHANGED),
        ("config_sha256", "9" * 64, fp.ReasonCode.CONFIG_CHANGED),
    ],
)
def test_classify_review_invalidates_on_each_changed_input(field, value, reason):
    old = fp.ReviewFingerprint(**_review_kwargs())
    new = fp.ReviewFingerprint(**_review_kwargs(**{field: value}))
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.INVALID
    assert result.reasons == (reason,)


def test_classify_review_reports_every_changed_input_at_once():
    old = fp.ReviewFingerprint(**_review_kwargs())
    new = fp.ReviewFingerprint(
        **_review_kwargs(base_tree_sha="9" * 40, intent_sha256="9" * 64)
    )
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.INVALID
    assert set(result.reasons) == {
        fp.ReasonCode.BASE_TREE_CHANGED,
        fp.ReasonCode.INTENT_CHANGED,
    }


# -- pure classification: docs ------------------------------------------------


def test_classify_docs_reuses_when_every_input_matches():
    old = fp.DocsFingerprint(**_docs_kwargs())
    new = fp.DocsFingerprint(**_docs_kwargs())
    result = fp.classify_docs(old, new)
    assert result.disposition == fp.Disposition.REUSABLE
    assert result.reasons == ()


def test_classify_docs_is_unknown_with_no_prior_fingerprint():
    new = fp.DocsFingerprint(**_docs_kwargs())
    result = fp.classify_docs(None, new)
    assert result.disposition == fp.Disposition.UNKNOWN
    assert result.reasons == (fp.ReasonCode.FINGERPRINT_MISSING,)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("base_tree_sha", "9" * 40, fp.ReasonCode.BASE_TREE_CHANGED),
        ("head_tree_sha", "9" * 40, fp.ReasonCode.HEAD_TREE_CHANGED),
        ("doc_surface_sha256", "9" * 64, fp.ReasonCode.DOC_SURFACE_CHANGED),
        ("config_sha256", "9" * 64, fp.ReasonCode.CONFIG_CHANGED),
    ],
)
def test_classify_docs_invalidates_on_each_changed_input(field, value, reason):
    old = fp.DocsFingerprint(**_docs_kwargs())
    new = fp.DocsFingerprint(**_docs_kwargs(**{field: value}))
    result = fp.classify_docs(old, new)
    assert result.disposition == fp.Disposition.INVALID
    assert result.reasons == (reason,)


# -- configuration scoping ----------------------------------------------------


def test_review_relevant_config_excludes_unrelated_sections():
    snapshot = {
        "review": {"executor": "in_harness"},
        "commands": {"test": "pytest"},
        "docs": {"enabled": True},
    }
    scoped = fp.review_relevant_config(snapshot)
    assert "commands" not in scoped
    assert "docs" not in scoped
    assert scoped["review"] == {"executor": "in_harness"}


def test_docs_relevant_config_excludes_unrelated_sections():
    snapshot = {"docs": {"enabled": True}, "review": {"executor": "in_harness"}}
    scoped = fp.docs_relevant_config(snapshot)
    assert "review" not in scoped
    assert scoped["docs"] == {"enabled": True}


def test_a_test_command_change_does_not_move_the_review_config_fingerprint(
    feature_repo, tmp_path
):
    """Requirement 1: a `[commands] test` change must not discard review evidence."""
    _configure(feature_repo, "[commands]\nlint = 'true'\ntest = 'true'\n")
    base = git("rev-parse", "main", cwd=feature_repo)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    before = _review_fingerprint(feature_repo, tmp_path, base, head)

    write(feature_repo, ".agentic-preflight.toml", "[commands]\nlint = 'true'\ntest = 'echo changed'\n")
    new_head = commit_all(feature_repo, "change the test command only")

    after = _review_fingerprint(feature_repo, tmp_path, base, new_head)
    assert after.config_sha256 == before.config_sha256


# -- computed against real git trees -----------------------------------------


def test_a_history_only_rebase_leaves_the_review_fingerprint_reusable(feature_repo, tmp_path):
    _configure(feature_repo, "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n")
    base = git("rev-parse", "main", cwd=feature_repo)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    old = _review_fingerprint(feature_repo, tmp_path, base, head)

    # History-only rebase of main: same tree, new commit SHA. Mirrors the
    # "already contains the fresh base" scenario in test_rebase_tolerance.py.
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", base, "-m", "repeat the tree", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, base, cwd=feature_repo)

    new = _review_fingerprint(feature_repo, tmp_path, new_main, head)
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.REUSABLE


def test_a_history_only_rebase_leaves_the_docs_fingerprint_reusable(feature_repo, tmp_path):
    _configure(feature_repo, "[commands]\nlint = 'true'\ntest = 'true'\n")
    base = git("rev-parse", "main", cwd=feature_repo)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    old = _docs_fingerprint(feature_repo, tmp_path, base, head)

    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", base, "-m", "repeat the tree", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, base, cwd=feature_repo)

    new = _docs_fingerprint(feature_repo, tmp_path, new_main, head)
    result = fp.classify_docs(old, new)
    assert result.disposition == fp.Disposition.REUSABLE


def test_an_upstream_content_change_invalidates_review_even_with_the_same_patch(
    feature_repo, tmp_path
):
    _configure(feature_repo, "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n")
    base = git("rev-parse", "main", cwd=feature_repo)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    old = _review_fingerprint(feature_repo, tmp_path, base, head)

    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "unrelated.txt", "new upstream content\n")
    new_base = commit_all(feature_repo, "change an unrelated upstream file")
    git("switch", "feature/x", cwd=feature_repo)

    new = _review_fingerprint(feature_repo, tmp_path, new_base, head)
    assert new.diff_sha256 == old.diff_sha256, "the patch itself did not change"
    result = fp.classify_review(old, new)
    assert result.disposition == fp.Disposition.INVALID
    assert result.reasons == (fp.ReasonCode.BASE_TREE_CHANGED,)
