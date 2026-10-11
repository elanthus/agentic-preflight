"""Trusted merge-approval policy for an attested pull-request head."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from . import attestation, ci_policy, gitx, risk
from .config import Config
from .models import Attestation, RiskLevel

_DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
_TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Modes whose approval is a pull-request review that can be carried across heads.
_CARRYOVER_MODES = {"peer_review", "owner_review"}
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Makes an approved head that is absent locally available, returning whether it did.
CommitFetcher = Callable[[str], bool]


class HeadApproval(NamedTuple):
    """One reviewer's counted approval and the commit it was submitted on."""

    login: str
    commit_id: str
    order: int


def _flatten_reviews(value: object) -> list[Mapping[str, Any]]:
    """Accept one GitHub API page or ``gh api --slurp`` pages."""
    if not isinstance(value, list):
        raise ValueError("reviews JSON must be an array")
    flattened: list[Mapping[str, Any]] = []
    pending: list[object] = list(value)
    while pending:
        item = pending.pop(0)
        if isinstance(item, list):
            pending[0:0] = item
        elif isinstance(item, Mapping):
            flattened.append(item)
        else:
            raise ValueError("each review must be a JSON object")
    return flattened


def latest_approvals(
    reviews: object,
    *,
    heads: Collection[str] | None,
    pull_request_author: str,
    owner_reviewer: str | None = None,
) -> list[HeadApproval]:
    """Return eligible reviewers whose latest decisive review is an approval.

    Only reviews submitted on one of ``heads`` are considered. ``None`` considers
    reviews on every head, so a later revocation on any head of the pull request
    replaces an earlier approval. Peer comments do not erase an approval; dismissal
    or changes requested does. Explicit owner mode also accepts an author's Comment
    review containing only ``approved``. Their latest Comment review replaces that
    self-approval, including edited text.
    """
    latest: dict[str, tuple[int, str, str]] = {}
    for review in _flatten_reviews(reviews):
        user = review.get("user")
        if not isinstance(user, Mapping):
            continue
        login = user.get("login")
        user_type = user.get("type")
        commit_id = review.get("commit_id")
        state = str(review.get("state", "")).upper()
        association = str(review.get("author_association", "")).upper()
        if (
            not isinstance(login, str)
            or user_type != "User"
            or association not in _TRUSTED_AUTHOR_ASSOCIATIONS
            or (owner_reviewer is None and login.casefold() == pull_request_author.casefold())
            or (owner_reviewer is not None and login.casefold() != owner_reviewer.casefold())
            or not isinstance(commit_id, str)
            or (heads is not None and commit_id not in heads)
            or state not in _DECISIVE_REVIEW_STATES | {"COMMENTED"}
        ):
            continue
        if state == "COMMENTED":
            if not (
                owner_reviewer is not None and login.casefold() == pull_request_author.casefold()
            ):
                continue
            state = "APPROVED" if str(review.get("body", "")).strip() == "approved" else "COMMENTED"
        if owner_reviewer is not None:
            login = owner_reviewer
        review_id = review.get("id")
        order = review_id if isinstance(review_id, int) else 0
        if login not in latest or order >= latest[login][0]:
            latest[login] = (order, state, commit_id)
    return [
        HeadApproval(login, commit_id, order)
        for login, (order, state, commit_id) in sorted(latest.items())
        if state == "APPROVED"
    ]


def current_human_approvers(
    reviews: object,
    *,
    head_sha: str,
    pull_request_author: str,
    owner_reviewer: str | None = None,
) -> list[str]:
    """Return repository-associated human approvals for the exact current head."""
    return [
        approval.login
        for approval in latest_approvals(
            reviews,
            heads={head_sha},
            pull_request_author=pull_request_author,
            owner_reviewer=owner_reviewer,
        )
    ]


def equivalent_heads(
    repo: Path | str,
    *,
    base_sha: str,
    approved_sha: str,
    head_sha: str,
    fetch_commit: CommitFetcher | None = None,
) -> bool:
    """Return whether ``head_sha`` carries exactly the change approved at ``approved_sha``.

    Computed only from Git objects, never from attestation notes. Both heads must
    change the same paths against ``base_sha`` with the same stable patch identity,
    and the current tree must equal Git's clean merge of the approved head with the
    base commit the current head now contains. The tree comparison closes what patch
    identity cannot see: Git hashes patches without whitespace or line numbers, so
    an indentation change would otherwise compare equal. Merging either head into
    the base must also give the same clean tree, which catches criss-cross history
    that hides a revert from the single chosen merge base. Finally, the non-merge
    commits since the base must have the same patch identities, so the new head
    cannot carry extra commits that a rebase merge would replay. A merge or rebase
    that resolves conflicts, adds content, or touches context lines is not
    equivalent.

    An approved head absent from the repository is requested once from
    ``fetch_commit``. Without it, or when it cannot be fetched, carry fails closed.
    """
    if approved_sha == head_sha:
        return True
    if not _OID.fullmatch(approved_sha):
        return False
    try:
        if not gitx.commit_exists(repo, approved_sha) and (
            fetch_commit is None
            or not fetch_commit(approved_sha)
            or not gitx.commit_exists(repo, approved_sha)
        ):
            return False
        if sorted(gitx.changed_files(repo, base_sha, approved_sha)) != sorted(
            gitx.changed_files(repo, base_sha, head_sha)
        ):
            return False
        approved_patch = gitx.range_patch_id(repo, base_sha, approved_sha)
        if approved_patch is None or approved_patch != gitx.range_patch_id(
            repo, base_sha, head_sha
        ):
            return False
        integrated = gitx.merge_tree(repo, approved_sha, gitx.merge_base(repo, base_sha, head_sha))
        if integrated is None or integrated != gitx.tree_sha(repo, head_sha):
            return False
        # The checks above use the single merge base Git picks. With criss-cross
        # history, the real merge into the base uses a different virtual base, so
        # compare what would actually land: both clean merges must be identical.
        landed = gitx.merge_tree(repo, base_sha, approved_sha)
        if landed is None or landed != gitx.merge_tree(repo, base_sha, head_sha):
            return False
        # A rebase merge replays non-merge commits, so the new head may not add any
        # (for example through an ``-s ours`` merge whose content never reaches the tree).
        return _contribution_patches(repo, base_sha, head_sha) == _contribution_patches(
            repo, base_sha, approved_sha
        )
    except gitx.GitError:
        # Unrelated histories or unreadable objects cannot prove equivalence.
        return False


def _contribution_patches(repo: Path | str, base_sha: str, head_sha: str) -> list[str]:
    """Return the sorted patch identities of the non-merge commits in ``base..head``."""
    merges = set(gitx.out(repo, "rev-list", "--merges", f"{base_sha}..{head_sha}").split())
    return sorted(
        patch_id or ""
        for sha, patch_id in gitx.patch_ids(repo, base_sha, head_sha).items()
        if sha not in merges
    )


def _carried_approvals(
    repo: Path | str,
    reviews: object,
    *,
    base_sha: str,
    head_sha: str,
    pull_request_author: str,
    owner_reviewer: str | None,
    fetch_commit: CommitFetcher | None,
) -> list[HeadApproval]:
    """Return approvals of earlier heads whose content equals the current head."""
    verdicts: dict[str, bool] = {}
    carried = []
    for candidate in latest_approvals(
        reviews,
        heads=None,
        pull_request_author=pull_request_author,
        owner_reviewer=owner_reviewer,
    ):
        sha = candidate.commit_id
        if sha not in verdicts:
            verdicts[sha] = equivalent_heads(
                repo,
                base_sha=base_sha,
                approved_sha=sha,
                head_sha=head_sha,
                fetch_commit=fetch_commit,
            )
        if verdicts[sha]:
            carried.append(candidate)
    return carried


def evaluate(
    repo: Path | str,
    *,
    base_sha: str,
    head_sha: str,
    reviews: object,
    pull_request_author: str,
    environment_approved: bool = False,
) -> dict[str, Any]:
    """Evaluate whether the exact PR head satisfies its merge-review policy."""
    repo = Path(repo)
    value = attestation.verify(repo, head_sha)
    cfg = ci_policy.committed_config(repo, base_sha)
    return evaluate_value(
        repo,
        value=value,
        cfg=cfg,
        base_sha=base_sha,
        head_sha=head_sha,
        reviews=reviews,
        pull_request_author=pull_request_author,
        environment_approved=environment_approved,
    )


def evaluate_value(
    repo: Path | str,
    *,
    value: Attestation,
    cfg: Config,
    base_sha: str,
    head_sha: str,
    reviews: object,
    pull_request_author: str,
    environment_approved: bool = False,
    fetch_commit: CommitFetcher | None = None,
) -> dict[str, Any]:
    """Combine validated evidence with a caller-supplied protected approval policy.

    In ``peer_review`` and ``owner_review`` modes, an approval recorded on an
    earlier head still counts when that head is equivalent to the current one (see
    ``equivalent_heads``). ``fetch_commit`` may supply an approved head that is not
    an ancestor of the current head. It is used only when carryover is needed.
    """
    changed_files = gitx.changed_files(repo, base_sha, head_sha)
    path_assessment = risk.assess(
        changed_files,
        [],
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )
    severe_findings = sum(
        value.findings_summary.get(severity, 0) for severity in ("critical", "high")
    )
    requires_approval = path_assessment.level is RiskLevel.HIGH or severe_findings > 0
    approval_mode = cfg.approval.mode
    owner_reviewer = cfg.approval.reviewer if approval_mode == "owner_review" else None
    approvals = latest_approvals(
        reviews,
        heads={value.sha},
        pull_request_author=pull_request_author,
        owner_reviewer=owner_reviewer,
    )
    carried = False
    if requires_approval and not approvals and approval_mode in _CARRYOVER_MODES:
        approvals = _carried_approvals(
            repo,
            reviews,
            base_sha=base_sha,
            head_sha=value.sha,
            pull_request_author=pull_request_author,
            owner_reviewer=owner_reviewer,
            fetch_commit=fetch_commit,
        )
        carried = bool(approvals)
    approvers = sorted({approval.login for approval in approvals})
    manual_merge_required = requires_approval and approval_mode == "manual_merge"
    if not requires_approval or approval_mode == "manual_merge":
        approved = True
    elif approval_mode == "environment":
        approved = environment_approved
    else:
        approved = bool(approvers)
    return {
        "approved": approved,
        "requires_human_approval": requires_approval,
        "approval_mode": approval_mode,
        "approval_environment": cfg.approval.environment,
        "manual_merge_required": manual_merge_required,
        "environment_approved": environment_approved,
        "risk_level": "high" if requires_approval else path_assessment.level.value,
        "head_sha": value.sha,
        "base_sha": gitx.rev_parse(repo, base_sha),
        "changed_files": changed_files,
        "path_reasons": [reason.model_dump(mode="json") for reason in path_assessment.reasons],
        "severe_findings": severe_findings,
        "human_approvers": approvers,
        "approved_head": (
            max(approvals, key=lambda approval: approval.order).commit_id if approvals else None
        ),
        "approval_carried": carried,
    }
