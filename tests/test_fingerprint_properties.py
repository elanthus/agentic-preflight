"""Property tests for the fingerprint contract in ``docs/fingerprint-contract.md``.

The failure these guard against is the one in ``docs/dogfooding-case-study.md``: a
green result stays reusable after an input that shaped it changed. A fingerprint is a
flat record of content digests, so the properties are stated at two levels:

- each digest is deterministic and moves when any byte of what it binds moves, and
  does not move for configuration outside its declared sections; and
- classification reports exactly the fields that differ, so no bound field is
  silently ignored and no unchanged field is blamed.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from agentic_preflight import fingerprints as fp
from agentic_preflight.digests import json_digest

# -- strategies ---------------------------------------------------------------

_HEX = "0123456789abcdef"
sha40 = st.text(_HEX, min_size=40, max_size=40)
sha64 = st.text(_HEX, min_size=64, max_size=64)

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=12),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=6), children, max_size=3),
    ),
    max_leaves=6,
)

REVIEW_SECTIONS = ("general", "review", "policy", "context", "diff", "stage")
DOCS_SECTIONS = ("general", "docs", "diff", "context", "policy")
KNOWN_SECTIONS = tuple(sorted(fp._KNOWN_CONFIG))


def _snapshot(sections: tuple[str, ...]) -> st.SearchStrategy[dict[str, Any]]:
    """A configuration snapshot whose sections are tables of JSON values."""
    return st.dictionaries(
        st.sampled_from(sections),
        st.dictionaries(st.text(min_size=1, max_size=8), json_values, max_size=4),
        max_size=len(sections),
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reordered(value: Any, rng: random.Random) -> Any:
    """``value`` with every mapping's insertion order shuffled."""
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _reordered(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_reordered(item, rng) for item in value]
    return value


def _with_leaf(snapshot: dict[str, Any], section: str, key: str, value: Any) -> dict[str, Any]:
    changed = {name: dict(body) for name, body in snapshot.items()}
    changed.setdefault(section, {})[key] = value
    return changed


# -- digest determinism -------------------------------------------------------


@settings(max_examples=100)
@given(intent=st.text(max_size=200))
def test_the_intent_digest_is_a_pure_function_of_the_text(intent):
    """An equal string built separately, and the contract's own definition, agree."""
    rebuilt = "".join(list(intent))
    assert fp.intent_digest(rebuilt) == fp.intent_digest(intent)
    # docs/fingerprint-contract.md: the SHA-256 of the exact UTF-8 bytes.
    assert fp.intent_digest(intent) == hashlib.sha256(intent.encode("utf-8")).hexdigest()


@settings(max_examples=200)
@given(first=st.text(max_size=60), second=st.text(max_size=60))
def test_any_change_to_the_intent_text_moves_its_digest(first, second):
    """No whitespace or Unicode normalization: distinct text, distinct digest."""
    assume(first != second)
    assert fp.intent_digest(first) != fp.intent_digest(second)


@settings(max_examples=100)
@given(snapshot=_snapshot((*KNOWN_SECTIONS, "future")), seed=st.integers(0, 2**16))
def test_scoped_config_digests_ignore_key_order(snapshot, seed):
    shuffled = _reordered(snapshot, random.Random(seed))  # noqa: S311 - test data, not a secret
    for scope in (fp.review_relevant_config, fp.docs_relevant_config):
        assert json_digest(scope(snapshot)) == json_digest(scope(shuffled))


# -- configuration sensitivity --------------------------------------------------


@settings(max_examples=200)
@given(
    snapshot=_snapshot((*KNOWN_SECTIONS, "future")),
    section=st.sampled_from((*REVIEW_SECTIONS, "future")),
    key=st.text(min_size=1, max_size=8),
    value=json_values,
)
def test_any_change_inside_a_review_bound_section_moves_the_review_config_digest(
    snapshot, section, key, value
):
    """An unknown section counts as bound, so a future dependency cannot be dropped."""
    assume(not (section == "general" and key == "network_timeout_seconds"))
    before = snapshot.get(section, {}).get(key, ...)
    assume(before is ... or _canonical(before) != _canonical(value))
    changed = _with_leaf(snapshot, section, key, value)

    assert json_digest(fp.review_relevant_config(changed)) != json_digest(
        fp.review_relevant_config(snapshot)
    )


@settings(max_examples=200)
@given(
    snapshot=_snapshot((*KNOWN_SECTIONS, "future")),
    section=st.sampled_from((*DOCS_SECTIONS, "future")),
    key=st.text(min_size=1, max_size=8),
    value=json_values,
)
def test_any_change_inside_a_docs_bound_section_moves_the_docs_config_digest(
    snapshot, section, key, value
):
    assume(not (section == "general" and key == "network_timeout_seconds"))
    before = snapshot.get(section, {}).get(key, ...)
    assume(before is ... or _canonical(before) != _canonical(value))
    changed = _with_leaf(snapshot, section, key, value)

    assert json_digest(fp.docs_relevant_config(changed)) != json_digest(
        fp.docs_relevant_config(snapshot)
    )


@settings(max_examples=150)
@given(
    snapshot=_snapshot(KNOWN_SECTIONS),
    key=st.text(min_size=1, max_size=8),
    value=json_values,
    scope=st.sampled_from(["review", "docs"]),
)
def test_a_change_outside_the_bound_sections_leaves_the_config_digest_alone(
    snapshot, key, value, scope
):
    """A user-level ``[commands] test`` edit must not invalidate review evidence."""
    bound, relevant = (
        (REVIEW_SECTIONS, fp.review_relevant_config)
        if scope == "review"
        else (DOCS_SECTIONS, fp.docs_relevant_config)
    )
    unbound = sorted(set(KNOWN_SECTIONS) - set(bound))
    for section in unbound:
        changed = _with_leaf(snapshot, section, key, value)
        assert json_digest(relevant(changed)) == json_digest(relevant(snapshot)), section


@settings(max_examples=100)
@given(
    snapshot=_snapshot(KNOWN_SECTIONS),
    first=st.integers(min_value=1, max_value=10**6),
    second=st.integers(min_value=1, max_value=10**6),
)
def test_the_network_timeout_is_transport_only(snapshot, first, second):
    before = _with_leaf(snapshot, "general", "network_timeout_seconds", first)
    after = _with_leaf(snapshot, "general", "network_timeout_seconds", second)
    for relevant in (fp.review_relevant_config, fp.docs_relevant_config):
        assert json_digest(relevant(after)) == json_digest(relevant(before))


@settings(max_examples=100)
@given(
    snapshot=_snapshot(KNOWN_SECTIONS),
    model=st.sampled_from(["model-a", "model-b", "model-c"]),
    other=st.sampled_from(["model-a", "model-b", "model-c"]),
    prompt=st.sampled_from(["prompts/review.md", "prompts/strict.md"]),
    other_prompt=st.sampled_from(["prompts/review.md", "prompts/strict.md"]),
)
def test_a_different_reviewer_model_or_prompt_moves_the_review_config_digest(
    snapshot, model, other, prompt, other_prompt
):
    """The model and prompt reach the review only through ``[review] command``.

    The prompt file's own bytes are bound by the head tree; the command that names it
    is bound by the configuration digest.
    """
    assume((model, prompt) != (other, other_prompt))

    def configured(name: str, path: str) -> dict[str, Any]:
        command = f"reviewer --model {name} --prompt {path}"
        return _with_leaf(snapshot, "review", "command", command)

    assert json_digest(fp.review_relevant_config(configured(model, prompt))) != json_digest(
        fp.review_relevant_config(configured(other, other_prompt))
    )


# -- classification reports exactly what changed --------------------------------

_REVIEW_FIELDS: dict[str, tuple[fp.ReasonCode, st.SearchStrategy[Any]]] = {
    "base_tree_sha": (fp.ReasonCode.BASE_TREE_CHANGED, sha40),
    "head_tree_sha": (fp.ReasonCode.HEAD_TREE_CHANGED, sha40),
    "diff_sha256": (fp.ReasonCode.DIFF_CONTENT_CHANGED, sha64),
    "excluded_files": (
        fp.ReasonCode.EXCLUSIONS_CHANGED,
        st.lists(st.text(min_size=1, max_size=8), max_size=3).map(lambda xs: tuple(sorted(xs))),
    ),
    "grounding_sha256": (fp.ReasonCode.GROUNDING_CHANGED, st.none() | sha64),
    "intent_sha256": (fp.ReasonCode.INTENT_CHANGED, sha64),
    "executor": (fp.ReasonCode.EXECUTOR_CHANGED, st.sampled_from(["in_harness", "command"])),
    "config_sha256": (fp.ReasonCode.CONFIG_CHANGED, sha64),
}

_DOCS_FIELDS: dict[str, tuple[fp.ReasonCode, st.SearchStrategy[Any]]] = {
    "base_tree_sha": (fp.ReasonCode.BASE_TREE_CHANGED, sha40),
    "head_tree_sha": (fp.ReasonCode.HEAD_TREE_CHANGED, sha40),
    "doc_surface_sha256": (fp.ReasonCode.DOC_SURFACE_CHANGED, sha64),
    "config_sha256": (fp.ReasonCode.CONFIG_CHANGED, sha64),
    "intent_sha256": (fp.ReasonCode.INTENT_CHANGED, sha64),
    "grounding_sha256": (fp.ReasonCode.GROUNDING_CHANGED, st.none() | sha64),
}


def test_the_property_tables_cover_every_bound_field():
    """A new fingerprint field must join these tables, or the properties skip it."""
    unbound = {"version", "configuration_supported"}
    assert set(fp.ReviewFingerprint.model_fields) - unbound == set(_REVIEW_FIELDS)
    assert set(fp.DocsFingerprint.model_fields) - unbound == set(_DOCS_FIELDS)


def _fields(table: dict[str, tuple[fp.ReasonCode, st.SearchStrategy[Any]]]):
    return st.fixed_dictionaries({name: strategy for name, (_, strategy) in table.items()})


@settings(max_examples=200)
@given(old=_fields(_REVIEW_FIELDS), new=_fields(_REVIEW_FIELDS))
def test_review_classification_names_exactly_the_changed_inputs(old, new):
    changed = {name for name in _REVIEW_FIELDS if old[name] != new[name]}
    result = fp.classify_review(fp.ReviewFingerprint(**old), fp.ReviewFingerprint(**new))

    if changed:
        assert result.disposition is fp.Disposition.INVALID
    else:
        assert result.disposition is fp.Disposition.REUSABLE
    assert set(result.reasons) == {_REVIEW_FIELDS[name][0] for name in changed}
    assert len(result.reasons) == len(set(result.reasons))


@settings(max_examples=200)
@given(old=_fields(_REVIEW_FIELDS), name=st.sampled_from(sorted(_REVIEW_FIELDS)), data=st.data())
def test_changing_any_single_review_input_invalidates_with_its_reason(old, name, data):
    """The case-study failure: one shaping input moved and the green stayed."""
    reason, strategy = _REVIEW_FIELDS[name]
    value = data.draw(strategy.filter(lambda candidate: candidate != old[name]))
    result = fp.classify_review(
        fp.ReviewFingerprint(**old), fp.ReviewFingerprint(**{**old, name: value})
    )
    assert result.disposition is fp.Disposition.INVALID
    assert result.reasons == (reason,)


@settings(max_examples=200)
@given(old=_fields(_DOCS_FIELDS), new=_fields(_DOCS_FIELDS))
def test_docs_classification_names_exactly_the_changed_inputs(old, new):
    changed = {name for name in _DOCS_FIELDS if old[name] != new[name]}
    result = fp.classify_docs(fp.DocsFingerprint(**old), fp.DocsFingerprint(**new))

    if changed:
        assert result.disposition is fp.Disposition.INVALID
    else:
        assert result.disposition is fp.Disposition.REUSABLE
    assert set(result.reasons) == {_DOCS_FIELDS[name][0] for name in changed}


@settings(max_examples=100)
@given(
    old=_fields(_REVIEW_FIELDS),
    old_supported=st.booleans(),
    new_supported=st.booleans(),
    old_version=st.sampled_from([1, fp.FINGERPRINT_VERSION, fp.FINGERPRINT_VERSION + 1]),
    new_version=st.sampled_from([1, fp.FINGERPRINT_VERSION, fp.FINGERPRINT_VERSION + 1]),
)
def test_unknown_provenance_is_never_reusable_even_when_every_field_matches(
    old, old_supported, new_supported, old_version, new_version
):
    first = fp.ReviewFingerprint(**old, configuration_supported=old_supported, version=old_version)
    second = fp.ReviewFingerprint(**old, configuration_supported=new_supported, version=new_version)
    known = old_supported and new_supported and old_version == new_version
    known = known and old_version == fp.FINGERPRINT_VERSION
    expected = fp.Disposition.REUSABLE if known else fp.Disposition.UNKNOWN
    assert fp.classify_review(first, second).disposition is expected
