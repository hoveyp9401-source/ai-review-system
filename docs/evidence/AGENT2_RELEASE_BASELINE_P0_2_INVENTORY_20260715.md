# Agent2 Release Baseline P0.2：逐文件分类与敏感隔离

日期：2026-07-15  
结论：`CLASSIFICATION_COMPLETE_BASELINE_COMMIT_PENDING`

## 完成情况

- 已在 `codex/agent2-release-baseline-20260715` 建立本地发布基线分支，起点为 `36c2199a983f093f54138f7f7128fbaf077cdb4d`。
- 本地 628 个当前 Git 可见文件全部完成分类，未知项为 0。
- 生产 966 个文件全部完成分类，未知项为 0；扫描只写入仓库外的受限证据目录，生产仓库文件和业务数据库均未修改。
- 清单只保存路径、Git 状态、分类、大小、哈希与封闭风险信号，不保存匹配到的凭证值。
- `.env.example` 被视为部署模板；空值或 `${...}` 占位符不会被误判为真实凭证。
- 本地临时上传目录、服务器副本、凭证导出文件和本地 GPT 分享包已加入忽略范围，不允许进入发布基线提交。
- 生产独有的 `app/agent/__init__.py` 与 5 个 `app/progress/` 源文件已按生产 SHA-256 逐字节恢复到本地基线；项目虚拟环境导入与相关入口测试为 `11 passed`。
- 全仓测试结果为 `2296 passed, 142 failed, 1 skipped`；与 P0.1 相比失败数增量为 0，新增通过数来自本阶段 5 个清单分类测试。JUnit 已保存在受限快照目录，SHA-256 为 `858cf9fabbabbb3a8c12c765dcb9f4e17be2ab1bf42243e0e59ca8656bf98458`。142 项仍需在 P1 按行为合同裁决，当前不能据此宣布发布可用。

## 本地分类

| 处理建议 | 数量 | 含义 |
|---|---:|---|
| `commit_candidate` | 543 | 正式源码、测试、部署或文档候选 |
| `review_before_commit` | 85 | 评测、证据、根目录源码快照或含秘密字段处理逻辑，提交前必须审查 |
| 当前 Git 可见的受限文件 | 0 | 已通过精确忽略规则从提交范围隔离 |
| 敏感忽略候选 | 28 | 仍保留在本机，但 Git 默认不可见，禁止提交 |

完整清单保存在仓库外：

`C:\Users\00020271\.codex\backups\agent2_release_baseline_pre_20260715T114500+0800\local-repository-inventory.json`

SHA-256：`074f9c8ad143429c604150f0535b69c053d0c14360370ae8647a0fa3f584d086`

## 生产分类

| 处理建议 | 数量 | 含义 |
|---|---:|---|
| `commit_candidate` | 481 | 可进入后续生产源码恢复候选 |
| `review_before_commit` | 109 | 必须逐项比较后再恢复 |
| `gitignore_candidate` | 333 | 历史上传、临时同步或备份副本 |
| `restricted_backup_only` | 43 | 凭证、含敏感风险的副本或案件原始数据，只允许受限备份 |
| 敏感忽略候选 | 29 | 已被生产 Git 忽略，但需要后续收紧权限或清理策略 |

完整清单保存在：

`/home/ai_review_tunnel/codex_backups/agent2_release_baseline_pre_20260715T033913Z/production-repository-inventory.json`

SHA-256：`9cb52dfe350b405496e13a674d530a1365f441253f8799431a1028406a21d238`

## 安全边界

本阶段没有删除、移动或覆盖任何用户文件；没有修改生产 `.env`；没有改变 Agent1/Agent2 路由、Canary、主动消息或日报投影开关；没有写入业务数据库。

下一步只会从明确的源码、测试、部署、迁移、ADR 和证据范围准备基线提交。`artifacts/` 分享包、案件原始数据、临时同步目录、历史备份和凭证文件不会进入提交。
