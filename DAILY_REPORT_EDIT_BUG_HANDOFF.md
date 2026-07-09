# 日报编辑上下文丢失问题交接说明

## 当前用户看到的问题

用户在钉钉里说：

```text
今天的日报我改下
```

机器人仍然回复：

```text
您想补充到哪个部分？今日工作、问题还是明日计划？
```

这是错误行为。正确行为应该是：

1. 直接锁定“今天这份日报草稿”。
2. 展示当前今天日报。
3. 后续用户发整段内容时，直接按 `今日工作 / 问题风险 / 明日计划` 更新今天草稿。
4. 不应该再问“补充到哪个部分”。

## 最重要结论

不要继续在多个副本里乱改。

当前仓库里至少有多份日报服务代码：

| 路径 | 状态 |
|---|---|
| `remote_edit/app/services/report_service.py` | 已被我改过，也加了测试，但用户反馈线上无效 |
| `remote_edit/agent_fix/...` | 实验/旁路版本，包含 agent/cursor 相关代码，不要继续优先改 |
| `remote_edit/agent_rebuild/...` | 另一套实验版本，不要继续优先改 |
| `remote_edit/pending_interaction/...` | 旧实验版本，不要继续优先改 |
| `app_services_report_service.py` | 根目录扁平化版本，和部署服务更像，需要优先确认/修复 |

部署脚本显示线上服务是：

```ini
WorkingDirectory=/home/ai_review_tunnel/ai-review-system
Environment=PYTHONPATH=/home/ai_review_tunnel/ai-review-system
ExecStart=/home/ai_review_tunnel/ai-review-system/venv/bin/python -m app.stream_runner
```

API 服务是：

```ini
ExecStart=/home/ai_review_tunnel/ai-review-system/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

所以真正线上入口应是部署目录里的：

```text
app/services/report_service.py
```

本地仓库根目录没有真实 `app/services/report_service.py` 目录结构，而是有扁平文件：

```text
app_services_report_service.py
```

因此后续必须先确认：根目录扁平文件是否就是部署包 `app/services/report_service.py` 的源。如果是，补丁应该落到 `app_services_report_service.py`，再同步/还原到部署目录的 `app/services/report_service.py`。

## 已经做过但线上未生效的事

我已经修改了：

```text
remote_edit/app/services/report_service.py
```

新增了：

```text
PENDING_INTERACTION_CURRENT_REPORT_EDIT_FLOW = "current_report_edit_flow"
```

并添加了处理逻辑：

- `今天的日报我改下` 进入当前日报编辑流。
- 展示今天日报。
- 后续整段日报内容直接替换三段。
- `函件改成邮件` 只在当前草稿里做文本替换。

新增测试：

```text
remote_edit/tests/test_current_report_edit_flow.py
```

验证过：

```text
python -m py_compile remote_edit/app/services/report_service.py remote_edit/tests/test_current_report_edit_flow.py
```

通过。

由于本机缺 `pytest/sqlalchemy`，我用模块桩执行过新增测试逻辑，三条 smoke 通过：

1. `今天的日报我改下` 会设置 `current_report_edit_flow`。
2. 整段内容会替换为：
   - 今日工作：`出差去南京开庭`、`审核8份合同`、`撰写10份函件`、`处理工人讨薪`
   - 问题/风险：`发现部分工人未签劳动合同`
   - 明日计划：`去苏州开庭`
3. `函件改成邮件` 只改对应文本，不改其它字段。

但是用户线上测试仍然无效，说明这不是线上正在跑的代码，或者线上进程没有重启/没有部署该文件。

## 已确认的反证

在仓库中搜索用户线上收到的句子：

```text
您想补充到哪个部分？今日工作、问题还是明日计划？
```

这句没有出现在 `remote_edit/app/services/report_service.py`。

它出现在：

```text
remote_edit/agent_fix/app/agent/prompts/report_agent.md
remote_edit/agent_fix/tests/...
remote_edit/agent_rebuild/...
```

以及历史压测结果里。

这说明线上实际回复可能来自：

1. 根目录部署包的旧逻辑/旧 prompt。
2. 未重启的旧进程。
3. 另一个实际运行目录，不是我改的 `remote_edit/app/services/report_service.py`。

## 下一步不要做什么

不要继续：

- 在 `remote_edit/agent_fix` 里补规则。
- 在 `remote_edit/agent_rebuild` 里补规则。
- 只改 `remote_edit/app/services/report_service.py` 后就认为线上会生效。
- 继续加 prompt 让 LLM 猜。

## 下一步应该做什么

优先顺序：

1. 确认线上实际代码路径：
   - 看 systemd `WorkingDirectory`。
   - 看运行进程命令。
   - 看部署机器 `/home/ai_review_tunnel/ai-review-system/app/services/report_service.py` 的内容。
2. 在实际线上入口的 `app/services/report_service.py` 加当前日报编辑流短路。
3. 如果本地源是扁平文件，则先改：

   ```text
   app_services_report_service.py
   ```

   再同步成部署目录中的：

   ```text
   app/services/report_service.py
   ```

4. 重启服务：

   ```bash
   sudo systemctl restart ai-review-stream
   sudo systemctl restart ai-review-api
   ```

5. 用钉钉原话验收：

   ```text
   今天的日报我改下
   ```

   期望不是追问字段，而是展示今天日报。

6. 再发：

   ```text
   出差去了南京开庭，同时审核了8份合同，写了10份函件，处理了工人讨薪。发现部分工人没签劳动合同，明天计划去苏州开庭。
   ```

   期望直接更新日报三段。

## 正确行为定义

这类“改下今天日报”的入口不是“补充意图”，而是“进入当前日报编辑模式”。

伪代码：

```python
if looks_like_current_report_edit_entry(raw_input):
    if existing_report:
        set_pending_interaction(type="current_report_edit_flow", target_date=today)
        return display_current_report(existing_report)
```

进入该模式后：

```python
if pending_interaction.type == "current_report_edit_flow":
    if input_is_full_report_paragraph:
        replace today_work/problems/tomorrow_plan
    elif input_is_text_replacement:
        replace matched text inside locked report
    elif input_is_delete:
        delete from locked report
    else:
        keep asking for edit content, not field scope
```

核心原则：

```text
一旦用户说“今天的日报我改下”，系统必须锁定今天日报。
后续不允许再把这句话解释成“补充到哪个字段”。
```

## 给后续 Codex 的提醒

如果上下文被压缩，直接从本文件继续。

先不要相信之前已经修好了。用户刚反馈“还是傻逼一样回我您想补充到哪个部分”，说明线上未吃到补丁。

下一步最关键不是继续设计，而是把补丁落到真正线上入口，并重启服务。

## 2026-06-17 后续同步结果

已经把最新可用的 `remote_edit/app` 正式同步到了根目录 `app/` 包结构：

```text
app/services/report_service.py
app/stream_runner.py
app/main.py
app/api/reports.py
app/api/webhook.py
app/services/dingtalk.py
app/llm/client.py
```

并把扁平候选文件同步为同一份：

```text
app_services_report_service.py
```

现在这三处都包含 `current_report_edit_flow`：

```text
app/services/report_service.py
app_services_report_service.py
remote_edit/app/services/report_service.py
```

已验证：

```text
python -m py_compile app/**/*.py
python -m py_compile app_services_report_service.py app_schemas.py app_repositories.py app_config.py app_llm_extractor.py app_scheduler_jobs.py app_scheduler_runner.py
```

并用依赖桩直接执行 `app/services/report_service.py` 的 smoke：

1. `今天的日报我改下` 命中 `current_report_edit_flow`，不进入 LLM。
2. 后续整段：

   ```text
   出差去了南京开庭，同时审核了8份合同，写了10份函件，处理了工人讨薪。发现部分工人没签劳动合同，明天计划去苏州开庭。
   ```

   会更新为：

   - 今日工作：`出差去南京开庭`、`审核8份合同`、`撰写10份函件`、`处理工人讨薪`
   - 问题/风险：`发现部分工人未签劳动合同`
   - 明日计划：`去苏州开庭`

通过输出：

```text
formal_app_current_report_edit_flow_ok
```

注意：本机 Python 缺少 `sqlalchemy`，所以真实 import 依赖需要在线上 venv 中验证。下一步上线必须把根目录 `app/` 包部署到 `/home/ai_review_tunnel/ai-review-system/app/`，然后重启：

```bash
sudo systemctl restart ai-review-stream
sudo systemctl restart ai-review-api
```
