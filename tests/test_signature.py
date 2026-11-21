"""The verifier's signature: the gh classifiers, ``verify --signature``, the statement, the sign job."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation as attestationmod
from agentic_preflight import signature
from agentic_preflight.envelope import ExitCode
from agentic_preflight.gitx import GitError
from agentic_preflight.signature import classify_lookup, classify_verify
from tests.conftest import configure_base, git
from tests.driver import ScriptedAgent, findings_json

NOT_FOUND = "gh: Not Found (HTTP 404)\n"
PROGRESS = "Loaded digest sha256:00 for file://note.json\n"


def test_a_listed_attestation_is_found() -> None:
    assert classify_lookup(0, '{"attestations": [{}]}', "") == (True, None)


@pytest.mark.parametrize("stdout", ['{"attestations": []}', "{}", "[]", "null"])
def test_an_empty_listing_is_not_found(stdout: str) -> None:
    found, why = classify_lookup(0, stdout, "")
    assert found is False
    assert why == "the forge holds no signature for this note"


@pytest.mark.parametrize("stdout", ["", "not json"])
def test_an_unreadable_listing_is_not_found(stdout: str) -> None:
    assert classify_lookup(0, stdout, "") == (False, "the forge's answer could not be read")


def test_a_404_is_not_found_and_does_not_say_why() -> None:
    found, why = classify_lookup(1, "", PROGRESS + NOT_FOUND)
    assert found is False
    # The 404 does not say whether the signature is missing or hidden.
    assert why is not None
    assert "cannot show it" in why


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: API rate limit exceeded (HTTP 403)",
        "To get started with GitHub CLI, please run:  gh auth login",
        "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable",
        "dial tcp: lookup api.github.com: no such host",
        "error connecting to api.github.com\ncheck your internet connection",
        "some failure nobody has seen yet",
    ],
)
def test_every_other_lookup_failure_is_not_found_and_names_the_cause(stderr: str) -> None:
    found, why = classify_lookup(1, "", stderr)
    assert found is False
    assert why is not None
    assert why.startswith("the forge could not be asked: ")
    assert stderr.splitlines()[-1][:40] in why


def test_a_silent_lookup_failure_is_not_found() -> None:
    assert classify_lookup(4, "", "") == (
        False,
        "the forge could not be asked: gh exited 4 without a message",
    )


def test_a_verified_attestation_is_verified() -> None:
    assert classify_verify(0, '[{"verificationResult": {}}]', "") == ("verified", None)


@pytest.mark.parametrize("stdout", ["[]", "{}", "null", "", "not json"])
def test_success_without_a_verified_attestation_is_invalid(stdout: str) -> None:
    state, detail = classify_verify(0, stdout, "")
    assert state == "invalid"
    assert detail


def test_a_failed_verify_is_invalid_and_reports_the_last_line() -> None:
    state, detail = classify_verify(1, "", PROGRESS + "Error: verifying: signature mismatch\n")
    assert (state, detail) == ("invalid", "Error: verifying: signature mismatch")


def test_a_silent_failed_verify_is_invalid() -> None:
    assert classify_verify(2, "", "") == ("invalid", "gh exited 2 without a message")


def test_a_long_reason_is_cut() -> None:
    _, detail = classify_verify(1, "", "x" * 500)
    assert detail is not None
    assert len(detail) == 200


@pytest.mark.parametrize(
    "base_ref", ["main", "origin/main", "refs/heads/main", "refs/remotes/origin/main"]
)
def test_every_accepted_base_ref_form_names_the_branch(base_ref: str) -> None:
    assert signature._branch(base_ref) == "main"


def test_a_slash_in_a_short_name_is_part_of_the_branch() -> None:
    # verify reads "upstream/main" as a branch of that name, not as a remote's branch.
    assert signature._branch("upstream/main") == "upstream/main"


@pytest.mark.parametrize("base_ref", ["refs/remotes/upstream/main", "refs/tags/v1"])
def test_a_full_ref_that_verify_does_not_accept_names_no_branch(base_ref: str) -> None:
    assert signature._branch(base_ref) is None


def test_a_base_ref_that_names_no_branch_is_absent_and_asks_nothing(
    attested: Path, fake_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_gh(stdout='[{"verificationResult": {}}]')
    monkeypatch.setattr(signature, "_branch", lambda base_ref: None)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "absent"
    assert "names no branch" in data["signature"]["detail"]
    assert not (calls / "api-argv").exists()


# --- verify SHA --signature, with a fake gh on PATH ---------------------------------

PROTECTED = "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"

FAKE_GH = """#!/bin/sh
if [ "$1" = "api" ]; then
  printf '%s\\n' "$@" > "$FAKE_GH_DIR/api-argv"
  printf '%s' "$FAKE_GH_API_STDOUT"
  printf '%s' "$FAKE_GH_API_STDERR" >&2
  exit "$FAKE_GH_API_EXIT"
fi
printf '%s\\n' "$@" > "$FAKE_GH_DIR/argv"
cp "$3" "$FAKE_GH_DIR/subject"
echo call >> "$FAKE_GH_DIR/verify-calls"
if [ -n "${FAKE_GH_FIRST_EXIT:-}" ] && [ "$(wc -l < "$FAKE_GH_DIR/verify-calls")" -eq 1 ]; then
  printf '%s' "transient failure" >&2
  exit "$FAKE_GH_FIRST_EXIT"
fi
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

    def answer(
        *, exit_code: int = 0, stdout: str = "", stderr: str = "", lookup_error: str | None = None
    ) -> Path:
        """Answer the lookup with one attestation, or with ``lookup_error`` and exit 1."""
        monkeypatch.setenv("FAKE_GH_API_EXIT", "0" if lookup_error is None else "1")
        monkeypatch.setenv(
            "FAKE_GH_API_STDOUT", '{"attestations": [{}]}' if lookup_error is None else ""
        )
        monkeypatch.setenv("FAKE_GH_API_STDERR", lookup_error or "")
        monkeypatch.setenv("FAKE_GH_EXIT", str(exit_code))
        monkeypatch.setenv("FAKE_GH_STDOUT", stdout)
        monkeypatch.setenv("FAKE_GH_STDERR", stderr)
        return bin_dir

    return answer


def _head(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo)


def _verified(repo: Path, **change: str) -> str:
    """What gh prints for one verified attestation whose predicate names the head."""
    predicate = {
        "sha": _head(repo),
        "tree_sha": git("rev-parse", "HEAD^{tree}", cwd=repo),
        **change,
    }
    return json.dumps([{"verificationResult": {"statement": {"predicate": predicate}}}])


def test_without_the_flag_verify_asks_nothing(attested: Path, fake_gh) -> None:
    calls = fake_gh(exit_code=1)
    data = ScriptedAgent(attested).run("verify", _head(attested))["data"]
    assert data["signature"] is None
    assert not (calls / "argv").exists()
    assert not (calls / "api-argv").exists()


def test_a_signed_note_is_reported_verified(attested: Path, fake_gh) -> None:
    calls = fake_gh(stdout=_verified(attested))
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]

    stored = signature.note_blob(attested, _head(attested))
    assert stored is not None
    assert data["signature"] == {
        "state": "verified",
        "note_sha256": hashlib.sha256(stored).hexdigest(),
        "detail": None,
        "repository": "octo/widgets",
    }
    # gh was handed the note byte for byte, trailing newline included.
    assert (calls / "subject").read_bytes() == stored
    assert stored.endswith(b"\n")


def test_the_signer_is_pinned_to_the_workflow_and_the_base_branch(attested: Path, fake_gh) -> None:
    calls = fake_gh(stdout=_verified(attested))
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
    calls = fake_gh(lookup_error=NOT_FOUND)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["verified"] is True
    assert data["signature"]["state"] == "absent"
    # Nothing was found, so nothing was verified.
    assert not (calls / "argv").exists()


def test_the_lookup_asks_for_this_note_and_this_predicate_type(attested: Path, fake_gh) -> None:
    calls = fake_gh(stdout=_verified(attested))
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    digest = data["signature"]["note_sha256"]
    assert (calls / "api-argv").read_text().splitlines() == [
        "api",
        "--method",
        "GET",
        f"/repos/octo/widgets/attestations/sha256:{digest}",
        "-f",
        f"predicate_type={signature.PREDICATE_TYPE}",
    ]


@pytest.mark.parametrize(
    "lookup_error",
    [
        "To get started with GitHub CLI, please run:  gh auth login\n",
        "dial tcp: lookup api.github.com: no such host\n",
    ],
)
def test_a_caller_who_cannot_reach_the_forge_passes_as_absent(
    attested: Path, fake_gh, lookup_error: str
) -> None:
    # Even with a verify step that would fail, an unanswered lookup is not "invalid".
    fake_gh(exit_code=1, stderr="Error: signature mismatch\n", lookup_error=lookup_error)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "absent"
    assert lookup_error.strip() in data["signature"]["detail"]


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
    calls = fake_gh(stdout=_verified(attested))
    monkeypatch.delenv("GITHUB_REPOSITORY")
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "absent"
    assert "GITHUB_REPOSITORY" in data["signature"]["detail"]
    assert not (calls / "api-argv").exists()


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


def test_a_statement_that_cannot_be_built_does_not_fail_verify(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise GitError(["rev-list"], 128, "fatal: bad object")

    monkeypatch.setattr(signature, "statement", broken)
    data = ScriptedAgent(attested).run("verify", _head(attested))["data"]
    assert data["verified"] is True
    assert data["statement"] is None


def test_a_shallow_clone_reports_no_root_commit(attested: Path, tmp_path: Path) -> None:
    shallow = tmp_path / "shallow"
    git("clone", "--depth", "1", f"file://{attested}", str(shallow), cwd=tmp_path)
    assert signature._root_commit(shallow, "HEAD") is None
    assert signature._root_commit(attested, "HEAD") is not None


def test_a_note_that_cannot_be_read_again_fails_nothing(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/widgets")
    monkeypatch.setattr(signature, "note_blob", lambda repo, sha: None)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["verified"] is True
    assert data["statement"] is None
    assert data["signature"] == {
        "state": "absent",
        "note_sha256": "",
        "detail": "the note could not be read again",
        "repository": None,
    }


def test_an_unreadable_note_blob_is_no_note(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signature.gitx, "note_blobs", lambda repo, ref: {_head(attested): "0" * 40})
    assert signature.note_blob(attested, _head(attested)) is None


def test_a_git_error_while_reading_the_note_is_no_note(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> dict[str, str]:
        raise GitError(["notes", "list"], 128, "fatal: bad notes ref")

    monkeypatch.setattr(signature.gitx, "note_blobs", broken)
    assert signature.note_blob(attested, _head(attested)) is None


def test_the_statement_and_the_signature_describe_the_same_bytes(attested: Path, fake_gh) -> None:
    fake_gh(stdout=_verified(attested))
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["statement"]["subject_sha256"] == data["signature"]["note_sha256"]


def test_a_signature_for_another_commit_is_invalid(attested: Path, fake_gh) -> None:
    fake_gh(stdout=_verified(attested, sha="0" * 40))
    env = ScriptedAgent(attested).run(
        "verify", _head(attested), "--signature", expect=ExitCode.STAGE_FAILED
    )
    assert env["data"]["signature"]["state"] == "invalid"
    assert env["data"]["signature"]["detail"] == "the signed statement names another commit or tree"


def test_a_verified_result_without_a_predicate_is_invalid(attested: Path, fake_gh) -> None:
    fake_gh(stdout='[{"verificationResult": {}}]')
    env = ScriptedAgent(attested).run(
        "verify", _head(attested), "--signature", expect=ExitCode.STAGE_FAILED
    )
    assert env["data"]["signature"]["state"] == "invalid"


def test_a_verify_step_that_fails_once_is_tried_again(
    attested: Path, fake_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_gh(stdout=_verified(attested))
    monkeypatch.setenv("FAKE_GH_FIRST_EXIT", "1")
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["signature"]["state"] == "verified"
    assert (calls / "verify-calls").read_text().splitlines() == ["call", "call"]


def test_a_verify_step_that_fails_twice_is_invalid(attested: Path, fake_gh) -> None:
    calls = fake_gh(exit_code=1, stderr="Error: signature mismatch\n")
    ScriptedAgent(attested).run(
        "verify", _head(attested), "--signature", expect=ExitCode.STAGE_FAILED
    )
    assert (calls / "verify-calls").read_text().splitlines() == ["call", "call"]


def test_a_base_policy_that_cannot_be_read_again_is_absent(
    attested: Path, fake_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh(stdout=_verified(attested))

    def unreadable(*args: object, **kwargs: object) -> None:
        raise GitError(["cat-file"], 128, "fatal: bad object")

    monkeypatch.setattr(signature, "committed_config", unreadable)
    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["verified"] is True
    assert data["signature"]["state"] == "absent"
    assert data["signature"]["repository"] == "octo/widgets"


def test_the_statement_uses_the_base_that_verify_used(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, base = _head(attested), git("rev-parse", "main", cwd=attested)
    seen: list[str] = []
    real = signature.statement

    def record(repo, value, base_sha, payload):
        seen.append(base_sha)
        return real(repo, value, base_sha, payload)

    monkeypatch.setattr(signature, "statement", record)
    # Named by a branch, the base is resolved once, by verify, and handed on as a commit.
    data = ScriptedAgent(attested).run("verify", head, "--base", "main")["data"]
    assert seen == [base]
    assert data["statement"]["predicate"]["base_sha"] == base


def test_a_note_replaced_after_verify_gives_no_statement_and_no_lookup(
    attested: Path, fake_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_gh(stdout=_verified(attested))
    stored = signature.note_blob(attested, _head(attested))
    assert stored is not None
    replaced = json.loads(stored)
    replaced["green_at"] = "2001-01-01T00:00:00Z"
    monkeypatch.setattr(
        signature, "note_blob", lambda repo, sha: json.dumps(replaced).encode() + b"\n"
    )

    data = ScriptedAgent(attested).run("verify", _head(attested), "--signature")["data"]
    assert data["verified"] is True
    assert data["statement"] is None
    assert data["signature"]["state"] == "absent"
    assert not (calls / "api-argv").exists()


def test_a_stored_note_that_no_longer_decodes_is_not_the_verified_note(
    attested: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = attestationmod.read(attested, _head(attested))
    assert value is not None
    assert signature.verified_blob(attested, value) == signature.note_blob(
        attested, _head(attested)
    )
    monkeypatch.setattr(signature, "note_blob", lambda repo, sha: b"\xff not a note")
    assert signature.verified_blob(attested, value) is None


def test_the_sign_job_signs_only_pull_requests_that_target_the_default_branch() -> None:
    sign = " ".join(_jobs()["sign"].split())
    condition = sign.split("if: >- ", 1)[1].split(" runs-on:", 1)[0]
    assert condition.split(" && ") == [
        "always()",
        "needs.attestation.result == 'success'",
        "needs.attestation.outputs.note_sha256 != ''",
        "github.event.pull_request.base.ref == github.event.repository.default_branch",
    ]
