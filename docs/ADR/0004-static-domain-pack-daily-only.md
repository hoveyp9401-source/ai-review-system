# ADR-0004：静态 Domain Pack，Phase 1 只执行 Daily

## 状态

Experimental，2026-07-10。

## 背景

Planner 已生成 Daily 和 business commands，但生产只消费 Daily。未来需要 case、travel、performance、knowledge，当前 Core/Prompt/Planner 尚未实现真正动态 catalog。

## 决定

Phase 1 使用代码内静态 `DomainPackRegistry`：

- action type 和 typed command type 必须有唯一 owner；
- duplicate domain ID 或 owner 在 composition/startup 时失败；
- Daily 注册 typed simulator executor；
- case/travel 仅因当前 Planner 已能生成 typed business command 而注册 contract-only owner；
- performance/knowledge 仅提供 contract-only descriptor，不进入 Phase 1 registry、没有 executor；名称仍为 experimental，第二个真实 Domain 接入前不得视为稳定 v1；
- contract-only command 产生 `unsupported_domain_contract` receipt；
- 所有 executor 只接收 typed commands，不读取 raw text。

Harness 校验每个 RequiredAction 恰好进入 typed command 或 PlanningBlock；Registry 校验每个 planned command 恰好由一个 owner 处理并返回一个 receipt。Receipt 必须匹配完整 typed command body、command ID 和成功所需 validation status，不能只复述 ID。

Daily simulator 复用 `execute_typed_daily_command`，不复制 validator 或 mutation 语义。

## 放弃的方案

- Dynamic Skill Marketplace；
- 数据库加载 executable handler；
- 为未来 Domain 建空业务目录和假实现；
- 未执行的 business command 只写 audit、不进入 Outcome；
- 重写稳定 typed Daily Executor。

## Phase 1 影响

该 registry 是 experimental seam，不能宣称 Domain Pack 已完全插件化。

## 后续方向

第二个真实 Domain 到来时，以 versioned action/entity contract 和 Capability Catalog 替换 Core/Planner 中的中央硬编码。
