# Legal Ops 两用户 UI 问题台账（2026-07-13）

## 已关闭

| ID | 问题 | 根因 | 处置与证据 |
|---|---|---|---|
| UI-P0-001 | 中台登录失败/无 Sandbox 凭证 | live 只有单管理员 token，本地演示 tenant 启动参数错误 | 建立庞浩/刘聪独立 principal；本地演示显式使用 `sandbox-alpha`；两身份登录通过 |
| UI-P0-002 | 默认页面混入 Fixture、技术字段和英文枚举 | demo/live 共用旧信息架构，live 直接渲染 read model | live 导航重构、后端 DTO 投影、中文闭集、内部 ID 隐藏 |
| UI-P0-003 | 报告和团队在 live 页面缺失 | `/api/reports/*` 仅服务 demo | 接入 DailyReport、PeriodicReport、IdentityBinding 和 server-side team mapping |
| UI-P0-004 | 删除案件进展后下一步计划残留 | 删除 CaseProgress 未反转由其创建的 CaseLifecycleState | SQL Executor 在同事务删除或恢复 lifecycle；20 项专项测试通过 |
| UI-P0-005 | 页面案件计划出现 `????????` | 早期错误编码验收进展已软删除，但孤儿 lifecycle 投影仍保留 | 受控 repair receipt `3be95cd6-e488-5392-ae6a-74c26a11d569` 删除孤儿状态；重复执行幂等 |
| UI-P0-006 | 软删除进展仍在默认详情显示 | 投影层未过滤 `deleted` 节点 | 默认时间线只展示有效进展，审计仍保留历史 |
| UI-P1-007 | 修改/删除依赖原生 prompt/confirm | 旧前端用浏览器原生对话框 | 改为行内编辑与显式二次点击确认 |
| UI-P1-008 | 案件写入后抽屉正确、背景列表仍旧 | 只重绘单案 DTO | 写入后重新加载当前案件分页再重绘详情 |
| UI-P1-009 | 单案页顶部胶囊看似页签但不可点击 | 使用静态 `<span>` | 改为六/七个真实区块导航按钮并完成浏览器点击验证 |
| UI-P1-010 | 单案 receipt/audit 已存在但页面不可见 | `project_case_detail` 丢弃 audits | 新增中文脱敏操作记录；隐藏 audit/receipt/message/resource ID |
| UI-P1-011 | Legal Ops 修改日报后来源显示“系统记录” | `legal_ops_ui` 未进入来源词典 | 映射为“法务业务中台” |
| UI-P1-012 | 单案缺少日报投影状态 | 详情查询未读取 CaseReportProjection | 按案件实时查询并脱敏展示；当前 tenant 0 条时显示真实空状态 |

## 尚未满足

| ID | 级别 | 事实 | 影响/下一步 |
|---|---|---|---|
| UI-OPEN-001 | 验收 Gate | 刘聪本人未执行一次真实写入、刷新、修改、删除闭环 | 不得裁决 `TWO_USER_UI_CANARY_VALIDATED`；请刘聪本人按清单验收 |
| UI-OPEN-002 | 验收 Gate | 庞浩/刘聪本人均未在本轮点击“提交报告”；代理未替用户提交不可逆业务报告 | 提交技术链已测试，真人选择一份可提交报告完成二次确认 |
| UI-OPEN-003 | P2 数据质量 | 刘聪 2026-07-10 真实历史日报正文为泛化的“今日工作” | 这是 PostgreSQL 历史正文，不是前端 Fixture；应单独清洗或让本人修订 |
| UI-OPEN-004 | P2 数据完整性 | 当前案件没有可靠的法院、案由、开庭日期、风险等级结构化字段 | 页面已 fail-closed 显示“暂未记录/暂未评估”；后续从正式数据源补齐 |
| UI-OPEN-005 | P2 管理体验 | 管理审计旧技术视图尚未完成面向管理员的分页与筛选验收 | 两用户默认导航不暴露；进入管理员扩围前完成产品化 |
| UI-OPEN-006 | 观察项 | 当前 CaseReportProjection 为 0，真实空状态已验证但尚无服务器 active 样本 | 首条真实投影产生后复核详情展示与撤销状态 |

这些未满足项均不造成两用户默认视图的假数据、跨用户泄漏或假成功；但前两项阻止升级为“两用户已验证”。
