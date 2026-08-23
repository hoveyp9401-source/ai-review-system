from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent2.personal_weekly_brief import (
    PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
    PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
)

from scripts.run_agent2_personal_weekly_brief_concurrency_probe import (
    _settings,
)
from scripts.run_agent2_personal_weekly_brief_model_eval import (
    _assert_complex,
    _read_model_only_config,
)


def test_real_model_gate_loads_only_allowlisted_llm_fields(tmp_path) -> None:
    env_file = tmp_path / "private.env"
    env_file.write_text(
        "\n".join(
            (
                "LLM_BASE_URL=https://model-gateway.invalid/v1",
                "LLM_API_KEY=redacted-test-key",
                "LLM_TIMEOUT_SECONDS=60",
                "LLM_MAX_RETRIES=1",
                "DATABASE_URL=postgresql://must:not@be-loaded/production",
                "DINGTALK_APP_SECRET=must-not-be-loaded",
                "DINGTALK_DEFAULT_ROBOT_WEBHOOK=https://must-not-be-loaded.invalid",
            )
        ),
        encoding="utf-8",
    )

    selected = _read_model_only_config(env_file)
    settings, host, loaded_fields = _settings(env_file)

    assert set(selected) == {
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    }
    assert loaded_fields == tuple(sorted(selected))
    assert host == "model-gateway.invalid"
    assert settings.database_url.endswith("@127.0.0.1:1/unused")
    assert settings.dingtalk_app_secret == ""
    assert settings.dingtalk_default_robot_webhook == ""


def test_production_weekly_brief_uses_bounded_reasoned_flash_reviews() -> None:
    assert PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED is False
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED is False
    assert PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED is False
    assert PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS == 8000
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS == 8000
    assert PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS == 8000

    runner = Path("app/scheduler/runner.py").read_text(encoding="utf-8")
    assert runner.count(
        "thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED"
    ) == 2
    assert runner.count(
        "thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED"
    ) == 2
    assert runner.count(
        "thinking_enabled=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED"
    ) == 2
    assert runner.count(
        "max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS"
    ) == 2
    assert runner.count("max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS") == 2
    assert runner.count(
        "max_tokens=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS"
    ) == 2
    assert PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS == 3
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES == 1
    assert runner.count(
        "max_semantic_attempts=PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS"
    ) == 2
    assert runner.count("review_votes=PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES") == 2
    model_eval = Path(
        "scripts/run_agent2_personal_weekly_brief_model_eval.py"
    ).read_text(encoding="utf-8")
    assert "maximum_fifteen_call_owner_seconds" in model_eval
    assert model_eval.count("PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES") >= 5


def test_complex_oracle_rejects_weekly_plan_linked_to_wrong_daily_matter() -> None:
    def item(matter_key, source_ids, *, status="", text=""):
        return SimpleNamespace(
            matter_key=matter_key,
            source_ids=set(source_ids),
            status=status,
            text=text,
        )

    completed = SimpleNamespace(
        items=(
            item(
                "star-river",
                {"daily:star:mon", "daily:star:thu"},
                text="对方没有承诺付款，只有付款条件确认后才答复。",
            ),
        )
    )
    plan_progress = SimpleNamespace(
        items=(
            item("a", {"plan:completed", "daily:completed"}, status="已完成"),
            item("b", {"plan:ongoing", "daily:ongoing"}, status="持续推进"),
            item("c", {"plan:adjusted", "daily:adjusted"}, status="安排调整"),
            item("d", {"plan:future", "daily:future"}, status="后续安排"),
            item("e", {"plan:no-followup"}, status="暂时没有找到后续记录"),
        )
    )
    content = SimpleNamespace(
        completed=completed,
        plan_progress=plan_progress,
        possible_open_loops=SimpleNamespace(items=()),
        message_text=(
            "涉案金额120万元，对方原定8月20日前回复。"
            "对方没有承诺付款，只有付款条件确认后才答复。"
            "暂无后续记录仅表示现有记录中没有找到明确对应内容。"
        ),
    )
    _assert_complex(content)

    content.plan_progress.items[0].source_ids = {
        "plan:completed",
        "daily:star:mon",
    }
    with pytest.raises(AssertionError, match="wrong daily matter"):
        _assert_complex(content)
