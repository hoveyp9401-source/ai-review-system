# Legal Ops 页面—按钮—能力矩阵（2026-07-13）

| 页面 | 按钮/控件 | 当前行为 | 目标行为/API | 权限 | 状态要求 | 处置 |
|---|---|---|---|---|---|---|
| 登录 | 验证并进入 | localhost 自动使用硬编码 token，错误时显示 `invalid sandbox credential` | 服务端 principal token；过期态和中文错误 | 每个 principal | 验证中/失败/过期 | 修复 |
| 全局 | 导航 | demo 与 live 两套菜单 | 仅保留真实总览、报告、团队、案件、出差；审计仅管理员 | role-based | 当前页、加载失败 | 重构 |
| 全局 | 团队下拉 | demo 有假团队；live 为空 | 读取 server-side Team Mapping | team/admin | loading/empty/forbidden | 重构 |
| 全局 | 刷新 | toast“已从只读 BFF 刷新”并重载当前页 | 重新请求真实 API，显示最后更新时间 | 当前 scope | loading/success/error | 保留并修复 |
| 全局 | 当前身份头像 | demo 跳 admin；live 仍可触发不存在页面 | 展示当前姓名、角色、数据范围、退出 | 当前 principal | popover/退出 | 重构 |
| demo 首页 | 指标“进入明细” | 跳演示页 | 删除 demo；真实卡片携带稳定 filter | principal scope | loading/empty/error | 删除替换 |
| demo 报告 | 团队/人员/明细视图 | 只切换同一 Fixture 数据 | 真实报告筛选 | report scope | selected/empty | 替换 |
| demo 绩效 | 周/月/指标/目标/维护 tabs | 只展示演示数据 | 周报/月报保留；无真实口径的三项删除或禁用 | report/admin | selected/disabled | 精简 |
| demo 绩效 | 导出 Word/PDF/Excel | 读取预生成演示文件 | 仅真实报告生成导出；无 artifact 时明确禁用 | report/admin | generating/error | 后续接入或禁用 |
| demo 案件 | 缩放 +/- | 仅缩放森林 | 默认列表不需要；关系辅助视图可保留 | case read | selected zoom | 默认页删除 |
| demo 案件 | 状态/风险筛选 | 原始英文值 | 中文阶段筛选；无风险数据时隐藏风险筛选 | case read | loading/empty | 重构 |
| live 案件 | 批量配置追问 | summary 展开表单；预览/确认/正式写入已接 API | 管理员专用，中文字段、范围预览、逐案结果 | tenant_admin | preview/confirm/partial/conflict | 保留并完善 |
| live 案件 | 原告/被告/负责人/风险筛选 | 表单存在但列表仍森林；风险无真实字段 | 原告/被告、阶段、负责人真实筛选；隐藏风险 | case scope/admin | loading/empty | 重构 |
| live 案件 | 打开完整案件工作台 | drawer，加载 case+followup | 独立单案详情，权限再校验 | allowed_case_ids | loading/not_found/forbidden | 重构 |
| live 单案 | 保存单案策略 | PUT 正式 policy API，version/idempotency/receipt | 保存后回读；中文冲突/权限提示 | tenant_admin | saving/success/conflict | 保留并完善 |
| live 单案 | 立即追问一次 | POST typed command；conversation 不唯一时冲突 | 明确“创建任务≠已发送”，send kill switch 可见 | tenant_admin | queued/blocked/conflict | 保留并完善 |
| live 单案 | 取消未发送任务 | POST typed cancel | 仅 scheduled/queued 可见；回读结果 | tenant_admin | saving/success/conflict | 保留 |
| live 单案 | 新增/修改/删除案件进展 | 当前没有按钮 | 正式 CaseProgress typed API + version/idempotency/receipt/audit | owner/admin | saving/success/conflict/deleted | 新增 |
| live 报告 | 查询/新增/修改/删除/提交 | 当前无入口 | Report Executor；成功后回读完整用户可见报告 | owner/admin | full lifecycle | 新增 |
| live 团队 | 团队/人员详情 | 当前无入口 | server-side mapping、案件/报告下钻 | team/admin | loading/empty/forbidden | 新增 |
| live 出差 | 状态/候选/通知详情 | 只读原始表 | 中文状态；隐藏 provider ID；按来源过滤 | participant/admin | loading/empty | 重构 |
| live 审计 | receipt/audit 查看 | 直接全量展示技术字段 | 管理员分页筛选和脱敏详情 | tenant_admin | loading/empty/error | 重构 |

## 实施后核心按钮状态

| 页面 | 按钮/控件 | 正式后端/行为 | 回读与证据 | 状态 |
|---|---|---|---|---|
| 登录 | 验证并进入/退出身份 | principal credential；无 localhost 硬编码 live 凭证 | 庞浩、刘聪独立登录通过 | 已闭环 |
| 总览 | 指标卡/查看案件工作台 | scoped Phase 2 + case filters | 40/20/20 构成可下钻 | 已闭环 |
| 案件 | 搜索、类型、阶段、进展、分页 | `/api/workspace/cases` | 服务器回读 | 已闭环 |
| 单案 | 新增/修改/删除进展 | CaseProgress typed command + version + idempotency | receipt/audit + 刷新持久化 | 已闭环 |
| 单案 | 概览/生命周期/进展/当事人/操作记录/追问 | 真实区块导航按钮 | 浏览器点击定位对应内容 | 已闭环 |
| 报告 | 新增/修改/删除条目 | Report Executor / typed-daily adapter | 三张 committed receipt + 刷新持久化 | 已闭环 |
| 报告 | 提交报告 | Report Executor + 二次确认 | 自动化与服务器测试通过；未替庞浩提交真实日报 | 技术可用，待真人验收 |
| 出差 | 查看意图/候选/通知 | 只读 scoped API | 状态中文化且不越级声明送达 | 已闭环 |
| 团队 | 查看成员汇总 | server-side mapping | 两用户真实聚合 | 已闭环 |
| 追问 | 保存策略/立即追问/取消 | typed policy/task API | 管理员权限、receipt、回读 | 技术可用；发送受 kill switch 控制 |

所有删除和提交使用显式二次点击确认；新增、修改、删除后重新读取服务器状态。原 `window.prompt/window.confirm`、无效页签和“只读 BFF”文案已移除。
