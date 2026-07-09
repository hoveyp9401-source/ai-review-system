# AI Review System

法务中心协同机器人项目，当前包含两代实现：

- Agent1：早期日报机器人，偏状态机和规则补丁，仍保留线上兼容能力。
- Agent2：新的 action-first / workflow-first 架构，用统一入口识别用户行为，再交给日报、月报、案件、出差、问答等 workflow 执行。

## 目录

- `app/agent/`：Agent1 旧日报执行器与兼容逻辑。
- `app/agent2/`：Agent2 新架构核心，包括认知判定、日报命令、执行层、RAG 问答、上下文包、审计与回放。
- `app/workflows/`：第一层工作流入口、意图/动作抽取与保护性 gate。
- `app/api/`：FastAPI 接口。
- `app/services/`：钉钉、日报、月报等业务服务。
- `scripts/`：线上 smoke、历史回放、部署辅助脚本。
- `tests/`：Agent1/Agent2 单元测试与回归测试。
- `evals/`：Agent2 离线回放样例和评测用例。
- `deploy/`：部署相关脚本。

## 本地测试

```bash
python -m pytest tests/test_agent2_daily_commands.py tests/test_action_intake.py tests/test_workflow_intake.py tests/test_agent2_daily_execution.py tests/test_agent2_llm_dialogue_generation.py -q
```

Agent2 对话回放：

```bash
python scripts/replay_agent2_daily_execution.py evals/agent2/dialogues --output-dir /tmp/agent2_daily_execution_reports --require-gray-ready
```

需要真实数据库的历史回放建议在服务器执行：

```bash
PYTHONPATH=. venv/bin/python scripts/run_workflow_gate_replay.py --source all --mode protective_gate --days 14 --limit 5000 --output /tmp/workflow_gate_replay.json
```

## 配置

不要把 `.env`、数据库、日志、RAG 原始表格、输出报告提交到 GitHub。仓库只保留代码、测试、脚本和可复用的评测样例。

Agent2 灰测开关示例：

```bash
AGENT2_DAILY_ENABLED=true
AGENT2_DAILY_ENABLED_USER_IDS=0515246015778891
```

## 当前重点

当前优先目标是让 Agent2 在日报场景中稳定超过 Agent1：

- 先判断用户想做什么，而不是默认写日报。
- 闲聊、问答、月报回复、案件查询不污染日报。
- 写库前有执行层保护，避免上游误判导致落错库。
- 支持日报多轮填写、修改、合并、清空、复制昨天、昨日计划完成、历史查看等高频操作。
- 9 点后禁止直接编辑昨天及更早日报，只允许查看、复制到今天或条件复制。

