from app.agent2.tool_calling.canary_config import canary_system_prompt


def test_daily_source_policy_preserves_complete_risk_and_separate_matters() -> None:
    prompt = " ".join(
        canary_system_prompt(
            allowed_tool_names=frozenset({"add_daily_items"})
        ).split()
    )

    assert "A risk item's exact_quote must" in prompt
    assert "conditions, deadlines, consequences, exceptions" in prompt
    assert "smallest coherent work topic or outcome" in prompt
    assert "not the smallest verb-object pair" in prompt
    assert "Split when the source switches to an unrelated goal" in prompt
    assert "shared workstream" in prompt
    assert "source spans for separate items must not overlap" in prompt
    assert "Keep conditions and consequences attached" in prompt
    assert "to their governing item" in prompt
    assert "Never shorten exact_quote into a summary" in prompt
    assert "exact_quote must be contiguous user-authored text" in prompt
    assert "Never drop a negation, condition, deadline" in prompt
    assert "even when source punctuation or whitespace separates it" in prompt
    assert "unmistakably identifies the failed write" in prompt
    assert "Broad delegation, general permission" in prompt
    assert "leaving the action to the assistant does not authorize" in prompt
