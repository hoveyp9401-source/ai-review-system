# 法务业务中台

这是一个面向法务协作场景的后端系统，提供日报、案件、绩效查询、数据接入和对话式工具调用能力。

> 本仓库内容是 **2026-07-29 受控私有生产基线**。它包含生产源码、迁移脚本、测试和安全模板，不包含生产配置、业务数据、日志或内部证据。为保证生产行为一致，少量必要业务规则常量会保留，因此不得公开传播。
> 在这套基线完成审阅并合并前，GitHub `main` **不等同于当前生产源码**。

## 能力状态

| 能力 | 状态 | 说明 |
|---|---|---|
| 日报填写、查询、编辑和定时任务 | LIVE | 已进入生产链路，所有写入仍受权限、日期和状态保护 |
| 被告绩效查询 | LIVE | 只读查询，不在对话链路中修改绩效来源 |
| 数据接入中心 | LIVE | 支持受控上传、解析和人工确认；自动发布关闭 |
| 案件主数据与现有案件进展 | LIVE | 支持权限范围内的查询和既有页面/API 操作 |
| Agent2 工具、回执和审计 | CONTROLLED LIVE | 仅对受控白名单开放，不代表全量启用 |
| Agent1 回退链路 | LIVE | 作为兼容路径保留，不得在未验证时删除 |
| 出差通知发送、主动跟盯发送、案件进展自动投影到日报 | NOT LIVE | 仅保留基础设施或实验实现 |

完整边界见 [生产基线](docs/PRODUCTION_BASELINE.md) 和 [未上线能力](docs/NOT_YET_LIVE.md)。

## 运行结构

系统由三个独立进程组成：

```text
API：网页、Webhook 和业务接口
Stream：接收钉钉长连接消息
Scheduler：执行提醒、汇总和受控后台任务
```

消息进入系统后，会经过意图判断、权限检查、结构化命令、业务执行、回执和审计。模型输出不能直接绕过执行层写入数据库。

## 目录

```text
app/
  api/                       HTTP 接口
  agent/                     Agent1 兼容链路
  agent2/                    Agent2 认知、工具、回执与审计
  legal_ops/                 案件工作台
  legal_ops_data_intake/     数据接入与绩效口径
  legal_daily_dashboard/     日报看板
  scheduler/                 定时任务
  services/                  日报、钉钉和汇总服务
database/                    基础数据库脚本
scripts/                     迁移、回放和核验脚本
tests/                       随生产快照保留的安全测试子集
deploy/examples/             脱敏后的服务模板
docs/                        受控基线、能力地图和部署说明
```

文件与能力的对应关系见 [功能—文件地图](docs/CAPABILITY_FILE_MAP.md)。

## 本地启动

本地环境只用于开发和沙箱验证，不能接入生产数据。

```bash
cp .env.example .env
```

至少设置：

- `POSTGRES_PASSWORD`
- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL`

`LLM_BASE_URL` 填 API 基地址即可，不要包含 `/chat/completions`；工具调用代码会自动追加该路径。

启动数据库和 API：

```bash
docker compose up --build postgres api
```

健康检查：

```bash
curl http://localhost:8000/health
```

Scheduler 和 Stream 默认不启动。确认使用的是测试配置后，分别启用：

```bash
docker compose --profile scheduler up --build scheduler
docker compose --profile stream up --build stream
```

更完整的说明见 [部署与配置](docs/DEPLOYMENT.md)。

## 测试

安装依赖后运行：

```bash
python -m pytest -q
```

当前基线保留的是随生产源码整理出的安全测试子集。发布前仍需在受控环境完成完整回归、数据库迁移核验和无真实消息的健康检查。

## 安全与数据

禁止向本仓库提交：

- `.env`、令牌、密钥、真实 Webhook 和数据库连接信息；
- 用户、案件、日报、绩效、对话、录音转写等业务数据；
- 生产 IP、绝对路径、用户白名单、运行日志和逐文件指纹；
- 部署回放、验收包、清理报告和其他内部证据。

发现安全问题时，请按 [SECURITY.md](SECURITY.md) 处理。
