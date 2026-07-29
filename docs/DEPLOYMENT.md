# 部署与配置

本文只提供脱敏后的通用流程。它不是生产服务器操作记录，也不能替代发布审批。

## 前置条件

- Python 3.11；
- PostgreSQL 16 或经过兼容验证的版本；
- 独立运行用户和最小文件权限；
- 受保护的环境文件；
- 反向代理、TLS、日志轮转、数据库备份和监控；
- 已批准的数据库迁移顺序和可用回滚包。

## 配置

从模板开始：

```bash
cp .env.example .env
```

必须单独设置的值包括：

- 数据库口令或完整 `DATABASE_URL`；
- LLM 地址、密钥和模型名；
- 需要启用 Stream 时的钉钉应用凭据；
- 经过审批的租户和用户范围。

`DINGTALK_API_BASE_URL` 和 `DINGTALK_OAPI_BASE_URL` 默认指向钉钉官方接口。只有使用经过审批的兼容网关时才修改。
`AGENT2_FACT_ALL_ACCESS_DINGTALK_USER_IDS` 和 `AGENT2_FACT_ALL_ACCESS_NAMES` 是特殊全量查询授权清单，默认必须留空；只有完成权限审批后才能填写。
`LEGAL_DAILY_DASHBOARD_SYSTEM_USER_ID` 是日报看板内部系统身份，不应填写真实员工身份。

模型、规则理解、绩效知识、日报分析和 Tool-Call（工具调用）的超时、重试、用户上限与批次窗口均在 `.env.example` 中提供安全模板值。`AGENT2_TOOL_CALL_CANARY_MAX_ACTIVE_USERS=1` 只是新环境的保守上限，不代表生产用户数量。身份、抄送和白名单默认留空；填写配置不等于功能上线，仍须同时开启对应功能开关。`LLM_BASE_URL` 必须是 API 基地址，不包含 `/chat/completions`。

安全模板中的写入、发送、自动发布和管理能力默认关闭。不要把真实 `.env` 提交到 Git。

## 本地容器

在 `.env` 设置一个本地数据库口令后，启动数据库和 API：

```bash
docker compose up --build postgres api
```

Scheduler 和 Stream 使用独立 profile，默认不启动：

```bash
docker compose --profile scheduler up --build scheduler
docker compose --profile stream up --build stream
```

Stream 只有在测试凭据齐全时才能启动。Scheduler 启动前必须确认提醒发送关闭或处于 dry-run（只演练、不发送）状态。

## 数据库迁移

数据库迁移属于发布步骤，不能把所有 SQL 文件一次性执行。

1. 备份数据库并验证备份可读；
2. 核对待执行迁移的顺序和适用版本；
3. 在隔离数据库执行迁移与回滚演练；
4. 使用 `scripts/apply_agent2_postgres_migration.py` 或经过批准的等价流程逐项执行；
5. 记录迁移结果，但不要把数据库内容或生产连接信息提交到源码仓库。

`database/schema.sql` 用于新建本地环境，不应直接覆盖现有生产数据库。

## 服务安装

脱敏后的 systemd 模板位于：

- `deploy/examples/ai-review-api.service.example`
- `deploy/examples/ai-review-stream.service.example`
- `deploy/examples/ai-review-scheduler.service.example`

复制前替换：

- `<APP_USER>`：独立运行用户；
- `<APP_DIR>`：发布目录；
- `<ENV_FILE>`：受保护的环境文件；
- 其他端口或进程参数占位符。

模板中的三个服务必须独立部署。Scheduler 只运行一个实例。

## 发布顺序

1. 从经过审阅的发布标记构建制品；
2. 保存当前源码、配置位置、迁移版本和回滚清单；
3. 部署到新的发布目录，不直接覆盖正在运行的目录；
4. 执行必要迁移；
5. 依次启动 API、Stream、Scheduler；
6. 进行只读健康检查；
7. 观察错误日志和任务状态；
8. 通过后再切换流量。

禁止用本地 Git 工作区直接覆盖服务器运行文件。

## 验收

最小只读验收：

```bash
curl http://localhost:8000/health
```

还需确认：

- API、Stream、Scheduler 均正常；
- 用户授权范围没有变化；
- 日报和绩效只读查询正常；
- 未发送真实测试消息；
- 未修改生产业务数据；
- Agent1 回退和 Agent2 受控切换保持原状。

## 回滚

出现异常时：

1. 停止切换，不清理当前发布目录；
2. 恢复上一份已验证发布制品；
3. 仅按已演练方案回滚数据库迁移；
4. 恢复原配置引用；
5. 再次进行只读健康检查；
6. 保存故障证据到内部存储。

源码仓库不保存生产备份、配置、日志或回滚制品。
