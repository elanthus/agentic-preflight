"""Expensive review inputs are computed a bounded number of times per command.

These tests count calls rather than time them. Each CLI command opens one
``Session``, and the session memo lets every consumer in that command share one
diff bundle, one grounding assembly, and one documentation inventory walk for
the snapshot it is looking at.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

import pytest

from agentic_preflight import diff as diffmod
from agentic_preflight import grounding, refresh_validation
from agentic_preflight.models import Stage
from agentic_preflight.runs import evidence, review_protocol
from agentic_preflight.runs._session import Session, _load_current, open_session
from agentic_preflight.stages import docs as docsstage
from agentic_preflight.stages import shellstage
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _prepare(repo) -> None:
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    body = '[worktree]\nmode = "in_place"\n' + f"[commands]\nlint = {command}\ntest = {command}\n"
    for stage in ("lint", "test"):
        body += f'[reuse.{stage}]\nmode = "content"\nfiles = []\nenvironment = []\ntoolchain = []\n'
    write(repo, ".agentic-preflight.toml", body)
    write(repo, "AGENTS.md", "# Agents\n\nEdit src/app.py carefully.\n")
    write(repo, "docs/guide.md", "# Guide\n\nThe app.py module defines greet.\n")
    commit_all(repo, "configure evidence reuse")


class CallCounts:
    """Count expensive operations, keyed by name, for the command in flight."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._manifest_depth = 0

    def take(self) -> Counter[str]:
        counts, self.counts = self.counts, Counter()
        return counts


@pytest.fixture
def call_counts(monkeypatch) -> CallCounts:
    counter = CallCounts()
    assemble = grounding.assemble
    build_bundle = diffmod.build_bundle
    build_inventory = docsstage.build_inventory
    manifest = refresh_validation._manifest

    def counted_assemble(*args, **kwargs):
        counter.counts["grounding"] += 1
        return assemble(*args, **kwargs)

    def counted_bundle(*args, **kwargs):
        # Verifying stored evidence rebuilds the original and current manifests
        # from Git. Those bundles describe other snapshots, so they are counted
        # apart from the review bundle for the command's own snapshot.
        key = "manifest bundle" if counter._manifest_depth else "bundle"
        counter.counts[key] += 1
        return build_bundle(*args, **kwargs)

    def counted_inventory(*args, **kwargs):
        counter.counts["inventory"] += 1
        return build_inventory(*args, **kwargs)

    def counted_manifest(*args, **kwargs):
        counter.counts["manifest pair half"] += 1
        counter._manifest_depth += 1
        try:
            return manifest(*args, **kwargs)
        finally:
            counter._manifest_depth -= 1

    monkeypatch.setattr(grounding, "assemble", counted_assemble)
    monkeypatch.setattr(diffmod, "build_bundle", counted_bundle)
    monkeypatch.setattr(docsstage, "build_inventory", counted_inventory)
    monkeypatch.setattr(refresh_validation, "_manifest", counted_manifest)
    return counter


LIFECYCLE = (
    ("start",),
    ("context",),
    ("submit-findings", "review"),
    ("context", "--section", "docs"),
    ("submit-findings", "docs"),
    ("status",),
    ("stage", "run", "lint"),
    ("stage", "run", "test"),
    ("mergeback",),
)


def _drive(agent: ScriptedAgent, tmp_path, call_counts: CallCounts) -> dict[str, Counter[str]]:
    payload = tmp_path / "findings.json"
    per_command: dict[str, Counter[str]] = {}
    call_counts.take()
    for step in LIFECYCLE:
        if step[0] == "submit-findings":
            payload.write_text(
                '{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}'
                if step[1] == "review"
                else '{"findings":[]}'
            )
            agent.run("submit-findings", "--file", str(payload))
        else:
            agent.run(*step)
        per_command[" ".join(step)] = call_counts.take()
    return per_command


def test_each_command_builds_review_inputs_once(feature_repo, tmp_path, call_counts):
    _prepare(feature_repo)
    per_command = _drive(ScriptedAgent(feature_repo), tmp_path, call_counts)

    for command, counts in per_command.items():
        assert counts["grounding"] <= 1, (command, counts)
        assert counts["bundle"] <= 1, (command, counts)
        assert counts["inventory"] <= 1, (command, counts)
    # The commands that compare or record review and docs fingerprints really do
    # use these inputs, so "at most once" is exactly once for them.
    for command in (
        "submit-findings review",
        "context --section docs",
        "submit-findings docs",
        "status",
        "stage run lint",
    ):
        assert per_command[command]["grounding"] == 1, command
        assert per_command[command]["bundle"] == 1, command
    for command in ("context --section docs", "submit-findings docs"):
        assert per_command[command]["inventory"] == 1, command


def test_stage_command_clears_memo_before_after_fingerprint(feature_repo, tmp_path, monkeypatch):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    payload = tmp_path / "findings.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))

    events: list[str] = []
    run_stage = shellstage.run_stage
    clear_memo = Session.clear_memo
    fingerprint = evidence.fingerprint

    def recorded_run_stage(*args, **kwargs):
        events.append("command")
        return run_stage(*args, **kwargs)

    def recorded_clear(self):
        events.append("clear")
        return clear_memo(self)

    def recorded_fingerprint(*args, **kwargs):
        events.append("fingerprint")
        return fingerprint(*args, **kwargs)

    monkeypatch.setattr(shellstage, "run_stage", recorded_run_stage)
    monkeypatch.setattr(Session, "clear_memo", recorded_clear)
    monkeypatch.setattr(evidence, "fingerprint", recorded_fingerprint)
    agent.run("stage", "run", "lint")

    command = events.index("command")
    assert events[command + 1] == "clear"
    assert "fingerprint" in events[command + 2 :]


def test_memo_follows_head_within_one_session(feature_repo):
    _prepare(feature_repo)
    ScriptedAgent(feature_repo).run("start")
    session = open_session(feature_repo)
    run = _load_current(session)

    first = review_protocol.bundle_for(session, run)
    grounded = review_protocol.grounding_for(session, run, first.files)
    assert review_protocol.bundle_for(session, run) is first
    assert review_protocol.grounding_for(session, run, first.files) is grounded

    write(feature_repo, "src/extra.py", "VALUE = 1\n")
    commit_all(feature_repo, "add extra module")

    second = review_protocol.bundle_for(session, run)
    assert "src/extra.py" in second.files
    assert "src/extra.py" not in first.files
    assert review_protocol.grounding_for(session, run, first.files) is not grounded


def test_docs_fingerprint_without_surface_matches_memoized_surface(feature_repo):
    _prepare(feature_repo)
    ScriptedAgent(feature_repo).run("start")
    session = open_session(feature_repo)
    run = _load_current(session)
    head = review_protocol._snapshot(session, run, None)[1]
    bundle = review_protocol.bundle_for(session, run)
    arguments = {
        "base_sha": run.merge_base_sha,
        "head_sha": head,
        "changed_files": bundle.files,
        "doc_paths": session.config.docs.paths,
        "config_snapshot": run.config_snapshot,
    }
    from agentic_preflight.fingerprints import compute_docs_fingerprint

    assert compute_docs_fingerprint(run.worktree_path, **arguments) == compute_docs_fingerprint(
        run.worktree_path,
        **arguments,
        surface=review_protocol.docs_surface(session, run, bundle.files),
    )


def _restack(repo) -> None:
    base = git("rev-parse", "main", cwd=repo)
    tree = git("rev-parse", "main^{tree}", cwd=repo)
    new = git("commit-tree", tree, "-p", base, "-m", "history only", cwd=repo)
    git("update-ref", "refs/heads/main", new, base, cwd=repo)


def _complete(agent: ScriptedAgent, payload, start: dict) -> None:
    """Follow ``next.command`` from ``start`` through a verified merge-back."""
    envelope = start
    while envelope["state"] != "VERIFIED":
        command = envelope["next"]["command"]
        if command.startswith("agentic-preflight submit-findings"):
            payload.write_text(
                '{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}'
                if envelope["state"] == "REVIEW_AWAITING_FINDINGS"
                else '{"findings":[]}'
            )
            envelope = agent.run("submit-findings", "--file", str(payload))
        else:
            envelope = agent.run(*command.split()[1:])


def test_reuse_start_cost_does_not_grow_with_prior_runs(
    feature_repo, tmp_path, call_counts, monkeypatch
):
    _prepare(feature_repo)
    verify_stage = evidence.verify_stage
    fingerprint = evidence.fingerprint
    starts: dict[int, Counter[str]] = {}

    def counted_verify(*args, **kwargs):
        call_counts.counts["verify_stage"] += 1
        return verify_stage(*args, **kwargs)

    def counted_fingerprint(*args, **kwargs):
        call_counts.counts["fingerprint"] += 1
        return fingerprint(*args, **kwargs)

    monkeypatch.setattr(evidence, "verify_stage", counted_verify)
    monkeypatch.setattr(evidence, "fingerprint", counted_fingerprint)
    payload = tmp_path / "findings.json"
    agent = ScriptedAgent(feature_repo)
    _complete(agent, payload, agent.run("start"))
    agent.run("abort", "--force")

    for prior in (1, 2, 3, 4):
        _restack(feature_repo)
        agent = ScriptedAgent(feature_repo)
        call_counts.take()
        envelope = agent.run("start")
        starts[prior] = call_counts.take()
        assert envelope["state"] == "TEST_GREEN"
        _complete(agent, payload, envelope)
        agent.run("abort", "--force")

    for prior, counts in starts.items():
        # `discover` computes each stage's current fingerprint at most once, and
        # `advance` once more to import it; shell fingerprints are never reused.
        assert counts["fingerprint"] <= 8, (prior, counts)
        assert counts["grounding"] <= 2, (prior, counts)
    assert starts[2] == starts[3] == starts[4]


def test_older_reusable_evidence_still_replaces_newer_unusable_evidence(feature_repo, tmp_path):
    _prepare(feature_repo)
    payload = tmp_path / "findings.json"
    first = ScriptedAgent(feature_repo)
    _complete(first, payload, first.run("start", "--intent", "the original objective"))
    original_run = first.steps[0].envelope["run_id"]
    first.run("abort", "--force")

    _restack(feature_repo)
    second = ScriptedAgent(feature_repo)
    started = second.run("start", "--intent", "a different objective")
    assert started["data"]["applicability"]["review"]["disposition"] == "invalid"
    _complete(second, payload, started)
    second.run("abort", "--force")

    _restack(feature_repo)
    third = ScriptedAgent(feature_repo)
    resumed = third.run("start", "--intent", "the original objective")
    assert resumed["data"]["applicability"]["review"]["disposition"] == "reusable"
    run = Store(feature_repo / ".git" / "agentic-preflight").load_run(resumed["run_id"])
    assert run.reuse_candidates[Stage.REVIEW].origin.run_id == original_run
    assert run.reuse_candidates[Stage.DOCS].origin.run_id == original_run
