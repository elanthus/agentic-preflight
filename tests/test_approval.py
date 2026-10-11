import json
import subprocess

import pytest

from agentic_preflight import attestation, gitx
from agentic_preflight.approval import current_human_approvers, evaluate
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import AttestedStage, ReviewCoverage, Stage
from tests.attestation_helpers import make_attestation
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _review(
    review_id,
    *,
    login="reviewer",
    user_type="User",
    author_association="MEMBER",
    state="APPROVED",
    commit_id="a" * 40,
    body="",
):
    return {
        "body": body,
        "id": review_id,
        "state": state,
        "commit_id": commit_id,
        "author_association": author_association,
        "user": {"login": login, "type": user_type},
    }


def _stages():
    return {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor="in_harness",
            coverage=ReviewCoverage(
                manifest="d" * 64,
                head_sha="e" * 40,
                total_units=1,
                clean_units=["U0001"],
            ),
        ),
        Stage.DOCS: AttestedStage(status="green"),
        Stage.TEST: AttestedStage(
            status="green", command="pytest", exit_code=0, output_sha256="a" * 64
        ),
        Stage.LINT: AttestedStage(
            status="green", command="ruff", exit_code=0, output_sha256="b" * 64
        ),
    }


def _stub_attestation(monkeypatch, *, head, branch, findings_summary):
    value = make_attestation(
        stages=_stages(), sha=head, branch=branch, findings_summary=findings_summary
    )
    monkeypatch.setattr(attestation, "verify", lambda *_args, **_kwargs: value)


def test_only_a_current_repository_associated_non_author_approval_counts():
    head = "a" * 40
    reviews = [
        _review(1, login="author", commit_id=head),
        _review(2, login="automation", user_type="Bot", commit_id=head),
        _review(3, login="stale", commit_id="b" * 40),
        _review(4, login="reviewer", commit_id=head),
        _review(5, login="outsider", author_association="NONE", commit_id=head),
    ]
    assert current_human_approvers(reviews, head_sha=head, pull_request_author="author") == [
        "reviewer"
    ]


def test_a_later_changes_request_revokes_the_same_reviewers_approval():
    head = "a" * 40
    reviews = [
        _review(1, commit_id=head),
        _review(2, state="CHANGES_REQUESTED", commit_id=head),
    ]
    assert current_human_approvers(reviews, head_sha=head, pull_request_author="author") == []


def test_high_risk_attested_change_requires_exact_head_human_approval(
    tmp_repo, tmp_path, monkeypatch
):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n\n[approval]\nmode = 'peer_review'\n",
    )
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/risky", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    head = commit_all(tmp_repo, "change application")
    _stub_attestation(monkeypatch, head=head, branch="feature/risky", findings_summary={"open": 0})

    missing = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert missing["requires_human_approval"] is True
    assert missing["approved"] is False

    approved = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[_review(1, commit_id=head)],
        pull_request_author="author",
    )
    assert approved["approved"] is True
    assert approved["human_approvers"] == ["reviewer"]

    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text(json.dumps([]))
    env = ScriptedAgent(tmp_repo).run(
        "approval-check",
        head,
        "--base",
        base,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        expect=ExitCode.NEEDS_HUMAN,
    )
    assert env["error"]["code"] == "needs_human"
    assert env["data"]["head_sha"] == head


def test_high_severity_attestation_summary_also_requires_approval(tmp_repo, monkeypatch):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nmedium_risk_paths = ['src/**']\n\n[approval]\nmode = 'peer_review'\n",
    )
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/finding", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    head = commit_all(tmp_repo, "change application")
    _stub_attestation(
        monkeypatch,
        head=head,
        branch="feature/finding",
        findings_summary={"fixed": 1, "high": 1},
    )
    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert result["risk_level"] == "high"
    assert result["severe_findings"] == 1
    assert result["approved"] is False


def test_manual_merge_is_the_default_and_passes_without_a_peer(tmp_repo, tmp_path, monkeypatch):
    write(tmp_repo, ".agentic-preflight.toml", "[policy]\nhigh_risk_paths = ['src/**']\n")
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/manual-merge", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet():\n    return 'hello'\n")
    head = commit_all(tmp_repo, "change application")
    _stub_attestation(
        monkeypatch,
        head=head,
        branch="feature/manual-merge",
        findings_summary={"open": 0},
    )

    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert result["approval_mode"] == "manual_merge"
    assert result["manual_merge_required"] is True
    assert result["approved"] is True

    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text("[]")
    envelope = ScriptedAgent(tmp_repo).run(
        "approval-check",
        head,
        "--base",
        base,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
    )
    assert envelope["data"]["approved"] is True
    assert envelope["data"]["manual_merge_required"] is True


def test_environment_mode_requires_the_environment_release(tmp_repo, tmp_path, monkeypatch):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n"
        "\n[approval]\nmode = 'environment'\nenvironment = 'high-risk-review'\n",
    )
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/environment", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet():\n    return 'hello'\n")
    head = commit_all(tmp_repo, "change application")
    _stub_attestation(
        monkeypatch,
        head=head,
        branch="feature/environment",
        findings_summary={"open": 0},
    )

    pending = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    released = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
        environment_approved=True,
    )
    assert pending["approved"] is False
    assert pending["approval_environment"] == "high-risk-review"
    assert released["approved"] is True
    assert released["environment_approved"] is True

    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text("[]")
    agent = ScriptedAgent(tmp_repo)
    blocked = agent.run(
        "approval-check",
        head,
        "--base",
        base,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        expect=ExitCode.NEEDS_HUMAN,
    )
    assert blocked["data"]["approval_mode"] == "environment"
    reported = agent.run(
        "approval-check",
        head,
        "--base",
        base,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        "--report-only",
    )
    assert reported["data"]["approved"] is False
    accepted = agent.run(
        "approval-check",
        head,
        "--base",
        base,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        "--environment-approved",
    )
    assert accepted["data"]["approved"] is True


def test_approval_policy_comes_from_the_base_commit_not_the_pr_head(tmp_repo, monkeypatch):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n\n[approval]\nmode = 'peer_review'\n",
    )
    base = commit_all(tmp_repo, "require peer review")
    git("switch", "-c", "feature/weaken", cwd=tmp_repo)
    write(tmp_repo, ".agentic-preflight.toml", "[policy]\nhigh_risk_paths = ['src/**']\n")
    write(tmp_repo, "src/app.py", "def greet():\n    return 'hello'\n")
    head = commit_all(tmp_repo, "relax approval and change application")
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = []\n\n[approval]\nmode = 'manual_merge'\n",
    )
    _stub_attestation(monkeypatch, head=head, branch="feature/weaken", findings_summary={"open": 0})

    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert result["approval_mode"] == "peer_review"
    assert result["approved"] is False


def test_approval_check_reports_a_malformed_reviews_file_as_unreadable_input(tmp_repo, tmp_path):
    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text("{not json")
    env = ScriptedAgent(tmp_repo).run(
        "approval-check",
        "a" * 40,
        "--base",
        "b" * 40,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "invalid_findings"
    assert "reviews file is not valid JSON" in env["error"]["message"]


def test_approval_check_reports_a_non_utf8_reviews_file_as_unreadable_input(tmp_repo, tmp_path):
    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_bytes(b"\xff\xfe[]")
    env = ScriptedAgent(tmp_repo).run(
        "approval-check",
        "a" * 40,
        "--base",
        "b" * 40,
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "invalid_findings"


@pytest.mark.parametrize(
    ("author", "state", "body", "accepted"),
    [
        ("owner", "COMMENTED", " approved ", True),
        ("owner", "COMMENTED", "Approved", False),
        ("owner", "COMMENTED", "approved with conditions", False),
        ("other", "APPROVED", "", True),
        ("other", "COMMENTED", "approved", False),
    ],
)
def test_owner_review_accepts_only_explicit_current_head_review(author, state, body, accepted):
    reviews = [_review(1, login="owner", state=state, body=body)]
    assert (
        bool(
            current_human_approvers(
                reviews, head_sha="a" * 40, pull_request_author=author, owner_reviewer="owner"
            )
        )
        is accepted
    )


@pytest.mark.parametrize(
    "change",
    [
        {"login": "other"},
        {"user_type": "Bot"},
        {"author_association": "NONE"},
        {"commit_id": "b" * 40},
    ],
)
def test_owner_review_rejects_untrusted_or_stale_review(change):
    values = {"login": "owner", "state": "COMMENTED", "body": "approved", **change}
    assert (
        current_human_approvers(
            [_review(1, **values)],
            head_sha="a" * 40,
            pull_request_author="owner",
            owner_reviewer="owner",
        )
        == []
    )


@pytest.mark.parametrize(
    ("state", "body"),
    [
        ("DISMISSED", "approved"),
        ("CHANGES_REQUESTED", ""),
        ("COMMENTED", "other text"),
    ],
)
def test_owner_review_revocation_and_edited_self_review_fail_closed(state, body):
    reviews = [
        _review(1, login="owner", state="COMMENTED", body="approved"),
        _review(2, login="owner", state=state, body=body),
    ]
    assert (
        current_human_approvers(
            reviews, head_sha="a" * 40, pull_request_author="owner", owner_reviewer="owner"
        )
        == []
    )
    # Editing an existing review changes its API body rather than adding a review.
    assert (
        current_human_approvers(
            [_review(1, login="owner", state=state, body=body)],
            head_sha="a" * 40,
            pull_request_author="owner",
            owner_reviewer="owner",
        )
        == []
    )


def test_owner_review_policy_uses_configured_reviewer(tmp_repo, monkeypatch):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n"
        "[approval]\nmode = 'owner_review'\nreviewer = 'owner'\n",
    )
    base = commit_all(tmp_repo, "configure owner review")
    git("switch", "-c", "feature/owner", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "value = 1\n")
    head = commit_all(tmp_repo, "change code")
    _stub_attestation(monkeypatch, head=head, branch="feature/owner", findings_summary={})
    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[_review(1, login="owner", state="COMMENTED", body="approved", commit_id=head)],
        pull_request_author="owner",
    )
    assert result["approved"] is True
    assert result["human_approvers"] == ["owner"]


# -- approval carryover across heads with unchanged content -------------------

WAVE = "def wave(name):\n    if name:\n        print(name)\n    return name\n"
GREET = "def greet(name):\n    return f'hi {name}'\n"


def _approved_branch(repo, mode="owner_review"):
    """Return (approved head, advanced base) for a high-risk branch behind main."""
    write(
        repo,
        ".agentic-preflight.toml",
        f"[policy]\nhigh_risk_paths = ['src/**']\n[approval]\nmode = '{mode}'\n"
        "reviewer = 'owner'\nenvironment = 'high-risk-review'\n",
    )
    write(repo, "docs/notes.md", "one\n")
    commit_all(repo, "configure approval")
    git("switch", "-c", "feature/carry", cwd=repo)
    write(repo, "src/app.py", GREET + "\n\n" + WAVE)
    approved = commit_all(repo, "add wave")
    git("switch", "main", cwd=repo)
    write(repo, "docs/notes.md", "one\ntwo\n")
    base = commit_all(repo, "advance main")
    git("switch", "feature/carry", cwd=repo)
    return approved, base


def _merge_main(repo):
    git("merge", "--no-ff", "--no-edit", "main", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def _evaluate_head(repo, monkeypatch, *, base, head, reviews, author="author"):
    _stub_attestation(monkeypatch, head=head, branch="feature/carry", findings_summary={})
    return evaluate(repo, base_sha=base, head_sha=head, reviews=reviews, pull_request_author=author)


@pytest.mark.parametrize(
    ("mode", "author", "review"),
    [
        ("owner_review", "author", {"login": "owner"}),
        ("owner_review", "owner", {"login": "owner", "state": "COMMENTED", "body": "approved"}),
        ("peer_review", "author", {"login": "reviewer"}),
    ],
)
def test_approval_carries_across_a_clean_merge_from_the_base(
    tmp_repo, monkeypatch, mode, author, review
):
    approved, base = _approved_branch(tmp_repo, mode)
    head = _merge_main(tmp_repo)
    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=[_review(1, commit_id=approved, **review)],
        author=author,
    )
    assert result["requires_human_approval"] is True
    assert result["approved"] is True
    assert result["approval_carried"] is True
    assert result["approved_head"] == approved
    assert result["human_approvers"] == [review["login"]]


def test_exact_head_approval_is_reported_without_carryover(tmp_repo, monkeypatch):
    approved, base = _approved_branch(tmp_repo)
    head = _merge_main(tmp_repo)
    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=[
            _review(1, login="owner", commit_id=approved),
            _review(2, login="owner", commit_id=head),
        ],
    )
    assert result["approved"] is True
    assert result["approval_carried"] is False
    assert result["approved_head"] == head


def _conflicting_merge(repo):
    git("switch", "main", cwd=repo)
    write(repo, "src/app.py", GREET + "\n\ndef bow():\n    return 'bow'\n")
    base = commit_all(repo, "main also appends")
    git("switch", "feature/carry", cwd=repo)
    merged = subprocess.run(
        ["git", "merge", "--no-edit", "main"], cwd=repo, capture_output=True, text=True
    )
    assert merged.returncode != 0
    write(repo, "src/app.py", GREET + "\n\n" + WAVE + "\n\ndef bow():\n    return 'bow'\n")
    git("add", "-A", cwd=repo)
    git("commit", "--no-edit", cwd=repo)
    return base, git("rev-parse", "HEAD", cwd=repo)


def _evil_clean_merge(repo):
    git("merge", "--no-ff", "--no-commit", "main", cwd=repo)
    write(repo, "src/app.py", GREET + "\n\n" + WAVE.replace("return name", "return secret()"))
    git("add", "-A", cwd=repo)
    git("commit", "--no-edit", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.mark.parametrize(
    "change", ["content_commit", "whitespace_commit", "new_file", "conflict_merge", "evil_merge"]
)
def test_changed_content_after_approval_needs_fresh_approval(tmp_repo, monkeypatch, change):
    approved, base = _approved_branch(tmp_repo)
    if change == "conflict_merge":
        base, head = _conflicting_merge(tmp_repo)
    elif change == "evil_merge":
        head = _evil_clean_merge(tmp_repo)
    else:
        if change == "content_commit":
            write(tmp_repo, "src/app.py", GREET + "\n\n" + WAVE.replace("name\n", "name.upper()\n"))
        elif change == "whitespace_commit":
            # Moving the call out of the ``if`` changes behavior but only whitespace.
            write(
                tmp_repo, "src/app.py", GREET + "\n\n" + WAVE.replace("        print", "    print")
            )
        else:
            write(tmp_repo, "src/extra.py", "EXTRA = 1\n")
        head = commit_all(tmp_repo, change)
        if change == "whitespace_commit":
            # Git's patch identity ignores whitespace; only the tree comparison sees it.
            assert gitx.range_patch_id(tmp_repo, base, head) == gitx.range_patch_id(
                tmp_repo, base, approved
            )
    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=[_review(1, login="owner", commit_id=approved)],
    )
    assert result["approved"] is False
    assert result["approval_carried"] is False
    assert result["approved_head"] is None
    assert result["human_approvers"] == []


@pytest.mark.parametrize(
    ("author", "later"),
    [
        ("author", {"id": 2, "state": "CHANGES_REQUESTED", "commit_id": "$head"}),
        # A revocation on an unrelated intermediate head also withdraws the approval.
        ("author", {"id": 2, "state": "CHANGES_REQUESTED", "commit_id": "c" * 40}),
        # Dismissal and editing change the original review rather than adding one.
        ("author", {"id": 1, "state": "DISMISSED", "commit_id": "$approved"}),
        ("owner", {"id": 1, "state": "COMMENTED", "body": "ok, but", "commit_id": "$approved"}),
    ],
)
def test_revocation_after_an_earlier_approval_prevents_carryover(
    tmp_repo, monkeypatch, author, later
):
    approved, base = _approved_branch(tmp_repo)
    head = _merge_main(tmp_repo)
    approval = {"login": "owner", "commit_id": approved}
    if author == "owner":
        approval.update(state="COMMENTED", body="approved")
    revocation = {**later, "login": "owner"}
    revocation["commit_id"] = {"$head": head, "$approved": approved}.get(
        later["commit_id"], later["commit_id"]
    )
    review_id = revocation.pop("id")
    reviews = {1: _review(1, **approval)}
    reviews[review_id] = _review(review_id, **revocation)

    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=list(reviews.values()),
        author=author,
    )
    assert result["approved"] is False
    assert result["approval_carried"] is False


@pytest.mark.parametrize("attack", ["criss_cross_revert", "ours_merge_extra_commit"])
def test_history_that_hides_content_from_the_tree_comparisons_is_not_carried(
    tmp_repo, monkeypatch, attack
):
    """An ``-s ours`` merge leaves the head tree equal to the approved tree.

    Merging a base side branch that way creates criss-cross history: Git's single
    chosen merge base hides that the real merge into the base reverts the side
    branch. Merging an unreviewed branch that way adds commits a rebase merge
    would replay. Neither may carry the earlier approval.
    """
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n[approval]\nmode = 'owner_review'\n"
        "reviewer = 'owner'\n",
    )
    write(tmp_repo, "src/security.py", "CHECK = 'on'\n")
    root = commit_all(tmp_repo, "configure approval")
    git("switch", "-c", "side", cwd=tmp_repo)
    if attack == "criss_cross_revert":
        write(tmp_repo, "src/security.py", "CHECK = 'on'\nSTRICT = 'on'\n")
    else:
        write(tmp_repo, "src/evil.py", "steal()\n")
    side = commit_all(tmp_repo, "side branch")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "docs/notes.md", "one\n")
    # A later fork commit makes Git choose it as the single merge base below.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T00:00:00+00:00")
    fork = commit_all(tmp_repo, "advance main")
    if attack == "criss_cross_revert":
        git("merge", "--no-ff", "--no-edit", "side", cwd=tmp_repo)
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "-c", "feature/carry", fork, cwd=tmp_repo)
    write(tmp_repo, "src/app.py", GREET + "\n\n" + WAVE)
    approved = commit_all(tmp_repo, "add wave")
    git("merge", "-s", "ours", "--no-edit", side, cwd=tmp_repo)
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert gitx.tree_sha(tmp_repo, head) == gitx.tree_sha(tmp_repo, approved)
    assert root != base
    if attack == "criss_cross_revert":
        # The single chosen merge base hides the revert from every diff-based check.
        assert gitx.merge_base(tmp_repo, base, head) == fork
        assert gitx.changed_files(tmp_repo, base, head) == ["src/app.py"]

    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=[_review(1, login="owner", commit_id=approved)],
    )
    assert result["approved"] is False
    assert result["approval_carried"] is False


@pytest.mark.parametrize("mode", ["manual_merge", "environment"])
def test_environment_and_manual_merge_ignore_earlier_head_reviews(tmp_repo, monkeypatch, mode):
    approved, base = _approved_branch(tmp_repo, mode)
    head = _merge_main(tmp_repo)
    result = _evaluate_head(
        tmp_repo,
        monkeypatch,
        base=base,
        head=head,
        reviews=[_review(1, login="owner", commit_id=approved)],
    )
    assert result["approved"] is (mode == "manual_merge")
    assert result["manual_merge_required"] is (mode == "manual_merge")
    assert result["approval_carried"] is False
    assert result["approved_head"] is None
    assert result["human_approvers"] == []


@pytest.mark.parametrize("evidence_ref", [True, False])
def test_hosted_check_carries_across_a_rebase_only_through_the_evidence_ref(
    tmp_repo, tmp_path, monkeypatch, evidence_ref
):
    from agentic_preflight import refresh_validation

    monkeypatch.setattr(refresh_validation, "verify_evidence", lambda *_: None)
    approved, base = _approved_branch(tmp_repo)
    git("rebase", "main", cwd=tmp_repo)
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert not gitx.is_ancestor(tmp_repo, approved, head)
    attestation.write(
        tmp_repo,
        make_attestation(
            stages=_stages(),
            sha=head,
            tree_sha=gitx.tree_sha(tmp_repo, head),
            merge_base_sha=base,
            branch="feature/carry",
        ),
    )
    source = tmp_path / "source.git"
    git("init", "--bare", str(source), cwd=tmp_path)
    git("push", str(source), "main", "feature/carry", attestation.NOTES_REF, cwd=tmp_repo)
    if evidence_ref:
        evidence_ref_spec = f"{approved}:refs/agentic-preflight/evidence/{approved}"
        git("push", str(source), evidence_ref_spec, cwd=tmp_repo)
    # Like the hosted runner, a transport clone holds only branch history.
    verifier = tmp_path / "verifier"
    git("clone", "--no-local", str(source), str(verifier), cwd=tmp_path)
    git("checkout", "--detach", base, cwd=verifier)
    assert not gitx.commit_exists(verifier, approved)
    reviews_file = tmp_path / "reviews.json"
    reviews_file.write_text(json.dumps([_review(1, login="owner", commit_id=approved)]))

    result = ScriptedAgent(verifier).run(
        "hosted-check",
        head,
        "--base",
        base,
        "--source-remote",
        "origin",
        "--head-ref",
        "refs/heads/feature/carry",
        "--mode",
        "approval",
        "--reviews-file",
        str(reviews_file),
        "--author",
        "author",
        expect=ExitCode.OK if evidence_ref else ExitCode.NEEDS_HUMAN,
    )
    data = result["data"]
    assert data["approved"] is evidence_ref
    assert data["approval_carried"] is evidence_ref
    assert data["approved_head"] == (approved if evidence_ref else None)
    attempt = data["availability"]["attempts"][-1]
    assert attempt.get("fetched_approved_heads", []) == ([approved] if evidence_ref else [])
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=verifier)


def test_criss_cross_history_cannot_hide_a_high_risk_revert_from_approval(tmp_repo, monkeypatch):
    """An ``-s ours`` merge of a base side branch reverts it behind a hidden merge base.

    The base merged a side branch that changed a high-risk file. The head forks from the
    earlier main commit and merges that side branch with ``-s ours``, so Git has two merge
    bases. When it picks the main commit, ``base...head`` shows only the low-risk file,
    yet merging the head into the base reverts the side branch's high-risk change.
    """
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n"
        "[approval]\nmode = 'owner_review'\nreviewer = 'owner'\n",
    )
    write(tmp_repo, "src/security.py", "CHECK = 'on'\n")
    commit_all(tmp_repo, "configure approval")
    git("switch", "-c", "side", cwd=tmp_repo)
    write(tmp_repo, "src/security.py", "CHECK = 'on'\nSTRICT = 'on'\n")
    side = commit_all(tmp_repo, "tighten security")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "docs/notes.md", "one\n")
    # A later main commit makes Git choose it as the single merge base below.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T00:00:00+00:00")
    fork = commit_all(tmp_repo, "advance main")
    git("merge", "--no-ff", "--no-edit", "side", cwd=tmp_repo)
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "-c", "feature/hidden", fork, cwd=tmp_repo)
    write(tmp_repo, "docs/notes.md", "two\n")
    commit_all(tmp_repo, "edit notes")
    git("merge", "-s", "ours", "--no-edit", side, cwd=tmp_repo)
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert set(git("merge-base", "--all", base, head, cwd=tmp_repo).split()) == {fork, side}
    assert git("merge-base", base, head, cwd=tmp_repo) == fork
    assert gitx.changed_files(tmp_repo, base, head) == ["docs/notes.md"]
    _stub_attestation(monkeypatch, head=head, branch="feature/hidden", findings_summary={})

    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert "src/security.py" in result["changed_files"]
    assert result["risk_level"] == "high"
    assert result["requires_human_approval"] is True
    assert result["approved"] is False


def test_rebase_merge_cannot_land_a_high_risk_commit_hidden_by_an_evil_merge(tmp_repo, monkeypatch):
    """A merge commit whose resolution cancels an earlier high-risk commit hides it.

    The head tree and the merge-tree result both keep the base's high-risk file, so
    neither tree comparison reports it. A GitHub rebase merge replays the non-merge
    commits in ``base..head`` and drops the merge commit, so it lands the change anyway.
    """
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n"
        "[approval]\nmode = 'owner_review'\nreviewer = 'owner'\n",
    )
    write(tmp_repo, "src/security.py", "CHECK = 'on'\n")
    base = commit_all(tmp_repo, "configure approval")
    git("switch", "-c", "side", cwd=tmp_repo)
    write(tmp_repo, "docs/notes.md", "one\n")
    commit_all(tmp_repo, "edit notes")
    git("switch", "-c", "feature/evil", base, cwd=tmp_repo)
    write(tmp_repo, "src/security.py", "CHECK = 'off'\n")
    commit_all(tmp_repo, "disable security check")
    git("merge", "--no-ff", "--no-commit", "side", cwd=tmp_repo)
    git("checkout", base, "--", "src/security.py", cwd=tmp_repo)
    git("commit", "--no-edit", cwd=tmp_repo)
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert gitx.changed_files(tmp_repo, base, head) == ["docs/notes.md"]
    assert gitx.merge_result_files(tmp_repo, base, head) == ["docs/notes.md"]
    _stub_attestation(monkeypatch, head=head, branch="feature/evil", findings_summary={})

    result = evaluate(
        tmp_repo,
        base_sha=base,
        head_sha=head,
        reviews=[],
        pull_request_author="author",
    )
    assert "src/security.py" in result["changed_files"]
    assert result["risk_level"] == "high"
    assert result["requires_human_approval"] is True
    assert result["approved"] is False
