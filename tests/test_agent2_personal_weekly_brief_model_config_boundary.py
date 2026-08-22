from pathlib import Path

from app.agent2.personal_weekly_brief import (
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


def test_production_weekly_brief_uses_two_fast_independent_flash_calls() -> None:
    assert PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED is False
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED is False
    assert PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS == 4000
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS == 2000

    runner = Path("app/scheduler/runner.py").read_text(encoding="utf-8")
    assert runner.count(
        "thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED"
    ) == 2
    assert runner.count(
        "thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED"
    ) == 2
    assert runner.count(
        "max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS"
    ) == 2
    assert runner.count("max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS") == 2
    assert PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS == 3
    assert PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES == 3
    assert runner.count(
        "max_semantic_attempts=PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS"
    ) == 2
    assert runner.count("review_votes=PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES") == 2
    model_eval = Path(
        "scripts/run_agent2_personal_weekly_brief_model_eval.py"
    ).read_text(encoding="utf-8")
    assert "maximum_twelve_call_owner_seconds" in model_eval
    assert model_eval.count("PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES") >= 5
