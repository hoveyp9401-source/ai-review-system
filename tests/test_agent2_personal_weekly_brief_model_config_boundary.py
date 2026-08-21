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
