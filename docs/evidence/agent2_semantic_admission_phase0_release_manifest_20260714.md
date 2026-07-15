# Agent2 跨领域语义准入 Phase 0 Release Manifest

> 快照时间：2026-07-14 02:10:13 +08:00（Asia/Shanghai）  
> 证据性质：发布前只读基线；不是部署记录，不是生产验收结论。  
> 关键边界：下列服务器事实来自本轮实现开始前已采集的基线证据；编写本文时未重新连接、读取或修改服务器。

## 1. 快照结论

- `baseline_captured_before_current_implementation=true`。
- 本轮语义准入实现文件仍只存在于本地 dirty worktree，`current_implementation_deployed=false`。
- 生产服务器基线与本地基线不在同一 Git commit，且两端工作树均 dirty；不能用 Git 分支名或单一 commit 声称代码一致。
- 基线 PostgreSQL 中不存在本轮拟新增的 6 张语义准入表，且仓库/服务器没有可用的 migration history 表；在完成 migration、代码哈希对齐和限定 smoke 前，不具备发布可追溯性。
- 日报 allowlist 为 1 人、案件 Follow-up allowlist 为 2 人；两套范围不一致，若直接开启组合写入，可能出现同一用户可进入案件链但不能进入日报链的非对称行为。
- `action_context.py`、`action_intake.py`、`webhook.py` 已确认存在本地/服务器漂移。部署不得覆盖服务器未知修改，必须逐文件校验或形成明确部署包。

## 2. Git 与工作树基线

### 2.1 实现开始前基线

| 项目 | 本地 | 服务器 |
|---|---|---|
| branch | `main` | `main` |
| Git HEAD | `36c2199a983f093f54138f7f7128fbaf077cdb4d` | `96cecec81c4ac9fe4e0500c08a52e661d094147d` |
| tracked modified | 23 | 27 |
| untracked files | 443 | 826 |
| tracked diff | `+3034 / -125` | `+10512 / -1338` |
| worktree | dirty | dirty |

这些数字在本轮语义准入实现开始前采集。服务器数字属于既有基线证据，不是本文编写时重新查询所得。

### 2.2 本地只读复采

2026-07-14 02:09–02:10 +08:00 的本地只读复采结果：

| 项目 | 值 |
|---|---|
| branch / HEAD | `main@36c2199a983f093f54138f7f7128fbaf077cdb4d` |
| tracked modified | 23 |
| staged | 0 |
| untracked files（递归） | 454 |
| tracked diff | `+3066 / -125` |
| 系统 Python | `3.12.9` |
| 仓库 venv Python | `3.13.14` |

复采数字只说明当前本地工作树状态。相对实现前基线新增的未跟踪文件和 tracked diff 变化，与本轮及并行既有工作重叠，不能据此把全部差异归因于语义准入实现。

## 3. 服务器运行基线

以下均为实现开始前的服务器基线事实：

| 项目 | 基线值 | 证据边界 |
|---|---|---|
| Python | `3.11.6` | 既有服务器只读采集 |
| API health | `{"status":"ok"}` | `127.0.0.1:8000/health` 的既有采集 |
| API service | `active` | 既有服务状态采集 |
| Stream service | `active` | 既有服务状态采集 |
| Scheduler service | `active` | 既有服务状态采集 |
| Cognitive model | `deepseek-v4-flash` | 服务器生效配置基线 |
| thinking | `false` | 服务器生效配置基线 |
| cognitive contract | `cognitive_core.v3` | 服务器生效合同基线 |

健康和 `active` 只证明采样时进程可用，不证明三入口代码一致、语义准入生效或业务闭环通过。

## 4. PostgreSQL 基线

| 项目 | 值 |
|---|---|
| PostgreSQL | `15.18` |
| Agent2 相关表数 | 31 |
| schema hash | `04013f...`（既有采集所保留的脱敏前缀） |
| migration history | 不存在可用的 migration history 表 |
| 本轮 admission migration | 未执行 |

实现开始前，下列拟新增表均不存在于真实服务器 PostgreSQL：

- `agent2_semantic_admission_traces`
- `agent2_semantic_admission_decisions`
- `agent2_semantic_admission_tickets`
- `agent2_semantic_review_items`
- `agent2_deferred_semantic_events`
- `agent2_information_pendings`

因此，当前只能表述为“本地 schema/migration 候选已生成”；不得表述为“数据库已支持”或“服务器已部署”。

## 5. 核心开关与限定范围基线

| 能力 | 服务器基线 |
|---|---|
| 第一层 workflow protection | `protective_gate` |
| Cognitive v3 | 已开启 |
| Party / Case Progress / Travel | 对当前 Agent2 Sandbox 已开启 |
| Case Lifecycle Follow-up evaluation | `true` |
| Case Lifecycle Follow-up send | `false` |
| Case Follow-up report projection | `false` |
| 旧 Follow-up send effect | 关闭 |
| Semantic Admission enabled/enforce/review/shadow | 基线时均未配置（`UNSET`） |
| tenant scope | 仅 `sandbox-agent2-phase2-20260711` |
| Follow-up user allowlist | 2 人：庞浩、刘聪 |
| Daily user allowlist | 1 人 |

`UNSET` 不等于已验证的 `false`：部署时必须显式写出默认关闭值，并在启动日志/配置快照中证明实际生效。日报 1 人与 Follow-up 2 人的不一致必须在开启组合执行前统一裁决；不得靠任一入口的“最近用户”或隐式 fallback 扩大范围。

## 6. 已确认的本地/服务器漂移

实现开始前已确认以下三个生产关键文件本地与服务器内容不一致：

- `app/workflows/action_context.py`
- `app/workflows/action_intake.py`
- `app/api/webhook.py`

本文编写时的本地 SHA-256：

| 文件 | 本地 SHA-256 |
|---|---|
| `app/workflows/action_context.py` | `bcf2b47b40772f411fe0edeebdea4d39a7d0bb7b9eda9c6bc18d25672f068f71` |
| `app/workflows/action_intake.py` | `bc88575acc37fe37a5ee8db6ca79b4c9b69fb2f2e8c8cd698bc05fe021469236` |
| `app/api/webhook.py` | `dc61026f08d54160f611a6e253a56acce93cca743808b8dd2623fa2680351738` |

服务器对应 SHA-256 未在本文中重新采集，故不能生成“本地/服务器匹配”结论。后续部署前必须重新只读采集服务器哈希，并对漂移逐项归因。

## 7. 当前本地语义准入实现快照

这些文件在本地已存在，但本快照时尚未部署到服务器：

| 文件 | SHA-256 |
|---|---|
| `app/agent2/admission_contracts.py` | `9818347192a3ae180f65151484479156654518994cd78c94a5fb11360a16339c` |
| `app/agent2/domain_admission.py` | `3424ee2eac114fa31c6d9fa39b526e0c77361d251620940ea74f26b3fa303679` |
| `app/agent2/cognitive_core_v3.py` | `2f8b71166f92ce072bbe3ab6479b7e350be642b916dea00a857d728074516a4c` |
| `app/agent2/command_planner_v3.py` | `971370784c315b5275d7021b4aa959493e2ffa3ba2a18ccdb978ca2483b2f4e4` |
| `app/agent2/typed_daily_commands.py` | `adb0c080e9d4d25c0b1a3872e7a8b02eea59cfe3ae4956199566dee4b4a9f68e` |
| `app/agent2/runtime/context.py` | `5d36304c279ab21648bc1aca8aa2bac1068cb7f4684aa69af45bb98e52fda8b1` |
| `app/config.py` | `d75971de714ddff481c0d192e23c30b03e1168f9ba6d09dfdb52baf8bd0c3421` |
| `docs/ADR/0019-domain-admission-and-composition.md` | `480b45034951c003ca96fd5e0528e4b599ac57d4c188ebfd4d884744cea4c9dc` |
| `docs/schemas/agent2-domain-admission.schema.json` | `38cbf24d9659d2c6f05c71a812e95b718a37a52df589c9bf90bc6058fbb258d5` |
| `scripts/create_agent2_semantic_admission.sql` | `c4a680e5ec4f7584f191d1d36dda777c09bcd1d7d7018423e7fb56fd2d4d9872` |
| `docs/AGENT2_SEMANTIC_ADMISSION_ROLLBACK.md` | `d65c4b26fa0536e6b3d2db21e135d5d6dffd218e3707358dd19f8beab8fdd29b` |

上述 11 项按路径排序后，以 `path<TAB>sha256`、LF 连接形成的 bundle SHA-256 为：

`8c71f964c00500b3d86be2752a7d56432fb0fecadcbc4e88e4a832498b52aee5`

该 bundle 是 02:09–02:10 的开发中快照，不是最终 Runtime hash。任何后续代码修改都必须重新计算，且部署后必须在服务器重复计算并逐项一致，才能用于发布证据。

## 8. 可复现命令

以下命令均为只读。路径、主机和凭证使用占位符；不得把 `.env`、数据库 URL、token 或真实 user ID 输出到日志。

### 8.1 本地 Git / Python

```powershell
Set-Location <local-repo>
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --porcelain=v1
git diff --stat
git diff --numstat
@(git diff --name-only).Count
@(git diff --cached --name-only).Count
@(git ls-files --others --exclude-standard).Count
python --version
.\venv\Scripts\python.exe --version
```

### 8.2 本地文件哈希

```powershell
Set-Location <local-repo>
Get-FileHash -Algorithm SHA256 app\workflows\action_context.py
Get-FileHash -Algorithm SHA256 app\workflows\action_intake.py
Get-FileHash -Algorithm SHA256 app\api\webhook.py
Get-FileHash -Algorithm SHA256 app\agent2\admission_contracts.py
Get-FileHash -Algorithm SHA256 app\agent2\domain_admission.py
Get-FileHash -Algorithm SHA256 app\agent2\cognitive_core_v3.py
Get-FileHash -Algorithm SHA256 app\agent2\command_planner_v3.py
Get-FileHash -Algorithm SHA256 app\agent2\typed_daily_commands.py
Get-FileHash -Algorithm SHA256 app\agent2\runtime\context.py
Get-FileHash -Algorithm SHA256 app\config.py
```

### 8.3 服务器部署前只读复核

以下命令应在已经完成授权登录的服务器 shell 内执行；本文未执行它们：

```bash
cd <server-repo>
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short
git diff --stat
python3 --version
systemctl is-active <api-service> <stream-service> <scheduler-service>
curl --fail --silent http://127.0.0.1:8000/health
sha256sum app/workflows/action_context.py app/workflows/action_intake.py app/api/webhook.py
```

数据库结构复核应使用只读账号，仅返回 PostgreSQL 版本、表名/数量和 schema hash；不得输出连接串、主体 ID、案件正文或消息内容。

## 9. Release Guard

在以下条件全部满足前，本 manifest 维持 `CURRENT_IMPLEMENTATION_NOT_DEPLOYED`：

1. 处理三处本地/服务器漂移并生成双方 SHA-256 对照；
2. 显式设置 Semantic Admission 开关为默认关闭，并证明 tenant/user allowlist 只命中目标范围；
3. 统一或明确日报 1 人与 Follow-up 2 人 allowlist 的产品行为；
4. 在隔离 PostgreSQL 完成 migration/rollback 验证，再对服务器执行受控 migration；
5. 记录 migration 身份或等价的不可变 schema receipt；
6. 重新生成最终 Runtime bundle hash，并证明本地、部署包、服务器三方一致；
7. API、Stream、Scheduler 通过同一入口的 Shadow/限定 smoke，且 effect 开关保持关闭直至 Gate 通过。

