from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_name: str = "ai-review-system"
    app_env: str = "production"
    timezone: str = "Asia/Shanghai"
    clock_override_enabled: bool = False
    clock_override_now: str = ""

    database_url: str = "postgresql+asyncpg://review:review_password@localhost:5432/review"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4-pro"
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_intent_model: str = "deepseek-v4-flash"
    llm_extract_model: str = "deepseek-v4-flash"
    llm_summary_model: str = "deepseek-v4-pro"
    llm_high_risk_model: str = "deepseek-v4-pro"
    llm_draft_decision_enabled: bool = False
    llm_draft_decision_model: str = "deepseek-v4-flash"
    report_agent_enabled: bool = False
    report_agent_shadow_mode: bool = False
    report_agent_model: str = "deepseek-v4-flash"
    llm_intent_thinking: bool = False
    llm_extract_thinking: bool = False
    llm_summary_thinking: bool = True
    llm_high_risk_thinking: bool = True
    llm_draft_decision_thinking: bool = False
    report_agent_thinking: bool = False
    llm_intent_timeout_seconds: float = Field(default=8.0, gt=0)
    llm_extract_timeout_seconds: float = Field(default=15.0, gt=0)
    llm_summary_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_high_risk_timeout_seconds: float = Field(default=15.0, gt=0)
    llm_draft_decision_timeout_seconds: float = Field(default=10.0, gt=0)
    report_agent_timeout_seconds: float = Field(default=10.0, gt=0)
    llm_intent_max_retries: int = Field(default=0, ge=0)
    llm_extract_max_retries: int = Field(default=0, ge=0)
    llm_summary_max_retries: int = Field(default=2, ge=0)
    llm_high_risk_max_retries: int = Field(default=0, ge=0)
    llm_draft_decision_max_retries: int = Field(default=0, ge=0)
    report_agent_max_retries: int = Field(default=0, ge=0)
    llm_intent_fallback_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    admin_enabled: bool = False
    admin_token: str = ""
    shadow_memory_enabled: bool = False
    progress_enabled: bool = False
    progress_outbox_enabled: bool = False
    progress_worker_enabled: bool = False
    progress_shadow_only: bool = True
    progress_write_updates: bool = False
    progress_worker_batch_size: int = Field(default=50, ge=1, le=500)
    progress_worker_max_retries: int = Field(default=5, ge=0)
    progress_worker_stale_lock_minutes: int = Field(default=10, ge=1)
    progress_outbox_user_ids: str = ""
    progress_outbox_team_ids: str = ""
    workflow_intake_mode: str = "observe_only"
    agent2_daily_enabled: bool = False
    agent2_daily_enabled_user_ids: str = ""

    dingtalk_incoming_token: str = ""
    dingtalk_callback_token: str = ""
    dingtalk_callback_aes_key: str = ""
    dingtalk_default_robot_webhook: str = ""
    dingtalk_default_robot_secret: str = ""
    dingtalk_corp_id: str = ""
    dingtalk_agent_id: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""

    scheduler_enabled: bool = False
    reminder_send_enabled: bool = False
    reminder_dry_run: bool = True
    reminder_test_user_ids: str = ""
    reminder_cron_hour: int = Field(default=20, ge=0, le=23)
    second_reminder_cron_hour: int = Field(default=22, ge=0, le=23)
    auto_submit_cron_hour: int = Field(default=23, ge=0, le=23)
    catchup_reminder_enabled: bool = False
    catchup_reminder_cron_hour: int = Field(default=9, ge=0, le=23)
    summary_cron_hour: int = Field(default=9, ge=0, le=23)
    summary_cron_minute: int = Field(default=0, ge=0, le=59)
    scheduler_pause_dates: str = ""

    min_completeness_score: float = Field(default=1.0, ge=0.0, le=1.0)
    stream_worker_count: int = Field(default=6, ge=1, le=20)
    stream_queue_size: int = Field(default=200, ge=1)
    stream_processing_timeout_seconds: float = Field(default=120.0, gt=0)
    stream_reply_timeout_seconds: float = Field(default=8.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
