"""Hosted check publication, CLI recovery and shipped privilege separation."""

import copy
from importlib.resources import files
from pathlib import Path

import pytest

from agentic_preflight import attestation, ci_checks, cli_ci
from agentic_preflight.github_api import APIUnavailable
from tests.driver import ScriptedAgent


class CheckAPI:
    repository = "owner/repo"

    def __init__(self):
        self.checks = []
        self.writes = []
        self.pull = {
            "number": 86,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
            "merge_commit_sha": "c" * 40,
        }

    def pages(self, path, key=None):
        if path.startswith("pulls"):
            return [self.pull]
        assert path.startswith("commits/")
        assert key == "check_runs"
        subject = path.split("/")[1]
        return copy.deepcopy([item for item in self.checks if item.get("head_sha") == subject])

    def request(self, path, *, method="GET", body=None):
        if method == "GET":
            return copy.deepcopy(self.pull)
        self.writes.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            item = {**body, "id": max((item["id"] for item in self.checks), default=0) + 1}
            self.checks.append(item)
        else:
            item = next(item for item in self.checks if item["id"] == int(path.split("/")[-1]))
            item.update(body)
        item["app"] = {"id": 30}
        return copy.deepcopy(item)


def outcome(status="success"):
    return {
        "status": status,
        "repository": "owner/repo",
        "pr": 86,
        "merge_requirements_satisfied": status == "success",
        "reason": "test result",
        "candidate": {
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "base_branch": "main",
            "merge_sha": "c" * 40,
        },
        "tests": {"run_url": "https://github.com/owner/repo/actions/runs/1"},
    }


def test_reconcile_revokes_old_success_before_dispatch_and_does_not_publish_lagging_success(
    tmp_path, monkeypatch
):
    api = CheckAPI()
    ci_checks.publish_check(api, "a" * 40, outcome(), app_id=30)
    monkeypatch.setattr(ci_checks, "dispatch", lambda *args: {"dispatched": True})
    monkeypatch.setattr(ci_checks.ci_merge, "evaluate", lambda *args: outcome())
    result = ci_checks.reconcile(tmp_path, api, check_app_id=30)
    assert api.writes[1][2]["status"] == "in_progress"
    assert result[0]["merge_requirements_satisfied"] is False
    assert api.writes[-1][2]["status"] == "in_progress"
    assert api.writes[-1][0] == "PATCH"


@pytest.mark.parametrize(
    "status", ["success", "failed", "unavailable", "approval_pending", "expired", "stale"]
)
def test_reconcile_maps_explicit_verdicts_and_updates_existing_check(tmp_path, monkeypatch, status):
    api = CheckAPI()
    monkeypatch.setattr(ci_checks, "dispatch", lambda *args: {"dispatched": False})
    monkeypatch.setattr(ci_checks.ci_merge, "evaluate", lambda *args: outcome(status))
    result = ci_checks.reconcile(tmp_path, api, check_app_id=30, pr=86)
    assert result[0]["status"] == status
    body = api.writes[-1][2]
    assert body.get("conclusion") == (
        "success"
        if status == "success"
        else None
        if status in {"unavailable", "approval_pending"}
        else "failure"
    )
    assert "Run:" in body["output"]["summary"]
    assert len(api.checks) == 2
    head_check = next(item for item in api.checks if item["head_sha"] == "a" * 40)
    merge_check = next(item for item in api.checks if item["head_sha"] == "c" * 40)
    assert head_check["status"] == "in_progress"
    assert head_check.get("conclusion") is None
    assert merge_check["status"] == body["status"]


def test_reconcile_cannot_pass_after_last_head_or_base_change(tmp_path, monkeypatch):
    api = CheckAPI()
    monkeypatch.setattr(ci_checks, "dispatch", lambda *args: {"dispatched": False})

    def evaluated(*args):
        api.pull["head"]["sha"] = "c" * 40
        api.pull["base"]["sha"] = "d" * 40
        return outcome()

    monkeypatch.setattr(ci_checks.ci_merge, "evaluate", evaluated)
    assert ci_checks.reconcile(tmp_path, api, check_app_id=30)[0]["status"] == "stale"
    assert api.writes[-1][2]["conclusion"] == "failure"


def test_dispatch_outage_does_not_preserve_a_success(tmp_path, monkeypatch):
    api = CheckAPI()

    def unavailable(*args):
        raise APIUnavailable("offline")

    monkeypatch.setattr(ci_checks, "dispatch", unavailable)
    monkeypatch.setattr(ci_checks.ci_merge, "evaluate", lambda *args: outcome("unavailable"))
    assert ci_checks.reconcile(tmp_path, api, check_app_id=30)[0]["status"] == "unavailable"
    assert api.writes[-1][2]["status"] == "in_progress"


def test_check_name_from_another_app_cannot_be_used_as_our_authority():
    api = CheckAPI()
    api.checks = [
        {
            "id": 99,
            "app": {"id": 999},
            "external_id": "agentic-preflight-ci-v1:forged",
            "head_sha": "a" * 40,
        }
    ]
    ci_checks.publish_check(api, "a" * 40, outcome(), app_id=30)
    assert api.writes[-1][0] == "POST"
    assert api.checks[0]["app"] == {"id": 999}
    with pytest.raises(ValueError, match="dedicated GitHub App"):
        ci_checks.publish_check(api, "a" * 40, outcome(), app_id=999)


@pytest.mark.parametrize(
    "status",
    ["success", "pending", "failed", "unavailable", "stale", "expired", "approval_pending"],
)
def test_ci_status_cli_works_without_local_active_run(tmp_repo, monkeypatch, status):
    monkeypatch.setattr(cli_ci.ci_merge, "evaluate", lambda *args: outcome(status))
    result = ScriptedAgent(tmp_repo).run(
        "ci", "status", "--repo", "owner/repo", "--pr", "86", expect=0 if status == "success" else 3
    )
    assert result["data"]["status"] == status
    assert result["next"]["command"]


def test_templates_ship_and_never_overwrite(tmp_repo, tmp_path):
    agent = ScriptedAgent(tmp_repo)
    destination = tmp_path / "templates"
    agent.run("ci", "templates", "--directory", str(destination))
    original = (destination / "preflight-tests.yml").read_text()
    agent.run("ci", "templates", "--directory", str(destination), expect=2)
    assert (destination / "preflight-tests.yml").read_text() == original
    assert "run-name:" in original
    assert "inputs.candidate_id" in original
    assert "persist-credentials: false" in original
    assert "enable-cache: true" not in original
    tests = original.split("\n  test:", 1)[1].split("\n  approval:", 1)[0]
    assert "write" not in tests.replace("write credentials", "")
    assert "GH_TOKEN" not in tests
    assert "secrets." not in tests
    assert "Run delegated tests" in tests
    assert "Verify integration checkout" in tests
    assert "timeout-minutes: 30" in tests
    reconcile = (
        files("agentic_preflight").joinpath("templates", "ci", "preflight-ci.yml").read_text()
    )
    assert "ref: refs/heads/main" in reconcile
    assert "pull_request_target:" in reconcile
    assert "workflow_run:" in reconcile
    assert "cancel-in-progress: false" in reconcile
    assert "inputs.candidate" not in reconcile
    assert "download-artifact" not in reconcile


@pytest.mark.parametrize("name", ["preflight-tests.yml", "preflight-ci.yml"])
def test_templates_preserve_concurrently_created_destination(tmp_repo, tmp_path, monkeypatch, name):
    destination = tmp_path / "templates"
    contested = destination / name
    original_open = Path.open
    raced = False

    def competing_open(path, mode="r", *args, **kwargs):
        nonlocal raced
        if path == contested and mode in {"w", "x"} and not raced:
            raced = True
            with original_open(path, "w", encoding="utf-8") as handle:
                handle.write("concurrently created workflow\n")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)
    result = ScriptedAgent(tmp_repo).run(
        "ci", "templates", "--directory", str(destination), expect=2
    )
    assert raced
    assert "already exists" in result["error"]["message"]
    assert contested.read_text() == "concurrently created workflow\n"


@pytest.mark.parametrize("delegated", [True, False])
def test_verify_pending_guidance_uses_exception_type_not_message(tmp_repo, monkeypatch, delegated):
    def reject(*args, **kwargs):
        if delegated:
            raise attestation.DelegatedTestsPending("Remote execution is incomplete")
        raise attestation.InvalidAttestation("invalid evidence even though tests are delegated")

    monkeypatch.setattr(attestation, "verify", reject)
    result = ScriptedAgent(tmp_repo).run("verify", "HEAD", expect=2)
    if delegated:
        assert result["data"]["test_status"] == "delegated_pending"
        assert result["next"]["command"] is None
        assert "ci status --repo OWNER/REPO --pr N" in result["next"]["instruction"]
    else:
        assert "test_status" not in result["data"]
        assert result["next"]["command"].startswith("git fetch origin")
