import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_short_ap_console_alias_is_not_packaged():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"agentic-preflight": "agentic_preflight.cli:main"}


def test_codeowners_protects_the_policy_and_verifier_surfaces():
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for path in (
        "/.agentic-preflight.toml",
        "/.github/CODEOWNERS",
        "/.github/workflows/",
        "/agentic_preflight/",
        "/agentic_preflight/risk.py",
        "/agentic_preflight/attestation.py",
        "/skill/",
    ):
        assert path in codeowners


def test_built_wheel_smoke_checks_the_removed_console_alias():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'test ! -e "$UV_TOOL_BIN_DIR/ap"' in workflow
