from __future__ import annotations

from app.agent2.evaluation.adversarial_corpus import generate_adversarial_corpus


def test_adversarial_corpus_is_deterministic_blind_and_four_dimensional():
    first = generate_adversarial_corpus(seed=20260710, variants_per_template=1)
    second = generate_adversarial_corpus(seed=20260710, variants_per_template=1)

    assert first.input_pack.digest == second.input_pack.digest
    assert len(first.input_pack.cases) >= 32
    assert {row["category"] for row in first.source_records} == {
        "semantic",
        "context",
        "safety",
        "composition",
    }
    assert all(
        label["independent_review_status"] == "pending"
        for label in first.sealed_labels.labels
    )
    assert "expected_write_intent" not in str(first.input_pack.as_mapping())


def test_quoted_example_variants_are_sealed_as_no_write_candidates():
    corpus = generate_adversarial_corpus(seed=17, variants_per_template=4)
    labels = {
        (label["case_id"], label["turn_id"]): label
        for label in corpus.sealed_labels.labels
    }

    for record in corpus.source_records:
        if record["variant"] != "quoted_example":
            continue
        label = labels[(record["case_id"], record["turn_id"])]
        assert label["expected_write_intent"] is False
