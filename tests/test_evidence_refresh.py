"""End-to-end refresh and provenance verification."""

import json
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation, runs
from agentic_preflight.machine import State
from agentic_preflight.models import Stage
from agentic_preflight.runs import evidence
from agentic_preflight.stages import shellstage
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, set_home, write
from tests.driver import ScriptedAgent


def _prepare(repo, *, mode="in_place", contracts=False, commands_in_repo=True):
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    body = f'[worktree]\nmode = "{mode}"\n'
    if commands_in_repo:
        body += f"[commands]\nlint = {command}\ntest = {command}\n"
    if contracts:
        for stage in ("lint", "test"):
            body += (
                f'[reuse.{stage}]\nmode = "content"\nfiles = []\nenvironment = []\ntoolchain = []\n'
            )
    write(repo, ".agentic-preflight.toml", body)
    commit_all(repo, "configure evidence reuse")


def _finish(agent, tmp_path):
    agent.run("context")
    payload = tmp_path / "review.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    return agent.run("mergeback")


def _restack(repo):
    base = git("rev-parse", "main", cwd=repo)
    tree = git("rev-parse", "main^{tree}", cwd=repo)
    new = git("commit-tree", tree, "-p", base, "-m", "history only", cwd=repo)
    git("update-ref", "refs/heads/main", new, base, cwd=repo)


def test_invalid_snapshot_is_reported_as_invalid_attestation(feature_repo, tmp_path):
    from agentic_preflight.digests import json_digest

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    raw = json.loads(attestation.encode(value))
    raw["config_snapshot"]["review"]["executor"] = "invalid"
    raw["config_sha256"] = json_digest(raw["config_snapshot"])
    with pytest.raises(attestation.InvalidAttestation) as error:
        attestation.verify_value(
            feature_repo, attestation.decode(json.dumps(raw)), value.sha, purpose="local"
        )
    assert error.value.reason == "invalid_evidence"
    assert "review.executor" in str(error.value)


@pytest.mark.parametrize("mode", ["in_place", "reusable", "strict"])
def test_history_only_restack_reuses_review_docs_and_reruns_shell_stages(
    feature_repo, tmp_path, monkeypatch, mode
):
    _prepare(feature_repo, mode=mode)
    calls = []
    execute = shellstage.run_stage

    def counted(*args, **kwargs):
        calls.append(args[1])
        return execute(*args, **kwargs)

    monkeypatch.setattr(shellstage, "run_stage", counted)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    assert original.outcome == "verified"
    assert len(calls) == 2
    agent.run("abort", "--force")
    _restack(feature_repo)

    refreshed = ScriptedAgent(feature_repo)
    start = refreshed.run("start")
    assert start["state"] == "DOCS_GREEN"
    assert start["next"]["command"] == "agentic-preflight stage run lint"
    for stage in ("review", "docs"):
        assert start["data"]["applicability"][stage]["disposition"] == "reusable"
    for stage in ("lint", "test"):
        assert start["data"]["applicability"][stage]["reasons"] == ["shell_reuse_disabled"]
        refreshed.run("stage", "run", stage)
    refreshed.run("mergeback")
    value = attestation.verify(feature_repo, "HEAD")
    assert value.sha != original.sha
    assert value.tree_sha == original.tree_sha
    assert len(calls) == 4
    assert value.evidence is not None
    assert original.evidence is not None
    for stage in (Stage.REVIEW, Stage.DOCS):
        assert value.evidence[stage].origin == original.evidence[stage].origin
        assert value.evidence[stage].refreshed_at is not None
    for stage in (Stage.LINT, Stage.TEST):
        assert value.evidence[stage].origin.head_sha == value.sha
        assert value.evidence[stage].origin.run_id != original.run_id
        assert value.evidence[stage].refreshed_at is None
    assert attestation.verify(feature_repo, original.sha) == original


def test_retired_shell_contract_is_ignored_and_shell_stages_rerun(feature_repo, tmp_path):
    _prepare(feature_repo, contracts=True)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    _restack(feature_repo)
    resumed = ScriptedAgent(feature_repo)
    result = resumed.run("start")
    assert result["state"] == "DOCS_GREEN"
    assert result["data"]["applicability"]["lint"]["reasons"] == ["shell_reuse_disabled"]
    resumed.run("stage", "run", "lint")
    resumed.run("stage", "run", "test")
    resumed.run("mergeback")
    assert attestation.verify(feature_repo, "HEAD").outcome == "verified"


def test_upstream_content_invalidates_review_even_with_an_unchanged_patch(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "dependency.py", "API_VERSION = 2\n")
    commit_all(feature_repo, "change upstream source")
    git("switch", "feature/x", cwd=feature_repo)
    result = ScriptedAgent(feature_repo).run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"
    assert "base_tree_changed" in result["data"]["applicability"]["review"]["reasons"]


@pytest.mark.parametrize("mutation", ["origin", "config"])
def test_verifier_rejects_changed_origin_and_incomplete_provenance(
    feature_repo, tmp_path, mutation
):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    payload = json.loads(attestation.encode(value))
    item = payload["evidence"]["lint"]
    _corrupt_origin(item, mutation)
    decoded = attestation.decode(json.dumps(payload))
    with pytest.raises(attestation.InvalidAttestation, match="digest"):
        attestation.verify_value(feature_repo, decoded, value.sha, purpose="local")
    payload = json.loads(attestation.encode(value))
    del payload["evidence"]["test"]
    with pytest.raises(attestation.InvalidAttestation, match="complete"):
        attestation.decode(json.dumps(payload))


def test_user_test_command_change_reuses_review_docs_and_reruns_shell(
    feature_repo, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    user_config = home / ".config/agentic-preflight/config.toml"
    user_config.parent.mkdir(parents=True)
    command = f'"{sys.executable}" -c "print(1)"'
    user_config.write_text(
        f"[commands]\nlint = {json.dumps(command)}\ntest = {json.dumps(command)}\n"
    )
    _prepare(feature_repo, commands_in_repo=False)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    agent.run("abort", "--force")
    updated = command.replace("print(1)", "print(2)")
    user_config.write_text(
        f"[commands]\nlint = {json.dumps(command)}\ntest = {json.dumps(updated)}\n"
    )
    agent = ScriptedAgent(feature_repo)
    result = agent.run("start")
    assert result["state"] == "DOCS_GREEN"
    assert result["data"]["applicability"]["test"]["reasons"] == ["shell_reuse_disabled"]
    assert agent.run("status")["state"] == "DOCS_GREEN"
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    refreshed = attestation.verify(feature_repo, "HEAD")
    assert refreshed.evidence[Stage.LINT].origin.run_id != original.run_id
    assert refreshed.evidence[Stage.TEST].origin.run_id != original.run_id


def test_docs_evidence_survives_a_pending_review_and_restart(feature_repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    agent.run("abort", "--force")
    user_config = home / ".config/agentic-preflight/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("[review]\nmax_findings = 51\n")
    agent = ScriptedAgent(feature_repo)
    result = agent.run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"
    assert result["data"]["applicability"]["docs"]["disposition"] == "reusable"
    agent = ScriptedAgent(feature_repo)
    agent.run("status")
    agent.run("context")
    payload = tmp_path / "fresh-review.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    result = agent.run("submit-findings", "--file", str(payload))
    assert result["state"] == "DOCS_GREEN"
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    refreshed = attestation.verify(feature_repo, "HEAD")
    assert refreshed.evidence[Stage.DOCS].origin == original.evidence[Stage.DOCS].origin
    assert refreshed.evidence[Stage.REVIEW].origin.run_id != original.run_id


def test_equivalent_target_is_checked_under_current_policy(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    git("branch", "alternate", "main", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    result = agent.run("start", "--base-ref", "alternate")
    assert result["state"] == "DOCS_GREEN"
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    value = attestation.verify(feature_repo, "HEAD")
    assert value.base_ref == "alternate"
    assert value.evidence[Stage.REVIEW].origin.base_ref == "main"


@pytest.mark.parametrize("mutation", ["head", "fingerprint", "coverage", "cycle", "version"])
def test_derived_provenance_rejects_wrong_bindings(feature_repo, tmp_path, mutation):
    from agentic_preflight.digests import json_digest

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    _restack(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    value = attestation.verify(feature_repo, "HEAD")
    payload = json.loads(attestation.encode(value))
    evidence = payload["evidence"]["review"]
    if mutation == "head":
        evidence["origin"]["head_sha"] = git("rev-parse", "main", cwd=feature_repo)
    elif mutation == "fingerprint":
        evidence["fingerprint"]["grounding_sha256"] = "f" * 64
    elif mutation == "coverage":
        evidence["origin"]["result"]["coverage"]["manifest"] = "f" * 64
    elif mutation == "cycle":
        evidence["origin"]["evidence"] = {"ref": evidence["origin_sha256"]}
    else:
        evidence["version"] = 999
    evidence["origin_sha256"] = json_digest(evidence["origin"])
    git(
        "notes",
        "--ref=agentic-preflight",
        "add",
        "-f",
        "-m",
        json.dumps(payload),
        "HEAD",
        cwd=feature_repo,
    )
    with pytest.raises(attestation.InvalidAttestation):
        attestation.verify(feature_repo, "HEAD")


def test_another_linked_source_worktree_cannot_borrow_evidence(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    git("switch", "-c", "unrelated", cwd=feature_repo)
    other = tmp_path / "other-source"
    git("worktree", "add", str(other), "feature/x", cwd=feature_repo)
    result = ScriptedAgent(other).run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"
    assert result["data"]["applicability"]["review"]["reasons"] == ["fingerprint_missing"]


def test_three_branch_stack_refreshes_downstream_after_first_merge(
    feature_repo, tmp_path, monkeypatch
):
    _prepare(feature_repo)
    git("switch", "-c", "stack/second", cwd=feature_repo)
    write(feature_repo, "second.py", "second = True\n")
    commit_all(feature_repo, "second stacked change")
    git("branch", "stack/third", cwd=feature_repo)
    calls = []
    execute = shellstage.run_stage

    def counted(*args, **kwargs):
        calls.append(args[1])
        return execute(*args, **kwargs)

    monkeypatch.setattr(shellstage, "run_stage", counted)
    agent = ScriptedAgent(feature_repo)
    agent.run("start", "--base-ref", "feature/x")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    git("switch", "stack/third", cwd=feature_repo)
    write(feature_repo, "third.py", "third = True\n")
    commit_all(feature_repo, "third stacked change")
    agent = ScriptedAgent(feature_repo)
    agent.run("start", "--base-ref", "stack/second")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    assert len(calls) == 4

    git("switch", "main", cwd=feature_repo)
    git("merge", "--no-ff", "feature/x", "-m", "merge first stacked change", cwd=feature_repo)
    for branch, base in (("stack/second", "main"), ("stack/third", "stack/second")):
        git("switch", branch, cwd=feature_repo)
        agent = ScriptedAgent(feature_repo)
        result = agent.run("start", "--base-ref", base)
        assert result["state"] == "DOCS_GREEN"
        agent.run("stage", "run", "lint")
        agent.run("stage", "run", "test")
        agent.run("mergeback")
        assert attestation.verify(feature_repo, "HEAD").outcome == "verified"
        agent.run("abort", "--force")
    assert len(calls) == 8


@pytest.mark.parametrize(
    ("action", "severity"), [("auto_fix", "low"), ("ask_user", "low"), ("no_op", "high")]
)
def test_verifier_preserves_unresolved_finding_requirements(
    feature_repo, tmp_path, action, severity
):
    from agentic_preflight.digests import json_digest
    from agentic_preflight.models import Finding

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    payload = json.loads(attestation.encode(value))
    origin = payload["evidence"]["docs"]["origin"]
    finding = Finding(
        id="F001",
        stage=Stage.DOCS,
        path="README.md",
        action=action,
        severity=severity,
        title="Outstanding documentation decision",
    )
    origin["findings"] = [finding.model_dump(mode="json")]
    payload["evidence"]["docs"]["origin_sha256"] = json_digest(origin)
    payload["findings_summary"] = {"open": 1, severity: 1}
    git(
        "notes",
        "--ref=agentic-preflight",
        "add",
        "-f",
        "-m",
        json.dumps(payload),
        "HEAD",
        cwd=feature_repo,
    )
    with pytest.raises(attestation.InvalidAttestation, match="unresolved"):
        attestation.verify(feature_repo, "HEAD")


def test_attestation_builder_rejects_skipped_lint(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    _finish(agent, tmp_path)
    store = Store(feature_repo / ".git" / "agentic-preflight")
    run = store.load_run(started["run_id"])
    run.stages[Stage.LINT].status = "skipped"
    with pytest.raises(attestation.InvalidAttestation, match="lint stage is not green"):
        attestation.build(
            run,
            sha=run.head_sha,
            tree_sha=git("rev-parse", "HEAD^{tree}", cwd=feature_repo),
            docs_enabled=True,
            findings_summary={},
        )


@pytest.mark.parametrize("missing_path", [False, True])
def test_status_survives_unavailable_validation_worktree(feature_repo, missing_path):
    _prepare(feature_repo, mode="strict")
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    worktree = Path(started["data"]["worktree_path"])
    if missing_path:
        git("worktree", "remove", "--force", str(worktree), cwd=feature_repo)
    else:
        store = Store(feature_repo / ".git" / "agentic-preflight")
        with store.transaction(started["run_id"]) as run:
            run.worktree_path = None
    status = agent.run("status")
    assert status["data"]["has_run"]
    assert status["run_id"] == started["run_id"]
    assert status["state"] == "REVIEW_AWAITING_FINDINGS"
    assert status["data"]["reuse_error"]
    assert status["data"]["findings"] == []


def test_status_reloads_persisted_state_after_interrupted_reuse(feature_repo, monkeypatch):
    from agentic_preflight.errors import WrongState
    from agentic_preflight.fingerprints import Classification, Disposition, ReasonCode
    from agentic_preflight.runs import evidence

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    def interrupted(session, run):
        with session.store.transaction(run.run_id) as doc:
            doc.applicability[Stage.REVIEW] = Classification(
                disposition=Disposition.UNKNOWN, reasons=(ReasonCode.INPUTS_UNAVAILABLE,)
            )
        raise WrongState("interrupted import")

    monkeypatch.setattr(evidence, "advance", interrupted)
    status = agent.run("status")
    assert status["data"]["reuse_error"] == "interrupted import"
    assert status["data"]["applicability"]["review"]["reasons"] == ["inputs_unavailable"]


def test_fresh_verified_stages_preserve_explicit_command_overrides(feature_repo, tmp_path):
    _prepare(feature_repo, contracts=True)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    payload = tmp_path / "review.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    override = f'"{sys.executable}" -c "print(2)"'
    for stage in ("lint", "test"):
        agent.run("stage", "run", stage, "--command", override)
    assert agent.run("mergeback")["state"] == "VERIFIED"
    value = attestation.verify(feature_repo, "HEAD")
    assert value.outcome == "verified"
    for stage in (Stage.LINT, Stage.TEST):
        assert value.stages[stage].command == override
        assert value.evidence[stage].origin.result.command == override


def _with_snapshot_key(raw: dict, section: str, key: str, value: object) -> dict:
    from agentic_preflight.digests import json_digest

    snapshots = [raw["config_snapshot"]]
    snapshots += [item["origin"]["config_snapshot"] for item in raw["evidence"].values()]
    for snapshot in snapshots:
        snapshot.setdefault(section, {})[key] = value
    raw["config_sha256"] = json_digest(raw["config_snapshot"])
    for item in raw["evidence"].values():
        origin = item["origin"]
        origin["config_sha256"] = json_digest(origin["config_snapshot"])
        item["origin_sha256"] = json_digest(origin)
    return raw


def test_a_snapshot_with_a_removed_config_key_still_verifies(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    raw = _with_snapshot_key(json.loads(attestation.encode(value)), "hook", "enabled", True)
    verified = attestation.verify_value(
        feature_repo, attestation.decode(json.dumps(raw)), value.sha, purpose="local"
    )
    assert verified.config_snapshot["hook"]["enabled"] is True


def test_a_snapshot_with_a_never_existing_config_key_is_rejected(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    raw = _with_snapshot_key(json.loads(attestation.encode(value)), "hook", "sparkle", True)
    with pytest.raises(attestation.InvalidAttestation) as error:
        attestation.verify_value(
            feature_repo, attestation.decode(json.dumps(raw)), value.sha, purpose="local"
        )
    assert error.value.reason == "invalid_evidence"
    assert "sparkle" in str(error.value)


def test_interrupted_legacy_evidence_is_never_reused_after_retirement(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start")
    _finish(agent, tmp_path)
    store = runs.open_session(feature_repo).store
    old = store.load_run(first["run_id"])
    assert set(old.evidence) == set(Stage)
    assert all(item.origin.result.status == "green" for item in old.evidence.values())
    journal = store.run_dir(old.run_id) / "pending-update.json"
    journal.write_text("interrupted legacy update")

    # Discovery must also refuse the interrupted green record before start
    # retires it, even if another run is being initialized independently.
    session = runs.open_session(feature_repo)
    probe = store.create_run(
        old.model_copy(
            update={
                "run_id": "r_0000000000",
                "state": State.REVIEW_AWAITING_FINDINGS,
                "evidence": {},
                "reuse_candidates": {},
                "stages": {},
            }
        )
    )
    assert evidence.discover(session, probe).reuse_candidates == {}

    second = agent.run("start")
    retired = store.load_run(old.run_id)
    assert retired.state.value == "ORPHANED"
    assert retired.evidence == old.evidence
    assert not journal.exists()
    fresh = store.load_run(second["run_id"])
    assert fresh.state.value == "REVIEW_AWAITING_FINDINGS"
    assert fresh.evidence == {}
    assert fresh.reuse_candidates == {}
    assert fresh.stages == {}
    assert agent.run("status")["state"] == "REVIEW_AWAITING_FINDINGS"

    # With the journal gone, later discovery must still exclude the old green
    # run instead of rediscovering it from preserved audit history.
    agent.run("abort")
    agent.run("gc")
    assert store.load_run(old.run_id).orphaned_reason == "interrupted legacy update"
    third = agent.run("start")
    later = store.load_run(third["run_id"])
    assert later.state.value == "REVIEW_AWAITING_FINDINGS"
    assert later.evidence == {}
    assert later.reuse_candidates == {}
    assert later.stages == {}


def _corrupt_origin(item, mutation):
    from agentic_preflight.digests import json_digest

    if mutation == "origin":
        item["origin"]["finished_at"] = "2000-01-01T00:00:00Z"
    else:
        item["origin"]["config_sha256"] = "f" * 64
        # A consistent outer digest must not hide a mismatched snapshot digest.
        item["origin_sha256"] = json_digest(item["origin"])


@pytest.mark.parametrize("mutation", ["origin", "config"])
def test_discovery_rejects_changed_origin_after_structural_run_load(
    feature_repo, tmp_path, mutation
):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    store = Store(feature_repo / ".git" / "agentic-preflight")
    run_id = started["run_id"]
    path = store.run_dir(run_id) / "run.json"
    payload = json.loads(path.read_text())
    for item in payload["evidence"].values():
        _corrupt_origin(item, mutation)
    path.write_text(json.dumps(payload))
    # Loading owned audit data is cheap; admission still refuses its edited origins.
    assert store.load_run(run_id).evidence
    result = ScriptedAgent(feature_repo).run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"
    assert all(
        item["reasons"] == ["fingerprint_missing"]
        for stage, item in result["data"]["applicability"].items()
        if stage in {"review", "docs"}
    )


@pytest.mark.parametrize("mutation", ["origin", "config"])
def test_import_rechecks_candidates_edited_after_discovery(
    feature_repo, tmp_path, monkeypatch, mutation
):
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    user_config = home / ".config/agentic-preflight/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("[review]\nmax_findings = 51\n")
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    assert started["data"]["applicability"]["docs"]["disposition"] == "reusable"
    store = Store(feature_repo / ".git" / "agentic-preflight")
    path = store.run_dir(started["run_id"]) / "run.json"
    payload = json.loads(path.read_text())
    _corrupt_origin(payload["reuse_candidates"]["docs"], mutation)
    path.write_text(json.dumps(payload))
    agent.run("context")
    findings = tmp_path / "fresh-review.json"
    findings.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(findings))
    result = agent.run("context", "--section", "docs")
    assert result["state"] == "DOCS_AWAITING_FINDINGS"
    run = store.load_run(started["run_id"])
    assert [reason.value for reason in run.applicability[Stage.DOCS].reasons] == [
        "provenance_invalid"
    ]
    assert Stage.DOCS not in run.evidence


@pytest.mark.parametrize("refreshed", [False, True])
def test_shell_evidence_cannot_refresh_or_cross_runs_between_identical_trees(
    feature_repo, tmp_path, refreshed
):
    from datetime import UTC, datetime

    from agentic_preflight.refresh_validation import verify_stage

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    item = original.evidence[Stage.LINT]
    replacement = git(
        "commit-tree", original.tree_sha, "-p", original.sha, "-m", "same tree", cwd=feature_repo
    )
    if refreshed:
        item = item.model_copy(
            update={"refreshed_at": datetime.now(UTC), "derivation_reason": "equivalent_inputs"}
        )
    message = "cannot be refreshed" if refreshed else "transferred evidence requires"
    with pytest.raises(ValueError, match=message):
        verify_stage(
            feature_repo,
            item,
            head=replacement,
            base=original.merge_base_sha,
            run_id=original.run_id if refreshed else "r_other",
        )
    assert attestation.verify(feature_repo, original.sha) == original


def test_published_original_shell_contract_remains_valid_without_input_reads(
    feature_repo, tmp_path, monkeypatch
):
    from agentic_preflight.digests import json_digest
    from agentic_preflight.fingerprints import shell_config_digest

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    raw = json.loads(attestation.encode(original))
    contract = {
        "mode": "content",
        "files": ["retired-input"],
        "environment": ["OLD_ENV"],
        "toolchain": [],
    }
    raw = _with_snapshot_key(raw, "reuse", "lint", contract)
    for name in ("lint", "test"):
        item = raw["evidence"][name]
        digest = shell_config_digest(item["origin"]["config_snapshot"], name)
        for fp in (item["fingerprint"], item["origin"]["fingerprint"]):
            fp["config_sha256"] = digest
            fp["inputs_sha256"] = "f" * 64
        item["origin_sha256"] = json_digest(item["origin"])
    git(
        "notes",
        "--ref=agentic-preflight",
        "add",
        "-f",
        "-m",
        json.dumps(raw),
        original.sha,
        cwd=feature_repo,
    )
    read = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != "retired-input", "retired contract inputs must not be read"
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    verified = attestation.verify(feature_repo, original.sha)

    assert verified.config_snapshot["reuse"]["lint"] == contract
    assert verified.evidence[Stage.LINT].origin.config_snapshot["reuse"]["lint"] == contract
    assert verified.evidence[Stage.LINT].fingerprint.inputs_sha256 == "f" * 64


def test_retired_contract_does_not_capture_declared_files(feature_repo, tmp_path, monkeypatch):
    _prepare(feature_repo, contracts=True)
    config = feature_repo / ".agentic-preflight.toml"
    config.write_text(config.read_text().replace("files = []", 'files = ["retired-input"]'))
    write(feature_repo, "retired-input", "previously captured input\n")
    commit_all(feature_repo, "configure retired file capture")
    read = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != "retired-input", "retired contract files must not be captured"
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)

    verified = attestation.verify(feature_repo, "HEAD")
    assert "reuse" not in verified.config_snapshot
    for stage in (Stage.LINT, Stage.TEST):
        assert verified.evidence[stage].fingerprint.inputs_sha256 is None
