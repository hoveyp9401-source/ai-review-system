# 功能—文件地图

本表用于统一说明能力边界和主要入口，不代表列出的每个模块都已启用。状态以 [生产基线](PRODUCTION_BASELINE.md) 和 [未上线能力](NOT_YET_LIVE.md) 为准。

| 能力 | 状态 | 入口和核心文件 | 代表性安全测试 |
|---|---|---|---|
| API、Stream、Scheduler | LIVE | `app/main.py`、`app/stream_runner.py`、`app/scheduler/runner.py`、`app/scheduler/jobs.py` | `tests/test_agent2_stream_context_wiring.py` |
| 日报填写与查询 | LIVE | `app/api/reports.py`、`app/api/webhook.py`、`app/services/report_service.py`、`app/agent2/tool_calling/production_daily_executor.py` | `tests/test_agent2_daily_execution_guardrails.py`、`tests/test_agent_core_daily_capability.py` |
| 日报结构化命令 | LIVE | `app/agent2/daily_execution.py`、`app/agent2/typed_daily_commands.py`、`app/agent2/typed_daily_executor.py`、`app/agent2/report_sql_executor.py` | `tests/test_agent2_fact_invariants.py` |
| 被告绩效查询 | LIVE | `app/api/performance.py`、`app/legal_ops_data_intake/performance_reporting.py`、`app/agent2/performance_knowledge.py`、`app/agent2/performance_qa.py`、`app/agent2/performance_tool_service.py`、`app/agent2/tool_calling/performance_handlers.py`、`app/agent2/tool_calling/production_performance_executor.py` | 发布前在完整测试库核验 |
| 数据接入中心 | LIVE | `app/legal_ops_data_intake/api.py`、`service.py`、`workbook.py`、`cases.py`、`rule_understanding.py`、`rule_literals.py`、`rule_package.py` | 发布前在完整测试库核验 |
| 案件主数据与进展 | LIVE | `app/legal_ops/api.py`、`app/legal_ops/write_service.py`、`app/legal_ops_data_intake/cases.py`、`app/agent2/business/case_progress.py` | `tests/test_legal_ops_write_service.py` |
| Agent2 工具调用 | CONTROLLED LIVE | `app/agent2/tool_calling/registry.py`、`production_runtime.py`、`production_store.py`、`production_handlers.py`、`canary_service.py`、`canary_control.py`、`canary_store.py` | `tests/test_agent2_runtime_anti_oracle.py` |
| 回执、审计和状态推进 | CONTROLLED LIVE | `app/agent2/tool_calling/receipt_reply.py`、`app/agent2/workflow_audit.py`、`app/agent2/operation_outcomes.py`、`app/agent2/conversation_state_store.py` | `tests/test_agent2_audit_viewer.py`、`tests/test_agent_core_operation_ledger.py` |
| Agent1 兼容回退 | LIVE | `app/agent/`、`app/services/report_service.py` | `tests/test_decision_router.py`、`tests/test_llm_executor_safety.py` |
| 出差协同基础设施 | NOT LIVE | `app/agent2/business/travel.py`、`travel_pipeline.py`、`notifications.py` | `tests/test_agent2_travel_response_outcome_contract.py` |
| 事项跟盯基础设施 | NOT LIVE | `app/agent2/case_followup_scheduler.py`、`case_followup_service.py`、`case_followup_outbox.py`、`case_followup_dispatch.py`、`case_followup_pending.py` | `tests/test_agent2_case_followup_schema.py`、`tests/test_agent2_case_followup_invalidation.py` |
| 案件到报告投影 | NOT LIVE | `app/agent2/case_report_projection_runtime.py`、`report_projection_executor.py`、`daily_report_projection_executor.py` | 发布前在完整测试库核验 |
| 离线回放与评测 | NOT LIVE | `app/agent2/runtime/`、`app/agent2/harness/`、`app/agent2/evaluation/`、`scripts/replay_agent2_dialogues.py` | `tests/test_agent2_dialogue_replay.py`、`tests/test_agent2_shadow_replay.py` |

## 动态加载

以下文件即使没有被普通文本搜索直接引用，也不能据此删除：

- `app/agent/prompts/` 和 `app/llm/prompts/` 中的提示模板；
- `database/` 和 `scripts/*.sql` 中的迁移历史；
- Agent2 工具注册表及其处理器；
- 回滚、兼容和启动入口依赖的模块。

判断一个文件是否可删除前，还需核对启动脚本、配置开关、数据库迁移、回滚链和实际发布制品。
