# ADR-0010：Conversation Pending 与企业 Approval 分离

## 状态

Accepted，2026-07-10。

## 背景

Conversation State 可保存多个 pending，但当前 continuation 仍有全局唯一 active pending 假设。企业审批还可能由另一 actor 在数天后完成，不能使用同一模型。

## 决定

Conversation pending 只表达会话内 clarification/confirmation：

- 必须绑定 pending ID、user、conversation、intent、action、entities 和 expiry；
- Context Assembly 暴露全部 bounded active pending，不私自选一条；
- 多候选下裸“确认/是的/对”必须 clarification、零写；
- pending 只有在成功 receipt、明确取消或过期后消费。

企业 approval 属于未来 ProcessInstance/Approval contract，具有 approver、target/version、grant、expiry、revocation 和 signal identity。

## 放弃的方案

- 用 confirmation 修复 ambiguous target；
- 使用 Conversation pending 保存跨天审批；
- 多 pending 时按最近一条猜测；
- 模型说“已确认”就直接消费 pending。

## Phase 1 影响

Harness 在 unavailable/blocked/failed typed receipt 时保留原 state；Phase 1 不实现 approval 系统。

## 后续方向

修正 Core 的显式 pending ID 多候选语义，并在第一个 high-impact remote mutation 前实现 typed ApprovalRequest/Decision。
