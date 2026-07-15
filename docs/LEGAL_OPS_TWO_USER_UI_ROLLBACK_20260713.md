# Legal Ops 两用户 UI 回滚方案（2026-07-13）

## 立即止损

1. 在服务器 `/home/ai_review_tunnel/ai-review-system/.env` 将 `LEGAL_OPS_LIVE_ENABLED=false`。
2. 仅重启 `ai-review-api.service`，不重启 Stream 或 Scheduler。
3. 核验 `/health` 正常、live 登录被关闭、钉钉 Stream 与 Scheduler 保持 active。

该操作只关闭 Legal Ops live UI，不删除 PostgreSQL 数据，也不影响 Agent2 对话、案件、报告、出差和主动协同主链。

## 文件级回滚

服务器增量备份位于：

- `backups/20260713-legal-ops-report-projection`
- `backups/20260713-legal-ops-case-section-navigation`
- `backups/20260713-legal-ops-case-audit`
- `backups/20260713-legal-ops-case-reconcile`
- `backups/20260713-legal-ops-inline-actions`
- `backups/20260713-legal-ops-lifecycle-reversal`

按时间逆序恢复对应目录中的文件，完成后执行：

```bash
cd /home/ai_review_tunnel/ai-review-system
./venv/bin/python -m pytest -q /tmp/test_legal_ops_product_ui_final.py /tmp/test_legal_ops_phase2_read_final.py /tmp/test_agent2_business_sql_executor_final.py
systemctl restart ai-review-api.service
systemctl is-active ai-review-api.service ai-review-stream.service ai-review-scheduler.service
curl -fsS http://127.0.0.1:8000/health
```

若回滚 `sql_executor.py`，必须同时保持 `LEGAL_OPS_LIVE_ENABLED=false`，避免重新暴露“删除进展后 lifecycle 残留”的旧缺陷。

## 数据回滚边界

- 浏览器案件验收记录已软删除，关联 lifecycle 已事务反转，不需要手工 SQL。
- 浏览器报告验收条目已通过正式 `delete_item` 删除，临时正文残留为 0。
- 受控孤儿 lifecycle 修复有正式 receipt/audit；除非确认修复判断错误，不应恢复污染状态。
- receipt/audit 是合规证据，不随 UI 回滚删除。

## 回滚后验收

- API/Stream/Scheduler 均 active；
- Agent2 两用户路由不变；
- live UI 已关闭或回到指定备份版本；
- 不存在临时报告正文；
- 目标案件仍无有效验收进展、无孤儿下一步计划；
- 无跨 tenant、跨用户可见性变化。
