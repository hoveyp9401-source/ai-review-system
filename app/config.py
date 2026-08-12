from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )

    app_name: str = "ai-review-system"
    app_env: str = "production"
    timezone: str = "Asia/Shanghai"
    clock_override_enabled: bool = False
    clock_override_now: str = ""

    database_url: str = (
        "postgresql+asyncpg://example_user:example_password@localhost:5432/example_db"
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)

    llm_base_url: str = "https://api.example.invalid/v1"
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
    legal_ops_sandbox_enabled: bool = False
    legal_ops_sandbox_data_path: str = "data/legal_ops_sandbox.json"
    legal_ops_sandbox_seed_manifest: str = "app/legal_ops/fixtures/phase0_manifest.json"
    legal_ops_sandbox_default_tenant: str = ""
    legal_ops_sandbox_token: str = ""
    legal_ops_sandbox_principals_json: str = ""
    legal_ops_live_enabled: bool = False
    legal_ops_live_tenant_id: str = ""
    legal_ops_live_token: str = ""
    legal_ops_live_principals_json: str = ""
    legal_ops_data_intake_enabled: bool = False
    agent2_performance_knowledge_enabled: bool = False
    agent2_performance_tool_enabled: bool = False
    agent2_performance_knowledge_timeout_seconds: float = Field(
        default=8.0,
        gt=0,
        le=15.0,
    )
    legal_ops_data_intake_storage_path: str = "data/legal_ops_data_intake"
    legal_ops_data_intake_max_file_mb: int = Field(default=25, ge=1, le=100)
    legal_ops_data_intake_max_rows: int = Field(default=100000, ge=1, le=500000)
    legal_ops_rule_understanding_enabled: bool = True
    legal_ops_rule_understanding_model: str = "deepseek-v4-pro"
    legal_ops_rule_understanding_timeout_seconds: float = Field(default=90.0, gt=0)
    legal_ops_rule_understanding_long_document_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        le=300,
    )
    legal_daily_dashboard_enabled: bool = False
    agent2_cross_user_daily_read_enabled: bool = False
    legal_daily_dashboard_tenant_id: str = ""
    legal_daily_dashboard_token: str = ""
    legal_daily_dashboard_principals_json: str = ""
    legal_daily_dashboard_system_user_id: str = "system:legal-daily-dashboard"
    management_daily_briefing_department_cc_user_ids: str = ""
    management_daily_briefing_hidden_missing_detail_user_ids: str = ""
    legal_daily_dashboard_manager_write_enabled: bool = False
    legal_daily_dashboard_analysis_enabled: bool = False
    legal_daily_dashboard_analysis_model: str = ""
    legal_daily_dashboard_analysis_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=300,
    )
    legal_daily_dashboard_analysis_max_retries: int = Field(
        default=0,
        ge=0,
        le=1,
    )
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
    agent2_cognitive_core_v3_enabled: bool = False
    agent2_cognitive_core_v3_model: str = "deepseek-v4-pro"
    agent2_cognitive_core_v3_thinking: bool = True
    agent2_semantic_admission_enabled: bool = False
    agent2_semantic_admission_enforce: bool = False
    agent2_semantic_admission_review_capture: bool = False
    agent2_semantic_admission_deferred_capture: bool = False
    agent2_semantic_admission_shadow_replay: bool = False
    agent2_semantic_admission_tenant_allowlist: str = ""
    agent2_semantic_admission_user_allowlist: str = ""
    agent2_business_phase2_enabled: bool = False
    agent2_business_tenant_ids: str = ""
    agent2_business_party_query_enabled: bool = False
    agent2_business_case_progress_enabled: bool = False
    agent2_business_case_progress_write_enabled: bool = False
    agent2_business_travel_enabled: bool = False
    agent2_business_travel_write_enabled: bool = False
    agent2_travel_notification_worker_enabled: bool = False
    agent2_travel_notification_worker_interval_seconds: int = Field(
        default=15, ge=5, le=3600
    )
    agent2_travel_notification_batch_size: int = Field(default=50, ge=1, le=500)
    agent2_travel_notification_max_attempts: int = Field(default=5, ge=1, le=20)
    agent2_travel_notification_retry_base_seconds: int = Field(
        default=30, ge=1, le=3600
    )
    agent2_travel_notification_stale_lock_minutes: int = Field(
        default=10, ge=1, le=1440
    )
    agent2_case_followup_enabled: bool = False
    agent2_case_followup_send_enabled: bool = False
    agent2_case_followup_tenant_ids: str = ""
    agent2_case_followup_user_ids: str = ""
    case_followup_enabled: bool = False
    case_followup_send_enabled: bool = False
    case_followup_report_projection_enabled: bool = False
    case_followup_user_allowlist: str = ""
    case_followup_tenant_allowlist: str = ""
    case_followup_trigger_allowlist: str = ""
    case_followup_conversation_map_json: str = ""
    case_followup_daily_limit: int = Field(default=3, ge=1, le=50)
    case_followup_case_daily_limit: int = Field(default=1, ge=1, le=10)
    case_followup_merge_window_minutes: int = Field(default=30, ge=1, le=1440)
    case_followup_reminder_interval_hours: int = Field(default=48, ge=1, le=720)
    case_followup_max_reminders: int = Field(default=1, ge=0, le=10)

    dingtalk_incoming_token: str = ""
    dingtalk_callback_token: str = ""
    dingtalk_callback_aes_key: str = ""
    dingtalk_default_robot_webhook: str = ""
    dingtalk_default_robot_secret: str = ""
    dingtalk_corp_id: str = ""
    dingtalk_agent_id: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_api_base_url: str = "https://api.dingtalk.com"
    dingtalk_oapi_base_url: str = "https://oapi.dingtalk.com"

    scheduler_enabled: bool = False
    reminder_send_enabled: bool = False
    reminder_dry_run: bool = True
    reminder_test_user_ids: str = ""
    reminder_cron_hour: int = Field(default=20, ge=0, le=23)
    second_reminder_cron_hour: int = Field(default=22, ge=0, le=23)
    auto_submit_cron_hour: int = Field(default=8, ge=0, le=23)
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
    agent2_tool_call_canary_max_active_users: int = Field(
        default=1,
        ge=1,
        le=74,
    )
    agent2_canary_turn_batch_quiet_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=5.0,
    )
    agent2_canary_turn_batch_max_window_seconds: float = Field(
        default=4.0,
        ge=0.1,
        le=10.0,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
