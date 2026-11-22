"""The signature the protected verifier makes over an attestation note (ADR 0004).

The verifier workflow signs the note blob it accepted and stores the signature in
the forge's attestation store. This module computes what is signed and asks the
forge's CLI whether a signature exists for the note that is in the notes ref now.
It shells out to ``gh`` and imports no signing library.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal, NamedTuple

from . import gitx
from .attestation import NOTES_REF, Attestation, decode
from .config import REPO_CONFIG_NAME, Config, ConfigError, committed_config
from .telemetry import git_output, normalize_remote

#: The in-toto predicate type of the verifier's statement.
PREDICATE_TYPE = "https://github.com/elanthus/agentic-preflight/verified-note/v1"

#: The workflow file whose identity the signing certificate must carry.
SIGNER_WORKFLOW_PATH = ".github/workflows/attestation.yml"

SignatureState = Literal["verified", "invalid", "absent"]

#: Seconds to wait for the forge before the check gives up.
_TIMEOUT_SECONDS = 60


class SignatureCheck(NamedTuple):
    """What ``verify SHA`` reports in ``data.signature``."""

    state: SignatureState
    note_sha256: str
    #: Why the state is not ``verified``; ``None`` when it is.
    detail: str | None
    #: The ``owner/name`` the signature was checked against, which the caller's
    #: environment or ``origin`` supplies; ``None`` when it is unknown.
    repository: str | None = None
    #: ``[attestation] require_signature`` at the trusted base; ``None`` when the
    #: base policy could not be read again.
    required: bool | None = None


def note_blob(repo: Path | str, sha: str) -> bytes | None:
    """Return the note on ``sha`` byte for byte as stored, or None when it cannot be read.

    ``gitx.read_note`` strips the trailing newline. A signature covers bytes, so
    this reads the blob unchanged. Callers run after ``verify`` accepted the
    note, so a failure here means the notes ref changed under them; it is
    reported as no note, never raised.
    """
    try:
        blob = gitx.note_blobs(repo, NOTES_REF).get(gitx.rev_parse(repo, sha))
    except gitx.GitError:
        return None
    if blob is None:
        return None
    result = subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=repo, capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def verified_blob(repo: Path | str, value: Attestation) -> bytes | None:
    """Return the stored blob of the note that ``verify`` accepted, or None.

    ``verify`` decoded the note from its own read. This reads the blob again for
    its exact bytes and requires it to decode to the same note. A notes ref that
    changed in between gives None, so a statement or a signature check never
    describes a note that was not verified.
    """
    payload = note_blob(repo, value.sha)
    if payload is None:
        return None
    try:
        same = decode(payload.decode().rstrip("\n")) == value
    except ValueError:  # includes UnicodeDecodeError and InvalidAttestation
        return None
    return payload if same else None


def note_sha256(payload: bytes) -> str:
    """The subject digest of a note blob."""
    return hashlib.sha256(payload).hexdigest()


def _root_commit(repo: Path | str, sha: str) -> str | None:
    """The smallest root commit of ``sha``, which is the same in every full clone."""
    # A shallow clone ends at its boundary commits, which differ from clone to clone.
    if gitx.run(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return None
    roots = gitx.run(repo, "rev-list", "--max-parents=0", sha).stdout.split()
    return min(roots) if roots else None


def _policy_sha256(repo: Path | str, base_sha: str) -> str | None:
    """The SHA-256 of the configuration file committed at the base, or None without one."""
    shown = subprocess.run(
        ["git", "cat-file", "blob", f"{base_sha}:{REPO_CONFIG_NAME}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return hashlib.sha256(shown.stdout).hexdigest() if shown.returncode == 0 else None


def statement(
    repo: Path | str, value: Attestation, base_sha: str, payload: bytes | None
) -> dict[str, Any] | None:
    """What the protected verifier signs for a note that ``verify`` accepted.

    ``payload`` is the note blob from :func:`verified_blob`, and ``base_sha`` is
    the base commit that ``verify`` used. The subject is that blob as stored. The
    predicate binds the statement to one repository and commit, and to the base
    commit and policy that judged the note. Returns None without a payload.
    """
    if payload is None:
        return None
    return {
        "subject_sha256": note_sha256(payload),
        "predicate_type": PREDICATE_TYPE,
        "predicate": {
            "root_commit": _root_commit(repo, value.sha),
            "sha": value.sha,
            "tree_sha": value.tree_sha,
            "base_sha": base_sha,
            "policy_sha256": _policy_sha256(repo, base_sha),
            "note_schema_version": value.schema_version,
            "records_commit": value.records.commit if value.records else None,
        },
    }


def _lines(stderr: str) -> list[str]:
    return [line.strip() for line in stderr.splitlines() if line.strip()]


def classify_lookup(returncode: int, stdout: str, stderr: str) -> tuple[bool, str | None]:
    """Say whether the forge holds a signature for a digest, and why not if it does not.

    This reads one ``gh api`` call that lists the attestations for the digest.
    With gh 2.104.0, a digest with none gives exit 1 and ``HTTP 404`` on stderr,
    and so does a repository the caller cannot read. Every failure here means
    "no signature was seen", whatever its cause: a caller who is not signed in,
    or who is offline, has not seen a bad signature.
    """
    if returncode == 0:
        try:
            listed = json.loads(stdout)
        except ValueError:
            return False, "the forge's answer could not be read"
        found = isinstance(listed, dict) and listed.get("attestations")
        if isinstance(found, list) and found:
            return True, None
        return False, "the forge holds no signature for this note"
    lines = _lines(stderr)
    if any("HTTP 404" in line for line in lines):
        return False, "the forge holds no signature for this note, or cannot show it"
    cause = lines[-1][:200] if lines else f"gh exited {returncode} without a message"
    return False, f"the forge could not be asked: {cause}"


def classify_verify(returncode: int, stdout: str, stderr: str) -> tuple[SignatureState, str | None]:
    """Map ``gh attestation verify`` to a state, for a signature the forge holds.

    The lookup has already shown that a signature exists for the digest, so a
    failure here is a signature that did not pass the checks. On success gh exits
    0 and stdout is a JSON array with one entry for each verified attestation.
    """
    if returncode == 0:
        try:
            verified = json.loads(stdout)
        except ValueError:
            return "invalid", "gh reported success without a readable result"
        if isinstance(verified, list) and verified:
            return "verified", None
        # gh exits 0 only when at least one attestation verified, so an empty or
        # misshapen result contradicts the exit code. Do not read it as success.
        return "invalid", "gh reported success without a verified attestation"
    lines = _lines(stderr)
    # The error is the last thing gh prints, after any progress lines.
    return "invalid", lines[-1][:200] if lines else f"gh exited {returncode} without a message"


def _branch(base_ref: str) -> str | None:
    """The branch a ``[general] base_ref`` names, as ``verify`` resolves it, or None.

    ``verify`` accepts a short name, ``origin/<branch>``, and a full ref under
    ``refs/heads/`` or ``refs/remotes/origin/``. Any other full ref names no
    branch that the signer could have run for.
    """
    for prefix in ("refs/heads/", "refs/remotes/origin/"):
        if base_ref.startswith(prefix):
            return base_ref.removeprefix(prefix)
    if base_ref.startswith("refs/"):
        return None
    return base_ref.removeprefix("origin/")


def repository_slug(repo: Path | str) -> str | None:
    """The ``owner/name`` the forge knows this repository by, or None.

    Hosted jobs get it from ``GITHUB_REPOSITORY``. A clone falls back to its
    ``origin`` URL, and only when that names github.com.
    """
    hosted = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if hosted.count("/") == 1 and all(hosted.split("/")):
        return hosted
    remote = normalize_remote(git_output(Path(repo), "remote", "get-url", "origin")) or ""
    host, _, path = remote.partition("/")
    if host == "github.com" and path.count("/") == 1 and all(path.split("/")):
        return path
    return None


def check_verified(
    repo: Path | str, value: Attestation, base_sha: str, payload: bytes | None
) -> SignatureCheck:
    """Check the signature of a note that ``verify`` already accepted.

    ``payload`` is the note blob from :func:`verified_blob`, the same bytes the
    statement describes, and ``base_sha`` is the base commit that ``verify``
    used. The branch the signing workflow must have run from is
    ``[general] base_ref`` in the configuration committed at that base, not the
    branch the note names, and ``required`` is ``[attestation] require_signature``
    there, or ``None`` when that policy could not be read again.
    """
    try:
        policy: Config | None = committed_config(repo, base_sha)
        policy_error = ""
    except (ConfigError, ValueError, gitx.GitError) as exc:
        # verify read this policy from the same commit a moment ago.
        policy, policy_error = None, f"the base policy could not be read again: {exc}"[:200]
    required = None if policy is None else policy.attestation.require_signature
    return _check_note(repo, value, payload, policy, policy_error)._replace(required=required)


def failure_reason(checked: SignatureCheck) -> str | None:
    """Return why ``verify SHA --signature`` must fail on ``checked``, or None to pass.

    The reasons are ``signature_invalid`` (a signature exists and does not verify)
    and ``signature_required`` (the trusted base sets ``[attestation]
    require_signature = true`` and no signature was found). ``checked.required`` is
    ``None`` when the base policy could not be read again, a moment after ``verify``
    read it from the same commit. Then no one can show that the key is off, so a
    missing signature fails as if it were on.
    """
    if checked.state == "invalid":
        return "signature_invalid"
    if checked.state == "absent" and checked.required is not False:
        return "signature_required"
    return None


def _check_note(
    repo: Path | str,
    value: Attestation,
    payload: bytes | None,
    policy: Config | None,
    policy_error: str,
) -> SignatureCheck:
    """Look up and check the signature over ``payload`` under the base ``policy``."""
    if payload is None:
        # verify read this note a moment ago, so the notes ref changed under the check.
        return SignatureCheck("absent", "", "the note could not be read again")
    digest = note_sha256(payload)
    repository = repository_slug(repo)
    if repository is None:
        return SignatureCheck(
            "absent",
            digest,
            "the repository's name on the forge is unknown: GITHUB_REPOSITORY is not set "
            "and origin is not a github.com remote",
        )
    if policy is None:
        return SignatureCheck("absent", digest, policy_error, repository)
    branch = _branch(policy.general.base_ref)
    if branch is None:
        return SignatureCheck(
            "absent",
            digest,
            f"the base policy's base_ref {policy.general.base_ref!r} names no branch"[:200],
            repository,
        )
    checked = check(
        payload,
        repository=repository,
        base_branch=branch,
        expected={"sha": value.sha, "tree_sha": value.tree_sha},
    )
    return checked._replace(repository=repository)


def names(stdout: str, expected: dict[str, str]) -> bool:
    """Whether a verified attestation's predicate names the expected commit and tree.

    The subject digest already covers the note, and the note names both. This
    also holds the signed statement itself to the clone.
    """
    try:
        verified = json.loads(stdout)
    except ValueError:
        return False
    for entry in verified if isinstance(verified, list) else []:
        try:
            predicate = entry["verificationResult"]["statement"]["predicate"]
        except (KeyError, TypeError):
            continue
        if isinstance(predicate, dict) and all(
            predicate.get(key) == want for key, want in expected.items()
        ):
            return True
    return False


def _gh(argv: list[str]) -> subprocess.CompletedProcess[str] | str:
    """Run gh, or return why it could not answer."""
    try:
        return subprocess.run(
            ["gh", *argv], capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        return "the gh CLI is not installed"
    except subprocess.TimeoutExpired:
        return "the forge did not answer in time"


def check(
    payload: bytes, *, repository: str, base_branch: str, expected: dict[str, str]
) -> SignatureCheck:
    """Ask the forge whether a note blob carries the verifier's signature.

    ``repository`` is ``owner/name``. The check has two steps. It first asks the
    forge whether it holds a signature for the digest, and anything but a clear
    yes is ``absent``. Only then does it verify, so ``invalid`` always means that
    a signature exists and failed. The signing certificate must name this
    repository's verifier workflow, run from ``base_branch``. GitHub runs a
    ``pull_request_target`` workflow from the default branch whatever branch the
    pull request targets, so this holds only when ``base_branch`` is the default
    branch; the workflow signs only pull requests that target it. The signed
    predicate must name the ``expected`` commit and tree.

    The verify step needs the network too, and gh does not say whether a failure
    was a bad signature or a failed fetch. So a failed verify runs once more
    before it is reported as ``invalid``.
    """
    digest = note_sha256(payload)
    listed = _gh(
        [
            "api",
            "--method",
            "GET",
            f"/repos/{repository}/attestations/sha256:{digest}",
            "-f",
            f"predicate_type={PREDICATE_TYPE}",
        ]
    )
    if isinstance(listed, str):
        return SignatureCheck("absent", digest, listed)
    found, why_not = classify_lookup(listed.returncode, listed.stdout, listed.stderr)
    if not found:
        return SignatureCheck("absent", digest, why_not)
    with tempfile.TemporaryDirectory() as scratch:
        subject = Path(scratch) / "note.json"
        subject.write_bytes(payload)
        argv = [
            "attestation",
            "verify",
            str(subject),
            "--repo",
            repository,
            "--predicate-type",
            PREDICATE_TYPE,
            "--signer-workflow",
            f"{repository}/{SIGNER_WORKFLOW_PATH}",
            "--source-ref",
            f"refs/heads/{base_branch}",
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ]
        result = _gh(argv)
        if not isinstance(result, str) and result.returncode != 0:
            result = _gh(argv)
    if isinstance(result, str):
        # The forge answered the lookup a moment ago, so this is not a bad signature.
        return SignatureCheck("absent", digest, result)
    state, detail = classify_verify(result.returncode, result.stdout, result.stderr)
    if state == "verified" and not names(result.stdout, expected):
        return SignatureCheck(
            "invalid", digest, "the signed statement names another commit or tree"
        )
    return SignatureCheck(state, digest, detail)
