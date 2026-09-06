"""Publication may precede CI without manufacturing a local test success."""

import json
import sys
from datetime import datetime

import pytest

from agentic_preflight import attestation, ci_authority, ci_merge
from agentic_preflight.cli_policy import _has_valid_attestation
from agentic_preflight.models import Stage
from agentic_preflight.stages import shellstage
from tests.conftest import commit_all, git, set_home, write
from tests.driver import ScriptedAgent
from tests.test_ci_authority import NOW, POLICY, FakeGitHub
from tests.test_evidence_refresh import _finish, _restack


def configure(repo, *, mode="in_place", enabled=True, approval="manual_merge"):
    git("switch", "main", cwd=repo)
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    policy = POLICY if enabled else ""
    policy += (
        f'\n[worktree]\nmode="{mode}"\n'
        f"[commands]\nlint={command}\ntest={command}\n"
        f'[approval]\nmode="{approval}"\n'
        '[policy]\nhuman_review_paths=["src/**"]\n'
        '[reuse.lint]\nmode="content"\nfiles=[]\nenvironment=[]\ntoolchain=[]\n'
    )
    write(repo, ".agentic-preflight.toml", policy)
    write(repo, "agentic_preflight/refresh_validation.py", "\nREFRESH_WIRE_VERSION = 5\n")
    commit_all(repo, "install protected consumer and policy")
    git("switch", "feature/x", cwd=repo)
    git("rebase", "main", cwd=repo)
    return policy


@pytest.fixture
def delegated(feature_repo, tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    policy = configure(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD", purpose="publish")
    return feature_repo, agent, policy, value


def remote_for(repo, policy, value):
    api = FakeGitHub(policy)
    api.note_payload = attestation.encode(value)
    api.pull["base"]["sha"] = value.merge_base_sha
    api.pull["head"]["sha"] = value.sha
    merge = git(
        "commit-tree",
        value.tree_sha,
        "-p",
        value.merge_base_sha,
        "-p",
        value.sha,
        "-m",
        "integration",
        cwd=repo,
    )
    api.pull["merge_commit_sha"] = merge
    api.merge = {
        "sha": merge,
        "parents": [{"sha": value.merge_base_sha}, {"sha": value.sha}],
        "tree": {"sha": value.tree_sha},
    }
    api.succeed()
    return api


@pytest.fixture
def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(ci_authority, "datetime", FixedDatetime)


@pytest.mark.parametrize("mode", ["in_place", "reusable", "strict"])
def test_zero_local_test_executions_and_explicit_pending_schema(
    feature_repo, tmp_path, monkeypatch, mode
):
    set_home(monkeypatch, tmp_path / "home")
    configure(feature_repo, mode=mode)
    calls = []
    execute = shellstage.run_stage

    def counted(*args, **kwargs):
        calls.append(args[1])
        return execute(*args, **kwargs)

    monkeypatch.setattr(shellstage, "run_stage", counted)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    result = _finish(agent, tmp_path)
    assert len(calls) == 1  # Actual lint process only; no local test process.
    assert result["state"] == "PUBLICATION_READY"
    assert result["data"]["merge_requirements_satisfied"] is False
    value = attestation.verify(feature_repo, "HEAD", purpose="publish")
    assert value.schema_version == 6
    assert value.green_at is None
    assert value.publication_ready_at is not None
    assert set(value.evidence) == {Stage.REVIEW, Stage.DOCS, Stage.LINT}
    assert value.stages[Stage.TEST].status == "delegated"
    restored = ScriptedAgent(feature_repo).run("status")
    record = restored["data"]["stages"]["test"]
    for key in ("command", "exit_code", "output_sha256", "finished_at", "fingerprint"):
        assert record[key] is None
    assert _has_valid_attestation(feature_repo, value.sha)
    with pytest.raises(attestation.DelegatedTestsPending, match="tests are delegated"):
        attestation.verify(feature_repo, "HEAD")
    assert agent.run("verify", "HEAD", "--purpose", "publish")["data"]["purpose"] == "publish"
    agent.run("verify", "HEAD", expect=2)


def test_default_still_runs_tests_and_preserves_legacy_wire(feature_repo, tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    configure(feature_repo, enabled=False)
    calls = []
    execute = shellstage.run_stage

    def counted(*args, **kwargs):
        calls.append(args[1])
        return execute(*args, **kwargs)

    monkeypatch.setattr(shellstage, "run_stage", counted)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    assert _finish(agent, tmp_path)["state"] == "VERIFIED"
    assert len(calls) == 2
    payload = json.loads(attestation.encode(attestation.verify(feature_repo, "HEAD")))
    assert "test_delegation" not in payload
    assert "publication_ready_at" not in payload
    assert "ci" not in payload["config_snapshot"]


def test_producer_rollout_before_base_consumer_runs_local_tests(
    feature_repo, tmp_path, monkeypatch
):
    set_home(monkeypatch, tmp_path / "home")
    configure(feature_repo, enabled=False)
    with (feature_repo / ".agentic-preflight.toml").open("a") as handle:
        handle.write(POLICY)
    commit_all(feature_repo, "propose CI authority before protected rollout")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    assert _finish(agent, tmp_path)["state"] == "VERIFIED"
    value = attestation.verify(feature_repo, "HEAD")
    assert value.schema_version == 4  # Older protected consumer cannot parse [ci].
    assert value.stages[Stage.TEST].status == "green"
    assert value.test_delegation is None


@pytest.mark.parametrize(
    "mutation", ["green", "fake_process", "missing_lint", "legacy", "policy", "review_coverage"]
)
def test_partial_notes_cannot_claim_complete_or_weaken_local_evidence(delegated, mutation):
    repo, _, _, value = delegated
    raw = value.model_dump(mode="json")
    if mutation == "green":
        raw["green_at"] = NOW.isoformat()
    elif mutation == "fake_process":
        raw["stages"]["test"]["exit_code"] = 0
    elif mutation == "missing_lint":
        raw["evidence"].pop("lint")
    elif mutation == "legacy":
        raw["schema_version"] = 5
    elif mutation == "policy":
        raw["test_delegation"]["policy"]["required_jobs"] = ["linux"]
    else:
        raw["stages"]["review"]["coverage"]["head_sha"] = "f" * 40
    with pytest.raises((ValueError, attestation.InvalidAttestation)):
        attestation.verify_value(
            repo, attestation.decode(json.dumps(raw)), value.sha, purpose="publish"
        )


def test_restored_publication_pushes_pending_note_then_remote_completion_without_new_note(
    delegated, bare_remote, fixed_clock
):
    repo, _, policy, value = delegated
    # The bare remote fixture initially had main before protected policy installation.
    git("push", "origin", "main", cwd=repo)
    agent = ScriptedAgent(repo)
    gate = agent.run("gate")
    assert gate["data"]["test_status"] == "delegated_pending"
    agent.run("push", "--confirm", gate["data"]["token"])
    finished = agent.run("finish")
    assert finished["data"]["merge_requirements_satisfied"] is False
    notes_tip = git("rev-parse", attestation.NOTES_REF, cwd=repo)
    api = remote_for(repo, policy, value)
    api.runs[0]["status"] = "queued"
    assert ci_merge.evaluate(repo, api, 86)["status"] == "pending"
    api.runs[0]["status"] = "completed"
    result = ci_merge.evaluate(repo, api, 86)
    assert result["merge_requirements_satisfied"] is True, result
    assert result["approval"]["manual_merge_required"] is True
    assert git("rev-parse", attestation.NOTES_REF, cwd=repo) == notes_tip
    assert ScriptedAgent(repo).run("status")["data"]["has_run"] is False


def test_remote_verifier_requires_current_human_handling_and_fails_unknown(delegated, fixed_clock):
    repo, _, policy, value = delegated
    api = remote_for(repo, policy, value)
    api.pull["auto_merge"] = {"enabled_by": "agent"}
    assert ci_merge.evaluate(repo, api, 86)["status"] == "approval_pending"
    api.pull["auto_merge"] = None
    api.unavailable = True
    assert ci_merge.evaluate(repo, api, 86)["status"] == "unavailable"


@pytest.mark.parametrize("change", ["attempt", "base", "policy", "head", "note", "reviews"])
def test_changes_during_evaluation_cannot_produce_success(delegated, fixed_clock, change):
    repo, _, policy, value = delegated
    api = remote_for(repo, policy, value)
    calls = 0

    def mutate(path):
        nonlocal calls
        if path != "pulls/86/reviews":
            return
        calls += 1
        if calls != 1:
            return
        if change == "attempt":
            api.runs[0]["run_attempt"] = 2
            api.runs[0]["status"] = "queued"
        elif change == "base":
            api.pull["base"]["sha"] = "e" * 40
        elif change == "policy":
            api.policy = api.policy.replace(
                'required_jobs = ["linux", "windows"]', 'required_jobs = ["linux"]'
            )
        elif change == "head":
            api.pull["head"]["sha"] = "e" * 40
        elif change == "note":
            api.note_payload = "{}"
        else:
            # First read observes this update; change it again at the next read.
            api.reviews.append({"id": 1})
            api.change = lambda path: (
                api.reviews.append({"id": 2}) if path == "pulls/86/reviews" else None
            )

    api.change = mutate
    assert ci_merge.evaluate(repo, api, 86)["merge_requirements_satisfied"] is False


def test_base_advance_requests_fresh_integration_without_model_review(delegated, fixed_clock):
    repo, _, policy, value = delegated
    api = remote_for(repo, policy, value)
    _restack(repo)
    current_base = git("rev-parse", "main", cwd=repo)
    # Same head and contribution; the integration has a different base parent.
    merge = git(
        "commit-tree",
        value.tree_sha,
        "-p",
        current_base,
        "-p",
        value.sha,
        "-m",
        "updated integration",
        cwd=repo,
    )
    api.pull["base"]["sha"] = current_base
    api.pull["merge_commit_sha"] = merge
    api.merge = {
        "sha": merge,
        "parents": [{"sha": current_base}, {"sha": value.sha}],
        "tree": {"sha": value.tree_sha},
    }
    result = ci_merge.evaluate(repo, api, 86)
    assert result["status"] == "pending", result
    assert result["publication_ready"] is True
    api.succeed()
    assert ci_merge.evaluate(repo, api, 86)["merge_requirements_satisfied"] is True


def test_same_head_restart_reuses_local_stages_but_never_imports_a_test_pass(delegated, tmp_path):
    repo, agent, _, _ = delegated
    agent.run("abort", "--force")
    result = ScriptedAgent(repo).run("start")
    assert result["state"] == "LINT_GREEN", result
    restarted = ScriptedAgent(repo)
    assert restarted.run("stage", "run", "test")["state"] == "TEST_DELEGATED"
    assert restarted.run("mergeback")["state"] == "PUBLICATION_READY"


def test_protected_ci_policy_update_needs_new_ci_without_a_new_local_note(delegated, fixed_clock):
    repo, _, policy, value = delegated
    api = remote_for(repo, policy, value)
    updated = policy.replace("check_app_id = 30", "check_app_id = 30\nmax_age_seconds = 3600")
    git("switch", "main", cwd=repo)
    write(repo, ".agentic-preflight.toml", updated)
    base = commit_all(repo, "update protected CI freshness policy")
    git("switch", "feature/x", cwd=repo)
    tree = git("merge-tree", "--write-tree", base, value.sha, cwd=repo)
    merge = git(
        "commit-tree", tree, "-p", base, "-p", value.sha, "-m", "new policy integration", cwd=repo
    )
    api.policy = updated
    api.pull["base"]["sha"] = base
    api.pull["merge_commit_sha"] = merge
    api.merge = {
        "sha": merge,
        "parents": [{"sha": base}, {"sha": value.sha}],
        "tree": {"sha": tree},
    }
    result = ci_merge.evaluate(repo, api, 86)
    assert result["publication_ready"] is True, result
    assert result["status"] == "pending"
    api.succeed()
    assert ci_merge.evaluate(repo, api, 86)["merge_requirements_satisfied"] is True
    assert attestation.verify(repo, value.sha, purpose="publish") == value


def test_peer_approval_uses_current_head_and_trusted_reviews(
    feature_repo, tmp_path, monkeypatch, fixed_clock
):
    set_home(monkeypatch, tmp_path / "home")
    policy = configure(feature_repo, approval="peer_review")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD", purpose="publish")
    api = remote_for(feature_repo, policy, value)
    assert ci_merge.evaluate(feature_repo, api, 86)["status"] == "approval_pending"
    review = {
        "id": 1,
        "user": {"login": "peer", "type": "User"},
        "state": "APPROVED",
        "author_association": "MEMBER",
        "commit_id": value.sha,
    }
    api.reviews = [review]
    assert ci_merge.evaluate(feature_repo, api, 86)["merge_requirements_satisfied"] is True
    api.reviews.append({**review, "id": 2, "state": "CHANGES_REQUESTED"})
    assert ci_merge.evaluate(feature_repo, api, 86)["status"] == "approval_pending"


@pytest.mark.parametrize("schema", [5, 6])
def test_ci_rejects_lint_override_even_with_matching_config(delegated, fixed_clock, schema):
    from agentic_preflight.digests import json_digest
    from agentic_preflight.refresh_validation import json_digest_command

    repo, _, policy, value = delegated
    raw = value.model_dump(mode="json")
    override = f'"{sys.executable}" -c "print(2)"'
    raw["stages"]["lint"]["command"] = override
    lint = raw["evidence"]["lint"]
    lint["origin"]["result"]["command"] = override
    lint["origin"]["fingerprint"]["command_sha256"] = json_digest_command(override)
    lint["fingerprint"]["command_sha256"] = json_digest_command(override)
    lint["origin_sha256"] = json_digest(lint["origin"])
    if schema == 5:
        raw["schema_version"] = 5
        raw["green_at"] = NOW.isoformat()
        raw["publication_ready_at"] = None
        raw["test_delegation"] = None
        # Supply valid local test provenance, independent of the overridden lint.
        import copy

        test = copy.deepcopy(lint)
        test["origin"]["stage"] = "test"
        from agentic_preflight.config import Config
        from agentic_preflight.refresh_validation import shell_execution_config

        cfg = Config.model_validate(raw["config_snapshot"])
        test_command = cfg.commands.test
        test["origin"]["result"]["command"] = test_command
        for fp in (test["fingerprint"], test["origin"]["fingerprint"]):
            fp["command_sha256"] = json_digest_command(test_command)
            fp["config_sha256"] = json_digest(
                {
                    "execution": shell_execution_config(raw["config_snapshot"], Stage.TEST),
                    "contract": None,
                }
            )
        test["origin_sha256"] = json_digest(test["origin"])
        raw["evidence"]["test"] = test
        raw["stages"]["test"] = test["origin"]["result"]
    overridden = attestation.decode(json.dumps(raw))
    if schema == 5:
        assert (
            attestation.verify_value(repo, overridden, value.sha, purpose="publish") == overridden
        )
    api = remote_for(repo, policy, overridden)
    result = ci_merge.evaluate(repo, api, 86)
    assert result["status"] == "stale"
    assert result["reason"] == (
        "local lint execution differs from protected-base command"
        if schema == 5
        else "current shell command differs from configured command"
    )
    assert result["merge_requirements_satisfied"] is False
