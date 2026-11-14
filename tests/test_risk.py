import pytest

from agentic_preflight.config import PolicySection, ReviewContractsSection, ReviewSection
from agentic_preflight.models import (
    Finding,
    FindingAction,
    FindingStatus,
    RiskLevel,
    Severity,
    Stage,
    Verdict,
)
from agentic_preflight.risk import (
    assess,
    command_path_patterns,
    include_attested_findings,
    reviewer_patterns,
)


def _finding(
    *,
    severity: Severity = Severity.HIGH,
    status: FindingStatus = FindingStatus.OPEN,
    action: FindingAction = FindingAction.AUTO_FIX,
    stage: Stage = Stage.REVIEW,
    code_owned: bool = False,
) -> Finding:
    return Finding(
        id="F001",
        stage=stage,
        code_owned=code_owned,
        status=status,
        path="src/auth.py",
        line=1,
        severity=severity,
        action=action,
        title="review finding",
    )


def _assess(
    paths,
    findings=(),
    policy=None,
    *,
    review_blocking_severities=("critical", "high"),
    docs_blocking_severities=("critical", "high"),
    review=None,
):
    return assess(
        list(paths),
        list(findings),
        policy=policy or PolicySection(),
        review=review or ReviewSection(blocking_severities=list(review_blocking_severities)),
        docs_blocking_severities=list(docs_blocking_severities),
    )


def test_unmatched_change_is_low_risk_and_passes():
    result = _assess(["src/app.py"])
    assert result.level is RiskLevel.LOW
    assert result.verdict is Verdict.CLEAR
    assert result.reasons == []


def test_path_policy_classifies_risk_without_using_diff_size():
    policy = PolicySection(
        high_risk_paths=["db/migrations/**"],
        medium_risk_paths=["dependencies/**"],
    )
    high = _assess(["db/migrations/0042_add_index.sql"], policy=policy)
    medium = _assess(["dependencies/catalog.toml"], policy=policy)
    assert high.level is RiskLevel.HIGH
    assert high.verdict is Verdict.NEEDS_HUMAN
    assert high.requires_human_review is True
    assert medium.level is RiskLevel.MEDIUM


def test_human_review_path_forces_a_human_verdict():
    result = _assess(
        [".github/workflows/ci.yml"],
        policy=PolicySection(human_review_paths=[".github/workflows/**"]),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.requires_human_review is True
    assert result.reasons[0].pattern == ".github/workflows/**"


def test_an_open_blocking_finding_requires_changes():
    result = _assess(["src/auth.py"], [_finding()])
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_fixed_high_finding_keeps_high_risk_but_no_longer_blocks():
    result = _assess(
        ["src/auth.py"],
        [_finding(status=FindingStatus.FIXED)],
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.requires_human_review is True
    assert result.reasons[0].finding_id == "F001"


def test_ask_user_blocks_at_any_severity():
    result = _assess(
        ["src/copy.py"],
        [_finding(severity=Severity.LOW, action=FindingAction.ASK_USER)],
    )
    assert result.level is RiskLevel.LOW
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_code_owned_finding_requires_changes_when_severity_is_excluded():
    result = _assess(
        ["CHANGELOG.md"],
        [
            _finding(
                stage=Stage.DOCS,
                code_owned=True,
            )
        ],
        docs_blocking_severities=("critical",),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_agent_finding_excluded_by_policy_does_not_require_changes():
    result = _assess(
        ["README.md"],
        [_finding(stage=Stage.DOCS)],
        docs_blocking_severities=("critical",),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN


def test_changes_required_takes_precedence_until_the_finding_is_resolved():
    result = _assess(
        ["policy.yml"],
        [_finding()],
        PolicySection(human_review_paths=["policy.yml"]),
    )
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_imported_attestation_preserves_medium_and_high_finding_risk():
    base = _assess(["src/app.py"])

    medium = include_attested_findings(base, {"medium": 2})
    assert medium.level is RiskLevel.MEDIUM
    assert medium.verdict is Verdict.CLEAR
    assert medium.requires_human_review is False
    assert medium.reasons[-1].severity is Severity.MEDIUM

    high = include_attested_findings(base, {"critical": 1})
    assert high.level is RiskLevel.HIGH
    assert high.verdict is Verdict.NEEDS_HUMAN
    assert high.requires_human_review is True
    assert high.reasons[-1].severity is Severity.CRITICAL


@pytest.mark.parametrize(
    ("command", "patterns"),
    [
        ("python3 reviewer/rev.py", ["reviewer/**"]),
        ("python3 ./rev.py --json", ["rev.py", "rev.py/**"]),
        (
            "env AP_REVIEWER_MODEL=claude-opus-5-5 uv run python -B "
            "docs/examples/reviewers/claude_review.py",
            ["docs/examples/reviewers/**"],
        ),
        # A directory the command changes to is protected, and later relative paths
        # are protected under both the root and that directory.
        ("cd tools&&python3 review.py", ["tools/**", "review.py", "review.py/**"]),
        ("pushd tools/reviewer; python3 ../run.py", ["tools/reviewer/**", "tools/**"]),
        ("cd tools/reviewer && make", ["tools/reviewer/**"]),
        ("cd -P tools && python3 review.py", ["tools/**", "review.py", "review.py/**"]),
        ("cd -- tools && python3 review.py", ["tools/**", "review.py", "review.py/**"]),
        ("uv run --directory tools python review.py", ["tools/**", "review.py", "review.py/**"]),
        ("uv run --project tools reviewer", ["tools/**"]),
        ("uv run --project=tools reviewer", ["tools/**"]),
        ("make -C tools review.mk", ["tools/**", "review.mk", "review.mk/**"]),
        ("make -Ctools", ["tools/**"]),
        ("npm --prefix tools/reviewer run review", ["tools/reviewer/**"]),
        ("env --chdir=tools ./review", ["tools/**", "review", "review/**"]),
        # Search-path variables name directories even without a slash or a dot.
        (
            "PYTHONPATH=tools:lib/py python3 -m reviewer",
            ["tools/**", "lib/py/**", "reviewer.py", "reviewer/**"],
        ),
        ("NODE_PATH=tools node review.js", ["tools/**", "review.js", "review.js/**"]),
        # ``-m`` imports the top-level package before the module.
        ("python3 -m tools.reviewer", ["tools/**"]),
        ("python3 -m reviewer --json", ["reviewer.py", "reviewer/**"]),
        ("python3 -Bm tools.reviewer", ["tools/**"]),
        ("python3 -mreviewer", ["reviewer.py", "reviewer/**"]),
        # Another tool's ``-m`` value that is not a module name is still a path.
        ("reviewer -vm prompts/review.txt", ["prompts/**"]),
        ("reviewer -mprompts/review.txt", ["prompts/**"]),
        # A leading expansion before a slash is dropped and the literal path kept.
        ('python3 "$PWD/tools/rev.py"', ["tools/**"]),
        ("python3 ${REPO}/tools/rev.py", ["tools/**"]),
        ("python3 $(git rev-parse --show-toplevel)/tools/rev.py", ["tools/**"]),
        # A command nested in one quoted word is read again.
        ("sh -c 'python3 tools/review.py'", ["tools/**"]),
        ("bash -c 'cd tools && python3 review.py'", ["tools/**", "review.py", "review.py/**"]),
        ("bash -lc 'python3 tools/review.py'", ["tools/**"]),
        ("/bin/sh -ec 'python3 tools/review.py'", ["tools/**"]),
        # Only a shell's ``-c`` takes a command; another tool's flag is an ordinary word.
        ("reviewer -vc 'conf/my review.toml'", ["conf/**"]),
        ("node 'my tools/review.mjs' --config=conf/reviewer.json", ["my tools/**", "conf/**"]),
        # Control operators split words only outside quotes.
        ("python3 'reviewers (local)/rev.py'", ["reviewers (local)/**"]),
        ('python3 "tools/a;b|c/rev.py"', ["tools/a;b|c/**"]),
        ("python3 a.py;python3 b/c.py", ["a.py", "a.py/**", "b/**"]),
        ("python3 a.py && python3 b/c.py", ["a.py", "a.py/**", "b/**"]),
        ("(cd tools; python3 rev.py)", ["tools/**", "rev.py", "rev.py/**"]),
        ("python3 tools/rev.py|tee out/log.txt", ["tools/**", "out/**"]),
        # Unbalanced quoting ignores quotes and still splits on every operator.
        (
            "python3 tools/rev.py # don't\ncd tools2&&python3 rev2.py",
            ["tools/**", "tools2/**", "rev2.py", "rev2.py/**"],
        ),
        ("CONFIG=conf/review.toml reviewer", ["conf/**"]),
        ("node tools/reviewer/", ["tools/**"]),
        # Bare program names, absolute paths, escapes from the repository, and paths
        # that the shell would compute are not repository paths.
        ("codex exec --json", []),
        ("/usr/local/bin/reviewer ../outside/review.py", []),
        ("python3 $REVIEWER tools/*.py ~/review.py", []),
        # Unbalanced quoting falls back to whitespace splitting.
        ("python3 tools/review.py 'unbalanced", ["tools/**"]),
    ],
)
def test_command_path_patterns_name_the_repository_paths_a_command_runs(command, patterns):
    assert command_path_patterns(command) == patterns


def test_reviewer_patterns_combine_both_commands_and_the_trusted_paths():
    review = ReviewSection(
        command="python3 reviewer/rev.py",
        trusted_paths=["prompts/**", "reviewer/**"],
        contracts=ReviewContractsSection(planner_command="python3 planner/plan.py"),
    )
    assert reviewer_patterns(review) == ["prompts/**", "reviewer/**", "planner/**"]
    assert reviewer_patterns(ReviewSection()) == []


@pytest.mark.parametrize(
    "review",
    [
        ReviewSection(command="python3 reviewer/rev.py"),
        ReviewSection(trusted_paths=["reviewer/**"]),
        ReviewSection(contracts=ReviewContractsSection(planner_command="python3 reviewer/rev.py")),
    ],
)
def test_a_change_to_the_reviewer_requires_human_review(review):
    result = _assess(["reviewer/rev.py", "src/app.py"], review=review)
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert [(reason.kind, reason.path, reason.pattern) for reason in result.reasons] == [
        ("human_review_path", "reviewer/rev.py", "reviewer/**")
    ]
    assert _assess(["src/app.py"], review=review).level is RiskLevel.LOW


def test_a_reviewer_path_already_in_the_policy_is_reported_once():
    result = _assess(
        ["reviewer/rev.py"],
        policy=PolicySection(human_review_paths=["reviewer/**"]),
        review=ReviewSection(command="python3 reviewer/rev.py"),
    )
    assert len(result.reasons) == 1


def test_default_trusted_paths_stay_out_of_configuration_snapshots():
    assert "trusted_paths" not in ReviewSection().model_dump()
    configured = ReviewSection(trusted_paths=["reviewer/**"])
    assert configured.model_dump()["trusted_paths"] == ["reviewer/**"]


def test_trusted_paths_must_be_repository_relative():
    with pytest.raises(ValueError, match="repo-relative"):
        ReviewSection(trusted_paths=["../reviewer/**"])


def test_operator_commands_use_posix_quoting_even_where_backslash_is_a_separator(monkeypatch):
    # A command with an operator runs through a POSIX shell (Git Bash on Windows),
    # so its escaped quote must not hide the paths after it.
    from agentic_preflight.stages import command as commandmod

    monkeypatch.setattr(commandmod, "BACKSLASH_ESCAPES", False)
    escaped = "python3 x\\' ; cd tools && python3 rev.py ; echo \\'"
    assert command_path_patterns(escaped) == ["tools/**", "rev.py", "rev.py/**"]
    # Without an operator, a backslash stays a Windows path separator.
    assert command_path_patterns("python3 tools\\rev.py") == ["tools/**"]
