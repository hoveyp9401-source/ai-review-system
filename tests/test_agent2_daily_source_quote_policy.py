from app.agent2.tool_calling.canary_config import canary_system_prompt


def test_daily_source_policy_preserves_complete_risk_and_separate_matters() -> None:
    prompt = " ".join(
        canary_system_prompt(
            allowed_tool_names=frozenset({"add_daily_items"})
        ).split()
    )

    assert "A risk item's exact_quote must" in prompt
    assert "conditions, deadlines, consequences, exceptions" in prompt
    assert "Independent matters with" in prompt
    assert "different actions or objects must remain separate items" in prompt
    assert "one independently editable action or object per item" in prompt
    assert "compound sentence must not hide two actions in one item" in prompt
    assert "source spans for separate items must not overlap" in prompt
    assert "Keep conditions and consequences attached" in prompt
    assert "to their governing item" in prompt
    assert "Never shorten exact_quote into a summary" in prompt
    assert "exact_quote must be contiguous user-authored text" in prompt
    assert "Never drop a negation, condition, deadline" in prompt
    assert "even when source punctuation or whitespace separates it" in prompt
