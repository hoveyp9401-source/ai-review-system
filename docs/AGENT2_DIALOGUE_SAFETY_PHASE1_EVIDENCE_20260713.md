# Agent2 对话安全协议 Phase 1 验收证据（2026-07-13）

## 结论

**GO——仅限现有庞浩、刘聪双人 Canary。** 不允许扩大 tenant 或用户范围；这不是全量生产放量裁决。

## 本地测试

- 全部 `tests/test_agent2_*.py`：864 passed。
- Outcome、Selection、Cognitive Runtime、Business Compiler、跨域重点集：65 passed。
- Outcome、Selection 与 Legal Ops 联合重点集：88 passed。
- 没有删除、skip 或 xfail 既有测试。

## 服务器部署

- 服务器：`/home/ai_review_tunnel/ai-review-system`
- 16 个核心运行文件部署后 SHA-256 与本地一致，mismatch=0；后续只追加同步了 Outcome adapter、Webhook、Stream 的审查修订。
- 服务器重点回归：64 passed。
- systemd 重启后：API、Stream、Scheduler 均为单实例 active。
- 健康检查：`GET http://127.0.0.1:8000/health` -> `200 {"status":"ok"}`。
- 启动日志显示 API startup complete，Stream 已建立钉钉 WebSocket 连接；重启后未发现启动异常。

## 只读 PostgreSQL / Legal Ops 证据

Tenant `sandbox-agent2-phase2-20260711`：

- route_mode=`agent2_canary`
- canary_user_ids 恰为庞浩、刘聪的两个系统 user id
- active identity bindings=2：庞浩、刘聪
- agent1_rollback_enabled=false，route version=3
- conversation states=29
- business receipts=38
- notification outbox=3
- selection audit=0（部署后尚未发生真实 Selection Pending 对话，未伪造审计记录）

Legal Ops live read API 返回 tenant 一致，限量读取成功：parties=5、identity_bindings=2、cases=2、travel_intents=5、collaboration_candidates=1、notifications=3、case_progress=5、periodic_reports=2、receipts=5、audits=15。

## 限定写验证

- 服务器 P0 测试验证：唯一 Pending 可在 receipt 成功后写入并消费；多 Pending、过期、候选删除、版本变化、权限撤销、跨 tenant/user/conversation、已消费重放全部为零写入。
- Webhook 与 Stream 都先经过服务器解析的 identity binding 和 tenant route；Selection validator 再从 permission-scoped Case repository 复核 stable ID/version。
- 本次部署没有生成 Fixture、没有手工 SQL、没有写入业务证明数据、没有触发真实钉钉通知，也没有改 Canary 配置。
- 因未冒充庞浩或刘聪发起钉钉消息，真实线上 Selection audit 仍为 0；首个真实歧义选择应作为 Canary 观察项，而不是伪造验收证据。

## 修改文件清单

### 协议与运行时

- `app/agent2/operation_outcomes.py`
- `app/agent2/outcome_adapters.py`
- `app/agent2/selection_pending.py`
- `app/agent2/selection_continuation.py`
- `app/agent2/selection_runtime.py`
- `app/agent2/conversation_state.py`
- `app/agent2/conversation_state_store.py`（复用，无结构迁移）
- `app/agent2/cognitive_orchestrator_v3.py`
- `app/agent2/cognitive_runtime_v3.py`
- `app/agent2/command_planner_v3.py`

### 业务适配与生产入口

- `app/agent2/business/case_progress.py`
- `app/agent2/business/compiler.py`
- `app/agent2/business/composition.py`
- `app/agent2/business/repositories.py`
- `app/agent2/workflow_audit.py`
- `app/api/webhook.py`
- `app/stream_runner.py`

### 测试与文档

- `tests/test_agent2_operation_outcomes.py`
- `tests/test_agent2_selection_pending.py`
- `tests/test_agent2_business_compiler.py`
- `CONTEXT.md`
- `docs/ADR/0017-outcome-selection-and-reply-seam.md`
- `docs/AGENT2_DIALOGUE_SAFETY_PHASE1.md`
- `docs/schemas/agent2-operation-outcome.schema.json`
- `docs/schemas/agent2-selection-pending.schema.json`

## 继续观察项

1. 首个真实歧义案件选择是否生成 `selection_pending_resolution` 审计并带真实 receipt。
2. 钉钉 provider acceptance 当前仍不能证明 delivery；没有可靠回调时不得升级状态。
3. 旧 composition reply helper 仅保留给兼容测试，生产 Webhook/Stream 已无引用。
4. 工作区在本阶段前已有大量未提交/未跟踪改动，因此没有进行可能混入他人修改的批量 Git commit。
