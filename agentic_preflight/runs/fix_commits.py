"""Derive a run's fix commits from the validation checkout's history.

Review, lint, and test validate the validation checkout's whole history on top
of the reviewed head, not the commits an agent happens to cite. Merge-back
replays the recorded list onto the source branch, so the list must be that
history, in commit order. Every path that registers fix commits uses
:func:`derive_fix_commits`, so the three registration points cannot disagree.
"""

from __future__ import annotations

from .. import gitx, worktree
from ..errors import StaleRun
from ..models import RunDoc
from ._session import Session, _require_worktree, _start_command


def derive_fix_commits(session: Session, run: RunDoc) -> list[str]:
    """Return every commit in ``head_sha..HEAD`` of the validation checkout, oldest first.

    ``run.head_sha`` is the reviewed head. Isolated modes never advance it while
    the run validates, so the result is the complete repair history. The
    in-place mode advances it as each repair is registered, so the result is the
    repairs made since the last registration.

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
    commits = gitx.commits_between(checkout, run.head_sha, current)
    for sha in commits:
        # Independent of the preflight copy refusal: a .gitignore edited mid-run
        # must not be able to open this hole.
        worktree.assert_commit_is_clean_of(checkout, sha, run.copied_files)
    return commits


def record_fix_commits(run: RunDoc, derived: list[str]) -> list[str]:
    """Store ``derived`` as the tail of ``run.fix_commits`` and return the new entries.

    Earlier entries that ``derived`` does not repeat keep their place (in-place
    repairs registered before ``head_sha`` advanced); the derived history follows
    in commit order, whatever order the agent cited it in.
    """
    added = [sha for sha in derived if sha not in run.fix_commits]
    run.fix_commits = [sha for sha in run.fix_commits if sha not in derived] + derived
    return added
