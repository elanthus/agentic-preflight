"""Round-trip properties for the stdout envelope, the attestation note, and redaction.

The envelope and the note are the two JSON documents other programs parse: an agent
reads the envelope from stdout, and a verifier reads the note from
``refs/notes/agentic-preflight``. Each must serialize to JSON and parse back to the
same value, and the note's canonical encoding must be a fixed point, because its
bytes are what gets published and compared.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_preflight import attestation
from agentic_preflight.cli_contract import CommandData
from agentic_preflight.digests import json_digest
from agentic_preflight.envelope import Envelope
from agentic_preflight.findings import redact_submissions, redact_text
from agentic_preflight.models import (
    AttestedStage,
    EventsAnchor,
    FindingAction,
    FindingSubmission,
    RecordsPointer,
    ReviewCoverage,
    Severity,
    Stage,
    TranscriptDigest,
)
from agentic_preflight.stages.shellstage import REDACTED, combine_secrets
from tests.attestation_helpers import make_attestation

_HEX = "0123456789abcdef"
sha40 = st.text(_HEX, min_size=40, max_size=40)
sha64 = st.text(_HEX, min_size=64, max_size=64)

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=8,
)
json_objects = st.dictionaries(st.text(max_size=8), json_values, max_size=4)
optional_text = st.none() | st.text(max_size=30)

# -- envelope -----------------------------------------------------------------

envelopes = st.builds(
    Envelope,
    ok=st.booleans(),
    run_id=optional_text,
    state=optional_text,
    stage=optional_text,
    data=json_objects.map(CommandData.model_validate),
    blocking=st.lists(json_values, max_size=3),
    next_instruction=optional_text,
    next_command=optional_text,
    error_code=optional_text,
    error_message=optional_text,
    error_detail=st.none() | json_objects,
    human=optional_text,
)


def _envelope_from_payload(payload: dict[str, Any]) -> Envelope:
    """The inverse a consumer applies: every field read back from its contract key."""
    next_block = payload["next"] or {}
    error_block = payload["error"] or {}
    return Envelope(
        ok=payload["ok"],
        run_id=payload["run_id"],
        state=payload["state"],
        stage=payload["stage"],
        data=CommandData.model_validate(payload["data"]),
        blocking=payload["blocking"],
        next_instruction=next_block.get("instruction"),
        next_command=next_block.get("command"),
        error_code=error_block.get("code"),
        error_message=error_block.get("message"),
        error_detail=error_block.get("detail"),
    )


@settings(max_examples=200)
@given(envelope=envelopes)
def test_the_envelope_json_parses_back_to_its_payload(envelope):
    payload = envelope.to_payload()
    parsed = json.loads(envelope.to_json())

    assert parsed == payload
    assert set(parsed) == {"ok", "run_id", "state", "stage", "data", "blocking", "next", "error"}


@settings(max_examples=200)
@given(envelope=envelopes)
def test_an_envelope_rebuilt_from_its_json_prints_the_same_json(envelope):
    """``human`` goes to stderr, so it is the one field the JSON need not carry."""
    rebuilt = _envelope_from_payload(json.loads(envelope.to_json()))
    assert rebuilt.to_json() == envelope.to_json()


# -- attestation --------------------------------------------------------------


@st.composite
def attested_stages(draw) -> dict[Stage, AttestedStage]:
    executor = draw(st.sampled_from(["in_harness", "command"]))
    process = (
        {
            "command": draw(st.text(min_size=1, max_size=20)),
            "exit_code": 0,
            "output_sha256": draw(sha64),
        }
        if executor == "command"
        else {}
    )
    units = draw(st.integers(min_value=0, max_value=4))
    ids = [f"U{index + 1:04d}" for index in range(units)]
    split = draw(st.integers(min_value=0, max_value=units))
    stages = {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor=executor,
            coverage=ReviewCoverage(
                manifest=draw(sha64),
                head_sha=draw(sha40),
                grounding_sha256=draw(st.none() | sha64),
                total_units=units,
                cited_units=ids[:split],
                clean_units=ids[split:],
                excluded_files=draw(st.lists(st.text(min_size=1, max_size=10), max_size=2)),
                contracts_sha256=draw(st.none() | sha64),
            ),
            **process,
        ),
        Stage.DOCS: draw(
            st.sampled_from(
                [AttestedStage(status="green"), AttestedStage(status="skipped", reason="disabled")]
            )
        ),
    }
    for stage in (Stage.LINT, Stage.TEST):
        if draw(st.booleans()):
            stages[stage] = AttestedStage(
                status="green",
                command=draw(st.text(min_size=1, max_size=20)),
                exit_code=0,
                output_sha256=draw(sha64),
            )
        else:
            stages[stage] = AttestedStage(
                status="skipped", reason=draw(st.text(min_size=1, max_size=20))
            )
    return stages


@st.composite
def attestations(draw):
    value = make_attestation(
        stages=draw(attested_stages()),
        sha=draw(sha40),
        tree_sha=draw(sha40),
        merge_base_sha=draw(sha40),
        branch=draw(st.text(min_size=1, max_size=30)),
        findings_summary=draw(
            st.dictionaries(
                st.sampled_from([severity.value for severity in Severity]),
                st.integers(min_value=0, max_value=50),
            )
        ),
    )
    snapshot = draw(st.dictionaries(st.sampled_from(["review", "diff", "policy"]), json_objects))
    lines = draw(st.integers(min_value=0, max_value=1000))
    events = EventsAnchor(
        # Both digests are null exactly when the event log has no lines.
        head_sha256=draw(sha64) if lines else None,
        lines=lines,
        chained_lines=draw(st.integers(min_value=0, max_value=lines)),
        prefix_sha256=draw(sha64) if lines else None,
    )
    transcripts = draw(
        st.dictionaries(
            st.sampled_from([Stage.REVIEW.value, Stage.DOCS.value]),
            st.builds(TranscriptDigest, sha256=sha64, bytes=st.integers(0, 10**9)),
        )
    )
    records = draw(st.none() | st.builds(RecordsPointer, commit=sha40))
    # Validate the whole document, as decode will, rather than trusting model_copy.
    return type(value).model_validate(
        {
            **value.model_dump(mode="json"),
            "config_snapshot": snapshot,
            "config_sha256": json_digest(snapshot),
            "events": events.model_dump(mode="json"),
            "transcripts": {
                stage: digest.model_dump(mode="json") for stage, digest in transcripts.items()
            },
            "records": None if records is None else records.model_dump(mode="json"),
        }
    )


@settings(max_examples=100)
@given(value=attestations())
def test_an_attestation_decodes_to_the_value_it_encoded(value):
    assert attestation.decode(attestation.encode(value)) == value


@settings(max_examples=100)
@given(value=attestations())
def test_the_canonical_note_encoding_is_a_fixed_point(value):
    """Re-encoding a decoded note reproduces its bytes exactly."""
    encoded = attestation.encode(value)
    assert attestation.encode(attestation.decode(encoded)) == encoded
    assert encoded == json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":"))


# -- redaction ----------------------------------------------------------------

# A second pass can rewrite inside an earlier ``[redacted]`` marker when a secret
# shares characters with it (secrets ``["X", "]b"]`` turn ``"Xb"`` into
# ``"[redacted]b"`` and then ``"[redacted[redacted]"``). That redacts more, never
# less, and ``shellstage.redact`` only promises not to match a marker within one
# pass. So idempotency is stated for secrets drawn from characters the marker lacks.
_SECRET_ALPHABET = "".join(sorted(set("abcfghijklmnopsuvwxyz0123456789=-_") - set(REDACTED)))
secrets = st.lists(st.text(_SECRET_ALPHABET, min_size=1, max_size=6), max_size=4).map(
    combine_secrets
)
_TEXT_ALPHABET = _SECRET_ALPHABET + " [].\n"
texts = st.text(_TEXT_ALPHABET, max_size=80)


@settings(max_examples=300)
@given(text=texts, values=secrets, limit=st.integers(min_value=1, max_value=120))
def test_redacting_finding_text_twice_changes_nothing(text, values, limit):
    once = redact_text(text, values, limit=limit)
    assert redact_text(once, values, limit=limit) == once
    assert len(once) <= limit
    assert not any(secret in once for secret in values)


@settings(max_examples=100)
@given(
    values=secrets,
    title=st.text(_TEXT_ALPHABET, min_size=1, max_size=200),
    detail=st.text(_TEXT_ALPHABET, max_size=300),
    suggestion=st.none() | st.text(_TEXT_ALPHABET, max_size=300),
)
def test_redacting_submissions_twice_changes_nothing(values, title, detail, suggestion):
    submission = FindingSubmission(
        path="src/app.py",
        severity=Severity.HIGH,
        action=FindingAction.AUTO_FIX,
        title=title,
        detail=detail,
        suggestion=suggestion,
    )
    once = redact_submissions([submission], values)
    assert redact_submissions(once, values) == once
    for field in ("title", "detail", "suggestion"):
        redacted = getattr(once[0], field) or ""
        assert not any(secret in redacted for secret in values)
