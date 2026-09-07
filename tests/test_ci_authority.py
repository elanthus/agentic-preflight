"""Trusted API identities, attempts and integration subjects, without network access."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from agentic_preflight import ci_authority as authority
from agentic_preflight.ci_models import CISection
from agentic_preflight.ci_policy import parse_policy
from agentic_preflight.digests import json_digest
from agentic_preflight.github_api import APIUnavailable

BASE, HEAD, MERGE, TREE = (char * 40 for char in "abcd")
NOW = datetime(2026, 9, 6, tzinfo=UTC)
POLICY = """
[ci]
test_authority = "github_actions"
consumer_schema = 6
repository_id = 10
workflow_id = 20
check_app_id = 30
required_jobs = ["linux", "windows"]
"""


@pytest.mark.parametrize(
    "invalid",
    [
        "[review]\nexecutor = 'invalid'\n",
        "[policy]\nhuman_review_paths = ['../private']\n",
        "[approval]\nmode = 'environment'\nenvironment = ''\n",
    ],
)
def test_invalid_protected_models_keep_policy_error_boundary(invalid):
    with pytest.raises(ValueError, match="invalid protected policy"):
        parse_policy(POLICY + invalid)


class FakeGitHub:
    repository = "owner/repo"

    def __init__(self, policy=POLICY):
        self.policy = policy
        self.pull = {
            "number": 86,
            "state": "open",
            "mergeable": True,
            "merge_commit_sha": MERGE,
            "base": {"sha": BASE, "ref": "main", "repo": {"id": 10, "full_name": self.repository}},
            "head": {
                "sha": HEAD,
                "repo": {"id": 11, "full_name": "contributor/fork", "private": False},
            },
            "user": {"login": "author"},
            "auto_merge": None,
        }
        self.merge = {
            "sha": MERGE,
            "parents": [{"sha": BASE}, {"sha": HEAD}],
            "tree": {"sha": TREE},
        }
        self.workflow = {
            "id": 20,
            "path": ".github/workflows/preflight-tests.yml",
            "state": "active",
        }
        self.runs = []
        self.jobs = []
        self.reviews = []
        self.requests = []
        self.note_payload = ""
        self.change = None
        self.unavailable = False
        self.protected = True

    def scoped(self, repository, *, public=False):
        assert repository == self.pull["head"]["repo"]["full_name"]
        assert public is not self.pull["head"]["repo"]["private"]
        return self

    def contents(self, path, revision):
        assert path == ".agentic-preflight.toml"
        return self.policy

    def note(self, sha):
        return self.note_payload

    def request(self, path, *, method="GET", body=None):
        if self.unavailable:
            raise APIUnavailable("offline")
        self.requests.append((method, path, body))
        if self.change:
            self.change(path)
        if method == "POST":
            return None
        if path == "pulls/86":
            return copy.deepcopy(self.pull)
        if path.startswith("branches/"):
            return {
                "name": self.pull["base"]["ref"],
                "protected": self.protected,
                "commit": {"sha": self.pull["base"]["sha"]},
            }
        if path.startswith("git/commits/"):
            return copy.deepcopy(self.merge)
        if path == "actions/workflows/20":
            return copy.deepcopy(self.workflow)
        if path.startswith("environments/"):
            return {
                "name": "high-risk-review",
                "protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 1}]}],
            }
        if path.startswith("actions/runs/"):
            return copy.deepcopy(
                next(run for run in self.runs if run["id"] == int(path.split("/")[-1]))
            )
        raise AssertionError(path)

    def pages(self, path, key=None):
        if self.unavailable:
            raise APIUnavailable("offline")
        self.requests.append(("GET", path, None))
        if self.change:
            self.change(path)
        if path.startswith("actions/workflows/"):
            return copy.deepcopy(self.runs)
        if path.startswith("actions/runs/"):
            assert "/attempts/" in path  # Never aggregate old attempt results.
            return copy.deepcopy(self.jobs)
        if path == "pulls/86/reviews":
            return copy.deepcopy(self.reviews)
        raise AssertionError(path)

    def succeed(self):
        candidate, cfg, _ = authority.snapshot(self, 86)
        self.runs = [
            {
                "id": 100,
                "run_attempt": 1,
                "workflow_id": 20,
                "event": "workflow_dispatch",
                "path": cfg.ci.workflow_path,
                "head_sha": candidate.base_sha,
                "head_branch": "main",
                "repository": {"id": 10},
                "head_repository": {"id": 10},
                "display_title": candidate.title,
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/owner/repo/actions/runs/100",
            }
        ]
        names = {
            "prepare": ("Validate candidate",),
            "linux": authority.TEST_STEPS,
            "windows": authority.TEST_STEPS,
        }
        if cfg.approval.mode == "environment":
            names["approval"] = ("Environment released",)
        self.jobs = [
            {
                "id": index + 200,
                "name": name,
                "run_id": 100,
                "run_attempt": 1,
                "head_sha": candidate.base_sha,
                "status": "completed",
                "conclusion": "success",
                "completed_at": (NOW - timedelta(minutes=5)).isoformat(),
                "steps": [
                    {"name": step, "status": "completed", "conclusion": "success"} for step in steps
                ],
            }
            for index, (name, steps) in enumerate(names.items())
        ]
        return candidate, cfg


def test_complete_matrix_binds_actual_integration_subject():
    api = FakeGitHub()
    candidate, cfg = api.succeed()
    result = authority.evaluate_tests(api, candidate, cfg, now=NOW)
    assert result["status"] == "success"
    assert result["tested_sha"] == MERGE != HEAD
    assert result["tested_tree"] == TREE
    assert {item["name"] for item in result["jobs"]} == {"prepare", "linux", "windows"}
    assert result["policy_revision"] == BASE


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("head_sha", HEAD),
        ("event", "pull_request"),
        ("workflow_id", 21),
        ("path", ".github/workflows/fake.yml"),
        ("head_branch", "feature/x"),
        ("repository", {"id": 11}),
        ("head_repository", {"id": 11}),
        ("run_attempt", 0),
    ],
)
def test_job_names_cannot_substitute_untrusted_execution(key, bad):
    api = FakeGitHub()
    candidate, cfg = api.succeed()
    api.runs[0][key] = bad
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "stale"


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "skipped", "neutral", "timed_out", None]
)
def test_required_leg_must_succeed(conclusion):
    api = FakeGitHub()
    candidate, cfg = api.succeed()
    api.jobs[-1]["conclusion"] = conclusion
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "failed"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "step_skipped",
        "wrong_attempt",
        "wrong_run",
        "wrong_head",
        "expired",
        "future",
        "pending",
    ],
)
def test_incomplete_ambiguous_or_stale_jobs_fail_closed(change):
    api = FakeGitHub()
    candidate, cfg = api.succeed()
    job = api.jobs[-1]
    if change == "missing":
        api.jobs.pop()
    elif change == "duplicate":
        api.jobs.append(copy.deepcopy(job))
    elif change == "step_skipped":
        job["steps"][-1]["conclusion"] = "skipped"
    elif change == "wrong_attempt":
        job["run_attempt"] = 2
    elif change == "wrong_run":
        job["run_id"] = 99
    elif change == "wrong_head":
        job["head_sha"] = HEAD
    elif change == "expired":
        job["completed_at"] = (NOW - timedelta(days=2)).isoformat()
    elif change == "future":
        job["completed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    else:
        job["status"] = "queued"
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] != "success"


def test_latest_pending_run_supersedes_success_and_latest_attempt_cannot_inherit_jobs():
    api = FakeGitHub()
    candidate, cfg = api.succeed()
    api.runs.append({**api.runs[0], "id": 101, "status": "queued", "conclusion": None})
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "pending"
    api.runs.pop()
    api.runs[0]["run_attempt"] = 2
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "stale"
    for job in api.jobs:
        job["run_attempt"] = 2
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "success"


@pytest.mark.parametrize(
    "change", ["head", "base", "parents", "retarget", "repo", "mergeable", "policy"]
)
def test_candidate_changes_reject_old_success(change):
    api = FakeGitHub()
    old, _ = api.succeed()
    if change == "head":
        api.pull["head"]["sha"] = "e" * 40
        api.merge["parents"][1]["sha"] = "e" * 40
    elif change == "base":
        api.pull["base"]["sha"] = "e" * 40
        api.merge["parents"][0]["sha"] = "e" * 40
    elif change == "parents":
        api.merge["parents"].reverse()
    elif change == "retarget":
        api.pull["base"]["ref"] = "other"
    elif change == "repo":
        api.pull["base"]["repo"]["id"] = 12
    elif change == "mergeable":
        api.pull["mergeable"] = None
    else:
        api.policy += "max_age_seconds = 3600\n"
    if change in {"parents", "mergeable"}:
        reason = (
            "integration commit is stale"
            if change == "parents"
            else "integration commit is unavailable"
        )
        with pytest.raises(authority.CandidatePending, match=reason):
            authority.snapshot(api, 86)
    elif change in {"retarget", "repo"}:
        with pytest.raises(ValueError, match="repository or retargeted base") as rejected:
            authority.snapshot(api, 86)
        assert type(rejected.value) is ValueError
    else:
        current, cfg, _ = authority.snapshot(api, 86)
        assert current != old
        assert authority.evaluate_tests(api, current, cfg, now=NOW)["status"] == "pending"


def test_dispatch_is_idempotent_and_prepare_rejects_forged_inputs(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    # Dispatch uses the wall clock; keep it aligned with the recorded job times.
    monkeypatch.setattr(authority, "datetime", FixedDateTime)
    api = FakeGitHub()
    candidate, _ = api.succeed()
    assert authority.dispatch(api, 86)["dispatched"] is False
    assert not any(method == "POST" for method, _, _ in api.requests)
    assert authority.dispatch(api, 86, force=True)["dispatched"] is True
    _, _, body = api.requests[-1]
    assert body["ref"] == "main"
    assert (
        authority.prepare(
            api,
            body["inputs"]["candidate"],
            workflow_sha=BASE,
            candidate_id=body["inputs"]["candidate_id"],
        )
        == candidate
    )
    for update in ({"merge_sha": HEAD}, {"head_repository_id": 99}, {"pr": 87}):
        forged = candidate.model_copy(update=update)
        with pytest.raises((ValueError, AssertionError)):
            authority.prepare(
                api,
                forged.model_dump_json(),
                workflow_sha=BASE,
                candidate_id=json_digest(forged.model_dump()),
            )
    with pytest.raises(ValueError, match="forged"):
        authority.prepare(
            api,
            candidate.model_dump_json(),
            workflow_sha=HEAD,
            candidate_id=json_digest(candidate.model_dump()),
        )
    with pytest.raises(ValueError, match="forged"):
        authority.prepare(
            api, candidate.model_dump_json(), workflow_sha=BASE, candidate_id="f" * 64
        )


def test_no_run_and_api_outage_are_not_success():
    api = FakeGitHub()
    candidate, cfg, _ = authority.snapshot(api, 86)
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "pending"
    api.unavailable = True
    with pytest.raises(APIUnavailable):
        authority.evaluate_tests(api, candidate, cfg, now=NOW)


@pytest.mark.parametrize(
    "update",
    [
        {"repository_id": None},
        {"workflow_id": None},
        {"consumer_schema": None},
        {"required_jobs": []},
        {"required_jobs": ["prepare"]},
        {"required_jobs": ["x", "x"]},
        {"workflow_path": "../evil.yml"},
        {"max_age_seconds": 0},
    ],
)
def test_weak_or_malformed_authority_is_rejected(update):
    raw = parse_policy(POLICY).ci.model_dump()
    with pytest.raises(ValueError, match="validation error"):
        CISection.model_validate({**raw, **update})


def test_environment_requires_protected_environment_job():
    api = FakeGitHub(POLICY + '\n[approval]\nmode="environment"\n')
    candidate, cfg = api.succeed()
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "success"
    api.jobs.pop()
    assert authority.evaluate_tests(api, candidate, cfg, now=NOW)["status"] == "failed"


def test_unprotected_retarget_cannot_authorize_its_own_policy_or_dispatch():
    api = FakeGitHub()
    api.pull["base"]["ref"] = "attacker-base"
    api.policy += 'base_branch = "attacker-base"\n'
    api.protected = False
    with pytest.raises(ValueError, match="protected base branch"):
        authority.dispatch(api, 86)
    assert not any(method == "POST" for method, _, _ in api.requests)
