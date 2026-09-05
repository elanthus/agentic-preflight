"""End-to-end refresh, provenance verification and producer/consumer rollout."""

import json
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation
from agentic_preflight.models import Stage
from agentic_preflight.stages import shellstage
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, set_home, write
from tests.driver import ScriptedAgent


def _prepare(repo, *, mode="in_place", contracts=True, consumer=True, commands_in_repo=True):
    if consumer:
        git("switch", "main", cwd=repo)
        write(
            repo,
            "agentic_preflight/refresh_validation.py",
            "# consumer\nREFRESH_WIRE_VERSION = 5\n",
        )
        commit_all(repo, "install protected-base consumer")
        git("switch", "feature/x", cwd=repo)
        git("rebase", "main", cwd=repo)
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


@pytest.mark.parametrize("mode", ["in_place", "reusable", "strict"])
def test_history_only_restack_reuses_all_stages_without_execution(
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
    assert original.schema_version == 5
    assert len(calls) == 2
    agent.run("abort", "--force")
    _restack(feature_repo)

    refreshed = ScriptedAgent(feature_repo)
    start = refreshed.run("start")
    assert start["state"] == "TEST_GREEN"
    assert start["next"]["command"] == "agentic-preflight mergeback"
    assert {value["disposition"] for value in start["data"]["applicability"].values()} == {
        "reusable"
    }
    refreshed.run("mergeback")
    value = attestation.verify(feature_repo, "HEAD")
    assert value.sha != original.sha
    assert value.tree_sha == original.tree_sha
    assert len(calls) == 2
    assert value.evidence is not None
    assert original.evidence is not None
    for stage in Stage:
        assert value.evidence[stage].origin == original.evidence[stage].origin
        assert value.evidence[stage].refreshed_at is not None
    assert attestation.verify(feature_repo, original.sha) == original


def test_undeclared_shell_dependencies_rerun_only_shell_stages(feature_repo, tmp_path):
    _prepare(feature_repo, contracts=False)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    _restack(feature_repo)
    resumed = ScriptedAgent(feature_repo)
    result = resumed.run("start")
    assert result["state"] == "DOCS_GREEN"
    assert result["data"]["applicability"]["lint"]["reasons"] == ["contract_undeclared"]
    resumed.run("stage", "run", "lint")
    resumed.run("stage", "run", "test")
    resumed.run("mergeback")
    assert attestation.verify(feature_repo, "HEAD").schema_version == 5


def test_old_protected_base_gets_legacy_format_and_fresh_stages(feature_repo, tmp_path):
    _prepare(feature_repo, consumer=False)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    assert original.schema_version == 4
    payload = json.loads(attestation.encode(original))
    assert "evidence" not in payload
    assert "config_snapshot" not in payload
    agent.run("abort", "--force")
    _restack(feature_repo)
    result = ScriptedAgent(feature_repo).run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"


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


def test_verifier_rejects_changed_origin_and_incomplete_provenance(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    payload = json.loads(attestation.encode(value))
    payload["evidence"]["lint"]["origin"]["finished_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(attestation.InvalidAttestation, match="digest"):
        attestation.decode(json.dumps(payload))
    payload = json.loads(attestation.encode(value))
    del payload["evidence"]["test"]
    with pytest.raises(attestation.InvalidAttestation, match="complete"):
        attestation.decode(json.dumps(payload))


def test_user_test_command_change_reuses_review_docs_and_lint(feature_repo, tmp_path, monkeypatch):
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
    assert result["state"] == "LINT_GREEN"
    assert result["data"]["applicability"]["test"]["reasons"] == ["command_changed"]
    assert agent.run("status")["state"] == "LINT_GREEN"
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    refreshed = attestation.verify(feature_repo, "HEAD")
    assert refreshed.evidence[Stage.LINT].origin == original.evidence[Stage.LINT].origin
    assert refreshed.evidence[Stage.TEST].origin.run_id != original.run_id


def test_later_evidence_survives_a_pending_review_and_restart(feature_repo, tmp_path, monkeypatch):
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
    user_config.write_text("[context]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    result = agent.run("start")
    assert result["state"] == "REVIEW_AWAITING_FINDINGS"
    assert result["data"]["applicability"]["test"]["disposition"] == "reusable"
    # A new CLI/session invocation recovers the durable later-stage candidates.
    agent = ScriptedAgent(feature_repo)
    agent.run("status")
    agent.run("context")
    payload = tmp_path / "fresh-review.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')
    result = agent.run("submit-findings", "--file", str(payload))
    assert result["state"] == "TEST_GREEN"
    agent.run("mergeback")
    refreshed = attestation.verify(feature_repo, "HEAD")
    assert refreshed.evidence[Stage.TEST].origin == original.evidence[Stage.TEST].origin
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
    assert result["state"] == "TEST_GREEN"
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


def test_unchanged_head_still_rechecks_declared_environment(feature_repo, tmp_path, monkeypatch):
    _prepare(feature_repo)
    config_path = feature_repo / ".agentic-preflight.toml"
    before, test_contract = config_path.read_text().split("[reuse.test]")
    config_path.write_text(
        before
        + "[reuse.test]"
        + test_contract.replace("environment = []", 'environment = ["TEST_MODE"]')
    )
    commit_all(feature_repo, "declare the test environment input")
    monkeypatch.setenv("TEST_MODE", "first")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    monkeypatch.setenv("TEST_MODE", "second")
    result = ScriptedAgent(feature_repo).run("start")
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head
    assert result["state"] == "LINT_GREEN"
    assert result["data"]["applicability"]["test"]["reasons"] == ["inputs_changed"]


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


def test_protected_configuration_enables_refresh_in_other_repositories(feature_repo, tmp_path):
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, ".agentic-preflight.toml", "[reuse]\nattestation_schema = 5\n")
    commit_all(feature_repo, "declare the deployed trusted consumer")
    git("switch", "feature/x", cwd=feature_repo)
    git("rebase", "main", cwd=feature_repo)
    _prepare(feature_repo, consumer=False)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    assert attestation.verify(feature_repo, "HEAD").schema_version == 5


def test_a_repair_reclassifies_preserved_later_evidence(feature_repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    agent.run("abort", "--force")
    user_config = home / ".config/agentic-preflight/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("[context]\nenabled = false\n")
    agent = ScriptedAgent(feature_repo)
    result = agent.run("start")
    assert result["data"]["applicability"]["test"]["disposition"] == "reusable"
    write(feature_repo, "repair.py", "changed = True\n")
    commit_all(feature_repo, "repair the source")
    result = ScriptedAgent(feature_repo).run("start")
    assert result["data"]["applicability"]["test"]["disposition"] == "invalid"
    assert "head_tree_changed" in result["data"]["applicability"]["test"]["reasons"]


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
        assert result["state"] == "TEST_GREEN"
        agent.run("mergeback")
        assert attestation.verify(feature_repo, "HEAD").schema_version == 5
        agent.run("abort", "--force")
    assert len(calls) == 4


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
