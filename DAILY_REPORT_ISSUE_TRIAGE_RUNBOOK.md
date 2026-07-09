# 日报填写问题排查与线上 Smoke Runbook

用途：当用户要求“整理昨晚/今天早上大家日报填写中发现的问题”“服务器看”“线上 smoke 所有问题”时，按本文件执行，避免重新摸索流程。

## 用户推荐口令

用户可以直接说：

> 按日报问题台账流程走。时间范围：昨晚到今天 9 点前。服务器看。整理问题、建台账、生成近义话术、多轮真实线上 smoke，失败就定位根因并修复。不要只跑本地，不要没验证就说修好了。

如果要更精确：

> 按日报问题台账流程走。时间范围：YYYY-MM-DD HH:mm 到 YYYY-MM-DD 09:00。重点看日报填写失败、误写、误拦截、复制/修改/确认/补充/合并/清空/历史日期相关问题。线上验证后给我 pass/fail 和剩余风险。

## 固定原则

- 先查线上真实数据和日志，再下结论。
- 新问题必须进入 `REPORT_ISSUE_LEDGER.md`。
- 每个问题至少生成 3 个同义但不同话术；多轮问题生成多轮对话。
- 必须跑线上 API smoke，不能只跑 service memory 或本地单测。
- 修复不能靠枚举孤立关键词硬糊；优先修意图分发、状态机、执行器兜底和结构化协议。
- 不能影响已有健全功能；每次修复后跑产品 gate 和线上 smoke。
- 没跑过就不能说“已全部修复”。

## 服务器

生产代码目录：

```bash
/home/ai_review_tunnel/ai-review-system
```

常用服务：

```bash
ai-review-api.service
ai-review-stream.service
ai-review-scheduler.service
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8000/health
```

## 标准执行步骤

1. 明确时间范围，并用绝对时间记录。例如：`2026-06-26 18:00` 到 `2026-06-27 09:00`。
2. 查询线上日志、DB、日报交互事件，筛出失败/拦截/误写/用户负反馈/重复追问。
3. 把问题写入 `REPORT_ISSUE_LEDGER.md`，标注状态、证据、根因、回归方式。
4. 为每个问题生成 3 个近义话术；多轮问题保留多轮结构。
5. 用真实线上 API 和真实 DB 跑 smoke：

```bash
cd /home/ai_review_tunnel/ai-review-system
PYTHONPATH=. venv/bin/python scripts/run_online_issue_ledger_smoke.py --output outputs/issue_ledger_online_smoke_latest.json
PYTHONPATH=. venv/bin/python scripts/run_online_system_smoke.py --output outputs/online_system_smoke_latest.json
```

6. 如果失败，先记录失败原文、实际回复、DB 结果，再定位根因并修复。
7. 修复后重启服务：

```bash
cd /home/ai_review_tunnel/ai-review-system
PYTHONPATH=. venv/bin/python -m py_compile app/services/report_service.py app/agent/executor.py app/agent/decision_router.py app/agent/state_resolver.py app/scheduler/jobs.py
systemctl restart ai-review-api.service ai-review-stream.service ai-review-scheduler.service
sleep 2
curl -fsS http://127.0.0.1:8000/health
```

8. 跑回归：

```bash
cd /home/ai_review_tunnel/ai-review-system
PYTHONPATH=. venv/bin/python scripts/run_product_gate.py --quick
PYTHONPATH=. venv/bin/python scripts/run_progress_gate.py
PYTHONPATH=. venv/bin/python scripts/run_online_system_smoke.py --output outputs/online_system_smoke_latest.json
PYTHONPATH=. venv/bin/python scripts/run_online_issue_ledger_smoke.py --output outputs/issue_ledger_online_smoke_latest.json
```

9. 把 smoke 产物同步回本地 `outputs/`，并更新 `REPORT_ISSUE_LEDGER.md` 的线上验证结果。
10. 最终汇报必须包含：
    - 新增问题数量。
    - 已修复数量。
    - 线上对话 smoke pass/fail。
    - 线上系统 smoke pass/fail。
    - product/progress gate 结果。
    - 未解决或 legacy-gap，不能隐藏。

## 不允许的汇报方式

- “应该好了。”
- “我测过了。”但不说明线上 smoke 数字。
- “全部修复了。”但还有失败项、skip 项或 legacy-gap 没说。
- 只说本地服务测试通过，不跑线上 API。

## 推荐最终口径

如果全绿：

> 已知台账内、能线上复现和 smoke 覆盖的问题，已修复并通过线上验证。线上对话 smoke X/X，系统 smoke Y/Y，product gate Z passed，progress gate N passed。剩余 watching/legacy-gap 是观察项，不冒充已修。

如果仍有红点：

> 不能说全部修复。当前线上 smoke X/Y，失败项是：...。根因是：...。我会继续修复并重跑。
