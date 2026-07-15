# Legal Ops 中文业务词典

前端只调用统一词典，不直接渲染后端枚举。未知值显示“数据状态异常”，并在浏览器日志记录原始值；数据库合同保持不变。

## 案件类型

| 后端值 | 中文 |
|---|---|
| `plaintiff_case` | 原告案件 |
| `defendant_case` | 被告案件 |
| `litigation`（旧类型） | 旧灰测案件 |

## 案件阶段

| 后端值 | 中文 |
|---|---|
| `intended_filing` | 拟诉 |
| `litigation` | 诉讼中 |
| `enforcement` | 执行中 |
| `accepted` | 受理 |
| `hearing` | 开庭 |
| `adjudicated` | 审结 |
| `performance` | 履行 |
| `closed` | 已结案 |
| `open` | 在办 |

## 通知与协同

| 后端值 | 中文事实 |
|---|---|
| `planned` | 已登记 |
| `candidate` | 待确认协同 |
| `notified` | 已创建通知 |
| `queued` | 已进入发送队列 |
| `processing` / `sending` | 正在发送 |
| `sent` / `accepted_by_provider` | 平台已接受发送请求 |
| `delivery_confirmed` | 已确认送达 |
| `waiting_for_reply` | 等待回复 |
| `accepted_by_one` | 一方已接受 |
| `accepted` / `accepted_by_both` | 双方已接受 |
| `declined` | 已拒绝 |
| `failed` | 发送失败 |
| `dead_letter` | 多次失败，需处理 |
| `cancelled` | 已取消 |
| `expired` | 已过期 |

`sent` 和 `accepted_by_provider` 绝不能显示为“已送达”或“对方已接受”。

## 报告

| 后端值 | 中文 |
|---|---|
| `daily` | 日报 |
| `weekly` | 周报 |
| `monthly` | 月报 |
| `collecting` | 收集中 |
| `pending_confirmation` | 待确认 |
| `completed` | 已提交 |
| `cancelled` | 已取消 |
| `skipped` | 已跳过 |

## 来源

| 后端值 | 中文 |
|---|---|
| `real_case_workbook` | 真实来源灰测副本 |
| `real_user_message` / 钉钉 stream | 真实用户消息 |
| `human_record` | 人工录入 |
| `robot_followup` | 主动追问提取 |
| `ai_extracted` | AI 提取待确认 |
| `system_fact` | 系统事实 |
| `system_derived` | 系统计算 |
| `legacy_import` | 历史导入 |
| `server_acceptance_smoke` | 服务器验收记录 |
| `sandbox_fixture` | 演示数据 |
| `sandbox_persisted` | 灰测数据库记录 |
| `legal_ops_ui` | 法务业务中台 |

## 追问策略

| 后端值 | 中文 |
|---|---|
| `daily` | 每天一次 |
| `weekly` | 每周一次 |
| `every_15_days` | 每 15 天一次 |
| `monthly` | 每月一次 |
| `custom_interval` | 自定义周期 |
| `event_only` | 仅关键节点 |
| `manual_only` | 仅人工追问 |
| `paused` | 已暂停 |
| `disabled` | 已关闭 |
