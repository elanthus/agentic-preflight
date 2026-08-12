import pytest

from agentic_preflight import findings as F
from agentic_preflight.machine import State
from agentic_preflight.models import (
    Finding,
    FindingAction,
    FindingStatus,
    FindingSubmission,
    Severity,
    Stage,
)
from tests.conftest import write


def sub(path="src/app.py", line=1, severity="high", action="auto_fix", title="t"):
    return FindingSubmission(path=path, line=line, severity=severity, action=action, title=title)


@pytest.fixture
def wt(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    write(root, "src/app.py", "one\ntwo\nthree\n")
    write(root, "README.md", "# demo\n")
    return root


def accept(subs, *, wt, stage=Stage.REVIEW, allowed=("src/app.py",), existing=(), max_findings=50):
    return F.validate_and_assign(
        list(subs),
        stage=stage,
        worktree_path=wt,
        allowed_paths=set(allowed),
        existing=list(existing),
        max_findings=max_findings,
    )


# -- identity is code's to assign -------------------------------------------


def test_ids_are_assigned_in_order(wt):
    result = accept([sub(title="a"), sub(title="b")], wt=wt)
    assert [f.id for f in result] == ["F001", "F002"]


def test_ids_are_append_only_across_stage_boundaries(wt):
    """Numbering is per *run*, not per stage — F003 must not restart at F001."""
    review = accept([sub(title="a"), sub(title="b")], wt=wt)
    docs = accept(
        [sub(path="README.md", title="c")],
        wt=wt,
        stage=Stage.DOCS,
        allowed=("README.md",),
        existing=review,
    )
    assert [f.id for f in docs] == ["F003"]


def test_stage_is_derived_not_taken_from_the_agent(wt):
    result = accept([sub(path="README.md")], wt=wt, stage=Stage.DOCS, allowed=("README.md",))
    assert result[0].stage is Stage.DOCS


def test_new_findings_start_open_with_no_fix_commit(wt):
    result = accept([sub()], wt=wt)
    assert result[0].status is FindingStatus.OPEN
    assert result[0].fix_commit is None


def test_agent_judgment_fields_are_preserved_verbatim(wt):
    result = accept([sub(severity="low", action="no_op", title="nit: spacing")], wt=wt)
    assert result[0].severity is Severity.LOW
    assert result[0].action is FindingAction.NO_OP
    assert result[0].title == "nit: spacing"


# -- path containment -------------------------------------------------------


def test_a_path_escaping_the_worktree_is_rejected(wt):
    with pytest.raises(F.FindingRejected) as exc:
        accept([sub(path="../outside.py")], wt=wt, allowed=("../outside.py",))
    assert "outside the worktree" in str(exc.value)


def test_an_absolute_path_is_rejected(wt):
    with pytest.raises(F.FindingRejected):
        accept([sub(path="/etc/passwd")], wt=wt, allowed=("/etc/passwd",))


def test_a_symlink_escaping_the_worktree_is_rejected(wt, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("secret\n")
    (wt / "link.py").symlink_to(secret)

    with pytest.raises(F.FindingRejected) as exc:
        accept([sub(path="link.py")], wt=wt, allowed=("link.py",))
    assert "outside the worktree" in str(exc.value)


def test_a_path_outside_the_allowed_set_is_rejected(wt):
    """Review findings must land on files the diff actually touched."""
    with pytest.raises(F.FindingRejected) as exc:
        accept([sub(path="README.md")], wt=wt, allowed=("src/app.py",))
    assert "README.md" in str(exc.value)
    assert "not in the changed-file set" in str(exc.value)


# -- line bounds ------------------------------------------------------------


def test_a_line_beyond_the_end_of_the_file_is_rejected(wt):
    with pytest.raises(F.FindingRejected) as exc:
        accept([sub(line=99)], wt=wt)
    assert "3 lines" in str(exc.value)


def test_the_last_line_of_the_file_is_accepted(wt):
    assert accept([sub(line=3)], wt=wt)[0].line == 3


def test_a_finding_with_no_line_is_accepted(wt):
    assert accept([sub(line=None)], wt=wt)[0].line is None


# -- volume -----------------------------------------------------------------


def test_exceeding_max_findings_is_rejected(wt):
    with pytest.raises(F.FindingRejected) as exc:
        accept([sub(title=f"f{i}") for i in range(4)], wt=wt, max_findings=3)
    assert "max_findings" in str(exc.value)


def test_the_existing_count_is_included_in_the_volume_check(wt):
    existing = accept([sub(title="a"), sub(title="b")], wt=wt)
    with pytest.raises(F.FindingRejected):
        accept([sub(title="c"), sub(title="d")], wt=wt, existing=existing, max_findings=3)


# -- the blocking set is code's to derive -----------------------------------


def _finding(id, severity, action, status=FindingStatus.OPEN, *, code_owned=False):
    return Finding(
        id=id,
        stage=Stage.REVIEW,
        code_owned=code_owned,
        status=status,
        path="src/app.py",
        severity=severity,
        action=action,
        title="t",
    )


def test_high_and_critical_severities_block():
    items = [
        _finding("F001", Severity.CRITICAL, FindingAction.AUTO_FIX),
        _finding("F002", Severity.HIGH, FindingAction.AUTO_FIX),
        _finding("F003", Severity.MEDIUM, FindingAction.AUTO_FIX),
        _finding("F004", Severity.LOW, FindingAction.NO_OP),
    ]
    blocking = F.blocking(items, blocking_severities=["critical", "high"])
    assert [f.id for f in blocking] == ["F001", "F002"]


def test_ask_user_blocks_regardless_of_severity():
    items = [_finding("F001", Severity.LOW, FindingAction.ASK_USER)]
    assert [f.id for f in F.blocking(items, blocking_severities=["critical", "high"])] == ["F001"]


def test_code_owned_blocks_regardless_of_severity_policy():
    items = [
        _finding(
            "F001",
            Severity.HIGH,
            FindingAction.AUTO_FIX,
            code_owned=True,
        )
    ]
    assert [f.id for f in F.blocking(items, blocking_severities=["critical"])] == ["F001"]


def test_resolved_findings_no_longer_block():
    items = [_finding("F001", Severity.CRITICAL, FindingAction.AUTO_FIX, FindingStatus.FIXED)]
    assert F.blocking(items, blocking_severities=["critical", "high"]) == []


def test_blocking_severities_are_configurable():
    items = [_finding("F001", Severity.MEDIUM, FindingAction.AUTO_FIX)]
    assert len(F.blocking(items, blocking_severities=["critical", "high", "medium"])) == 1


def test_non_code_owned_finding_excluded_by_policy_remains_non_blocking():
    items = [_finding("F001", Severity.HIGH, FindingAction.AUTO_FIX)]
    assert F.blocking(items, blocking_severities=["critical"]) == []


# -- stage derivation from state --------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (State.REVIEW_AWAITING_FINDINGS, Stage.REVIEW),
        (State.REVIEW_FIXING, Stage.REVIEW),
        (State.DOCS_AWAITING_FINDINGS, Stage.DOCS),
        (State.DOCS_FIXING, Stage.DOCS),
    ],
)
def test_stage_for_state(state, expected):
    assert F.stage_for_state(state) is expected


def test_stage_for_a_state_with_no_stage_is_none():
    assert F.stage_for_state(State.CREATED) is None
