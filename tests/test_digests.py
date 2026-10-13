"""Text and JSON evidence bindings retain their historical byte contracts."""

import hashlib
import json

import pytest

from agentic_preflight import attestation, config, fingerprints, refresh_validation
from agentic_preflight.digests import json_digest, sha256_text


@pytest.mark.parametrize("text", ["", "requested work", "café 雪", "line\r\nnext\n", "e\u0301"])
def test_text_bindings_hash_exact_utf8_bytes(text):
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sha256_text(text) == expected
    # Keep the historical keyword APIs as well as the digest representation.
    assert fingerprints.intent_digest(intent=text) == expected
    assert attestation.intent_digest(intent=text) == expected
    assert attestation.output_digest(output=text) == expected
    assert refresh_validation.json_digest_command(command=text) == expected


def test_configuration_binding_keeps_canonical_json_and_keyword_api():
    snapshot = {"z": ["雪", None], "a": {"enabled": True}}
    expected = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert config.config_digest(snapshot=snapshot) == expected
    assert json_digest(snapshot) == expected
    assert json_digest({"a": {"enabled": True}, "z": ["雪", None]}) == expected
