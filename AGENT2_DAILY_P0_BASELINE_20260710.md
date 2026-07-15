# Agent2 日报 P0 基线（2026-07-10）

## 生产基线

- 服务器目录：`/home/ai_review_tunnel/ai-review-system`
- Git HEAD：`96cecec81c4ac9fe4e0500c08a52e661d094147d`
- 工作树：已有 Agent2/workflow/tests 未提交改动；部署时不得覆盖无关改动。
- 进程观察：生产同时存在手工启动与 systemd 启动的 stream/scheduler 进程；API 8000 为手工进程，staging API 为 8010。部署验证必须按实际 PID/启动时间确认加载版本，不能只看 `systemctl restart` 返回值。

审查开始时的关键 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `app/agent2/daily_execution.py` | `597b991bf5552218255516283c9d9baad60192bbcbdd09992e2a157217d6c714` |
| `app/agent2/daily_commands.py` | `0974531848eae96d699c924463c0a9218b1eb491db1996c3fd021b34c4c4ea77` |
| `tests/test_agent2_daily_execution.py` | `0403d8398e9c11debe0f133338c4db2dd663cdb7312d26fc49f685801e84edc6` |
| `tests/test_agent2_daily_commands.py` | `230204cbd85d2362e4e446d9c86916093e420b560e61e2c75dbd7b9f47762b50` |
| `REPORT_ISSUE_LEDGER.md` | `1a6d58194da806750c2add94ab5f4634296c4232a6a7b0715c3103b15bf1227c` |

## 已确认的失败复现

初始快照：

```text
today_work = ["alpha", "beta"]
section_status = {}
command = DailyCommand(operation="edit", target_field="today_work", content=["那条删掉"])
```

基线实际结果：

```text
changed = true
today_work = ["alpha"]
edit_action = "delete_item"
```

具体路径：`apply_commands_to_snapshot()` → `_apply_edit_command()` → `_recent_item_target_from_text()`。无 focus 时，该函数依次按 `preferred_field`、唯一非空栏、`today_work` 回退到栏内末条。

对应红测：

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_agent2_daily_execution.py::test_apply_deictic_delete_without_focus_is_ambiguous_and_does_not_change
```

首次运行结果：`FAILED`，断言期望 `changed is False`，实际为 `True`，并删除 `beta`。该测试现在是永久回归门禁。

## 基线边界

- 本 P0 只收口日报的追加、明确修改、明确删除、明确合并、当前草稿提交。
- 不以案件、出差或通用 pending 框架为前置条件。
- 旧复杂编辑保留兼容回退；生产常规五类动作必须先经过 typed command 与 fail-closed validator。
