from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_production_runtime_constructs_and_injects_weekly_executor():
    source = _read("app/agent2/tool_calling/production_runtime.py")

    assert "ProductionWeeklyPlanExecutor(" in source
    assert "weekly_plan_executor=weekly_plan_executor" in source
    assert "current_turn_source=current_turn_source" in source


def test_production_runtime_counts_weekly_state_as_a_business_write():
    source = _read("app/agent2/tool_calling/production_runtime.py")

    assert 'before.payload().get("weekly_plans", [])' in source
    assert 'after.payload().get("weekly_plans", [])' in source
    assert "or weekly_changed" in source
