# 法务中心协同 Agent 项目规划

Status: planning baseline, 2026-07-03.

## 一、当前判断

这个项目不应继续按“日报机器人”规划，而应按“法务中心协同 Agent”规划。

日报仍然是第一优先级，因为它已经在线上真实使用，失败会直接影响用户信任。但日报不应再是系统默认归宿。新的核心问题应改成：

```text
用户这句话想做什么
属于哪个任务或工作流
需要读、写、修改、撤回、确认还是回答
是否能安全执行
执行后如何记录、回放和验证
```

钉钉能力暂时只作为输入输出通道，不作为当前阶段的架构重点。当前阶段优先做好纯文本 Agent Core，再把钉钉、定时任务、机器人消息作为适配层接回去。

## 二、现状盘点

### 已有基础

- Python + FastAPI 服务。
- SQLAlchemy 异步数据库访问。
- Pydantic 数据结构。
- pytest 测试体系。
- 旧日报服务已经覆盖大量真实业务行为。
- Agent2 已经有以下模块雏形：
  - `IncomingMessageEnvelope`
  - `WorkflowRouter`
  - `UserActionPlan`
  - `GateDecision`
  - `DailyCommand`
  - `ContextPack`
  - `KnowledgeResolver`
  - shadow replay / dialogue replay / harness
- 月报已经有数据结构、收集任务、渲染和线上发送脚本的基础。
- 已有大量真实历史记录、问题台账、线上 smoke 和回放材料。

### 主要问题

1. 旧日报服务太重，意图判断、状态处理、LLM 路由、执行保护、日期逻辑散在多处。
2. Agent2 虽然方向正确，但部分实现仍有“日报兜底”惯性。
3. 第一层意图判断曾被后续日报抽取绕过，说明缺少强制执行边界。
4. 测试通过曾经不等于真实用户灰测可用，评测口径需要更严格。
5. 仓库目前有大量临时脚本、并行文档和回放产物，需要治理，否则架构会继续发散。
6. 月报、出差、案件、问答还没有统一任务账本，容易互相误写。

## 三、外部参考结论

从 GitHub 上能找到不少日报、周报、钉钉、飞书、群聊总结项目，例如：

- 日报/周报台账项目：可参考日报、周报、月度汇总、历史版本、AI 生成等业务结构。
- DingTalk OpenClaw Connector：可参考日志提交、历史查询、多 Agent 接入方式。
- 飞书/微信群日报生成类项目：可参考从聊天记录中提取结构化事项。
- LangGraph、AgentScope 等 Agent 框架：可参考状态机、长任务、人机确认、工具调用和可回放执行。

但这些项目不能直接解决本项目最难的问题：多工作流、多轮上下文、编辑撤回、任务归属、操作审计和真实历史回放。所以它们只能作为局部参考，不能作为整体架构模板。

## 四、目标产品形态

最终产品是“法务中心协同助手”，而不是“日报 Agent”。

核心能力分为六类：

1. 日报：填写、修改、撤回、补交、复制昨天、查询历史、逐步填写、合并、编号编辑。
2. 月报：发送指标概览、收集团队负责人回复、生成团队月报、汇总部门月报。
3. 出差协同：从日报或表格识别出差计划，发现时间地点重叠并提示协同。
4. 案件进展：从案件台账、日报、固定节点收集承办人进展并汇总。
5. 内部问答：基于内部资料、制度、台账、历史记录回答问题。
6. 法律研究：未来能力，先不急于落地，必须先定义来源、输出和审核边界。

## 五、目标架构

```text
Channel Adapter
  钉钉 / API / 定时任务 / 测试 harness
        ↓
AgentTurnProcessor
  每条用户输入的统一入口
        ↓
ActionIntake
  先判断用户动作，不先猜日报内容
        ↓
TaskLedger
  找到当前用户的活跃任务、待确认事项、最近草稿
        ↓
CapabilityRouter
  选择日报、月报、出差、案件、问答等能力
        ↓
ExecutionPolicy
  决定 read_only / sandbox / write / needs_confirmation / forbidden
        ↓
ToolExecution
  调用具体业务能力
        ↓
OperationLedger + ReplyRenderer
  记录前后状态，并生成用户回复
```

### 关键原则

- 第一层先识别动作，不先写日报。
- 工作流归属不是单选题，一个消息可以拆成多个 segment。
- 所有写入都必须来自 ToolExecution，不能由解析层、协调层、renderer 偷偷写。
- 下游能力只能执行上游授权过的 workflow 和 operation。
- 任何能力都必须能输出操作账本，支持复盘。
- 真实上线前必须经过历史回放、随机多轮、线上 shadow 和人工灰测。

## 六、核心数据对象

### AgentTurn

每一条输入的统一记录：

- sender
- raw_text
- channel
- received_at
- active_task_snapshot
- action_plan
- routing_plan
- execution_policy
- tool_calls
- before_snapshot
- after_snapshot
- reply

### TaskLedger

统一管理活跃任务：

- task_id
- workflow
- owner_user_id
- status
- awaiting_user_id
- awaiting_fields
- last_artifact_id
- last_operation_id
- expires_at
- metadata

### OperationLedger

所有读写动作的审计账本：

- operation_id
- turn_id
- workflow
- capability
- operation
- target_artifact
- write_policy
- before_state
- after_state
- result
- safety_flags

### Artifact

各业务产物：

- DailyReportDraft
- DailyReportSubmission
- TeamMonthlyReport
- DepartmentMonthlyReport
- TravelPlan
- TravelOverlap
- CaseRecord
- CaseProgressItem
- KnowledgeAnswer

## 七、阶段规划

### Phase 0：项目治理和基线收口

目标：先把项目从救火状态变成可持续开发状态。

要做：

- 清理或归档临时脚本。
- 明确本地、服务器、正式运行版本的对应关系。
- 固化 release gate 文档。
- 把已有规划文档收束成一条主线。
- 明确 Agent2 暂不接管线上写入，除非通过灰测门槛。

验收：

- 能一眼看出当前生产入口、Agent2 入口、测试入口。
- 每次修改都有明确测试命令。
- 不再依赖散落临时脚本判断是否可上线。

### Phase 1：Agent Core 骨架，observe-only

目标：所有消息先进入统一 AgentTurnProcessor，但不改变线上行为。

要做：

- 定义 AgentTurn、TaskLedger、OperationLedger 的最小模型。
- 将现有 `IncomingMessageEnvelope` 升级为统一 turn 输入。
- ActionIntake 输出用户动作，而不是日报意图。
- CapabilityRouter 只做路由，不做写入。
- ExecutionPolicy 统一给出执行权限。
- 所有结果进入审计日志。

验收：

- 同一条消息能输出：
  ```text
  原话 → 用户动作 → 工作流归属 → 执行策略 → 是否写入
  ```
- observe-only 与旧系统执行结果可并排比较。
- 下游日报抽取无法绕过上游策略。

### Phase 2：日报能力重接入 Agent Core

目标：日报从旧服务里的“巨大流程”，变成 Agent Core 下的一个能力。

要做：

- 建立 DailyReportCapability。
- 支持三类写入：
  - add item
  - edit item
  - control action
- 建立稳定编号：
  - 今日工作 1、2、3
  - 问题风险 1、2、3
  - 明日计划 1、2、3
  - 同时支持“第5条”映射到全局编号。
- 支持复制昨天、昨日计划已完成、补交昨日日报。
- 支持清空、撤回、修改、合并、删除、移动。
- 写入前后必须有 snapshot。

验收：

- 20 组随机多轮日报填写通过。
- 20 组随机编辑/合并/删除/撤回/复制昨天通过。
- 20 组非日报/问答/闲聊/案件/出差混合表达不误写日报。
- 真实 60 天历史回放无高风险误写。
- 线上 shadow 至少一天无 P0/P1。

### Phase 3：月报能力接入 Agent Core

目标：月报不再靠优先级保护，而是成为正式任务能力。

要做：

- MonthlyReportCollectionTask 写入 TaskLedger。
- TeamMonthlyReport 和 MetricItem 作为正式 artifact。
- 团队负责人回复时，先通过 TaskLedger 找到活跃月报任务。
- 支持整段填写、部分填写、分多次填写、自然语言修改。
- 渲染团队月报和部门月报必须来自结构化数据，不拼 raw text。

验收：

- 月报回复不会写进日报。
- 复制【请回复】模板也能识别。
- 同模板不同团队不串任务。
- 同一人只有一个活跃任务时，能直接归属。
- 部门月报只输出筛选后的风险、重点动作、协调事项。

### Phase 4：出差协同 sandbox

目标：先只识别和记录候选，不自动通知。

要做：

- TravelPlan artifact。
- 从日报和表格识别：
  - 人员
  - 地点
  - 日期
  - 事项
  - 已出差/计划出差/可能出差
- TravelOverlap 检测。
- 只在 sandbox 输出候选，不私发。

验收：

- “明天去南京出差”既能写入明日计划，又能生成出差候选。
- “今天做出差协同系统”不能被识别为真实出差。
- “已经去南京开庭”能标记为已发生出差，但不误判返程。

### Phase 5：案件进展 sandbox

目标：先建立案件结构化上下文，避免把法律问答和案件进展混在一起。

要做：

- CaseRecord。
- CaseProgressItem。
- 案件承办人映射。
- 从日报中识别“具体案件进展”候选。
- 对泛泛的“案件沟通”保持日报，不进入案件台账。

验收：

- “XX案开庭/执行/调解/回款”进入案件候选。
- “被告缺席有什么后果”进入问答/法律研究，不进入日报。
- “撤回XX案起诉状”进入日报或案件进展候选，不误判为撤回日报。
- “撤回”单独出现时，必须结合活跃任务判断，否则不执行。

### Phase 6：内部问答和知识上下文

目标：问答作为 read-only 能力进入 Agent Core。

要做：

- 先接结构化数据源：
  - 人员/团队目录
  - 日报历史
  - 月报指标
  - 案件台账
- 再接文档 RAG。
- ContextPack 只接收摘要证据，不塞大量原文。

验收：

- “我手底下有几个案子”优先查结构化案件台账。
- “印章流程是什么”查制度或知识库。
- 没有可靠来源时明确说没有检索到，不猜。

### Phase 7：法律研究

目标：最后再做，不抢日报和月报稳定性。

前置问题：

- 法律研究输出是 memo、要点、检索清单还是风险提示？
- 使用哪些权威来源？
- 是否需要人工审核？
- 哪些结果不能直接给用户当结论？

验收：

- 未定义清楚前不接生产写入。

## 八、测试体系

测试不能只看“通过数”，必须看风险类型。

### 必备测试集

1. 单句单意图。
2. 单句多意图。
3. 多轮日报填写。
4. 多轮编辑、合并、删除、移动、撤回。
5. 上下文确认链。
6. 昨日日报、昨日计划、补交日期。
7. 月报整段回复和部分回复。
8. 出差候选和非出差误判。
9. 案件进展和法律问答区分。
10. 闲聊、反馈、辱骂、质疑、元问题。
11. 真实历史回放。
12. 随机生成压力测试。
13. 真实 LLM judge 抽查。

### 通过标准

- 不允许高风险误写。
- 不允许下游绕过上游策略写入。
- 不允许月报写进日报。
- 不允许问答写进日报。
- 对模糊表达可以问，但不能乱写。
- 写入类能力必须有 before/after snapshot。

## 九、灰测门槛

只有满足以下条件，才允许用户参与灰测：

1. Agent Core observe-only 已记录至少一天生产消息。
2. 日报能力在 harness 中通过随机多轮和真实历史回放。
3. 最近一轮真实测试没有高风险误写。
4. 可以展示每句话的：
   ```text
   原话 → 动作 → 工作流 → 操作 → 写入结果
   ```
5. 有一键关闭 Agent2 写入的开关。
6. 旧日报系统仍可兜底。

## 十、近期执行路线

### 最近 1 天

- 不急着灰测。
- 建立 Agent Core 最小 turn 记录和操作账本。
- 把日报测试结果改成“可解释报告”，不只显示 passed。
- 补齐“误写日报”类反例。

### 最近 3 天

- 日报能力接入 Agent Core dry-run。
- 完成编号体系和 item resolver。
- 完成 before/after snapshot。
- 跑真实历史 + 随机多轮。

### 最近 1 周

- 单用户灰测日报 Agent Core 写入。
- 月报接入 TaskLedger。
- 月报回复不再靠保护优先级，而靠活跃任务归属。
- 出差和案件保持 sandbox。

### 最近 2-3 周

- 日报 Agent Core 替换主要旧路径。
- 月报完成正式任务收集链路。
- 出差协同和案件进展开始小规模 shadow。
- 内部问答接结构化数据源。

## 十一、暂缓事项

- 不优先改钉钉通道。
- 不急着接多个向量库。
- 不急着做法律研究生产能力。
- 不把出差协同自动通知打开。
- 不再把新问题直接塞进旧日报服务里用规则补。

## 十二、下一步第一刀

下一步不是继续修某个识别规则，而是实现：

```text
AgentTurnProcessor + OperationLedger + DailyCapability dry-run
```

它必须做到：

- 每条消息有统一 turn。
- 每次动作有 operation。
- 每次日报变更有 before/after。
- 每条测试结果能解释“为什么这么判”。
- 旧系统和新系统能并排比较。

这一步做完，才算真正从 Agent1 的“日报兜底思维”里走出来。
