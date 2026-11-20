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
from .attestation import NOTES_REF, Attestation, trusted_base
from .config import REPO_CONFIG_NAME, ConfigError, committed_config
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


def note_blob(repo: Path | str, sha: str) -> bytes | None:
    """Return the note on ``sha`` byte for byte as stored, or None when it cannot be read.

    ``gitx.read_note`` strips the trailing newline. A signature covers bytes, so
    this reads the blob unchanged.
    """
    blob = gitx.note_blobs(repo, NOTES_REF).get(gitx.rev_parse(repo, sha))
    if blob is None:
        return None
    result = subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=repo, capture_output=True, check=False
    )
    # A blob that cannot be read is no note to sign or to look up.
    return result.stdout if result.returncode == 0 else None


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


def statement(repo: Path | str, value: Attestation, base: str | None) -> dict[str, Any] | None:
    """What the protected verifier signs for a note that ``verify`` accepted.

    The subject is the note blob as stored. The predicate binds the statement to
    one repository and commit, and to the base commit and policy that judged the
    note. Returns None when the commit has no note.
    """
    payload = note_blob(repo, value.sha)
    if payload is None:
        return None
    base_sha = trusted_base(repo, value, base)
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


def _branch(base_ref: str) -> str:
    """The branch a ``[general] base_ref`` names, as ``verify`` resolves it."""
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if base_ref.startswith(prefix):
            return base_ref.removeprefix(prefix)
    return base_ref


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


def check_verified(repo: Path | str, value: Attestation, base: str | None) -> SignatureCheck:
    """Check the signature of a note that ``verify`` already accepted.

    The branch the signer must have run for is ``[general] base_ref`` in the
    configuration committed at the trusted base, not the branch the note names.
    """
    payload = note_blob(repo, value.sha)
    if payload is None:
        # verify read this note a moment ago, so the notes ref changed under the check.
        return SignatureCheck("absent", "", "the note could not be read again")
    repository = repository_slug(repo)
    if repository is None:
        return SignatureCheck(
            "absent",
            note_sha256(payload),
            "the repository's name on the forge is unknown: GITHUB_REPOSITORY is not set "
            "and origin is not a github.com remote",
        )
    try:
        policy = committed_config(repo, trusted_base(repo, value, base))
    except (ConfigError, ValueError) as exc:
        # verify read this policy a moment ago, so this is a change under the check.
        return SignatureCheck(
            "absent", note_sha256(payload), f"the base policy could not be read: {exc}"[:200]
        )
    return check(payload, repository=repository, base_branch=_branch(policy.general.base_ref))


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


def check(payload: bytes, *, repository: str, base_branch: str) -> SignatureCheck:
    """Ask the forge whether a note blob carries the verifier's signature.

    ``repository`` is ``owner/name``. The check has two steps. It first asks the
    forge whether it holds a signature for the digest, and anything but a clear
    yes is ``absent``. Only then does it verify, so ``invalid`` always means that
    a signature exists and failed. The signing certificate must name this
    repository's verifier workflow, run for ``base_branch``: a pull request that
    targets another branch runs that branch's copy of the workflow, which the
    protected branch does not vouch for.
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
        result = _gh(
            [
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
        )
    if isinstance(result, str):
        # The forge answered the lookup a moment ago, so this is not a bad signature.
        return SignatureCheck("absent", digest, result)
    state, detail = classify_verify(result.returncode, result.stdout, result.stderr)
    return SignatureCheck(state, digest, detail)
