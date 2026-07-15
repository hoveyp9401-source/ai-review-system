# Agent2 Phase 2 显式回滚 Runbook

适用范围严格限定为：

- tenant：`sandbox-agent2-phase2-20260711`
- 用户：庞浩、刘聪
- 不适用于正式租户或其他用户

自动 fallback 始终禁止。认知、编译、权限、数据库或 transport 失败必须在 Agent2
链路内返回 blocked/failed，不能因失败而隐式调用 Agent1。

## 回滚前取证

1. 记录事故/演练编号、操作人、原因和当前时间。
2. 读取 `agent2_tenant_route_controls`，确认 tenant、当前 `version`、
   `route_mode=agent2_canary`、两名 canary user id。
3. 确认 `agent1_rollback_enabled=false`。本 Runbook 通过显式版本化路由变更回滚，
   不开启失败后的自动回退能力。
4. 记录以下 tenant-scoped 计数：TravelIntent、candidate、outbox、CaseProgress、
   Business receipt、ConversationState。
5. 如通知 transport 本身不安全，先关闭对应 effect 开关：
   `AGENT2_TRAVEL_NOTIFICATION_WORKER_ENABLED=false`、
   `AGENT2_CASE_FOLLOWUP_SEND_ENABLED=false`，再重启 Scheduler。案件追问 tenant/user
   allowlist 必须继续严格限定目标 tenant、庞浩和刘聪，不得仅依赖总开关。
6. 服务器三个进程由 systemd `Restart=always` 管理。使用
   `scripts/restart_production_user_processes.sh`；脚本会检测 systemd 并仅终止受监管
   MainPID，等待 systemd 拉起。禁止在 systemd 已启用时另起 nohup 进程，否则会产生
   双 stream/scheduler 和重复消费风险。

## 执行回滚

必须调用 `RouteControlRepository.change(...)`，并传入当前 `expected_version`：

- `route_mode="agent1"`
- `canary_user_ids` 保持原值，便于恢复和审计
- `agent1_rollback_enabled=false`
- 独立的 `source_message_id`
- 明确的 `reason`

禁止直接用无审计 SQL 改字段。

## 回滚验证

1. 分别用庞浩、刘聪的 DingTalk user id 调用入口解析；两者必须返回：
   `route=agent1`、`reason=tenant_route_mode:agent1`。
2. 验证新探针没有生成 typed Business command、Business receipt 或业务结果。
3. 对比回滚前后的六类计数，必须完全一致。
4. 检查 route audit 的 before/after、actor、source message、reason 和 version。
5. 不把同一 source message 再投递给两条链路；只有 Agent2 receipt 明确证明无写入时，
   才能由人工决定是否以新的受控事件重试。
6. 验证 API、Stream、Scheduler 各只有 1 个主进程；`/health` 为 200；Legal Ops Live
   API tenant 仍为 `sandbox-agent2-phase2-20260711`。

## 恢复 Canary

再次调用版本化 `RouteControlRepository.change(...)`：

- `route_mode="agent2_canary"`
- 只保留庞浩、刘聪两个 canary user id
- `agent1_rollback_enabled=false`
- 使用新的 source message 和恢复原因

恢复后两名用户必须解析为 `agent2_primary / canary_user`，业务计数仍与回滚前一致。

## 2026-07-12 服务器演练记录

- 控制版本：`1 → 2 (agent1) → 3 (agent2_canary)`
- 关闭时：两名用户均为 `agent1 / tenant_route_mode:agent1`
- 恢复时：两名用户均为 `agent2_primary / canary_user`
- `agent1_rollback_enabled` 全程为 `false`
- 前后计数一致：TravelIntent 2、candidate 0、outbox 0、CaseProgress 1、
  Business receipt 6、ConversationState 0
- 未创建业务结果、未重复消费、未触发通知
