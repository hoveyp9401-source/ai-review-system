import pytest

from app.agent.action_plan import ActionPlan, AgentAction
from app.agent.decision_router import DecisionRouter
from app.agent.report_agent import AgentDecisionResult
from app.llm.extractor import LLMOutputError


class FakeReportAgent:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def decide_with_meta(self, *, raw_input, context):
        self.calls += 1
        self.last_raw_input = raw_input
        self.last_context = context
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_safe_direct_plan_wins_without_llm():
    plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="clear_all")],
        reason="direct rule",
    )
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u6e05\u7a7a\u5f53\u524d\u65e5\u62a5",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: ("direct_clear_current_report", plan),
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "direct_rule"
    assert route.branch == "direct_clear_current_report"
    assert route.plan is plan
    assert route.entered_llm is False
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_edit_direct_plan_defers_to_report_agent():
    direct_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="delete_item", field="today_work", item_indices=[2, 3])],
        reason="old edit rule",
    )
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_text", field="today_work", item_indices=[2], text="combined item")],
        reason="llm semantic edit",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u628a\u4eca\u65e5\u5de5\u4f5c\u7684\u7b2c2\u6761\u6539\u6210combined item",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: ("direct_ordinal_delete", direct_plan),
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.plan is llm_plan
    assert route.entered_llm is True
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_pending_state_wins_without_llm():
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u786e\u8ba4",
        context={
            "pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "submit_report",
                "target_field": "none",
                "context": {},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "pending_state"
    assert route.branch == "confirm_submit_report"
    assert route.plan is not None
    assert route.plan.intent == "confirm_submit"
    assert route.entered_llm is False
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_pending_edit_instruction_defers_to_report_agent():
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_text", field="today_work", item_indices=[2], text="combined item")],
        reason="llm semantic edit",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u628a\u4eca\u65e5\u5de5\u4f5c\u7684\u7b2c2\u6761\u6539\u6210combined item",
        context={
            "pending_interaction": {
                "type": "current_report_edit_flow",
                "operation": "modify_report",
                "target_field": "none",
                "context": {
                    "target_date": "2026-06-22",
                    "current_report": {
                        "today_work": ["A", "B", "C"],
                        "problems": [],
                        "tomorrow_plan": [],
                    },
                },
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.plan is llm_plan
    assert route.entered_llm is True
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_report_agent_success_route():
    plan = ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=["semantic routing test"])],
        reason="llm decision",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="semantic routing test",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.plan is plan
    assert route.entered_llm is True
    assert route.meta["model"] == "fake"
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_report_agent_error_can_use_fallback_plan():
    fallback = ActionPlan(
        intent="fill_report",
        confidence="medium",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=["fallback record"])],
        reason="fallback",
    )
    agent = FakeReportAgent(error=LLMOutputError("bad json", meta={"model": "fake", "thinking": True, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="fallback record",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: fallback,
    )

    assert route.source == "report_agent_error_fallback"
    assert route.plan is fallback
    assert route.entered_llm is True
    assert route.meta["fallback"] == "simple_report_fields"
    assert route.meta["original_error"] == "bad json"
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_report_agent_error_rejects_edit_fallback_plan():
    fallback = ActionPlan(
        intent="edit_draft",
        confidence="medium",
        should_write=True,
        actions=[AgentAction(type="delete_item", field="today_work", item_indices=[2, 3])],
        reason="unsafe edit fallback",
    )
    agent = FakeReportAgent(error=LLMOutputError("bad json", meta={"model": "fake", "thinking": True, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u628a\u4eca\u65e5\u5de5\u4f5c\u7684\u7b2c2\u6761\u6539\u6210combined item",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: fallback,
    )

    assert route.source == "report_agent_error"
    assert route.plan is None
    assert route.entered_llm is True
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_pending_clarification_yields_to_new_edit_intent():
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field="today_work", items=["reorganized work"])],
        reason="llm handles new edit intent",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u91cd\u65b0\u6574\u7406\u4eca\u65e5\u5de5\u4f5c",
        context={
            "pending_interaction": {
                "type": "pending_clarification",
                "operation": "merge_items",
                "target_field": "today_work",
                "context": {"item_indices": [5, 6]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.entered_llm is True
    assert route.meta["pending_bypassed"] is True
    assert route.plan is not None
    assert route.plan.clear_pending_interaction is True
    assert "pending_interaction" not in agent.last_context


@pytest.mark.asyncio
async def test_pending_clarification_yields_to_full_report_paste():
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field="today_work", items=["full pasted report work"])],
        reason="llm handles full pasted report",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input=(
            "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. A\n2. B\n\n"
            "\u95ee\u9898\u98ce\u9669\uff1a\u65e0\n\n"
            "\u660e\u65e5\u8ba1\u5212\uff1a\u7ee7\u7eed\u5904\u7406"
        ),
        context={
            "pending_interaction": {
                "type": "pending_clarification",
                "operation": "merge_items",
                "target_field": "today_work",
                "context": {"item_indices": [2, 7]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.entered_llm is True
    assert route.plan is not None
    assert route.plan.clear_pending_interaction is True
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_pending_confirmation_keeps_explicit_confirm_delete_without_llm():
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u786e\u8ba4\u5220\u9664",
        context={
            "pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_item",
                "target_field": "today_work",
                "context": {"item_indices": [2]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "pending_state"
    assert route.branch == "confirm_delete_item"
    assert route.entered_llm is False
    assert agent.calls == 0
    assert route.plan is not None
    assert route.plan.actions[0].type == "delete_item"


@pytest.mark.asyncio
async def test_pending_append_target_keeps_section_choice_without_llm():
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input="\u4eca\u65e5\u5de5\u4f5c",
        context={
            "pending_interaction": {
                "type": "awaiting_append_target",
                "operation": "append",
                "target_field": "none",
                "context": {},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "pending_state"
    assert route.branch == "select_append_target"
    assert route.entered_llm is False
    assert agent.calls == 0
    assert route.plan is not None
    assert route.plan.pending_interaction_to_set is not None
    assert route.plan.pending_interaction_to_set.target_field == "today_work"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_input", ["\u55ef", "\u786e\u8ba4\u6e05\u7a7a", "\u53d6\u6d88"])
async def test_pending_action_confirmation_keeps_short_continuations_without_llm(raw_input):
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input=raw_input,
        context={
            "pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "clear_report",
                "target_field": "none",
                "context": {
                    "actions": [{"type": "clear_all", "requires_confirmation": True}],
                },
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "pending_state"
    assert route.entered_llm is False
    assert agent.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_input", ["\u7b2c3\u6761", "1-3", "\u4eca\u65e5\u5de5\u4f5c"])
async def test_pending_clarification_keeps_short_index_or_section_answers_without_llm(raw_input):
    agent = FakeReportAgent()
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input=raw_input,
        context={
            "pending_interaction": {
                "type": "pending_clarification",
                "operation": "merge_items",
                "target_field": "today_work",
                "context": {"item_indices": [2, 4]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "pending_state"
    assert route.entered_llm is False
    assert agent.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input",
    [
        "\u91cd\u65b0\u6574\u7406\u4eca\u65e5\u5de5\u4f5c",
        "\u5220\u9664\u4eca\u65e5\u5de5\u4f5c",
        "\u63091-7\u91cd\u65b0\u5217\u51fa\u6765",
        "\u628a\u4eca\u65e5\u5de5\u4f5c\u6539\u6210\u5ba1\u683810\u4efd\u5408\u540c",
    ],
)
async def test_pending_clarification_bypasses_for_new_edit_intents(raw_input):
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field="today_work", items=["updated by llm"])],
        reason="llm handles interrupted pending",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input=raw_input,
        context={
            "pending_interaction": {
                "type": "pending_clarification",
                "operation": "merge_items",
                "target_field": "today_work",
                "context": {"item_indices": [2, 7]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.entered_llm is True
    assert route.meta["pending_bypassed"] is True
    assert route.plan is not None
    assert route.plan.clear_pending_interaction is True
    assert "pending_interaction" not in agent.last_context


@pytest.mark.asyncio
async def test_awaiting_append_content_bypasses_for_full_report_payload():
    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field="today_work", items=["full report work"])],
        reason="llm handles full report while append pending",
    )
    agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(agent)

    route = await router.decide(
        raw_input=(
            "\u4eca\u65e5\u5de5\u4f5c\uff1a\n1. A\n\n"
            "\u95ee\u9898\u98ce\u9669\uff1a\u65e0\n\n"
            "\u660e\u65e5\u8ba1\u5212\uff1aB"
        ),
        context={
            "pending_interaction": {
                "type": "awaiting_append_content",
                "operation": "append",
                "target_field": "today_work",
                "context": {},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert route.source == "report_agent"
    assert route.entered_llm is True
    assert route.plan is not None
    assert route.plan.clear_pending_interaction is True


@pytest.mark.asyncio
async def test_confirm_delete_with_target_depends_on_pending_state():
    pending_agent = FakeReportAgent()
    router = DecisionRouter(pending_agent)

    pending_route = await router.decide(
        raw_input="\u786e\u8ba4\u5220\u9664\u4eca\u65e5\u5de5\u4f5c",
        context={
            "pending_interaction": {
                "type": "awaiting_action_confirmation",
                "operation": "delete_item",
                "target_field": "today_work",
                "context": {"item_indices": [2]},
            }
        },
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert pending_route.source == "pending_state"
    assert pending_route.entered_llm is False
    assert pending_agent.calls == 0

    llm_plan = ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="delete_item", field="today_work", item_indices=[1])],
    )
    llm_agent = FakeReportAgent(AgentDecisionResult(payload=llm_plan, meta={"model": "fake", "thinking": False, "timeout": False}))
    router = DecisionRouter(llm_agent)

    no_pending_route = await router.decide(
        raw_input="\u786e\u8ba4\u5220\u9664\u4eca\u65e5\u5de5\u4f5c",
        context={},
        existing=None,
        direct_plan_resolver=lambda _raw, _existing: None,
        fallback_plan_builder=lambda _raw, _existing: None,
    )

    assert no_pending_route.source == "report_agent"
    assert no_pending_route.entered_llm is True
    assert llm_agent.calls == 1
