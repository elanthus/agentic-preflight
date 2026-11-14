"""Derive a run's fix commits from the validation checkout's history.

Review, lint, and test validate the validation checkout's whole history on top
of the reviewed head, not the commits an agent happens to cite. Merge-back
replays the recorded list onto the source branch, so the list must be that
history, in commit order. Every path that registers fix commits uses
:func:`derive_fix_commits`, so the three registration points cannot disagree.
"""

from __future__ import annotations

import shlex

from .. import gitx, worktree
from ..envelope import ExitCode
from ..errors import AgenticError, StaleRun
from ..models import RunDoc
from ._session import Session, _is_in_place, _require_worktree, _start_command


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
    checkout = _require_worktree(run)
    current = gitx.rev_parse(checkout, "HEAD")
    if not gitx.is_ancestor(checkout, run.head_sha, current):
        raise StaleRun(
            "the validation branch was rewritten instead of advanced by repair commits",
            state=run.state.value,
            run_id=run.run_id,
            next_command=_start_command(
                run.intent,
                base_ref=run.base_ref,
                default_base_ref=session.config.general.base_ref,
                producer=run.producer,
            ),
        )
    span = f"{run.head_sha}..{current}"
    if not _is_in_place(run):
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
    if not _is_in_place(run):
        commits = [sha for sha in commits if not _is_empty(checkout, sha)]
    for sha in commits:
        # Independent of the preflight copy refusal: a .gitignore edited mid-run
        # must not be able to open this hole.
        worktree.assert_commit_is_clean_of(checkout, sha, run.copied_files)
    return commits


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
    if _is_in_place(run):
        run.fix_commits = [sha for sha in run.fix_commits if sha not in derived] + derived
    else:
        run.fix_commits = list(derived)
    return added
