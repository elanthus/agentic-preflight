"""Portable per-commit attestations stored as Git notes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from . import gitx, public_config
from .config import committed_config
from .digests import sha256_text
from .fingerprints import intent_digest
from .models import (
    ATTESTATION_SCHEMA_VERSION,
    Attestation,
    AttestedStage,
    EventsAnchor,
    RecordsPointer,
    RunDoc,
    Stage,
    StageStatus,
    TranscriptDigest,
)

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
    if reason == "missing_records":
        return (
            "The note names a records bundle that is not in this repository. Fetch "
            "refs/agentic-preflight/evidence/* from the same source remote as the note, "
            "then retry offline verify for the exact head."
        )
    if reason == "records_mismatch":
        return (
            "The records bundle does not match the note. Do not edit the bundle or the note; "
            "validate the exact head again with [records] publish = true to replace both."
        )
    if reason == "signature_invalid":
        return (
            "A signature exists for this note and did not verify, or was not made by this "
            "repository's verifier workflow for the protected base branch. Do not rely on "
            "the note. Read data.signature.detail, and ask a maintainer to check who "
            "created the attestation on the forge."
        )
    if reason == "signature_required":
        return (
            "No signature over the note in the notes ref could be confirmed, and the "
            "protected base sets [attestation] require_signature = true or its policy "
            "could not be read to show otherwise. If data.signature.required is null, "
            "the signature was never looked up: check that the base commit is present "
            "and readable, then run the "
            "command again. Otherwise read data.signature.detail: sign in to gh or set "
            "GITHUB_REPOSITORY if the forge could not be asked. If the forge was asked, "
            "the note was never signed or was replaced after signing; do not rely on it, "
            "and open or rerun the pull request so the protected verifier checks and "
            "signs the exact head again."
        )
    if reason == "records_required":
        return (
            "The protected base sets [records] publish = true, and this note names no "
            "records bundle, or its bundle leaves out a stage log or transcript that the "
            "note digests. Validate the exact head again with [records] publish = true "
            "in force, so that the run's own logs are complete, then publish it."
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
    events: EventsAnchor,
    transcripts: dict[Stage, TranscriptDigest],
    records: RecordsPointer | None = None,
) -> Attestation:
    """Build the attestation for a run that has passed every stage.

    ``events`` is the run's event chain as it stands now (see
    :func:`agentic_preflight.store.events_anchor`). The push authorization is
    recorded later, at ``push``, so no note holds it.
    """
    if run.review_coverage is None:
        raise InvalidAttestation("review stage has no coverage evidence")
    review_record = run.stages.get(Stage.REVIEW)
    if (
        review_record is None
        or review_record.status is not StageStatus.GREEN
        or review_record.executor is None
    ):
        raise InvalidAttestation("review stage has no executor evidence")
    stages: dict[Stage, AttestedStage] = {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor=review_record.executor,
            command=public_config.published_command(run, Stage.REVIEW, review_record.command),
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
        if record.status is StageStatus.SKIPPED:
            if stage is Stage.LINT:
                raise InvalidAttestation("lint stage is not green")
            stages[stage] = AttestedStage(status="skipped", reason=record.reason)
            continue
        if record.status is not StageStatus.GREEN:
            raise InvalidAttestation(f"{stage.value} stage is not green")
        stages[stage] = AttestedStage(
            status="green",
            command=public_config.published_command(run, stage, record.command),
            exit_code=record.exit_code,
            output_sha256=record.output_sha256,
        )
    from .refresh_validation import rebound_coverage, verify_evidence

    if not public_config.provenance_known(run):
        raise InvalidAttestation(
            "the run was started by an earlier release that did not record which "
            "configuration came from the user file; start a fresh run to publish"
        )

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
        schema_version=ATTESTATION_SCHEMA_VERSION,
        outcome="verified",
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        intent_sha256=intent_digest(run.intent or ""),
        config_sha256=public_config.published_config_digest(run),
        run_id=run.run_id,
        green_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stages=stages,
        findings_summary=findings_summary,
        evidence=run.evidence,
        # User-level configuration stays local; see ``public_config``.
        config_snapshot=public_config.published_snapshot(run),
        # Self-reports keep their *_source fields; no digest covers the producer.
        producer=run.producer,
        events=events,
        transcripts=transcripts,
        records=records,
    )
    for item in value.evidence.values():
        item.verify_origin()
    # Building checks the evidence is internally consistent. The protected base
    # policy applies when a verifier reads the note (see ``verify_value``).
    verify_evidence(run.worktree_path, value, policy=public_config.local_policy(run))
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


class VerifiedNote(NamedTuple):
    """A verified note and what was checked in the records bundle it names."""

    value: Attestation
    #: ``None`` when the note names no bundle and the base requires none.
    records: dict[str, Any] | None
    #: The commit that supplied the merge base and the policy for this verification.
    base_sha: str


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
    return verify_checked(repo, sha, purpose=purpose, base=base).value


def verify_checked(
    repo: Path | str,
    sha: str,
    *,
    purpose: Literal["local", "publish"] = "local",
    base: str | None = None,
) -> VerifiedNote:
    """Verify as :func:`verify` does, and report the records bundle check."""
    if purpose not in {"local", "publish"}:
        raise InvalidAttestation("unknown verification purpose")
    with gitx.object_cache():
        # One batch peels the commit and its tree; a name it cannot peel is looked
        # up by rev_parse below, which fails as it always did.
        gitx.prime_object_lookups(repo, [sha])
        resolved = gitx.rev_parse(repo, sha)
        value = read(repo, resolved)
        if value is None:
            raise InvalidAttestation(
                f"commit {resolved} has no agentic-preflight attestation in {NOTES_REF}",
                reason="missing_note",
            )
        return _verify_value(repo, value, resolved, purpose=purpose, base=base)


def _base_candidates(base_ref: str) -> list[str]:
    """Full ref names a note's ``base_ref`` may name, preferring the synchronized remote."""
    if base_ref.startswith("refs/"):
        # A tag or another namespace could pin an older, weaker policy, so a full
        # ref name must name a branch.
        if base_ref.startswith(("refs/heads/", "refs/remotes/origin/")):
            return [base_ref]
        return []
    branch = base_ref.removeprefix("origin/")
    return [f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"]


def resolve_base_ref(repo: Path | str, base_ref: str) -> str | None:
    """Return the branch commit ``base_ref`` names without ``--base``, or None."""
    for ref in _base_candidates(base_ref):
        # A full ref name that passes check-ref-format cannot carry revision
        # syntax such as ``~1``, so the note can only name a real branch.
        if gitx.run(repo, "check-ref-format", ref, check=False).returncode:
            continue
        found = gitx.run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if found.returncode == 0:
            return found.stdout.strip()
    return None


def trusted_base(repo: Path | str, value: Attestation, base: str | None) -> str:
    """Resolve the commit that the merge base and the policy are taken from."""
    if base is not None:
        given = gitx.run(
            repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False
        )
        if given.returncode:
            raise InvalidAttestation(
                f"protected base {base!r} does not name a commit", reason="base_mismatch"
            )
        return given.stdout.strip()
    found = resolve_base_ref(repo, value.base_ref)
    if found is not None:
        return found
    raise InvalidAttestation(
        f"attestation base {value.base_ref!r} does not name a local branch",
        reason="base_mismatch",
    )


def verify_merge_base(repo: Path | str, value: Attestation, base_sha: str) -> None:
    """Require the note's merge base to be the unique merge base of ``base_sha`` and the head."""
    # Exit 1 with no output means unrelated histories, which is a mismatch, not a Git failure.
    result = gitx.run(repo, "merge-base", "--all", base_sha, value.sha, check=False)
    if result.returncode not in (0, 1):
        raise gitx.GitError(
            ["merge-base", "--all", base_sha, value.sha], result.returncode, result.stderr
        )
    found = result.stdout.split()
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
    with gitx.object_cache():
        return _verify_value(repo, value, resolved, purpose=purpose, base=base).value


def _verify_value(
    repo: Path | str,
    value: Attestation,
    resolved: str,
    *,
    purpose: Literal["local", "publish"],
    base: str | None,
) -> VerifiedNote:
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
    from . import records as recordsmod
    from .refresh_validation import verify_evidence, verify_policy_bindings

    try:
        # Every commit the note names is peeled again and again below. One batch
        # resolves them all; anything it cannot resolve is looked up, and fails, as before.
        gitx.prime_object_lookups(repo, _named_commits(value, resolved))
        base_sha = trusted_base(repo, value, base)
        verify_merge_base(repo, value, base_sha)
        policy = committed_config(repo, base_sha)
        for item in value.evidence.values():
            item.verify_origin()
        verify_evidence(repo, value, policy=policy)
        verify_policy_bindings(value, policy)
        # The base decides whether a bundle is required; a run may publish one anyway.
        records = recordsmod.verify_records(repo, value, required=policy.records.publish)
    except InvalidAttestation:
        raise
    except gitx.GitError as exc:
        raise InvalidAttestation(
            f"Git evidence validation failed with exit {exc.returncode}", reason="git_failure"
        ) from exc
    except ValueError as exc:
        raise InvalidAttestation(str(exc)) from exc
    return VerifiedNote(value, records, base_sha)


def _named_commits(value: Attestation, resolved: str) -> list[str]:
    """The verified head, the note's merge base, and each evidence origin's commits."""
    commits = [resolved, value.merge_base_sha]
    for item in (value.evidence or {}).values():
        commits += [item.origin.head_sha, item.origin.base_sha]
    return commits


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
