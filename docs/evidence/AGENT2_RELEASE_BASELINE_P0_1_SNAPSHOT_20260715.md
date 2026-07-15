# Agent2 Release Baseline P0.1 快照

时间：2026-07-15（Asia/Shanghai）  
范围：`Agent2 Release Baseline & Two-User Production Acceptance Closure / P0.1`

## 裁决

当前生产服务在线，但本地、生产和 staging 不属于同一个可复现发布版本。P0.1 已完成不改变业务状态的工作树、Git 历史、配置、依赖、数据库 schema、服务状态和测试基线快照；在完成版本基线、写入边界和运行证据收口前，不得宣称 Agent2 可替代 Agent1。

## 安全边界

- 未修改业务代码；
- 未写入业务数据库记录；
- 未重启服务；
- 未修改生产开关；
- 未扩大 Canary；
- 未读取或复制 root-only 凭证 JSON 的内容；
- 本报告不包含账号、口令、Token、案件正文或用户 ID。

快照本身写入受限备份目录，用于防止后续整理造成当前运行版本丢失。

## 三套代码基线

| 位置 | HEAD | tracked | modified | untracked | 结论 |
|---|---:|---:|---:|---:|---|
| 本地 | `36c2199a` | 248 | 27 | 696 | 只有两次历史提交，大量正式源码未纳入 Git |
| 生产 `/home/ai_review_tunnel/ai-review-system` | `96cecec8` | 106 | 27 | 860 | 与本地不是同一当前提交，生产直接运行 dirty tree |
| Staging `/home/ai_review_tunnel/ai-review-system-staging` | `5a9220b5` | 90 | 25 | 46 | 独立旧历史，同样直接运行 dirty tree |

本地 tracked diff 为 5281 additions / 178 deletions；生产 tracked diff 为 11314 additions / 1350 deletions。

## 已验证快照

### 本地

- 路径：`C:/Users/00020271/.codex/backups/agent2_release_baseline_pre_20260715T114500+0800`
- 主归档覆盖 942 个文件；两个中文文件名图片由独立 sidecar 归档保存；
- working tree SHA-256：`615c1c9ed7b1c47123fb11385ca95bb20721982a8ebb103d50c093971eabf2e6`
- Git bundle SHA-256：`fa6cb74bd6fa87737cbb2e1141628537996a794a5278796bc33b5925305390e1`
- tracked patch SHA-256：`c5efea1b03e595956a0cb37d8c405d9d7574d159272c3d428413d6cf2a640220`

### 生产

- 路径：`/home/ai_review_tunnel/codex_backups/agent2_release_baseline_pre_20260715T033913Z`
- 965 个可读文件完成归档；1 个 root-only 凭证产物仅记录元数据并排除；
- working tree SHA-256：`b4652853f5bdfc36399441337d4d7b45437937678ab2885b2520f663b8c0f449`
- Git bundle SHA-256：`262f1d6a13fc55c9c40aa90074bf046f450386ef2e7a0f9bd8228a15898b370d`
- tracked patch SHA-256：`285e31924b3ca1b7cdd1560e59f8688e51e3d00159ea886951c58c9ded671687`

### Staging

- 路径：`/home/ai_review_tunnel/codex_backups/agent2_release_baseline_staging_20260715T035500Z`
- 136 个文件完成归档，无不可读文件；
- working tree SHA-256：`c4a97ab5cf66eaace38012c57ca848973906f488e484143499b0ded0d6f2451e`
- Git bundle SHA-256：`88e869b1c8e642b91e7e0f21a60ece357e969cc8e771a6bf147926c075154e0c`
- tracked patch SHA-256：`ad0470063d563c8ba585f2b79f53269e3dc3c243b27037cbce96781d413b18bc`

所有 bundle 和 tar 均完成结构校验。

## 本地与生产源码漂移

限定 `app/` 与 `deploy/` 的可执行源文件：

| 状态 | 文件数 |
|---|---:|
| 内容一致 | 204 |
| 同路径但内容不同 | 23 |
| 仅本地存在 | 3 |
| 仅生产存在 | 6 |

关键 Agent2 Runtime 文件 `semantic_interpreter_v3.py`、`report_document_contract.py`、`case_reference.py`、`webhook.py`、`stream_runner.py`、`scheduler/runner.py` 以及 Cognitive Core Prompt 当前本地与生产哈希一致。

`app/agent/prompts/report_agent.md` 当前哈希不一致。生产还独有 `app/agent/__init__.py` 以及 `app/progress/` 下 Outbox / Reconciliation / Worker 等五个源文件；这些文件必须从生产快照恢复并完成审查，不能在 Git 收口时丢失。

## 运行面

| 服务 | 状态 | 说明 |
|---|---|---|
| API | active | 端口 8000，health 200 |
| Stream | active | 单个 systemd MainPID |
| Scheduler | active | 单个 systemd MainPID |
| Legal Ops | 200 | 当前页面可访问 |
| Staging API | active | 监听 `0.0.0.0:8010`，需单独审计暴露范围 |

正式三服务均于 2026-07-15 10:50:53 左右启动，systemd 当前 `NRestarts=0`。

## PostgreSQL

- PostgreSQL：15.18；
- schema：`public`；
- 基础表：48；
- view：0；
- schema-only dump SHA-256：`2ce6548a827975f93f3cfe8ebcc7abf013b07788491c3784e540c29c02b48cc9`；
- 未发现 `alembic_version` 表，也没有可直接证明的唯一 migration head。

这不代表数据库不可用，但意味着“当前 schema 由哪一组 migration 唯一重建”尚未成立。

## 配置基线

- `APP_ENV=development`；
- Cognitive Core V3：开启，模型 `deepseek-v4-flash`；
- 主 LLM：`deepseek-v4-pro`；
- Semantic Admission：开启、Review Capture 开启、Enforce 关闭；
- Case Progress / Travel 真实写入：开启；
- Case Follow-up 决策：开启；真实发送：关闭；自动日报投影：关闭；
- Legal Ops Live：开启；Sandbox UI：关闭；
- Semantic Admission 与 Follow-up 均为两用户 allowlist；
- Daily allowlist 当前只有 1 个配置项；
- Reminder 真实发送开启，test allowlist 为 20 个配置项。

Daily、Semantic Admission、Follow-up 与 Reminder 的作用人群并不一致，必须在 P0-B 明确每条消息的唯一责任 Agent 和写入边界。

## 依赖基线

| 项目 | 本地 | 生产 |
|---|---:|---:|
| Python | 3.13.14 | 3.11.6 |
| pip freeze 条目 | 52 | 57 |

生产存在 `requirements.txt`、`Dockerfile`、`docker-compose.yml`，但当前本地发布基线缺少这些文件；两边依赖版本也有明显差异。因此当前无法从本地 Git 唯一重建生产 Python 环境。

## 测试基线

本地全仓 JUnit：

- 2434 tests；
- 2291 passed；
- 142 failed；
- 1 skipped；
- 91.56 秒；
- JUnit SHA-256：`bd546f42f78e3b00b41334448a0a8093310b13c142981bd331d0c0a8a209b34a`。

失败分布：

| 文件 | 失败数 |
|---|---:|
| `test_report_agent_state_protocol.py` | 136 |
| `test_agent_core_turn_memory.py` | 2 |
| `test_agent_core_processor.py` | 2 |
| `test_agent_core_operation_ledger.py` | 1 |
| `test_agent_core_task_ledger.py` | 1 |

142 项稳定复现，尚未完成行为合同裁决。不得直接标记为 deprecated，也不得通过 skip、xfail 或删除测试收口。

## 敏感文件风险

已确认但未读取内容：

- 生产存在 root-only Legal Ops 凭证 JSON；
- 本地 `.codex_tmp` 存在凭证命名的 JSON 和导出脚本；
- 生产存在多份历史 `.env` 备份，若干文件模式为 `644`；
- `.env.example` 当前模式为 `666`；
- `/home/ai_review_tunnel` 本身为 `700`，降低了其他系统用户直接访问风险，但文件模式仍不符合发布基线要求。

这些文件必须留在受控备份或密钥管理范围，绝不能进入 Git。

## P0.2 / P0-B 入口条件

下一步按以下顺序继续：

1. 建立逐文件分类和敏感信息处置表，不先删除文件；
2. 从生产快照恢复本地缺失的正式运行源文件和发布基础设施；
3. 明确 Daily 单用户、Agent2 双用户、Reminder 20 用户之间的路由关系；
4. 证明 Agent1 / Agent2 不会对同一消息双写，拒绝后也不会无约束回落；
5. 明确“查看80件案件”与“可修改哪些案件”的权限合同；
6. 将142项失败按行为合同聚类并形成 ADR；
7. 形成唯一 commit、schema migration head、依赖锁和 deployment manifest；
8. 完成回滚演练后，才进入 UI 真实性与双用户验收。

机器可读证据见 `agent2_release_baseline_p0_1_20260715.json`。
