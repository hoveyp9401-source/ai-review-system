# Agent1 与 Agent2 架构说明

## Agent1

Agent1 是早期日报机器人，核心目标是让用户通过钉钉自然语言填写日报。它的主要特征：

- 以日报为中心，很多输入会先被理解成日报内容。
- 依赖状态机、pending、规则分支和历史上下文补丁。
- 对单一日报流程可用，但在多任务混合时容易误写，例如闲聊、问答、月报回复被写进日报。
- 仍保留在仓库中，用于线上兼容和历史行为对照。

相关位置：

- `app/agent/`
- `app/services/report_service.py`
- `tests/test_report_agent_state_protocol.py`

## Agent2

Agent2 的目标不是重写一个更大的日报机器人，而是升级成法务中心协同操作系统的底座。它的核心原则：

1. Action first：先判断用户这句话想做什么。
2. Workflow first：日报、月报、案件、出差、问答是不同 workflow，不能互相污染。
3. Write gate：只有明确写库意图才能写库。
4. Execution invariant：即使入口或 LLM 误判，执行层也必须拦住高风险错误。
5. Context pack：上下文用于辅助判断，但不能替代写库安全规则。

相关位置：

- `app/workflows/action_intake.py`：第一层动作抽取。
- `app/workflows/intake.py`：workflow gate 与 legacy bridge。
- `app/agent2/daily_commands.py`：日报动作编译成结构化命令。
- `app/agent2/daily_execution.py`：日报命令执行层与最后保护。
- `app/agent2/context_pack.py`：上下文包。
- `app/agent2/cognitive_decision.py`：认知判定数据结构。
- `app/agent2/case_table_rag.py`、`app/agent2/rag_qa.py`：案件表格问答。
- `app/agent2/daily_execution_replay.py`：离线回放。

## 当前已固化的重要规则

- “我要写日报了”“帮我写日报吧”只是开始收集，不写入今日工作。
- “写日报了”“我写日报了”属于元话语，不写入日报。
- “写日报，把合同审核记上”可以写入真实业务内容。
- 9 点后不允许直接编辑、清空、删除、撤回昨天及更早日报。
- 9 点后仍允许查看历史日报、复制昨天日报、把昨天计划完成项带到今天。
- 案件查询、内部问答、月报填写不应进入日报。
- 执行层必须独立保护历史日报高风险写操作，不能只依赖入口识别。

## 验证方式

关键单元测试：

```bash
python -m pytest tests/test_agent2_daily_commands.py tests/test_action_intake.py tests/test_workflow_intake.py tests/test_agent2_daily_execution.py tests/test_agent2_llm_dialogue_generation.py -q
```

对话回放：

```bash
python scripts/replay_agent2_daily_execution.py evals/agent2/dialogues --output-dir /tmp/agent2_daily_execution_reports --require-gray-ready
```

线上日期规则 smoke：

```bash
PYTHONPATH=. venv/bin/python scripts/run_online_agent2_date_smoke.py --base-url http://127.0.0.1:8000 --output /tmp/online_agent2_date_smoke.json
```

真实历史 gate 回放：

```bash
PYTHONPATH=. venv/bin/python scripts/run_workflow_gate_replay.py --source all --mode protective_gate --days 14 --limit 5000 --output /tmp/workflow_gate_replay.json
```

## 下一阶段建议

- 用便宜模型批量生成 5000 到 6000 条单轮/多轮测试样例。
- 测试集必须覆盖日报、闲聊、问答、月报、案件、出差、修改、撤回、清空、复制昨天、历史日期、口语化表达。
- 每轮失败都先归类到“入口识别、命令编译、执行保护、回复层、RAG 数据”之一，再修复。
- 修复不得回到 Agent1 的“日报优先猜测”，必须维持 Agent2 的 action-first 方向。

