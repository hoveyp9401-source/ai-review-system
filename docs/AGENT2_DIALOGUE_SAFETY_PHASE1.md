# Agent2 对话安全协议 Phase 1

## 裁决边界

本阶段只改变 Agent2 当前业务域的执行结果、对象选择和回复边界；没有新增业务领域，没有迁移日报持久化模型，也没有改变 Canary 人员范围。

## 架构

```text
Semantic Interpreter -> Typed Planner -> Domain Compiler -> Executor/Repository
                                                       |
                                                       v
                                             committed receipt + snapshot
                                                       |
                                                       v
                                              OperationOutcome[]
                                                       |
                                                       v
                                            domain reply strategies
                                                       |
                                                       v
                                               OutcomeFactGuard
```

Executor、Repository 和旧的 `business_composition_reply_text` 不再是生产 Webhook/Stream 的最终文案来源。`OutcomeReplyComposer` 不判断业务是否成功，只表达 Outcome。表达失败时返回确定性安全模板，既有 receipt 不受影响。

## Operation Outcome Contract

正式 JSON Schema：[`schemas/agent2-operation-outcome.schema.json`](schemas/agent2-operation-outcome.schema.json)。运行时实现位于 `app/agent2/operation_outcomes.py`。

核心不变量：

- create/update/delete/submit/register 的成功结果必须同时满足 `actual_write=true` 与 `database/executed/actual_write=true` receipt。
- `accepted_by_provider` 必须有外部消息 ID，但不能升级为送达或用户同意。
- `delivery_confirmed` 必须有可靠回调证据。
- 用户可见快照与内部 object/receipt 分离；Reply 策略只读取前者。
- 多意图按 Outcome 数组逐项表达，部分失败不能抹掉已经成功的兄弟操作。

出差状态闭集为：registered、matched、queued、sending、accepted_by_provider、delivery_confirmed、waiting_for_reply、accepted_by_one_party、accepted_by_both、declined、failed、cancelled、expired。

## Selection Pending

正式 JSON Schema：[`schemas/agent2-selection-pending.schema.json`](schemas/agent2-selection-pending.schema.json)。Selection Pending 作为独立字段保存在现有 `agent2_conversation_states.state_json`，不与 Confirmation Pending 或 Information Pending 混用，因此不需要数据库迁移。

生命周期：

```text
ambiguous typed command
  -> active
  -> scope/state/expiry/candidate/version/permission/legality validation
  -> selected (仍为零写入)
  -> execute saved typed continuation
  -> receipt success -> consumed
  -> failure/conflict/permission change -> invalidated
  -> timeout -> expired
```

安全规则：

- Pending 绑定 tenant、user、conversation、source turn、候选 stable ID/version、可接受回答和状态版本。
- 候选 ID 来自服务器侧仓储；LLM 不选择或生成数据库 ID。
- “第二个”等回答由有限语法和 Pending 声明的 answer forms 解析，不按最近对象或模型猜测。
- 多个有效 Pending、跨作用域、过期、已消费、候选缺失、版本变化或权限变化全部零写入。
- 原始 typed command 保存在 continuation payload；选择后只绑定已复核 stable ID，再走原编译器、权限策略、Executor 和 receipt。
- 每次选择处理写入 `ReportInteractionEvent`，`audit_stage=selection_pending_resolution`，包括零写入原因、候选、Pending 终态和 receipt 引用。

## Reply Composer

领域策略为 ReportReply、CaseProgressReply、TravelReply、KnowledgeReply、ChatReply。KnowledgeReply 仅是现有查询结果的表达策略，本阶段没有新增 Knowledge 业务能力。

- 日报变更后展示完整 today_work/problems/tomorrow_plan；周报/月报同样展示完整用户可见 sections。
- 案件进展复述 Outcome 中的案件名称和真实持久化正文。
- 出差复述地点、时间、事由。
- 内部 ID、receipt、version、audit 不进入用户回复。
- `OutcomeFactGuard` 阻止无实际写入的成功动词和无可靠证据的送达声明。

## P0 回归映射

| # | 场景 | 主要自动化证据 |
|---|---|---|
| 1 | 唯一 Pending 回答“第二个” | `test_agent2_selection_pending.py` |
| 2 | 多 Pending 回答“确认” | 同上，executor 零调用 |
| 3–7 | 过期、删除、版本、权限、跨 scope | 同上，validator/contract tests |
| 8–9 | 重复投递、消费后再回复 | webhook 幂等既有回归 + consumed replay |
| 10 | 周报插入案件后恢复周报 | `test_agent2_report_domain.py` goal stack |
| 11 | 案件处理中插入出差 | `test_agent2_phase2_cross_domain_e2e.py` |
| 12 | DB 失败不宣称成功 | `test_agent2_operation_outcomes.py` |
| 13 | 通知排队不宣称送达 | 同上 |
| 14 | 多意图部分成功/失败 | 同上 |
| 15 | 正文不发生语义改写 | 同上，逐字断言 |
| 16 | 日报完整展示且不泄露内部字段 | 同上 |

## 新发现问题台账

| ID | 问题 | 本阶段处理 | 状态 |
|---|---|---|---|
| DS-P1-01 | 编译期歧义原先导致整轮状态不保存，后续“第二个”无可靠上下文 | 歧义保存独立 Selection Pending | 已修复 |
| DS-P1-02 | Webhook/Stream 使用 executor/composition 文案，可能先于事实或泄露 receipt | 生产入口统一改用 OutcomeReplyComposer | 已修复 |
| DS-P1-03 | outbox `sent` 曾容易被理解为用户送达 | 映射为 accepted_by_provider，可靠回调才可 delivery_confirmed | 已修复 |
| DS-P1-04 | Selection Pending 没有独立表 | 使用现有租户/用户/会话隔离的状态 JSON；选择结果写既有交互审计表 | 接受，Phase 1 无迁移 |
| DS-P1-05 | 旧 `business_composition_reply_text` 仍为测试兼容保留 | 已从生产 Webhook/Stream 主回复路径移除 | 后续清理 |

## Go / No-Go 门槛

只有以下全部成立才可 GO：新增 P0 全绿；Agent2 回归无新增失败；服务器代码与本地一致；当前租户仍只有庞浩、刘聪为 primary Canary；只读查询验证状态表、receipt、outbox 与审计结构可用；限定写 smoke 不越过这两个身份。任何一项缺证据即保持 NO-GO。
