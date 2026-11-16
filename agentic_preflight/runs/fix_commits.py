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
from ._session import Session, is_in_place, require_worktree, start_command, worktree_mode


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
    if not is_in_place(run):
        merges = gitx.merge_commits_between(checkout, run.head_sha, current)
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


#: States in which the validation branch can hold commits that nothing registered:
#: from the end of the base synchronization, which fixes ``head_sha``, until
#: merge-back registers the validated history.
_UNREGISTERED_STATES = frozenset(
    {
        State.SYNC_GREEN,
        State.REVIEW_AWAITING_FINDINGS,
        State.REVIEW_COMMAND_RUNNING,
        State.REVIEW_COMMAND_RED,
        State.REVIEW_BLOCKED,
        State.REVIEW_GREEN,
        State.DOCS_AWAITING_FINDINGS,
        State.DOCS_BLOCKED,
        State.DOCS_GREEN,
        State.LINT_RUNNING,
        State.LINT_RED,
        State.LINT_GREEN,
        State.TEST_RUNNING,
        State.TEST_RED,
        State.TEST_GREEN,
    }
)


class UnreadableFixHistory(Exception):
    """The validation branch's history could not be read, so it may hold work."""


def _tip_exists(cwd: str, tip: str) -> bool:
    """Whether ``tip`` names a commit; only a missing ref is ``False``.

    Any other failure, such as a checkout Git cannot open, raises ``gitx.GitError``.
    """
    args = ("rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}")
    result = gitx.run(cwd, *args, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1 and not result.stderr.strip():
        return False
    raise gitx.GitError(list(args), result.returncode, result.stderr)


def _branch_only_commits(run: RunDoc, checkout: str, derived: list[str]) -> list[str]:
    """Commits on the validation branch that ``HEAD`` does not reach.

    Release deletes the branch, not ``HEAD``, so a checkout detached at an older
    commit must not hide what the branch holds.
    """
    ref = f"refs/heads/{run.worktree_branch}"
    if not run.worktree_branch or not _tip_exists(checkout, ref):
        return []
    tip = gitx.rev_parse(checkout, ref)
    if tip == gitx.rev_parse(checkout, "HEAD"):
        return []
    if not gitx.is_ancestor(checkout, run.head_sha, tip):
        raise UnreadableFixHistory(
            f"the validation branch {run.worktree_branch} no longer descends from the reviewed head"
        )
    merges = gitx.merge_commits_between(checkout, run.head_sha, tip)
    if merges:
        raise UnreadableFixHistory(
            f"the validation branch {run.worktree_branch} contains merge commit(s) "
            f"{', '.join(merges)}"
        )
    return [
        sha
        for sha in gitx.commits_between(checkout, run.head_sha, tip)
        if sha not in derived and not _is_empty(checkout, sha)
    ]


def _changes_tree(cwd: str, sha: str) -> bool:
    """Whether a commit may hold work: anything but a single-parent empty commit."""
    try:
        return not _is_empty(cwd, sha)
    except gitx.GitError:
        # A root commit has no parent to compare; count it rather than lose it.
        return True


def _recorded_worktree_head(repo: str, checkout: str) -> str | None:
    """The ``HEAD`` the shared repository records for a checkout, if it lists one.

    Git keeps each linked worktree's ``HEAD`` in the shared repository, so it can be
    read even when the checkout's own ``.git`` file is gone.
    """
    target = Path(checkout).resolve()
    for record in gitx.list_worktrees(repo):
        if "worktree" in record and Path(record["worktree"]).resolve() == target:
            return record.get("HEAD") or None
    return None


def listed_branch_commits(session: Session, run: RunDoc, *, best_effort: bool = False) -> list[str]:
    """Every commit after the reviewed head on ``HEAD`` or the validation branch.

    A plain listing, with none of :func:`branch_fix_commits`'s checks: what ``abort``
    names, and what it weighs, when the history cannot be derived. Empty commits
    change no tree and are left out, as everywhere else. The branch ref is read from
    the shared repository, where it lives, and ``HEAD`` both from the checkout when
    its directory exists and from the shared repository's record of that worktree.
    A tip that does not exist holds nothing; any other failure, such as a checkout
    Git cannot open, raises ``gitx.GitError``, or with ``best_effort`` skips that
    tip, so a report still names what the others hold.
    """
    checkout = run.worktree_path
    tips: list[tuple[str, str]] = []
    if run.worktree_branch:
        tips.append((str(session.repo_root), f"refs/heads/{run.worktree_branch}"))
    if checkout and Path(str(checkout)).is_dir():
        tips.append((str(checkout), "HEAD"))
    if checkout:
        try:
            recorded = _recorded_worktree_head(str(session.repo_root), str(checkout))
        except gitx.GitError:
            if not best_effort:
                raise
            recorded = None
        if recorded:
            tips.append((str(session.repo_root), recorded))
    listed: list[str] = []
    for cwd, tip in tips:
        try:
            if not _tip_exists(cwd, tip):
                continue
            commits = gitx.commits_between(cwd, run.head_sha, tip)
        except gitx.GitError:
            if best_effort:
                continue
            raise
        listed += [sha for sha in commits if sha not in listed and _changes_tree(cwd, sha)]
    return listed


def branch_fix_commits(session: Session, run: RunDoc) -> list[str]:
    """The fix commits an isolated run's validation branch holds, without recording them.

    Before merge-back, a commit made outside ``respond`` and a red stage's restart is
    on the branch but not in ``fix_commits``, and releasing the worktree deletes the
    branch. The result is the recorded list followed by any such commit, read from
    both ``HEAD`` and the branch. It never drops a recorded commit, because a
    detached or reset checkout can hide one. When the checkout is gone, the branch
    ref, which lives in the shared repository, is still read. Any failure raises
    :class:`UnreadableFixHistory`.
    """
    recorded = list(run.fix_commits)
    if is_in_place(run) or run.state not in _UNREGISTERED_STATES:
        return recorded
    checkout = run.worktree_path
    if not checkout or not Path(checkout).is_dir():
        if worktree_mode(run) == "reusable":
            # Releasing a missing runner leaves its branch in place; nothing is lost.
            return recorded
        try:
            extra = _branch_only_commits(run, str(session.repo_root), [])
        except (gitx.GitError, OSError) as exc:
            raise UnreadableFixHistory(str(exc)) from exc
        return recorded + [sha for sha in extra if sha not in recorded]
    try:
        derived = derive_fix_commits(session, run)
        derived += _branch_only_commits(run, checkout, derived)
    except (AgenticError, gitx.GitError, OSError, worktree.CopiedFileInCommit) as exc:
        raise UnreadableFixHistory(str(exc)) from exc
    return recorded + [sha for sha in derived if sha not in recorded]
