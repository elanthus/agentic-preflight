"""How one ``gh attestation verify`` result maps to a signature state."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from agentic_preflight import signature
from agentic_preflight.envelope import ExitCode
from agentic_preflight.signature import classify
from tests.conftest import configure_base, git
from tests.driver import ScriptedAgent, findings_json

NOT_FOUND = (
    "\nError: HTTP 404: Not Found "
    "(https://api.github.com/repos/o/r/attestations/sha256:00?per_page=30)\n"
)


def test_a_verified_attestation_is_verified() -> None:
    assert classify(0, '[{"verificationResult": {}}]', "") == ("verified", None)


@pytest.mark.parametrize("stdout", ["[]", "{}", "null", "", "not json"])
def test_success_without_a_verified_attestation_is_invalid(stdout: str) -> None:
    state, detail = classify(0, stdout, "")
    assert state == "invalid"
    assert detail


def test_nothing_stored_for_the_digest_is_absent() -> None:
    state, detail = classify(1, "", NOT_FOUND)
    assert state == "absent"
    # The 404 does not say whether the signature is missing or hidden.
    assert detail is not None
    assert "cannot show it" in detail


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: HTTP 403: API rate limit exceeded (https://api.github.com/x)",
        "Error: HTTP 502: Bad Gateway (https://api.github.com/x)",
        "error connecting to api.github.com\ncheck your internet connection",
    ],
)
def test_a_failed_fetch_is_absent_and_names_the_cause(stderr: str) -> None:
    state, detail = classify(1, "", stderr)
    assert state == "absent"
    assert detail is not None
    assert stderr.splitlines()[0][:40] in detail


def test_a_failure_after_the_fetch_is_invalid() -> None:
    state, detail = classify(1, "", "Error: verifying with issuer: signature mismatch\n")
    assert state == "invalid"
    assert detail == "Error: verifying with issuer: signature mismatch"


def test_a_silent_failure_is_invalid() -> None:
    assert classify(2, "", "") == ("invalid", "gh exited 2 without a message")


def test_a_long_reason_is_cut() -> None:
    _, detail = classify(1, "", "x" * 500)
    assert detail is not None
    assert len(detail) == 200


# --- verify SHA --signature, with a fake gh on PATH ---------------------------------

PROTECTED = "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_GH_DIR/argv"
cp "$3" "$FAKE_GH_DIR/subject"
printf '%s' "$FAKE_GH_STDOUT"
printf '%s' "$FAKE_GH_STDERR" >&2
exit "$FAKE_GH_EXIT"
"""


@pytest.fixture
def attested(feature_repo: Path, tmp_path: Path) -> Path:
    """A repository whose head carries a verified note."""
    configure_base(feature_repo, PROTECTED)
    agent = ScriptedAgent(feature_repo)
    agent.run("start", "--client", "claude-code")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    return feature_repo


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put a gh on PATH that records its call and answers as told."""
    if sys.platform == "win32":
        pytest.skip("the fake gh is a POSIX shell script")
    bin_dir = tmp_path / "fake-gh"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(FAKE_GH)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_DIR", str(bin_dir))
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/widgets")

    def answer(*, exit_code: int = 0, stdout: str = "", stderr: str = "") -> Path:
        monkeypatch.setenv("FAKE_GH_EXIT", str(exit_code))
        monkeypatch.setenv("FAKE_GH_STDOUT", stdout)
        monkeypatch.setenv("FAKE_GH_STDERR", stderr)
        return bin_dir

    return answer


def _head(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo)


def test_without_the_flag_verify_asks_nothing(attested: Path, fake_gh) -> None:
    calls = fake_gh(exit_code=1)
    data = ScriptedAgent(attested).run("verify", _head(attested))["data"]
    assert data["signature"] is None
    assert not (calls / "argv").exists()


def test_a_signed_note_is_reported_verified(attested: Path, fake_gh) -> None:
    calls = fake_gh(stdout='[{"verificationResult": {}}]')
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]

    stored = signature.note_blob(attested, _head(attested))
    assert stored is not None
    assert data["signature"] == {
        "state": "verified",
        "note_sha256": hashlib.sha256(stored).hexdigest(),
        "detail": None,
    }
    # gh was handed the note byte for byte, trailing newline included.
    assert (calls / "subject").read_bytes() == stored
    assert stored.endswith(b"\n")


def test_the_signer_is_pinned_to_the_workflow_and_the_base_branch(attested: Path, fake_gh) -> None:
    calls = fake_gh(stdout='[{"verificationResult": {}}]')
    ScriptedAgent(attested).run("verify", _head(attested), "--signature")

    argv = (calls / "argv").read_text().splitlines()
    assert argv[:2] == ["attestation", "verify"]
    options = dict(zip(argv[3::2], argv[4::2], strict=False))
    assert options["--repo"] == "octo/widgets"
    assert options["--predicate-type"] == signature.PREDICATE_TYPE
    assert options["--signer-workflow"] == "octo/widgets/.github/workflows/attestation.yml"
    assert options["--source-ref"] == "refs/heads/main"
    assert "--deny-self-hosted-runners" in argv


def test_an_unsigned_note_passes_and_is_reported_absent(attested: Path, fake_gh) -> None:
    fake_gh(exit_code=1, stderr=NOT_FOUND)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["verified"] is True
    assert data["signature"]["state"] == "absent"


def test_a_signature_that_does_not_verify_fails(attested: Path, fake_gh) -> None:
    fake_gh(exit_code=1, stderr="Error: verifying with issuer: signature mismatch\n")
    env = ScriptedAgent(attested).run(
        "verify", _head(attested), "--signature", expect=ExitCode.STAGE_FAILED
    )
    assert env["data"]["reason"] == "signature_invalid"
    assert env["data"]["signature"]["state"] == "invalid"
    assert "signature mismatch" in env["data"]["signature"]["detail"]


def test_an_unknown_repository_is_absent_and_asks_nothing(
    attested: Path, fake_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_gh(stdout='[{"verificationResult": {}}]')
    monkeypatch.delenv("GITHUB_REPOSITORY")
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "absent"
    assert "GITHUB_REPOSITORY" in data["signature"]["detail"]
    assert not (calls / "argv").exists()


def test_a_missing_gh_is_absent(attested: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/widgets")
    monkeypatch.setattr(
        signature.subprocess,
        "run",
        _without_gh(signature.subprocess.run),
    )
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "absent"
    assert data["signature"]["detail"] == "the gh CLI is not installed"


def _without_gh(real):
    def run(argv, *args, **kwargs):
        if argv[0] == "gh":
            raise FileNotFoundError(argv[0])
        return real(argv, *args, **kwargs)

    return run


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("https://github.com/octo/widgets.git", "octo/widgets"),
        ("git@github.com:octo/widgets.git", "octo/widgets"),
        ("https://gitlab.com/octo/widgets.git", None),
        ("https://github.com/octo/group/widgets", None),
    ],
)
def test_the_repository_name_comes_from_a_github_origin(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch, remote: str, slug: str | None
) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    git("config", "remote.origin.url", remote, cwd=feature_repo)
    assert signature.repository_slug(feature_repo) == slug


def test_signature_needs_a_sha(attested: Path) -> None:
    ScriptedAgent(attested).run("verify", "--signature", expect=ExitCode.USAGE_ERROR)


def test_verify_reports_the_statement_the_verifier_signs(attested: Path) -> None:
    head = _head(attested)
    data = ScriptedAgent(attested).run("verify", head)["data"]

    stored = signature.note_blob(attested, head)
    assert stored is not None
    base = git("rev-parse", "main", cwd=attested)
    policy = git("cat-file", "blob", f"{base}:.agentic-preflight.toml", cwd=attested)
    assert data["statement"] == {
        "subject_sha256": hashlib.sha256(stored).hexdigest(),
        "predicate_type": signature.PREDICATE_TYPE,
        "predicate": {
            "root_commit": min(git("rev-list", "--max-parents=0", head, cwd=attested).split()),
            "sha": head,
            "tree_sha": git("rev-parse", f"{head}^{{tree}}", cwd=attested),
            "base_sha": base,
            "policy_sha256": hashlib.sha256((policy + "\n").encode()).hexdigest(),
            "note_schema_version": 8,
            "records_commit": None,
        },
    }


# --- the sign job in the verifier workflow ------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict[str, str]:
    """The text of each job in the verifier workflow, by name."""
    text = (ROOT / signature.SIGNER_WORKFLOW_PATH).read_text()
    body = text.split("\njobs:\n", 1)[1]
    jobs: dict[str, str] = {}
    name = ""
    for line in body.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip().removesuffix(":")
            jobs[name] = ""
        elif name:
            jobs[name] += line + "\n"
    return jobs


def test_only_the_sign_job_can_mint_an_identity_token() -> None:
    jobs = _jobs()
    assert "id-token: write" in jobs["sign"]
    assert "attestations: write" in jobs["sign"]
    for name, text in jobs.items():
        if name != "sign":
            assert "id-token" not in text, name
            assert "attestations: write" not in text, name
    assert "contents: write" not in (ROOT / signature.SIGNER_WORKFLOW_PATH).read_text()


def test_the_sign_job_runs_no_repository_code() -> None:
    sign = _jobs()["sign"]
    assert "actions/checkout" not in sign
    assert "run:" not in sign
    uses = [
        line.split("uses:", 1)[1].split("#")[0].strip()
        for line in sign.splitlines()
        if "uses:" in line
    ]
    assert len(uses) == 1
    action, _, pin = uses[0].partition("@")
    assert action == "actions/attest"
    # A full commit SHA, as the release workflow pins the same action.
    assert len(pin) == 40
    assert f"actions/attest@{pin}" in (ROOT / ".github/workflows/release.yml").read_text()


def test_the_sign_job_signs_only_what_the_verifier_job_handed_over() -> None:
    jobs = _jobs()
    sign = jobs["sign"]
    assert "needs: attestation" in sign
    assert "needs.attestation.result == 'success'" in sign
    assert "subject-digest: sha256:${{ needs.attestation.outputs.note_sha256 }}" in sign
    assert "predicate-type: ${{ needs.attestation.outputs.predicate_type }}" in sign
    assert "predicate: ${{ needs.attestation.outputs.predicate }}" in sign
    # The verifier job takes all three from the envelope of the base-trusted verify.
    for key in ("subject_sha256", "predicate_type", "predicate"):
        assert f".data.statement.{key}" in jobs["attestation"]
