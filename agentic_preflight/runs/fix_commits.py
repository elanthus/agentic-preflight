"""Derive a run's fix commits from the validation checkout's history.

Review, lint, and test validate the validation checkout's whole history on top
of the reviewed head, not the commits an agent happens to cite. Merge-back
replays the recorded list onto the source branch, so the list must be that
history, in commit order. Every path that registers fix commits uses
:func:`derive_fix_commits`, so the three registration points cannot disagree.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .. import gitx, worktree
from ..envelope import ExitCode
from ..errors import AgenticError, StaleRun
from ..machine import State
from ..models import RunDoc
from ._session import (
    Session,
    is_in_place,
    open_findings_refusal,
    require_worktree,
    start_command,
)


class NonlinearFixHistory(AgenticError):
    """The isolated validation history contains a merge, which cannot be replayed."""

    code = "nonlinear_fix_history"
    exit_code = ExitCode.PRECONDITION


def derive_fix_commits(session: Session, run: RunDoc) -> list[str]:
    """Return every commit in ``head_sha..HEAD`` of the validation checkout, oldest first.

    ``run.head_sha`` is the reviewed head. Isolated modes never advance it while
    the run validates, so the result is the complete repair history. The
    in-place mode advances it as each repair is registered, so the result is the
    repairs made since the last registration.

    In isolated modes, which replay the result with ``git cherry-pick``, empty
    commits are left out (they change no tree, and replaying one stops the
    cherry-pick) and a merge commit is refused with ``NonlinearFixHistory``. The
    in-place mode replays nothing, so it keeps every commit and ``head_sha`` can
    advance to the checkout's real ``HEAD``.

    Raises ``StaleRun`` when the validation branch was rewritten rather than
    advanced, and refuses any commit that carries a copied environment file.
    """
    checkout = require_worktree(run)
    current = gitx.rev_parse(checkout, "HEAD")
    if not gitx.is_ancestor(checkout, run.head_sha, current):
        raise StaleRun(
            "the validation branch was rewritten instead of advanced by repair commits",
            state=run.state.value,
            run_id=run.run_id,
            next_command=start_command(
                run.intent,
                base_ref=run.base_ref,
                default_base_ref=session.config.general.base_ref,
                producer=run.producer,
            ),
        )
    span = f"{run.head_sha}..{current}"
    if not is_in_place(run):
        merges = gitx._lines(gitx.out(checkout, "rev-list", "--reverse", "--merges", span))
        if merges:
            raise NonlinearFixHistory(
                f"the validation history contains merge commit(s) {', '.join(merges)}; "
                "merge-back replays repairs with cherry-pick and cannot apply a merge",
                state=run.state.value,
                run_id=run.run_id,
                data={"merge_commits": merges, "reviewed_head": run.head_sha},
                next_instruction=(
                    "Remove the merge from the validation branch, for example by rebasing "
                    "onto the reviewed head, and keep only your own repairs as linear "
                    "commits; commits the merge brought in would otherwise become fix "
                    "commits. Then rerun the command. The changed history returns the run "
                    "to review."
                ),
                next_command=shlex.join(["git", "-C", checkout, "rebase", run.head_sha]),
            )
    commits = gitx.commits_between(checkout, run.head_sha, current)
    if not is_in_place(run):
        commits = [sha for sha in commits if not _is_empty(checkout, sha)]
    for sha in commits:
        # Independent of the preflight copy refusal: a .gitignore edited mid-run
        # must not be able to open this hole.
        worktree.assert_commit_is_clean_of(checkout, sha, run.copied_files)
    return commits


def patch_matches(
    repo: Path | str,
    fix_commits: list[str],
    base: str,
    head: str,
    *,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Pair each fix commit with a distinct patch-equivalent commit in ``base..head``.

    Merge-back cherry-picks, so SHA ancestry cannot prove that a fix landed. A stable
    patch ID ignores commit metadata, so a clean cherry-pick, including a reworded one,
    compares equal to its source. Each landed commit accounts for one fix, commits in
    ``exclude`` (already accounted for) are never matched, and an empty fix has no
    patch ID and is never matched.
    """
    if not fix_commits:
        return {}
    pool: dict[str, list[str]] = {}
    for sha, patch_id in gitx.patch_ids(repo, base, head).items():
        if patch_id is not None and sha not in exclude:
            pool.setdefault(patch_id, []).append(sha)
    matches: dict[str, str] = {}
    for sha in fix_commits:
        patch_id = gitx.commit_patch_id(repo, sha)
        if patch_id is not None and pool.get(patch_id):
            matches[sha] = pool[patch_id].pop(0)
    return matches


def patch_unlanded(repo: Path | str, fix_commits: list[str], base: str, head: str) -> list[str]:
    """Return the fix commits with no patch-equivalent commit in ``base..head``."""
    matched = patch_matches(repo, fix_commits, base, head)
    return [sha for sha in fix_commits if sha not in matched]


def _is_empty(checkout: str, sha: str) -> bool:
    """Whether a single-parent commit leaves its parent's tree unchanged."""
    trees = gitx._lines(gitx.out(checkout, "rev-parse", f"{sha}^{{tree}}", f"{sha}^^{{tree}}"))
    return len(trees) == 2 and trees[0] == trees[1]


def record_fix_commits(run: RunDoc, derived: list[str]) -> list[str]:
    """Store the derived history on ``run`` and return the entries it adds.

    In isolated modes ``derived`` is the complete history, so it replaces the
    list, dropping any commit a rebase of the validation branch removed. In the
    in-place mode earlier entries (repairs registered before ``head_sha``
    advanced) keep their place and the derived repairs follow. Either way the
    order is commit order, whatever order the agent cited it in.
    """
    added = [sha for sha in derived if sha not in run.fix_commits]
    if is_in_place(run):
        run.fix_commits = [sha for sha in run.fix_commits if sha not in derived] + derived
    else:
        run.fix_commits = list(derived)
    return added


#: States in which commits made after the last ``respond`` may not be registered yet.
#: Merge-back registers the whole validated history at ``TEST_GREEN``.
_UNREGISTERED_STATES = frozenset(
    {
        State.LINT_RUNNING,
        State.LINT_RED,
        State.LINT_GREEN,
        State.TEST_RUNNING,
        State.TEST_RED,
        State.TEST_GREEN,
    }
)


def branch_fix_commits(session: Session, run: RunDoc) -> list[str]:
    """The fix commits an isolated run's validation branch holds, without recording them.

    Before merge-back, a commit made after the last ``respond`` is on the branch but
    not yet in ``fix_commits``. A refusal that sends the run to ``abort`` must name
    it, because releasing the worktree deletes the branch. Falls back to the
    recorded list when the history cannot be derived, and never drops a recorded
    commit: a detached or reset checkout can hide one from ``head_sha..HEAD``.
    """
    recorded = list(run.fix_commits)
    if is_in_place(run) or run.state not in _UNREGISTERED_STATES:
        return recorded
    try:
        derived = derive_fix_commits(session, run)
    except (AgenticError, gitx.GitError, OSError, worktree.CopiedFileInCommit):
        return recorded
    return recorded + [sha for sha in derived if sha not in recorded]


def register_branch_fix_commits(session: Session, run: RunDoc) -> RunDoc:
    """Record :func:`branch_fix_commits` so that ``abort`` refuses to discard them."""
    derived = branch_fix_commits(session, run)
    if derived == run.fix_commits:
        return run
    with session.store.transaction(run.run_id) as doc:
        recorded = list(doc.fix_commits)
        doc.fix_commits = derived
        run = doc
    session.store.append_event(
        run.run_id,
        {"event": "fix_commits_registered_for_abort", "recorded": recorded, "commits": derived},
    )
    return run


def refuse_open_findings_past_respond(session: Session, run: RunDoc, *, command: str) -> None:
    """Raise the open-findings refusal of a run past ``respond``, keeping its fix commits.

    The refusal names ``abort``. Registering the branch's commits first means a plain
    ``abort`` stops with ``unmerged_work`` instead of deleting commits it never knew of.
    """
    refusal = open_findings_refusal(session, run, command=command)
    if refusal is None:
        return
    register_branch_fix_commits(session, run)
    raise refusal
