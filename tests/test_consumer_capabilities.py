"""Protected declarations and the bounded legacy rollout matrix."""

import pytest

from agentic_preflight import consumer_capabilities as capabilities
from tests.conftest import commit_all, git, write


@pytest.mark.parametrize(
    ("probe", "section", "key", "version", "path", "marker"),
    [
        (
            capabilities.base_supports_refresh,
            "reuse",
            "attestation_schema",
            5,
            "refresh_validation.py",
            "\nREFRESH_WIRE_VERSION = 5\n",
        ),
        (
            capabilities.consumer_installed,
            "ci",
            "consumer_schema",
            6,
            "ci_models.py",
            "\nclass TestDelegation(BaseModel):\n",
        ),
    ],
)
@pytest.mark.parametrize(
    ("declaration", "legacy", "expected"),
    [
        (None, False, False),
        (None, True, True),
        ("{version}", False, True),
        ("{version}", True, True),
        ("4", True, False),
        ("7", True, False),
        ('"{version}"', True, False),
        ("true", True, False),
        ("[", True, False),
    ],
)
def test_capability_matrix(
    tmp_repo,
    monkeypatch,
    probe,
    section,
    key,
    version,
    path,
    marker,
    declaration,
    legacy,
    expected,
):
    if declaration is not None:
        write(
            tmp_repo,
            ".agentic-preflight.toml",
            f"[{section}]\n{key} = {declaration.format(version=version)}\n",
        )
    if legacy:
        write(tmp_repo, f"agentic_preflight/{path}", marker)
    git("add", "-A", cwd=tmp_repo)
    git("commit", "--allow-empty", "-m", "consumer state", cwd=tmp_repo)
    run = capabilities.gitx.run
    calls = []

    def recorded(repo, *args, **kwargs):
        calls.append(args)
        return run(repo, *args, **kwargs)

    monkeypatch.setattr(capabilities.gitx, "run", recorded)
    assert probe(tmp_repo, "main") is expected
    if declaration is not None:
        assert not any("agentic_preflight/" in str(args) for args in calls)


def test_producer_and_uncommitted_declarations_cannot_enable_protected_base(feature_repo):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[reuse]\nattestation_schema = 5\n[ci]\nconsumer_schema = 6\n",
    )
    for probe in (capabilities.base_supports_refresh, capabilities.consumer_installed):
        assert not probe(feature_repo, "main")
        assert not probe(feature_repo, "HEAD")
    commit_all(feature_repo, "producer only")
    for probe in (capabilities.base_supports_refresh, capabilities.consumer_installed):
        assert not probe(feature_repo, "main")
        assert probe(feature_repo, "HEAD")


@pytest.mark.parametrize("policy", ["reuse = 5\nci = 6\n", "[reuse\n", "reuse = []\nci = []\n"])
def test_malformed_policy_cannot_fall_back_to_source(tmp_repo, policy):
    write(tmp_repo, ".agentic-preflight.toml", policy)
    write(tmp_repo, "agentic_preflight/refresh_validation.py", "\nREFRESH_WIRE_VERSION = 5\n")
    write(tmp_repo, "agentic_preflight/ci_models.py", "\nclass TestDelegation(BaseModel):\n")
    commit_all(tmp_repo, "malformed declarations with legacy source")
    assert not capabilities.base_supports_refresh(tmp_repo, "main")
    assert not capabilities.consumer_installed(tmp_repo, "main")
