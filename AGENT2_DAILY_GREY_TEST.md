# Agent2 日报灰测说明

## 开关

默认关闭：

```env
AGENT2_DAILY_ENABLED=false
AGENT2_DAILY_ENABLED_USER_IDS=
```

只给指定用户开启：

```env
AGENT2_DAILY_ENABLED=true
AGENT2_DAILY_ENABLED_USER_IDS=<user.id 或 dingtalk_user_id，多个用逗号分隔>
```

全员开启只用于后续明确切换：

```env
AGENT2_DAILY_ENABLED_USER_IDS=*
```

## 当前灰测范围

Agent2 会真执行这些日报命令：

- `fill`：写入今日工作、问题/风险、明日计划。
- `clear`：清空日报。
- `copy_previous`：复制上一份日报到当前日报。
- `revoke`：已提交日报改回草稿。
- `confirm`：仅在存在待确认上下文时提交。
- `query_current` / `query_history`：只读查看。

`edit` 暂时回退老日报处理，避免复杂定位修改在第一版灰测中被做坏。

## 台账

Agent2 真执行后会写 `report_interaction_events`，记录：

- 原始消息；
- Agent2 命令；
- 执行动作；
- 操作前日报快照；
- 操作后日报快照。

如果 `SHADOW_MEMORY_ENABLED=true`，沿用原有事件写入；否则 Agent2 执行器会显式写一条台账。

## 非日报旁路

出差协同、案件进展等仍只进入沙箱候选，不正式通知、不正式写业务库。
