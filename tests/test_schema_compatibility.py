"""Wire formats retain distinct meanings through the compatibility boundary."""

import copy
import json

import pytest

from agentic_preflight import attestation
from agentic_preflight.ci_policy import parse_policy
from agentic_preflight.config import Config, config_digest
from tests.driver import ScriptedAgent
from tests.test_ci_authority import POLICY
from tests.test_evidence_refresh import _finish, _prepare


def test_wire_round_trips_and_cross_version_rejection(feature_repo, tmp_path):
    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    v5 = json.loads(attestation.encode(attestation.verify(feature_repo, "HEAD")))
    v4 = {key: value for key, value in v5.items() if key not in {"evidence", "config_snapshot"}}
    v4["schema_version"] = 4
    v6 = copy.deepcopy(v5)
    v6.update(
        schema_version=6,
        green_at=None,
        publication_ready_at="2026-09-06T00:00:00Z",
        test_delegation={
            "subject": "integration",
            "policy_revision": v5["merge_base_sha"],
            "policy": parse_policy(POLICY).ci.model_dump(mode="json"),
        },
    )
    v6["evidence"].pop("test")
    v6["stages"]["test"] = dict.fromkeys(v5["stages"]["test"])
    v6["stages"]["test"].update(status="delegated", reason="trusted CI tests pending")
    for version, raw in ((4, v4), (5, v5), (6, v6)):
        wire = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        restored = attestation.decode(wire)
        assert restored.schema_version == version
        assert attestation.encode(restored) == wire
        assert (restored.green_at is None) == (version == 6)
        assert (restored.stages["test"].status == "delegated") == (version == 6)
        for unsupported in (0, 3, 7):
            with pytest.raises(attestation.InvalidAttestation) as error:
                attestation.decode(json.dumps({**raw, "schema_version": unsupported}))
            assert error.value.reason == "incompatible_schema"
        for other_version in {4, 5, 6} - {version}:
            with pytest.raises(attestation.InvalidAttestation):
                attestation.decode(json.dumps({**raw, "schema_version": other_version}))


def test_default_snapshot_digest_remains_legacy_compatible():
    cfg = Config()
    snapshot = cfg.model_dump(mode="json")
    assert "ci" not in snapshot
    assert Config.model_validate(snapshot).model_dump(mode="json") == snapshot
    # Fixed against the pre-refactor default snapshot, including default omission.
    assert (
        config_digest(snapshot)
        == "005c1b0c4fbb79feb1b1ba45aaf5f83f5db4e0325431b5aacf07d4104e54b15e"
    )
    cfg.ci.consumer_schema = 6
    explicit = cfg.model_dump(mode="json")
    assert explicit["ci"]["consumer_schema"] == 6
    assert config_digest(explicit) != config_digest(snapshot)
