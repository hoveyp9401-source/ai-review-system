# ADR-0012：离线 Production Shadow Candidate 能力隔离边界

## 状态

Accepted for offline verification，2026-07-10。未部署、未接生产、未启用 Shadow。

## 背景

Phase 1 Harness 已保证 Shadow/Replay `actual_write=false`，但正式 Shadow 候选还必须约束入口、采样、故障扩散、日志 PII、用户可见结果以及 production capability。只检查返回 receipt 不足以证明某个任意 adapter 没有在返回前产生副作用。

## 决定

`ProductionShadowCandidateAdapter` 采用以下硬边界：

1. 只接受 `ShadowRuntimeInput` 只读副本，不持有 webhook、Session、Repository 或生产 executor；
2. 只接受仓库内精确类型 `OfflineShadowEvaluator`，后者每次调用新建 `InMemoryConversationStateStore` 与 `InMemoryDailyDomainExecutor`；
3. 配置默认 `enabled=false`、`kill_switch=true`；
4. deterministic sampling、timeout、连续失败 circuit breaker 与 cooldown 在 adapter 内执行；
5. 返回 `ShadowObservation`，类型中没有用户回复字段；调用方不得把 Shadow 结果返回给用户；
6. 日志只保留 trace id、输入 hash/长度、加盐 identity hash、状态和 write flags，不记录 raw text 或明文 tenant/actor/conversation/message identity；
7. 任意 `actual_write=true` receipt 视为 invariant violation 并失败；
8. Live 与 Shadow composition root 分离。此 ADR 不授权 wiring、部署或连接生产流量。

## 放弃的方案

- 让 adapter 接受任意符合 Protocol 的 business executor；
- 复用生产 Conversation State store；
- 将 Shadow reply 与 Live reply 竞争或回传给用户；
- 以“低采样”代替 kill switch、timeout 或 circuit breaker；
- 在独立安全评审前把配置默认开启。

## 证据与限制

离线测试覆盖 kill switch、ephemeral/no-write、PII 最小化、timeout 与 circuit open。该证据只证明本地 adapter contract，不证明生产输入复制基础设施、独立日志 sink、密钥治理、容量或网络隔离已就绪。
