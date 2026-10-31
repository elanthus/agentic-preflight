"""Portable per-commit attestations stored as Git notes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from . import gitx
from .config import committed_config, snapshot_config
from .digests import sha256_text
from .fingerprints import intent_digest
from .models import Attestation, AttestedStage, RunDoc, Stage

NOTES_REF = "refs/notes/agentic-preflight"


def recovery(reason: str) -> str:
    """Return the recovery instruction for an attestation failure reason."""
    if reason in {"missing_note", "missing_notes_ref"}:
        return (
            "Confirm publication of the expected head and its note to the source remote; "
            "fetch the exact head, notes, and retained evidence refs from that source remote, "
            "then retry offline verify for the exact head."
        )
    if reason == "incompatible_schema":
        return (
            "The verifier cannot understand this attestation format. Compare the trusted "
            "verifier revision with the producer and update them to the same release or emit "
            "a supported format. Unknown fields do not identify a producer version."
        )
    if reason == "verifier_mismatch":
        return "Install and run the helper from the original protected event-base checkout."
    if reason == "base_mismatch":
        return (
            "Synchronize the branch with its protected base and rerun validation, or pass "
            "the protected base the change will merge into with --base."
        )
    if reason == "stale_candidate":
        return "Run the check for the current PR event; never substitute its head into this event."
    if reason in {"git_failure", "git_timeout", "io_failure"}:
        return (
            "Inspect Git access, authentication, permissions, and transport; absence is unproven."
        )
    return (
        "Inspect the invalid evidence and its commit/tree bindings. Restore valid evidence "
        "for this exact head; fetching repeatedly cannot repair a present invalid note."
    )


def output_digest(output: str) -> str:
    """Return the SHA-256 hex digest of captured stage output."""
    return sha256_text(output)


def build(
    run: RunDoc,
    *,
    sha: str,
    tree_sha: str,
    docs_enabled: bool,
    findings_summary: dict[str, int],
) -> Attestation:
    """Build the attestation for a run that has passed every stage."""
    if run.review_coverage is None:
        raise InvalidAttestation("review stage has no coverage evidence")
    review_record = run.stages.get(Stage.REVIEW)
    if review_record is None or review_record.status != "green" or review_record.executor is None:
        raise InvalidAttestation("review stage has no executor evidence")
    stages: dict[Stage, AttestedStage] = {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor=review_record.executor,
            command=review_record.command,
            exit_code=review_record.exit_code,
            output_sha256=review_record.output_sha256,
            coverage=run.review_coverage,
        ),
        Stage.DOCS: AttestedStage(
            status="green" if docs_enabled else "skipped",
            reason=None if docs_enabled else "disabled by configuration",
        ),
    }
    for stage in (Stage.TEST, Stage.LINT):
        record = run.stages.get(stage)
        if record is None:
            raise InvalidAttestation(f"{stage.value} stage has no recorded result")
        if record.status == "skipped":
            if stage is Stage.LINT:
                raise InvalidAttestation("lint stage is not green")
            stages[stage] = AttestedStage(status="skipped", reason=record.reason)
            continue
        if record.status != "green":
            raise InvalidAttestation(f"{stage.value} stage is not green")
        stages[stage] = AttestedStage(
            status="green",
            command=record.command,
            exit_code=record.exit_code,
            output_sha256=record.output_sha256,
        )
    from .refresh_validation import rebound_coverage, verify_evidence

    required_evidence = set(Stage)
    if run.worktree_path is None:
        raise InvalidAttestation("run has no validation worktree for evidence verification")
    if set(run.evidence) != required_evidence:
        raise InvalidAttestation("run lacks complete per-stage evidence")
    stages[Stage.REVIEW].coverage = rebound_coverage(
        run.worktree_path,
        run.evidence[Stage.REVIEW].origin,
        head=sha,
        base=run.merge_base_sha,
    )
    value = Attestation(
        schema_version=7,
        outcome="verified",
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        intent_sha256=intent_digest(run.intent or ""),
        config_sha256=run.config_digest,
        run_id=run.run_id,
        green_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stages=stages,
        findings_summary=findings_summary,
        evidence=run.evidence,
        config_snapshot=run.config_snapshot,
    )
    for item in value.evidence.values():
        item.verify_origin()
    # Building checks the evidence is internally consistent. The protected base
    # policy applies when a verifier reads the note (see ``verify_value``).
    verify_evidence(run.worktree_path, value, policy=snapshot_config(run.config_snapshot))
    return value


def write(repo: Path | str, value: Attestation) -> None:
    """Retain evidence refs and write the attestation as a git note on its commit."""
    from . import evidence_transport

    evidence_transport.retain(repo, value)
    gitx.write_note(repo, NOTES_REF, value.sha, encode(value))


def read(repo: Path | str, sha: str) -> Attestation | None:
    """Return the attestation noted on a commit, or None when there is none."""
    payload = gitx.read_note(repo, NOTES_REF, sha)
    if payload is None:
        return None
    return decode(payload)


def verify(
    repo: Path | str,
    sha: str,
    *,
    purpose: Literal["local", "publish"] = "local",
    base: str | None = None,
) -> Attestation:
    """Verify the attestation noted on a commit for local or publish use.

    ``base`` is the protected revision the change merges into. Without it, the
    verifier resolves the note's ``base_ref`` to a local branch.
    """
    if purpose not in {"local", "publish"}:
        raise InvalidAttestation("unknown verification purpose")
    resolved = gitx.rev_parse(repo, sha)
    value = read(repo, resolved)
    if value is None:
        raise InvalidAttestation(
            f"commit {resolved} has no agentic-preflight attestation in {NOTES_REF}",
            reason="missing_note",
        )
    return verify_value(repo, value, resolved, purpose=purpose, base=base)


def _base_candidates(base_ref: str) -> list[str]:
    """Full ref names a note's ``base_ref`` may name, preferring the synchronized remote."""
    if base_ref.startswith("refs/"):
        return [base_ref]
    branch = base_ref.removeprefix("origin/")
    return [f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"]


def trusted_base(repo: Path | str, value: Attestation, base: str | None) -> str:
    """Resolve the commit that the merge base and the policy are taken from."""
    if base is not None:
        return gitx.rev_parse(repo, base)
    for ref in _base_candidates(value.base_ref):
        # A full ref name that passes check-ref-format cannot carry revision
        # syntax such as ``~1``, so the note can only name a real branch.
        if gitx.run(repo, "check-ref-format", ref, check=False).returncode:
            continue
        found = gitx.run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if found.returncode == 0:
            return found.stdout.strip()
    raise InvalidAttestation(
        f"attestation base {value.base_ref!r} does not name a local branch",
        reason="base_mismatch",
    )


def verify_merge_base(repo: Path | str, value: Attestation, base_sha: str) -> None:
    """Require the note's merge base to be the unique merge base of ``base_sha`` and the head."""
    found = gitx.out(repo, "merge-base", "--all", base_sha, value.sha).split()
    if found != [value.merge_base_sha]:
        raise InvalidAttestation(
            f"attestation merge base {value.merge_base_sha} is not the unique merge base "
            f"of {base_sha} and {value.sha}",
            reason="base_mismatch",
        )


def verify_value(
    repo: Path | str,
    value: Attestation,
    resolved: str,
    *,
    purpose: Literal["local", "publish"],
    base: str | None = None,
) -> Attestation:
    """Validate an already decoded note against the policy of a trusted base."""
    if purpose not in {"local", "publish"}:
        raise InvalidAttestation("unknown verification purpose")
    if value.outcome != "verified":
        raise InvalidAttestation("only verified local validation is supported")
    if value.sha != resolved:
        raise InvalidAttestation(
            f"attestation names {value.sha}, but it is attached to {resolved}",
            reason="commit_mismatch",
        )
    actual_tree = gitx.tree_sha(repo, resolved)
    if value.tree_sha != actual_tree:
        raise InvalidAttestation(
            f"attestation tree {value.tree_sha} does not match commit tree {actual_tree}",
            reason="tree_mismatch",
        )
    from .refresh_validation import verify_evidence, verify_policy_bindings

    try:
        base_sha = trusted_base(repo, value, base)
        verify_merge_base(repo, value, base_sha)
        policy = committed_config(repo, base_sha)
        for item in value.evidence.values():
            item.verify_origin()
        verify_evidence(repo, value, policy=policy)
        verify_policy_bindings(value, policy)
    except InvalidAttestation:
        raise
    except gitx.GitError as exc:
        raise InvalidAttestation(
            f"Git evidence validation failed with exit {exc.returncode}", reason="git_failure"
        ) from exc
    except ValueError as exc:
        raise InvalidAttestation(str(exc)) from exc
    return value


class InvalidAttestation(ValueError):
    """A strict evidence failure with a machine-readable recovery category."""

    def __init__(self, message: str, *, reason: str = "invalid_evidence") -> None:
        super().__init__(message)
        self.reason = reason


def encode(value: Attestation) -> str:
    """Serialise an attestation to canonical JSON."""
    payload = value.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def decode(payload: str) -> Attestation:
    """Parse an attestation from its JSON payload."""
    try:
        return Attestation.model_validate_json(payload)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        if any(error["type"] == "json_invalid" for error in errors):
            reason = "malformed_payload"
        elif any(
            error["type"] == "extra_forbidden" or error["loc"] == ("schema_version",)
            for error in errors
        ):
            reason = "incompatible_schema"
        else:
            reason = "invalid_evidence"
        # Pydantic's formatted exception includes raw inputs. Keep note bodies private.
        fields = ", ".join(
            f"{'.'.join(map(str, error['loc'])) or '<root>'}: {error['type']}"
            + (f" ({error['msg']})" if error["type"] == "value_error" else "")
            for error in errors
        )
        raise InvalidAttestation(
            f"attestation validation failed ({fields})", reason=reason
        ) from exc
