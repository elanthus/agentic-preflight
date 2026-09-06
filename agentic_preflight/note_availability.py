"""Explicit hosted note availability, with fixed candidates and immutable snapshots.

Only absence is retried. This module never updates the normal notes ref and never
executes the proposed checkout. Callers must run the installed protected-base tool.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import attestation, evidence_transport, gitx
from .models import Attestation

DEFAULT_DELAYS = (2.0, 4.0, 8.0)
GIT_TIMEOUT = 30.0
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class HostedCheckFailed(attestation.InvalidAttestation):
    def __init__(self, message: str, *, reason: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(message, reason=reason)
        self.diagnostics = diagnostics


def remote_identity(url: str) -> str:
    """Remove URL credentials, query, and fragment (including scp-style usernames)."""
    if "://" in url:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return url.split("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]


class RemoteNotes:
    """Bounded Git operations; errors deliberately omit raw arguments and stderr."""

    def __init__(self, repo: Path, remote: str, *, timeout: float = GIT_TIMEOUT) -> None:
        self.repo = repo
        self.remote = remote
        self.timeout = timeout

    def run(self, *args: str, absent_code: int | None = None) -> str | None:
        try:
            result = gitx.run(self.repo, *args, check=False, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise attestation.InvalidAttestation(
                f"Git {args[0]} exceeded its {self.timeout:g}-second timeout",
                reason="git_timeout",
            ) from exc
        except OSError as exc:
            raise attestation.InvalidAttestation(
                f"Git {args[0]} could not run ({type(exc).__name__})", reason="io_failure"
            ) from exc
        if absent_code is not None and result.returncode == absent_code:
            return None
        if result.returncode:
            raise attestation.InvalidAttestation(
                f"Git {args[0]} failed with exit {result.returncode}; absence is unproven",
                reason="git_failure",
            )
        return result.stdout.strip()

    def advertised(self, ref: str) -> str | None:
        raw = self.run("ls-remote", "--exit-code", "--refs", self.remote, ref, absent_code=2)
        if raw is None:
            return None
        rows = [line.split() for line in raw.splitlines()]
        if (
            len(rows) != 1
            or len(rows[0]) != 2
            or rows[0][1] != ref
            or not _OID.fullmatch(rows[0][0])
        ):
            raise attestation.InvalidAttestation(
                "Unexpected advertised ref data", reason="git_failure"
            )
        return rows[0][0]

    def fetch(self, ref: str, target: str) -> str:
        self.run("fetch", "--no-tags", "--no-write-fetch-head", self.remote, f"+{ref}:{target}")
        oid = self.run("rev-parse", "--verify", f"{target}^{{commit}}")
        if oid is None or not _OID.fullmatch(oid):
            raise attestation.InvalidAttestation(
                "Unexpected fetched commit identity", reason="git_failure"
            )
        return oid

    def note(self, snapshot: str, head: str) -> tuple[str, str] | None:
        # Read the immutable tree directly: `git notes --ref=<SHA>` expands SHA
        # into a ref name rather than reliably selecting that commit.
        listing = self.run("ls-tree", "-r", "--full-tree", snapshot)
        for line in (listing or "").splitlines():
            metadata, separator, path = line.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or not _OID.fullmatch(fields[2]):
                raise attestation.InvalidAttestation("Invalid notes tree", reason="git_failure")
            if path.replace("/", "") == head:
                if fields[1] != "blob":
                    raise attestation.InvalidAttestation(
                        "Expected note is not a blob", reason="malformed_payload"
                    )
                payload = self.run("cat-file", "blob", fields[2])
                return fields[2], payload or ""
        return None


def check(
    repo: Path,
    *,
    remote: str,
    head_ref: str,
    expected_head: str,
    base_sha: str,
    evaluate: Callable[[Attestation], dict[str, Any]],
    notes_ref: str = attestation.NOTES_REF,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fetch, strictly verify, evaluate once, and recheck the original candidate.

    Four attempts by default, with no sleep after the last. The evaluator consumes
    the decoded value from the recorded notes commit, never a mutable local ref.
    """
    if (
        not _OID.fullmatch(expected_head)
        or not _OID.fullmatch(base_sha)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote)
        or not head_ref.startswith("refs/heads/")
        or not notes_ref.startswith("refs/notes/")
        or len(delays) > 9
        or any(not 0 <= delay <= 30 for delay in delays)
    ):
        raise ValueError(
            "Use full event SHAs, a configured remote name, valid refs, and bounded delays"
        )
    source = RemoteNotes(repo, remote)
    diagnostics: dict[str, Any] = {
        "source_remote": remote,
        "expected_head": expected_head,
        "base_sha": base_sha,
        "notes_ref": notes_ref,
        "verifier_version": version("agentic-preflight"),
        "attempt_budget": len(delays) + 1,
        "git_timeout_seconds": source.timeout,
        "attempts": [],
    }
    prefix = f"refs/agentic-preflight/availability/{uuid.uuid4().hex}"
    refs: list[str] = []
    started = clock()

    def freshness(record: dict[str, Any], key: str = "observed_head") -> None:
        observed = source.advertised(head_ref)
        record[key] = observed
        if observed != expected_head:
            raise attestation.InvalidAttestation(
                f"Event head {expected_head} differs from observed head {observed}",
                reason="stale_candidate",
            )

    try:
        source.run("check-ref-format", head_ref)
        source.run("check-ref-format", notes_ref)
        resolved_remote = source.run("remote", "get-url", remote) or ""
        diagnostics["source_repository"] = remote_identity(resolved_remote)
        source.remote = resolved_remote
        revision = source.run("rev-parse", "--verify", "HEAD^{commit}")
        diagnostics["verifier_revision"] = revision
        if revision != base_sha:
            raise attestation.InvalidAttestation(
                "Hosted checking requires the protected event-base checkout",
                reason="verifier_mismatch",
            )
        for number in range(1, len(delays) + 2):
            record: dict[str, Any] = {
                "attempt": number,
                "observed_head": None,
                "advertised_notes_commit": None,
                "notes_commit": None,
                "note_present": False,
            }
            diagnostics["attempts"].append(record)
            try:
                freshness(record)
                head_target = f"{prefix}/head"
                if head_target not in refs:
                    refs.append(head_target)
                fetched_head = source.fetch(head_ref, head_target)
                record["fetched_head"] = fetched_head
                if fetched_head != expected_head:
                    raise attestation.InvalidAttestation(
                        "Fetched PR head differs from the original event", reason="stale_candidate"
                    )
                advertised = source.advertised(notes_ref)
                record["advertised_notes_commit"] = advertised
                reason = "missing_notes_ref"
                if advertised is not None:
                    target = f"{prefix}/notes"
                    if target not in refs:
                        refs.append(target)
                    snapshot = source.fetch(notes_ref, target)
                    record["notes_commit"] = snapshot
                    note = source.note(snapshot, expected_head)
                    reason = "missing_note"
                    if note is not None:
                        record["note_present"] = True
                        record["note_object"] = note[0]
                        with gitx.bounded_commands(GIT_TIMEOUT):
                            decoded = attestation.decode(note[1])
                            if decoded.sha != expected_head:
                                raise attestation.InvalidAttestation(
                                    "Note names a different commit", reason="commit_mismatch"
                                )
                            record["fetched_evidence_commits"] = []
                            for sha in evidence_transport.missing(repo, decoded):
                                target = f"{prefix}/evidence/{sha}"
                                refs.append(target)
                                fetched = source.fetch(evidence_transport.ref_for(sha), target)
                                if fetched != sha:
                                    raise attestation.InvalidAttestation(
                                        "Fetched evidence ref names a different commit",
                                        reason="commit_mismatch",
                                    )
                                record["fetched_evidence_commits"].append(sha)
                            value = attestation.verify_value(
                                repo, decoded, expected_head, purpose="local"
                            )
                            result = evaluate(value)
                        freshness(record, "completion_head")
                        return {**result, "availability": diagnostics}
                record["reason"] = reason
            finally:
                record["elapsed_seconds"] = round(clock() - started, 3)
            if number <= len(delays):
                sleeper(delays[number - 1])
        raise attestation.InvalidAttestation(
            f"Expected attestation still absent after {len(delays) + 1} attempts", reason=reason
        )
    except attestation.InvalidAttestation as exc:
        if diagnostics["attempts"]:
            diagnostics["attempts"][-1]["reason"] = exc.reason
        raise HostedCheckFailed(str(exc), reason=exc.reason, diagnostics=diagnostics) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise HostedCheckFailed(
            f"Git evidence validation failed ({type(exc).__name__})",
            reason="git_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "io_failure",
            diagnostics=diagnostics,
        ) from exc
    except gitx.GitError as exc:
        raise HostedCheckFailed(
            f"Git evidence validation failed with exit {exc.returncode}",
            reason="git_failure",
            diagnostics=diagnostics,
        ) from exc
    finally:
        failing = sys.exc_info()[0] is not None
        cleanup_errors = []
        for ref in refs:
            try:
                source.run("update-ref", "-d", ref)
            except attestation.InvalidAttestation as exc:
                cleanup_errors.append({"ref": ref, "reason": exc.reason})
        if cleanup_errors:
            diagnostics["cleanup_errors"] = cleanup_errors
            if not failing:
                raise HostedCheckFailed(
                    "Cannot remove temporary availability refs",
                    reason="git_failure",
                    diagnostics=diagnostics,
                )
