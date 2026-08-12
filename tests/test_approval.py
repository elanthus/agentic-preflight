import json

from agentic_preflight import attestation
from agentic_preflight.approval import current_human_approvers, evaluate
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Attestation, AttestedStage, ReviewCoverage, Stage
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
):
    return {
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


def test_high_risk_attested_change_requires_exact_head_human_approval(tmp_repo, tmp_path):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nhigh_risk_paths = ['src/**']\n\n[approval]\nmode = 'peer_review'\n",
    )
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/risky", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    head = commit_all(tmp_repo, "change application")
    attestation.write(
        tmp_repo,
        Attestation(
            sha=head,
            tree_sha=git("rev-parse", f"{head}^{{tree}}", cwd=tmp_repo),
            branch="feature/risky",
            base_ref="main",
            merge_base_sha=base,
            run_id="r_test",
            green_at="2026-01-01T00:00:00+00:00",
            stages=_stages(),
            findings_summary={"open": 0},
        ),
    )

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


def test_high_severity_attestation_summary_also_requires_approval(tmp_repo):
    write(
        tmp_repo,
        ".agentic-preflight.toml",
        "[policy]\nmedium_risk_paths = ['src/**']\n\n[approval]\nmode = 'peer_review'\n",
    )
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/finding", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    head = commit_all(tmp_repo, "change application")
    attestation.write(
        tmp_repo,
        Attestation(
            sha=head,
            tree_sha=git("rev-parse", f"{head}^{{tree}}", cwd=tmp_repo),
            branch="feature/finding",
            base_ref="main",
            merge_base_sha=base,
            run_id="r_test",
            green_at="2026-01-01T00:00:00+00:00",
            stages=_stages(),
            findings_summary={"fixed": 1, "high": 1},
        ),
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


def test_manual_merge_is_the_default_and_passes_without_a_peer(tmp_repo, tmp_path):
    write(tmp_repo, ".agentic-preflight.toml", "[policy]\nhigh_risk_paths = ['src/**']\n")
    base = commit_all(tmp_repo, "configure risk")
    git("switch", "-c", "feature/manual-merge", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet():\n    return 'hello'\n")
    head = commit_all(tmp_repo, "change application")
    attestation.write(
        tmp_repo,
        Attestation(
            sha=head,
            tree_sha=git("rev-parse", f"{head}^{{tree}}", cwd=tmp_repo),
            branch="feature/manual-merge",
            base_ref="main",
            merge_base_sha=base,
            run_id="r_test",
            green_at="2026-01-01T00:00:00+00:00",
            stages=_stages(),
            findings_summary={"open": 0},
        ),
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


def test_environment_mode_requires_the_environment_release(tmp_repo, tmp_path):
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
    attestation.write(
        tmp_repo,
        Attestation(
            sha=head,
            tree_sha=git("rev-parse", f"{head}^{{tree}}", cwd=tmp_repo),
            branch="feature/environment",
            base_ref="main",
            merge_base_sha=base,
            run_id="r_test",
            green_at="2026-01-01T00:00:00+00:00",
            stages=_stages(),
            findings_summary={"open": 0},
        ),
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
