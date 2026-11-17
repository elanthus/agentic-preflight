"""Human labels are supplied fixtures, never model judgments or network calls."""

import copy

import pytest

from evals.metrics import adjudication_digest, adjudication_score, wilson95


def fixtures():
    trials = [
        {
            "id": "case/vulnerable/on/1",
            "findings": [{"title": "seed"}, {"title": "duplicate"}, {"title": "false"}],
            "seeded_defects": ["bug"],
        },
        {
            "id": "case/fixed/on/1",
            "findings": [{"title": "other valid defect"}],
            "seeded_defects": [],
        },
        {"id": "miss/vulnerable/on/1", "findings": [], "seeded_defects": ["missed"]},
    ]
    labels = [
        {"trial": trials[0]["id"], "finding": 0, "label": "true_positive", "seeded_defect": "bug"},
        {"trial": trials[0]["id"], "finding": 1, "label": "duplicate", "duplicate_of": 0},
        {"trial": trials[0]["id"], "finding": 2, "label": "false_positive"},
        {"trial": trials[1]["id"], "finding": 0, "label": "true_positive"},
    ]
    return trials, {
        "schema_version": 1,
        "run_id": "fixture-run",
        "inventory_sha256": adjudication_digest(trials),
        "labels": [
            {**label, "reason": "Fixture evidence.", "labeller": "human:test"} for label in labels
        ],
    }


def test_precision_includes_fixed_controls_and_duplicates_recall_counts_seeds_once():
    trials, labels = fixtures()
    score = adjudication_score(trials, labels)
    assert score["precision"]["numerator"] == 2
    assert score["precision"]["denominator"] == 4
    assert score["recall"]["numerator"] == 1
    assert score["recall"]["denominator"] == 2
    assert score["duplicates"] == 1
    assert score["labellers"] == ["human:test"]
    assert score["precision"]["lower"] == pytest.approx(0.15003899)
    assert score["precision"]["upper"] == pytest.approx(0.84996101)


def test_empty_denominators_are_unknown_and_extremes_have_nonzero_uncertainty():
    assert wilson95(0, 0)["value"] is None
    assert wilson95(0, 10)["upper"] == pytest.approx(0.2775328)
    assert wilson95(10, 10)["lower"] == pytest.approx(0.7224672)
    for counts in [(-1, 1), (2, 1), (True, 1), (0, -1)]:
        with pytest.raises(ValueError, match=r"."):
            wilson95(*counts)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc["labels"].pop(),
        lambda doc: doc["labels"].append(copy.deepcopy(doc["labels"][0])),
        lambda doc: doc["labels"][0].update(label="maybe"),
        lambda doc: doc["labels"][0].update(reason=""),
        lambda doc: doc["labels"][0].update(labeller=""),
        lambda doc: doc["labels"][0].update(reason="two\nlines"),
        lambda doc: doc["labels"][0].update(seeded_defect="unknown"),
        lambda doc: doc["labels"][1].update(duplicate_of=1),
        lambda doc: doc["labels"][1].update(duplicate_of=2),
        lambda doc: doc["labels"][0].update(finding=True),
        lambda doc: doc.update(schema_version=True),
    ],
)
def test_incomplete_or_invalid_labels_cannot_produce_scores(mutation):
    trials, labels = fixtures()
    mutation(labels)
    with pytest.raises(ValueError, match=r"."):
        adjudication_score(trials, labels)


def test_stale_labels_cannot_score_a_changed_finding_inventory():
    trials, labels = fixtures()
    trials[0]["findings"][0]["title"] = "different finding at same index"
    with pytest.raises(ValueError, match="digest"):
        adjudication_score(trials, labels)


@pytest.mark.parametrize("trials", [None, {}, [None], ["bad trial"], [{"id": "bad"}]])
def test_malformed_inventory_is_rejected_before_digest_comparison(trials):
    _, labels = fixtures()
    with pytest.raises(ValueError, match=r"inventory|trial"):
        adjudication_score(trials, labels)


def test_non_json_inventory_is_rejected_as_invalid_input():
    trials, labels = fixtures()
    trials[0]["findings"][0]["unsupported"] = {1, 2}
    with pytest.raises(ValueError, match="JSON data"):
        adjudication_score(trials, labels)


@pytest.mark.parametrize("document", [None, [], "invalid", 1, True])
def test_adjudication_document_must_be_an_object(document):
    trials, _ = fixtures()
    with pytest.raises(ValueError, match="adjudication"):
        adjudication_score(trials, document)
