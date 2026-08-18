# 日报系统问题台账

创建日期：2026-06-26

本台账用于记录法务日报机器人从建设以来暴露过的产品和工程问题。它不是一次性复盘文档，而是后续修复、回归和验收的入口。

## 维护规则

- 每个真实用户问题必须进入本台账，除非确认是外部平台故障。
- 新问题先登记为 `open` 或 `watching`，修复并有回归用例后改为 `fixed`。
- 每条问题尽量记录：用户原话、现象、根因、修复位置、回归用例、线上验证。
- 不确定来源的问题可以登记，但证据级别标为 `partial`，不要假装完整。

## 状态说明

| 状态 | 含义 |
| --- | --- |
| fixed | 已修复，并有测试或线上验证 |
| fixed-candidate | 隔离候选已修复，并通过对应回归或回滚演练；尚未部署，不能视为线上已生效 |
| fixed-candidate-verifying | 隔离候选的定向回归已通过，仍在执行完整回归或独立审查；尚未部署，不能视为线上已生效 |
| watching | 已处理主要问题，但仍需观察真实对话 |
| open | 未修复或未完全定位 |
| deferred | 产品明确暂缓，本轮不实现，也不增加临时规则 |
| authorization-required | 已明确处理边界，但执行会修改真实数据，须取得单独授权 |
| legacy-gap | 旧测试/旧流程遗留，和当前修复不完全同口径 |
| evidence-gap | 只有间接证据，缺少完整用户对话或日志 |

## 证据来源

| 来源 | 说明 |
| --- | --- |
| `REPORT_FILLING_FRICTION_AUDIT_2026-06-22.md` | 2026-06-22 日报填写摩擦审计，包含庞浩多轮失败案例 |
| `P0_ACTION_PLANNER_HARDENING_AUDIT.md` | Action Planner P0 加固审计 |
| `PROJECT_STATE_2026-06-22.md` | 2026-06-22 运行状态和已知剩余问题 |
| `HANDOFF_PROGRESS_PHASE_1_5_2026-06-20.md` | progress outbox 阶段交接 |
| `SHADOW_MEMORY_V2_AUDIT_2026-06-22.md` | shadow memory / user habits 审计 |
| `tests/real_llm_regression_cases*.json` | 真实 LLM 回归语料 |
| `tests/fixtures/report_dialog_regression_cases.json` | 手工回归语料 |
| `tests/test_*.py` | 已沉淀到测试里的回归问题 |
| 2026-06-26 当前对话 | 陆健、庞浩暴露的复制、编号、昨日计划问题 |

## 当前重点缺口

| 编号 | 状态 | 问题 | 当前处理 |
| --- | --- | --- | --- |
| GAP-001 | legacy-gap | 服务器旧专项单测仍有 legacy 红点：`tests/test_concurrency_safety.py` 4 个 fake-store 并发断言未覆盖当前 report-agent 路径；`tests/test_report_risk_and_briefings.py` 2 个晨报旧文案断言与当前格式不一致 | 已用线上 API/真实 DB smoke 覆盖同类并发、幂等、复制、编号展开；旧测试需单独迁移为有效回归，不能算线上 smoke 通过项 |
| GAP-002 | watching | 完整问题台账此前不存在，早期真实钉钉对话若未落日志/测试/审计，无法 100% 还原 | 已建立本文件；后续新问题必须补录 |
| GAP-003 | watching | 首次抽取粒度仍可能过细，一个项目/系统计划被拆成过多条 | 已在 2026-06-22 审计中标为 P0.5，需后续优化 |
| GAP-004 | evidence-gap | shadow memory 有事件和 candidate habit，但无 active habit，真实用户个性化纠错尚未充分生效 | 需要 admin 审核/激活闭环或自动学习策略 |

## 问题条目

| 编号 | 状态 | 首次证据 | 用户/场景 | 问题现象 | 根因/判断 | 修复/回归 |
| --- | --- | --- | --- | --- | --- | --- |
| DR-001 | fixed | 早期回归测试 | 碎片化填报 | 多条碎片消息没有稳定合并到同一份日报，或不能进入待确认 | 状态机需要按三栏完整度累计 | `test_fragmented_report_enters_pending_confirmation`、`test_confirm_turns_pending_confirmation_into_completed` |
| DR-002 | fixed | 回归测试 | 确认提交 | 用户说“确认，辛苦了”等礼貌后缀时无法提交或被当正文 | 确认语识别过窄 | `test_confirmation_with_courtesy_suffix_submits_pending_report` |
| DR-003 | fixed | 回归测试 | 当前草稿清空 | 收集态/待确认态清空逻辑不一致，可能误入 LLM 或误写正文 | 高风险动作和普通填写混在一起 | `test_clear_current_report_*`、`test_pending_action_confirm_clear_runs_before_courtesy` |
| DR-004 | fixed | 回归测试 | 已提交日报修改 | 已提交日报被直接覆盖，缺少撤回/确认保护 | completed 状态下写操作风险分层不足 | `test_completed_report_can_be_modified_same_day`、`test_completed_full_rewrite_is_not_applied_directly` |
| DR-005 | fixed | 回归测试 | 闲聊/感谢/测试文本 | “谢谢”“哈哈”“测试”等被写入日报 | 非日报意图过滤不足 | `test_casual_chat_is_not_written_into_report`、`test_courtesy_reply_is_not_written_into_report`、`test_probable_noise_inputs_are_blocked_before_report_write` |
| DR-006 | fixed | 回归测试 | 短句槽位回答 | “没问题”“正常”“暂无”在不同槽位含义不同，曾误填工作或计划 | 缺少当前缺失槽位上下文 | `test_short_reply_means_no_problem_only_when_asking_problems`、`test_short_normal_in_tomorrow_slot_is_not_stored` |
| DR-007 | fixed | 回归测试 | 追加多轮 | 用户先说“补充”，再选“问题/计划”时，系统重复追问或写入字段名 | pending append target/content 状态恢复不足 | `test_pending_append_target_selects_problem_without_writing`、`test_pending_append_content_forces_selected_field_when_model_drifts` |
| DR-008 | fixed | 回归测试 | 笼统内容质量 | “处理了项目/恢复了技能”太笼统，系统直接写入导致信息质量差 | 缺少轻量质量澄清 | `test_vague_project_handling_triggers_clarification`、`test_specific_project_handling_does_not_trigger_clarification` |
| DR-009 | fixed | 回归测试 | 自然语言改单条 | “函件改成邮件”“合同改成增补合同”等可能追加新条而非替换 | 局部 replace_text 定位不足 | `test_replace_text_patch_updates_existing_item_instead_of_appending`、`test_direct_text_replace_unique_match_executes_and_multi_match_clarifies` |
| DR-010 | fixed | 回归测试 | 删除条目 | 删除第 N 条、范围删除、越界删除容易误删或假成功 | 编号和可见列表映射不足 | `test_delete_out_of_bounds_names_missing_index`、`test_batch_delete_uses_original_indices` |
| DR-011 | fixed | 回归测试 | 撤销删除 | 删除后“恢复/撤销”不能放回原位置 | 缺少 previous snapshot 和最近删除上下文 | `test_delete_then_undo_restores_items`、`test_restore_recent_deleted_item_from_snapshot` |
| DR-012 | fixed | 2026-06-22 审计 | 庞浩 | “2-7 是同一点”“合并今日工作 2、3、4、5”被规划成删除/替换/no_change | LLM action planner 对 merge-like 语言不稳，executor 无 mismatch guard | P0 prompt + executor guard；`test_direct_567_merge_is_parsed_without_confirmation`、`test_merge_like_delete_item_output_is_coerced_to_merge` |
| DR-013 | fixed | P0 审计 | 庞浩/真实回归 | LLM 输出 `should_write=false` 但带写动作，系统可能假成功或不执行 | plan 协议和执行器容错不足 | `test_no_write_merge_action_with_edit_intent_executes_when_safe`、`test_no_write_unsafe_write_action_with_edit_intent_asks_clarification` |
| DR-014 | fixed | P0 审计 | 撤回日报 | `unsubmit_report` executor/schema 有支持，但 prompt action list 缺失 | prompt/schema 不一致 | `test_report_agent_prompt_action_list_matches_schema_for_unsubmit_report` |
| DR-015 | fixed | 回归测试 | 历史日报查询 | 查询昨天日报后短句续接容易丢上下文或写入今天 | recent report context 缺失 | `test_history_query_reads_yesterday_without_writing`、`test_history_query_then_followup_edit_stays_on_yesterday` |
| DR-016 | fixed | 2026-06-26 对话 | 陆健 | “昨天除了第6项没做，其他正常完成；今日工作计划同昨日计划1~5项”被写成“完成昨日计划第1~5项” | 编号引用没有从昨日 tomorrow_plan 展开 | executor 兜底展开 previous plan indices；`test_previous_plan_range_reference_expands_items_and_drops_placeholders` |
| DR-017 | fixed | 2026-06-26 对话 | 陆健 | “第6项未完成”被写入今日工作或明日计划 | 未完成状态说明和计划滚动混淆 | 只有明确“明天继续”才 rollover；`test_previous_plan_reference_variant_phrasings_expand_safely` |
| DR-018 | fixed | 回归测试/2026-06-26 对话 | 昨天/某日期引用 | 只提“昨天/某日期”就被当成要修改历史日报并触发 9 点限制 | 日期引用和历史修改意图耦合过强 | `_explicitly_targets_previous_report_change` 收紧；`test_previous_date_reference_alone_does_not_trigger_history_cutoff` |
| DR-019 | fixed | 2026-06-26 对话 | 庞浩/陆健 | “复制昨天/前天/6月24日日报到今天”被历史限制拦截或不识别 | 整篇复制缺少后端直达动作 | 新增整篇复制路径；`test_direct_copy_day_before_yesterday_report_to_today` |
| DR-020 | fixed | 2026-06-26 对话 | 庞浩 | 整篇复制后编号丢失，多个事项挤成一行 | 源日报可能以单字符串保存，预览走普通确认格式 | 复制时拆旧编号/换行/连续空格，并专用编号预览；`test_whole_copy_splits_space_separated_source_sections` |
| DR-021 | fixed | 2026-06-26 对话 | 庞浩 | 系统出现“全部覆盖/选择性复制”二选一，用户回复“全部覆盖”后又不理解 | 老 LLM 路径未被后端整篇复制截断 | 后端识别“复制昨天的日报”，旧上下文“覆盖/全部覆盖”兼容为整篇复制；prompt 禁止二选一文案 |
| DR-022 | fixed | 回归测试 | 复制昨天日报 | “复制昨天的日报”多一个“的”未命中触发词 | 触发词过窄 | `test_direct_copy_yesterday_report_to_today_after_cutoff` 增加该说法 |
| DR-023 | fixed | 回归测试 | 粘贴昨日完整日报 | 用户粘贴过去日报做参考时，系统可能把过去 today_work 原样写入今天 | 参考日报和今日填报未隔离 | `load_reference_report`；`test_pasted_yesterday_report_is_intercepted_before_agent` |
| DR-024 | watching | 2026-06-22 审计 | 首次抽取 | 一个项目/系统计划拆成太多今日工作，后续需要合并 | 抽取粒度过细 | 有 merge 修复；粒度优化仍是 P0.5 |
| DR-025 | fixed | 长日报回归 | 长文本法务日报 | 长报告丢数量、主体、地点、风险判断，或编造明日计划 | 长文本抽取和锚点 fallback 不稳 | `test_standard_legal_long_report_enters_confirmation_and_preserves_key_facts`、`test_anchor_fallback_does_not_invent_tomorrow_plan_without_anchor` |
| DR-026 | fixed | 回归测试 | 同一句多字段 | 同一句被写入多个字段，或问题/计划互相污染 | 语义关系和槽位判定不稳 | `test_same_sentence_is_not_written_to_multiple_fields`、`test_duplicate_relation_does_not_cross_fill_missing_slot` |
| DR-027 | fixed | 9 点规则回归 | 昨日报补交/修改 | 9 点后查询昨天日报被拦，或 9 点前补昨天日报没有进入昨天 | cutoff 规则区分查询/修改/填写不够细 | `test_after_cutoff_allows_previous_report_display_request`、`test_yesterday_content_before_nine_targets_previous_report` |
| DR-028 | fixed | 回归测试 | 当天 9 点后 | 今天 9 点后继续填今天日报被误拦 | 昨日截止规则波及当天 | `test_same_report_day_after_nine_still_allows_report_content` |
| DR-029 | fixed | 并发测试 | 钉钉重复消息/并发 | 同用户并发碎片、重复 stream message 导致重复写或串用户 | 锁和幂等不足 | `test_duplicate_stream_message_id_is_processed_once`、`test_same_user_fragmented_messages_are_serialized_and_merged` |
| DR-030 | fixed | 并发测试 | 70 用户模拟 | 多用户并发可能串数据 | 用户锁和 upsert 冲突处理不足 | `test_70_different_users_submit_concurrently_without_cross_user_leak` |
| DR-031 | fixed | 手工接口回归 | 手工日报 API | idempotency payload 中日期不可 JSON 序列化，重复请求处理不稳 | date 序列化和幂等返回不足 | `test_manual_report_idempotency_payload_serializes_report_date` |
| DR-032 | fixed | 管理后台测试 | 管理后台 | admin/learning 路由缺少默认保护 | 鉴权边界不足 | `test_admin_learning_rejects_wrong_token`、`test_health_is_not_protected_by_admin_auth` |
| DR-033 | fixed | 调度测试 | 提醒/自动提交 | 提醒重复、确认提醒漏发、真实发送缺少白名单保护 | scheduler 状态和安全阈值不足 | `test_confirmation_reminder_sent_once_per_day_skips_second_reminder`、`test_reminder_without_test_user_ids_blocks_real_send` |
| DR-034 | fixed | progress handoff | progress outbox | outbox 写失败可能影响日报主链路，worker 重复处理或 stale lock 卡住 | 异步隔离和幂等不足 | `tests/test_progress_outbox.py` |
| DR-035 | watching | shadow memory 审计 | 用户习惯学习 | 有候选习惯，但 active habit 为 0，学习结果未实际影响用户 | 缺少审核/激活闭环 | `tests/test_shadow_memory_events.py`、`tests/test_user_habits_context.py` |
| DR-036 | watching | 2026-06-22 审计 | 完成漏斗 | 12 活跃用户中只有 2 人完成，草稿/待确认未及时闭环 | 交互摩擦、提醒和确认漏斗不足 | 已有风险引擎/晨报标记；仍需产品观察 |
| DR-037 | fixed | 2026-06-26 对话 | 历史日报删除 | “把昨天日报删了”可能被二次解析成其他日期或普通修改 | 历史删除意图没有专门拦截 | 直接返回历史日报不可删除，可整篇复制为今天草稿 |
| DR-038 | fixed | 回归测试 | 他人日报查看 | 查询他人日报后短句“复制/这个”不能写入当前用户日报 | 上下文 owner/can_copy 权限缺失 | recent report context 带 owner/can_copy_to_today |
| DR-044 | fixed | 2026-07-04 灰测 | Agent2 案件/出差候选 | 用户进入案件进展或出差候选后，机器人回复没有反馈感，不知道是否已进入候选 | sandbox 候选只进审计层，stream 回复没有展示；纯 sidecar 动作没有被 action-first 路由承认为工作流归属 | 增加纯 sidecar 路由、短案名识别、候选反馈拼接；线上 `online_candidate_feedback_smoke_20260704` 3/3 通过 |
| DR-045 | fixed | 2026-07-04 灰测 | Agent2 出差/案件候选抽取 | “明天出差三亚沟通案件”漏出差目的地且误把“出差三亚沟通案件”当案名；“明天出差三亚办理海花岛案件开庭”漏出差候选；“现在是agent2么”回复生硬 | 目的地正则贪婪吞掉动作后缀；action-intake 缺少候选案名有效性校验；机器人状态问题没有进入 no-write 反馈路径 | 目的地抽取改为非贪婪并加入“办理”等边界；泛称案件不进案件候选；Agent2/灰测状态问题走 small_talk no-write；待线上 smoke |
| DR-046 | fixed | 2026-07-05 灰测 | Agent2 短句/闲聊入口 | “咋说”“怎么闲聊”在活跃日报上下文中可能被 legacy 当作日报正文，或进入错误 QA 路径 | 第一层缺少元对话保护；manual 测试入口未走 agent2 gate 时会绕回旧日报写入 | `app/workflows/action_intake.py` 元对话 no-write；`app/api/reports.py` manual agent2 bridge；专门线上 smoke `meta-0/meta-1` 通过 |
| DR-047 | fixed | 2026-07-05 灰测 | Agent2 相对星期日期 | 周日说“周一预计出差去兰州”应进明日计划+出差候选；“周二应该出差去西宁”“下周五去南京中院沟通石山案进展”不应写进今日/明日计划 | 相对星期没有统一解析；future weekday 被“无日期工作片段”和宽泛日报上下文偷渡进日报 | 新增 `app/workflows/relative_dates.py`，路由/动作/coordination 共享；目标测试 277/277；专门线上 smoke `sunday-monday-tomorrow-plan`、`sunday-tuesday-travel-only` 通过 |
| DR-048 | fixed | 2026-07-05 灰测 | Agent2 复制昨天工作 | “还是昨天那些事儿”“今天的工作和昨天一样”可能被旧链路解释为“昨天计划已完成”，导致写入昨天明日计划展开项，而不是复制昨天今日工作 | copy_previous 语义在 agent2 命令层已正确，但 `/reports/manual` 旧入口绕过 agent2 executor；语音/口语变体覆盖不足 | `tests/test_agent2_daily_commands.py` 增加口语变体；manual agent2 direct executor；专门线上 smoke `same_things/same_work` 均复制昨天 today_work |
| DR-049 | fixed | 2026-07-05 线上 smoke | 手工测试入口 | `/reports/manual` 与钉钉 stream 的 agent2 执行链不一致，导致本地/路由测试通过但线上 manual smoke 仍走 legacy 写错 | manual endpoint 直接调用 `DailyReportService.submit_text`，没有先执行 agent2 gate/direct commands | `app/api/reports.py` 增加 agent2 前置层：灰测用户或 `source` 含 agent2 时先 gate/execute，必要时才 fallback；线上 system smoke 7/7、issue ledger smoke 104/104 通过 |
| DR-050 | fixed | 2026-07-06 灰测 | Agent2 元测试/案件数量查询 | “让我测试下”被写入今日工作；“王喜被告案件有多少”未进入案件 RAG 问答，反而展示“案件进展候选” | 第一层把无业务对象的“测试”漏到 active daily context；案件数量/检索问句未被 internal QA 接管，specific matter hint 又把“王喜被告案件”抽成进展候选；案件 RAG 只有 topN 检索，缺少负责人+原告/被告数量统计 evidence | `test_action_intake_treats_meta_test_probe_as_chat_even_with_active_daily`、`test_agent2_plan_routes_case_count_question_to_qa_not_case_progress`、`test_case_table_rag_adapter_answers_assignee_case_count`；线上专项 smoke PASS：`让我测试下` -> chat/no-write，`王喜被告案件有多少` -> internal_qa/no candidate，RAG count=56 |
| DR-051 | fixed | 2026-07-06 灰测截图 | 案件 RAG 多轮追问/未结案统计 | “何玉成被告案件有多少”后追问“目前未结案的有几件”回答 0；“目前全部案件中未结案的有几件”也回答 0 | 案件 RAG 数量 evidence 只支持负责人+原告/被告总数，缺少未结案过滤、全部案件统计，以及 stream 层把最近案件问答传给 RAG 的上下文；“目前未结案”还可能被误抽成负责人名 | `test_case_table_rag_adapter_counts_unclosed_by_assignee_and_context`、`test_agent2_stream_wires_recent_case_messages_into_knowledge_query`；线上专项 smoke PASS：何玉成被告 42 件、未结案 4 件；全部案件 6155 件、未结案 2826 件；stream 追问继承上下文 count=4；真实 LLM 回复层 PASS：回复含“未结案案件数量为4件”且未走 fallback |
| DR-052 | fixed | 2026-07-06 灰测反馈 | 案件 RAG 小数量统计展示 | “未结案 4 件”只回复数量，没有告诉用户具体是哪 4 件 | case_table_rag evidence 虽有 sample_case_names，但回复层只把 evidence 交给 LLM，缺少“小数量统计必须展开清单”的协议和兜底后处理；底层样例也固定取 5 条，无法保证小数量统计列全 | `test_tool_reply_lists_small_case_count_names_even_when_llm_omits_them`；case_count <= 10 时 evidence 尽量携带全部案件名；回复层新增 sample_case_names 协议和 deterministic append；线上专项 smoke PASS：何玉成被告未结案 4 件全部列明案件名称 |
| DR-053 | fixed | 2026-07-06 灰测截图 | 案件指标请求误进日报/范围误判 | “总体被告存量发我”曾被活跃日报上下文抢成当前日报查询，或在 RAG 层把“发我”误当成人名范围，导致不能返回全量被告存量 | 第一层把 `发我/看下` 等请求动词过早归到日报当前查询；案件 RAG 只识别“全部/所有”等全量词，未识别“总体/整体/总量”，且请求动词未从负责人抽取中剥离 | 已修：新增案件指标数据请求优先入口；RAG 支持总体/整体/总量等全量范围，剥离发我/给我/看下/统计下等请求尾巴；回归：`test_action_intake_routes_defendant_metric_requests_to_internal_qa_not_daily_current`、`test_case_table_rag_counts_total_defendant_metrics_with_request_verbs` |
| DR-054 | fixed | 2026-07-06 灰测截图 | 案件季度指标数据请求 | “发我被告二季度新增同比数据”被按 2026-06 单月口径回答 0 件，且 RAG 把“二季度数”误当成负责人/范围片段 | 案件指标 RAG 默认按月统计，缺少自然季度 period spec；负责人抽取没有先剥离“季度/月度/同比/数据”等时间和指标词 | 已修：被告指标统计支持自然季度 period_start/period_end，季度词不参与负责人抽取；回归：`test_case_table_rag_counts_defendant_second_quarter_new_yoy_without_treating_period_as_person`；线上专项 smoke：`2026-Q2` 新增 88 件、去年同期 133 件、同比下降 33.83%，不写日报；线上 system 7/7、issue ledger 104/104、product quick 140/140、progress 12/12 |
| DR-055 | fixed | 2026-07-06 灰测截图 | 历史日报复用省略句 | “今天和前天一样”被原文写入今日工作，而不是复用前天日报内容 | “沿用历史工作”协议只覆盖昨天，执行层复制源日期固定为昨天；前天/前日/大前天没有贯穿第一层、命令层和执行层 | 已修：历史复用 marker 扩展到前天/前日/大前天，复制执行按 command target_date 取源日报；回归：`test_active_daily_repeat_previous_work_compiles_to_today_work_copy_command`、`test_previous_report_for_copy_uses_command_relative_date`；线上专项 smoke：复制 2026-07-04 今日工作，不写“今天和前天一样”，也不误取 2026-07-05；线上 system 7/7、issue ledger 104/104、product quick 140/140、progress 12/12 |
| DR-056 | watching | 2026-07-06 用户追问 | 活跃日报上下文兜底误写 | 用户担心“奇怪的话还会不会被计入日报”；服务器证据显示 `这个机器人有点傻`、`今天太累了` 在 action-first 无写动作时仍会被 daily context 兜底成 `fill(today_work)` | 第一层已能识别很多 no-write，但 `legacy_daily_context_action` 到 `DailyCommand(fill)` 缺少写入资格门；只要 plan.primary_workflow 被 active daily context 抢到，仍可能绕过 action-first 的无写判断 | 已修待线上验证：新增 legacy daily write eligibility gate，只放行明确日报操作、具体编辑、业务动作+业务对象、问题证据；回归：`test_active_daily_context_write_eligibility_blocks_unstructured_feedback`、`test_active_daily_context_write_eligibility_keeps_structured_daily_actions` |
| DR-057 | fixed | 2026-07-07 08:00 线上日志/DB | 补交昨日草稿的查看和编辑 | 8 点前活跃草稿为 2026-07-06，但“发我看下/今日工作第四条删掉/昨天第四条删掉”可能按 2026-07-07 查改，回复“没有找到对应编号” | Agent2 active daily task 已有 `metadata.report_date`，但 effect/command/executor 没有贯通；执行器只让 `query_history/clear/revoke` 消费相对日期，`edit/fill` 忽略历史日期 | 已修：`WorkflowEffect.target.report_date` -> `DailyCommand.active_report_date` -> executor 目标日报选择；显式 `yesterday` 的 edit/fill 也能定位历史日报；回归：`test_active_daily_current_query_carries_active_report_date`、`test_active_daily_today_work_edit_carries_active_report_date`、`test_active_daily_yesterday_item_edit_keeps_explicit_history_date`、`test_report_date_uses_active_report_date_for_contextual_edit` |
| DR-058 | fixed | 2026-07-08 V4-PRO smoke | LLM 评测期望与产品语义不一致 | 生成器把“好，写日报了”“我刚才说的保利案，明天是不是要交报告？”“后天去保利案件开庭但明天穿啥出门吃饭睡觉”等标成应直接写日报 | 离线用例生成器缺少产品语义归一化，导致测试会奖励错误写库；非明日未来安排、空壳日报启动、业务问句、复用历史省略句需要按产品协议重新标注 | 已修：`scripts/generate_agent2_llm_dialogues.py` 增加 bare daily start、业务问句、非明日未来安排、生活闲聊、历史复用等归一化；回归：`test_llm_dialogue_generator_normalizes_v4_boundary_expectations`、`test_llm_dialogue_generator_aligns_case_candidate_and_tomorrow_trip_semantics` |
| DR-059 | fixed | 2026-07-08 V4-PRO smoke | 风险句被逗号拆碎后反问 | “那个庭可能要延期，法官临时有事，风险”被拆成“那个庭可能要延期 / 法官临时有事 / 风险”，最后只剩短词“风险”，系统变成澄清而不是写入问题/风险 | 分句器只看逗号片段，没有在拆句前保留整句的问题证据；同时“没风险”未作为无风险证据，容易误进问题字段 | 已修：风险句在非编辑场景下整体保留；`problem_evidence` 增加“没风险/没有风险”；回归：`test_action_intake_handles_round8_context_and_service_boundaries`、`test_daily_command_handles_round8_context_and_service_boundaries`、`test_action_intake_accepts_meeting_work_with_no_risk` |
| DR-060 | fixed | 2026-07-08 宽回归 | 案件指标数据请求被活跃日报抢成当前日报查询 | “发我被告二季度新增同比数据”在活跃日报上下文中被 `发我` 触发为 `query_current`，虽然不写库，但体验上像“查日报”而不是案件 RAG | 当前日报查询把 `发我/看下` 作为宽泛触发词；案件指标请求又因“发”被日报写入判断提前挡掉 | 已修：案件/被告/原告指标数据请求优先识别为 internal_qa，日报当前查询排除案件指标对象；回归：`test_generated_non_daily_queries_and_chatter_do_not_write_active_daily`、`test_action_intake_routes_defendant_metric_requests_to_internal_qa_not_daily_current` |
| DR-061 | fixed | 2026-07-08 V4-PRO smoke | 拒写、生活状态、历史写入、业务问题边界 | “日报明天再说”“今天日报先不写”被写入明日计划/今日工作；“今天热死了，不想动”被写为今日工作；“把今天的工作加到昨天日报里”被原文写入；“还有个问题，客户说合同金额不对”被当成机器人反馈；“明天去南京见客户”生成器误标为不应写 | 活跃日报上下文仍会把“日报/今天/明天”表面词当写入证据；assistant feedback 识别没有避开业务问题；生成器对明日外出工作语义偏保守 | 已修：拒写日报话术和生活状态进入 no-write；历史日报“加到/追加”进入历史上下文而非今日正文；业务问题优先于机器人反馈；生成器承认明日客户拜访为明日计划；回归：`test_action_intake_handles_round9_llm_smoke_boundaries`、`test_daily_command_handles_round9_llm_smoke_boundaries`、`test_llm_dialogue_generator_normalizes_round9_smoke_boundaries` |

## 今日新增问题记录：2026-06-26

| 编号 | 用户 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-06-26-001 | 陆健 | “昨天除了第6项没有做之外，其他正常完成。今日工作计划同昨日计划的1~5项。”输出编号占位 | fixed | `previous_plan_range_reference_expands_items_and_drops_placeholders` |
| 2026-06-26-002 | 庞浩 | “把6月24日日报整篇复制到今天”需要直接复制到今天，不受 9 点历史修改限制 | fixed | `direct_copy_day_before_yesterday_report_to_today` |
| 2026-06-26-003 | 庞浩 | 整篇复制后没有编号，内容挤成一行 | fixed | `whole_copy_splits_space_separated_source_sections` |
| 2026-06-26-004 | 庞浩 | “复制昨天的日报”进入旧路径，机器人问“全部覆盖/选择性复制” | fixed | `direct_copy_yesterday_report_to_today_after_cutoff` |
| 2026-06-26-005 | 庞浩 | 旧路径问完后用户回“全部覆盖”，机器人又说“不理解” | fixed | `recent_history_query_cover_reply_whole_copies_context_report` |

## 真实模拟测试：2026-06-26

| 项目 | 结果 |
| --- | --- |
| 变体生成 | 38 个问题条目，每个 3 个近义交互，共 114 条；多轮问题按多轮消息生成 |
| 执行方式 | 90 条走真实 `DailyReportService` 服务路径；24 条属于并发、后台、调度、outbox、shadow memory、完成漏斗等非单轮对话类问题，登记为 skip |
| 执行命令 | `PYTHONPATH=. venv/bin/python scripts/run_issue_ledger_simulation.py --dump-cases outputs/issue_ledger_simulation_cases_latest.json --output outputs/issue_ledger_simulation_latest.json` |
| 执行结果 | pass 90，fail 0，skip 24，真实 LLM 调用 47 次，耗时 105.2 秒 |
| 产物 | `scripts/run_issue_ledger_simulation.py`；`outputs/issue_ledger_simulation_cases_latest.json`；`outputs/issue_ledger_simulation_latest.json`；`outputs/issue_ledger_failure_root_cause_2026-06-26.md` |

有效失败项：无。24 条 `skip` 为非单轮日报服务路径问题，已保留在台账中用于人工/专项验证。

## 线上 Smoke：2026-06-26

| 项目 | 结果 |
| --- | --- |
| 线上对话问题 smoke | pass 90，fail 0，total 90；真实打生产机本地 API `http://127.0.0.1:8000/reports/manual`，真实 DB，隔离测试用户前缀 `__issue_smoke__`，产物 `outputs/issue_ledger_online_smoke_latest.json` |
| 线上系统 smoke | pass 7，fail 0，total 7；覆盖 `/health`、admin 学习页鉴权、手工接口幂等、同用户并发、多用户隔离、整篇复制编号预览、昨日计划编号展开、当前草稿短句查询；测试用户前缀 `__system_smoke__`，产物 `outputs/online_system_smoke_latest.json` |
| 产品 quick gate | pass 131，fail 0；命令 `PYTHONPATH=. venv/bin/python scripts/run_product_gate.py --quick` |
| progress outbox gate | pass 12，fail 0；命令 `PYTHONPATH=. venv/bin/python scripts/run_progress_gate.py` |
| shadow/admin/progress 补充专项 | pass 47，fail 0；覆盖 `test_shadow_memory_events.py`、`test_user_habits_context.py`、`test_admin_page.py`、`test_progress_outbox.py` |
| 非线上 legacy 单测红点 | `tests/test_concurrency_safety.py tests/test_manual_reports_api.py tests/test_scheduler_jobs.py tests/test_report_risk_and_briefings.py -q` 当前 30 passed、6 failed；这 6 个未声明为已修复，登记在 `GAP-001` |

本次线上 smoke 过程中暴露并修复的失败原因：

| 失败项 | 失败原因 | 修复 |
| --- | --- | --- |
| `DR-013-01` | `2到3合并` 已有确定性解析器，但 `direct_range_merge` 没有进入 LLM 前置直达白名单，偶发依赖 LLM 后返回“不理解稳妥” | 将 `direct_range_merge` 加入 decision router 前置 direct 分支 |
| `DR-007-02` | 已选择“风险那块”后，第三句普通内容仍被 pending 状态继续追问，没有按已选栏目写入 | pending append content 状态下，对普通内容直接生成 `append_items` 写入已选栏目 |
| `DR-012-02` | “2、3这两条合成一条吧”里的“吧”被误识别为合并后的替换文本 | 合并替换文本过滤语气词，只有真实替换内容才覆盖原条目拼接 |
| `DR-003-01` | “把当前草稿清掉”能清空但走通用更新文案，没有明确提示清空 | 全局清空判断增加对象明确的“当前草稿/日报/复盘 + 清掉/清除/删除”结构匹配，走服务层清空文案 |

## 后续台账动作

1. 把新增问题登记动作加入每次修复流程：先红用例，再修复，再在本文件补一行。
2. 对 `GAP-001` 单独做一次测试迁移/真实失败分类，避免旧测试长期污染验收信号。
3. 对 `GAP-003` 做抽取粒度专项，目标是减少用户后续“合并/不要拆这么碎”的纠正。
4. 定期从 `report_interaction_events` 和线上日志中抽取 `agent_no_change`、`ask_clarification`、用户负反馈，补进台账。

## 2026-06-27 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-06-27-001 | 历史查询/整篇复制 | 线上 smoke 发现显式 `report_date=2026-06-26` 时，“昨天日报发我看下”按服务器今天 2026-06-27 推成 2026-06-26，而不是按填报日推成 2026-06-25；查询后“覆盖/整篇复制”也继承错日期 | fixed | `test_history_query_uses_explicit_report_date_as_relative_anchor`；线上 `DR-015/DR-021/DR-027` |
| 2026-06-27-002 | 陆健/当前日报模板 | 粘贴机器人模板时，“当前填报日期：2026-06-26”的当前日报曾被当成参考日报；但“当前填报日期：2026-06-25”的历史模板又不能误写入今天 | fixed | `test_current_dated_template_matching_report_date_replaces_today`、`test_pasted_dated_report_template_is_reference_not_current` |
| 2026-06-27-003 | 张圆圆/确认提交 | 草稿界面显示“问题/风险：暂无”，但内部仍标记 problems 未填；用户回复“确认提交”后被要求继续补问题/风险 | fixed | `test_confirm_submit_defaults_missing_problem_to_no_problem` |

## 2026-06-28 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-06-28-001 | 2026-06-25 记录/剩余缺项 | 用户已有今日工作后说“其他没有了/其他的没有了”，系统没有把剩余问题/风险、明日计划按暂无处理，后续确认仍要求补缺项 | fixed | `test_no_remaining_content_fills_missing_sections_as_empty`；线上 `targeted_triage_20260625_27_fix_smoke` |
| 2026-06-28-002 | 2026-06-27 周六记录/业务日历 | 周六/周日不要求写日报，但手工消息仍可按周末自然日创建日报；周六 09:00 前也没有明确归属到周五补填窗口 | fixed | `test_weekend_default_report_date_uses_friday_only_before_saturday_cutoff`、`test_non_reporting_day_does_not_create_current_report`；线上 `targeted_triage_20260625_27_fix_smoke` |

## 2026-06-29 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-06-29-001 | 刘文娟/语音填报 ASR | 22:30:13 语音消息无可用识别文本时，服务调用 `https://api.dingtalk.com/v1.0/robot/audio/asr` 返回 404，日志记录 `error=asr_failed`、`status=voice_transcribe_failed`；机器人发出失败提示，用户约 30 秒后重发并最终完成提交 | fixed-deployed | 原修复只证明单条生产数据库隔离留痕，后续仍发现不同入口处理不一致，复发情况并入 `2026-08-16-002`。现已随 `8c008ee6` 统一上线：语音缺 downloadCode、ASR 异常或识别为空时先保存失败事件，再给出与事实一致的提示；没有恢复 Agent1。验证边界与本次上线结果见 `2026-08-16-002`。 |
| 2026-06-29-002 | 张圆圆/明日计划拆分 | “周日计划分两条”“明日计划分两条”“对的”“对的”连续进入 `ask_clarification`，确认语未执行拆分，23:00 自动提交，完整度 `0.6667`，未用户确认 | fixed | 已修：`awaiting_clarification/split_or_append` 下肯定回复直接执行 `split_suggestion`，并归一 `对的`；回归：`test_pending_split_or_append_confirmation_executes_suggestion` 与线上 `DR-039-01` 通过 |
| 2026-06-29-003 | 刘文娟/局部纠错删除括号 | “上海记载是对的，括号的内容删掉。第二个就是上实实是实在的实”被前置规则误判为全局 `clear_all` 确认，回复清空当前日报确认；后续用户继续纠错才恢复 | fixed | 已修：全局清空必须指向整份日报/当前草稿，局部“内容删掉”不再触发；序号删除也要求删除词与序号在同一短句绑定；回归：`test_local_delete_content_phrase_does_not_trigger_global_clear` 与线上 `DR-039-02` 通过 |
| 2026-06-29-004 | 29 号完成漏斗 | 真实用户 21 人中 11 人有 29 号日报；其中刘聪停留 `collecting` 且完整度 `0.0000`，翁亚兰 `pending_confirmation` 未提交，张圆圆/陆玉婷为 `auto_submitted_timeout` 且未补齐问题/计划字段 | watching | 继续观察提醒、缺项补全和确认闭环；本轮已修复张圆圆拆分闭环的已知代码原因，但本项不等同于整体漏斗已修复 |

## 2026-06-30 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-06-30-001 | 庞浩/团队绩效填报 stream 入口 | 庞浩回复绩效模板“未完成原因/下月目标/行动方案”后，`dingtalk-stream` 入口没有优先进入绩效任务，误写入 2026-06-30 日报今日工作 | fixed | 已修：`stream_runner` 在日报服务前优先调用 `PerformanceTaskService.submit_text`；回归：绩效单测 14/14、线上绩效 smoke 1/1、线上系统 smoke 7/7、台账线上 smoke 92/92、product quick 131/131、progress gate 12/12；历史误写数据已修复为绩效待确认，未发送钉钉消息 |
| 2026-06-30-002 | 刘聪/团队绩效预览修改与提交 | 完整绩效预览粘回后，解析器把“周目标/月度目标/年度目标”等展示行误解析进下月目标；“把第7项行动方案改为...”清理不干净；“预览+提交”未触发确认；“第7项重新清空”曾落入日报 | fixed | 已修：绩效字段解析先剥离本月完成情况展示段，目标字段不再裸匹配周/月/年度目标；支持指标行动方案改为、预览提交、指标级清空；回归：绩效单测 18/18、线上绩效 smoke 1/1、线上系统 smoke 7/7、台账线上 smoke 92/92、product quick 131/131、progress gate 12/12；刘聪历史绩效数据已修复，误建日报已删除，未发送钉钉消息 |
| 2026-06-30-003 | 庞浩/朱佳佳绩效月报 Markdown 小标题 | 朱佳佳月报测试中，用户按 `### 1. 指标名` 回复“未完成原因/存在问题、下月目标、行动方案”，绩效候选识别未剥离 Markdown 标题前缀，导致 stream 入口误认为不是绩效回复并写入 2026-06-30 日报明日计划 | fixed | 已修：指标头解析前剥离 Markdown 标题/引用/列表前缀，保留原指标编号与字段解析链路；回归：`test_markdown_heading_reply_stays_in_performance_context`、线上绩效 smoke 增加 Markdown heading signal；历史误写日报已清理 |
| 2026-06-30-004 | 姚菁华/晨报未报质疑 | 用户反馈“姚菁华说 6 月 29 日已写，但 6 月 30 日晨报显示未报”；生产库核查显示 6 月 29 日无姚菁华 `daily_reports`、`webhook_events`、`report_interaction_events`，仅有 6 月 30 日 17:22 的当日填报和确认 | watching | 暂未发现晨报统计漏算；6 月 29 日全库有 11 份日报、9 份 completed，姚菁华不在其中。若用户能提供 6 月 29 日发送截图/消息时间，再按原始消息链路继续追查 |

## 2026-07-01 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-01-001 | 陆健/昨日计划复用同句带问题 | “今天完成了昨天的计划，然后明天的计划照着昨天的计划来。今日苏建院借章流程...”曾把问题内容误带入明日计划，后续靠用户撤销和复制昨天才纠正 | fixed | 已修：前置识别“全部昨日计划完成”，同句/后句问题单独切入问题栏，“明天照昨日计划”复用昨日计划到明日计划；回归：`test_previous_plan_completion_reuses_plan_for_tomorrow_and_extracts_problem`、线上台账 `DR-040` |
| 2026-07-01-002 | 庞浩/昨日计划全部完成带入明日壳 | “昨天的计划全部完成”在昨日计划含“明天继续/明天开始”时，今日工作生成“完成明天继续...”这类脏句 | fixed | 已修：昨日明日计划转今日完成事项前剥离“明天/明日/继续/开始/做”等计划语气壳；回归：`test_previous_plan_completion_strips_future_plan_shell`、线上台账 `DR-041` |
| 2026-07-01-003 | 庞浩/月报测试模板串到旧绩效任务 | 用户复制综合管理部空白绩效模板测试时，名下仍有朱佳佳测试任务和法务二部测试任务，旧逻辑只取最新活跃任务并用通用字段词命中，导致空模板误写入朱佳佳第1项 | fixed | 已修：绩效回复候选增加“编号+指标名必须匹配当前任务”的保护；同一用户多个活跃绩效任务时按回复内容逐个匹配，不再只取最新任务；空绩效模板未匹配任务时不写日报；朱佳佳发送策略改为只发填报模板不发概览；线上恢复误写片段，相关单测 38/38、线上绩效 smoke 1/1、部门月报 smoke 1/1、定向线上 smoke 通过 |
## 2026-07-02 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-02-001 | 陆健/补交昨天日报 | 2026-07-02 08:39 用户说“昨天的复盘补一下...”，系统创建/更新了 2026-07-02 日报，且结构化字段为空，原文只落入 raw_input；2026-07-01 仍无陆健日报。 | fixed | 已修：显式“昨天/补一下/复盘/日报”优先按历史补交处理，不再被“昨日计划完成”规则抢走；9 点前落到 2026-07-01 并进入结构化抽取。回归：`test_previous_report_backfill_wording_targets_previous_before_cutoff`、`test_previous_report_backfill_before_nine_uses_previous_date`、线上系统 smoke 7/7 |
| 2026-07-02-002 | 曹俊/团队月报语音填报与日报串线 | 曹俊 2026-07-01 22:42-22:54 的月报/绩效语音内容同时污染 2026-07-01 日报 problems/plans；月报任务只识别 2/7 个指标，且第 1 项 next_target 被后续第 2 项 1000 万元覆盖。 | fixed | 已修：绩效候选优先于日报；一次性自然语言多指标按编号/指标名/字段锚点切段；完整预览回放不再二次解析污染“下月目标”。回归：`test_voice_like_multi_metric_reply_is_segmented_by_metric_boundaries`、`test_complete_preview_replay_does_not_reparse_completion_lines`、绩效单测 29/29、线上绩效 smoke 1/1 |
| 2026-07-02-003 | 庞浩/月报模板空字段进入日报链路 | 2026-07-01 17:32、17:33 复制“未完成原因/存在问题/下月目标/行动方案”空模板时，日报链路追问“作为今日工作/问题/明日计划？”，并把模板片段保留在 2026-07-01 日报 raw_input。 | fixed | 已修：绩效回复候选要求编号/指标名与当前任务匹配；别的团队空模板只识别为绩效形状，不写入错误任务，也不落入日报正文。回归：`test_candidate_router_rejects_other_task_metric_template`、`test_bracketed_plain_text_reply_template_is_performance_shaped`、绩效单测 29/29 |
| 2026-07-02-004 | 曹扬眉/粘贴完整日报草稿未结构化 | 2026-07-01 20:48 用户粘贴“当前日报草稿：今日工作/问题/明日计划...”完整文本，系统 status=collecting、score=0，today/problems/plans 均为空，原文只落入 raw_input。 | fixed | 已修：`当前日报草稿/当前复盘草稿/当前日志草稿` 等标题纳入完整当前日报识别；“暂无/无/没有”问题栏归一为“暂无明显问题”。回归：`test_current_report_draft_paste_is_structured_as_today`、产品 quick gate 134/134 |
| 2026-07-02-005 | 早 9 点前普通写日报体验 | 工作日 09:00 前用户说“写日报”或直接粘贴日报模板，实际业务含义通常是补交上一填报日，但旧逻辑工作日仍默认写当天，必须靠用户显式说“昨天/补交”才不误写。 | fixed | 已修：默认日期锚点和实际写入日期拆分；工作日 09:00 前未明确“今天”的日报输入自动写入上一填报日，周一写入上周五；“写今天的日报/按今天/就是今天/不是昨天”等元指令才写当天。回归：`test_weekday_before_nine_bare_report_defaults_to_previous_reporting_date`、`test_weekday_before_nine_explicit_today_report_uses_calendar_date`、`test_monday_before_nine_bare_report_defaults_to_friday`；服务器日期归属测试 19/19、线上系统 smoke 7/7、product quick 137/137、progress gate 12/12 |
| 2026-07-02-006 | 庞浩/Agent2 灰测短反馈与查询草稿 | 2026-07-02 21:15-21:16 灰测中，用户发“啥玩意”被旧日报链路写入今日工作；随后“发我”被 Agent2 当成 `fill(today_work)` 继续写入，7 月 2 日草稿出现“啥玩意”“发我”两条垃圾内容。 | fixed | 已修：Agent2 灰测用户入口改为 `protective_gate`，unknown/短反馈不再落回旧日报链路；`发我/发我下/给我看/看看` 等裸查询编译为 `query_current`，明确日期/历史词才走 `query_history`；旧日报链路增加“啥玩意/什么鬼/这是什么”等短反馈 no-op，并把裸“发我”归为当前草稿查询而非历史 follow-up。回归：`test_active_daily_bare_display_request_compiles_to_query_current`、`test_dated_display_request_still_compiles_to_query_history`、`test_daily_shadow_blocks_short_feedback_with_monthly_and_daily_active_tasks`、`test_short_repair_feedback_is_not_written_as_today_work`、`test_bare_send_me_current_report_is_read_only`；服务器定向 26/26，通过；线上 system smoke 7/7、问题台账 smoke 104/104、Agent2 执行回放 31/64 0 mismatch、历史 60 天回放 168/1375 0 mismatch。 |
## 2026-07-03 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-03-001 | 庞浩/Agent2 方向性灰测 | 活跃日报上下文仍可能先猜“写日报”，导致反馈、裸查询、法律研究/问答等用户动作被日报工作流抢占；本质问题是入口按 workflow signal 先行，而不是先判断用户要执行的 action。 | fixed | 新增 `app/workflows/action_intake.py`，在 `WorkflowRouter.plan()` 的 active daily 抢上下文前执行 action-first guard；新增 `tests/test_action_intake.py` 与 active daily 法律研究/待澄清回归；golden 增加反馈、显式法律研究、裸法律主题待澄清用例。服务器验证：Agent2 单测 113/113、golden 29/29、多轮执行回放 31/64 0 mismatch 且 gray_ready=True、历史 60 天 168/1375 0 mismatch、线上 system smoke 7/7、issue ledger 104/104、performance smoke 1/1、department performance smoke 1/1。Agent2 仍保持关闭，未给用户开放灰测。 |
| 2026-07-03-002 | 庞浩/Agent2 多轮跨工作流灰测 | 活跃日报上下文下，“恒大破产案今天和法院沟通了执行进展”一类案件进展可能被日报主流程抢占；同时“做日报系统的案件进展与出差协同两个模块”这类产品研发语句可能因含“案件进展/出差协同”被误判为案件或出差协同；“明日计划增加继续跟进案件材料”也可能把普通日报计划误抬成案件协同。 | fixed | 修复 action-first sidecar 合流规则：日报内容可同时生成日报写入和案件/出差沙箱候选，但主 workflow 保持案件/出差协同；同时收紧产品研发和通用案件事项过滤，`系统/模块/功能/工具/平台/协同` 不再被当作出差目的地，`案件进展/案件管理/案件资料/案件材料` 等通用名不再触发案件协同。回归：`test_agent2_action_first_keeps_case_progress_primary_when_daily_context_is_active`、`test_agent2_action_first_keeps_daily_primary_for_generic_case_material_plan`、`test_agent2_plan_does_not_treat_travel_coordination_product_work_as_trip`、`assistant_cross_workflow_20260703` 多轮执行 replay。 |
| 2026-07-03-003 | 庞浩/Agent2 助手化灰测 | Agent2 已能阻止闲聊、内部问答、法律研究误写日报，但用户体验仍停留在“这句我先不写入日报”，没有真正作为部门助手继续聊天或给出问答/法律研究回应。 | fixed | 新增 `app/agent2/assistant_responder.py`，在 `DailyShadowEvaluation` 中产出只读 `assistant_reply`；stream 灰测入口在 gate 阻断日报时优先发送助手回复。覆盖闲聊、印章流程内部问答、优先受偿权法律研究框架；不写日报、不动月报。回归：`tests/test_agent2_assistant_responder.py`、`assistant_reply_20260703` 对话 replay 与执行 replay。 |
| 2026-07-03-004 | 庞浩/Agent2 工具化问答灰测 | 只读助手回复仍是静态模板，法律研究/内部问答没有真正进入可替换的工具执行层，后续接 RAG、案例检索或 web search 时容易再次污染第一层意图识别。 | fixed | 新增 `app/agent2/assistant_tools.py`，把非日报的内部问答/法律研究放到异步工具回复层：先由 action-first/gate 判定“不写日报”，再调用 LLM 生成只读回复；超时、JSON 异常、空回复自动回退静态答复；闲聊不调用 LLM。stream 灰测入口仅在 Agent2 对用户开启时接入该层，当前 `AGENT2_DAILY_ENABLED=false`，不影响线上日报。回归：`tests/test_agent2_assistant_tools.py`、`tests/test_agent2_assistant_responder.py`、全量 Agent2 dialogue/execution replay。 |
| 2026-07-03-005 | 线上 smoke/追加问题风险续写 | `加一条内容 -> 风险那块 -> 供应商资料没有发全` 中，用户已经选定“问题/风险”栏后，第三句实质内容仍被质量追问拦截，导致没有追加到 problems。 | fixed | 在 `ReportAgentExecutor` 前置提升已有 `awaiting_append_content` 状态：字段已选且下一句是实质内容时，直接生成 `append_items` 并清理 pending，不再让低置信或质量追问抢占。回归：`test_existing_append_content_state_promotes_substantive_problem_reply`；线上 issue ledger smoke 104/104。 |
| 2026-07-03-006 | 庞浩/Agent2 单句多意图助手回复 | `今天完成合同审核。顺便问下印章流程是什么？` 这类单句多意图此前只能保证日报片段写入，内部问答/法律研究片段虽然被识别为 segment，但 stream 回复仍可能只有日报成功提示，没有真正“写日报后继续问答”。 | fixed | `assistant_reply` 支持 side reply：当 gate 允许日报写入且同句存在 `internal_qa` 或 `legal_research` segment 时，日报命令照常执行，同时把只读助手回复追加到钉钉回复；闲聊 side segment 不追加，避免污染成功提示。回归：`test_assistant_reply_handles_daily_plus_internal_qa_as_side_reply`、`test_assistant_reply_handles_daily_plus_legal_research_as_side_reply`、`assistant_side_reply_20260703` dialogue/execution replay。 |
| 2026-07-03-007 | 庞浩/Agent2 长链路编辑字段误判 | 多轮链路中，用户先正常写日报、问答、出差、案件进展、法律研究后，再说 `把合同审核改成合同审核及风险条款复核`；执行层因为新内容里含“风险”，把文本替换误定位到问题/风险栏，导致今日工作没有被修改，后续删除、合并继续沿着错误草稿执行。 | fixed | 文本替换时，字段提示只从 `改成/改为/替换成/替换为` 前的来源部分判断，不再从替换后的新内容中取“问题/风险/计划”等栏目词；如果来源部分没有栏目提示，则按全局唯一旧文本替换。回归：`test_apply_edit_replaces_text_without_treating_new_value_as_field_hint`、`assistant_long_context_edit_20260703` daily execution replay 10/10、0 mismatch、0 risk。 |
| 2026-07-03-008 | 庞浩/Agent2 “撤回”省略句语义 | `撤回` 不能按关键词直接当成撤回日报；单独说撤回应结合日报状态与上一步快照，`撤回XX案起诉状` 是业务内容，`我要撤回日报改一下` 是撤回已提交日报后继续编辑，三者动作完全不同。 | fixed | 已修：撤回语义按上下文矩阵处理。显式 `撤回日报/撤回提交/刚提交的日报` 或 completed 状态且无上一步快照的裸 `撤回` => `unsubmit_report`；有 `_previous_draft_snapshot` 的裸 `撤回` => 恢复上一步草稿；`撤回XX案起诉状/撤诉` => 日报业务内容；collecting 状态下无上下文的裸 `撤回` => no_write/澄清，不写入原文。回归：`test_bare_withdraw_*`、`test_withdrawing_lawsuit_document_is_daily_content_not_report_revoke`、`test_future_lawsuit_document_withdrawal_targets_tomorrow_plan`；服务器专项 74/74、Agent2 harness 29/29、线上 system smoke 7/7、线上 issue ledger 104/104、product quick 137/137、progress gate 12/12。 |
| 2026-07-03-009 | 庞浩/Agent2 昨日计划完成协议 | `昨天的明日计划已完成/昨天待办都完成了/昨日安排全部搞定` 在 Agent2 中会被当作普通日报内容写入今日工作，导致写入占位句，而不是展开昨天日报的明日计划条目。 | fixed | 已修：新增 `complete_previous_plan` 日报命令；action-first 在问答识别前识别昨日计划/待办/安排完成；执行层读取上一份日报 `tomorrow_plan`，转换为 `完成xxx` 后合并进今日工作，去除 `明天/继续/开始做` 等未来计划壳并去重。随机 60 组多轮样本已加入该场景。服务器验证：定向 Agent2 测试 113/113、随机多轮回放 60 组 326 轮 0 mismatch/0 risk、Agent2 harness 29/29、历史 60 天回放 168 组 1375 轮 0 mismatch、线上 system smoke 7/7、线上 issue ledger 104/104、product quick 137/137、progress gate 12/12。剩余风险：历史回放仍有 76 个 `fallback_to_legacy`，不能视作最终灰测完成。 |
| 2026-07-03-010 | 庞浩/Agent2 `撤回并修改` 上下文 | 历史真实语料中 `撤回并修改` 会 fallback 到旧日报链路；如果当前日报已提交，它应当是撤回提交进入可编辑状态；如果未提交，不应写入原文或进入旧链路猜测。同时 `撤回并修改XX案起诉状` 仍是业务内容。 | fixed | 已修：`撤回并修改/撤回改一下` 在 completed 日报上下文编译为 `revoke`，collecting 上下文编译为 `no_write`；法律文书场景继续按业务日报内容写入，并保证 `把第二条改成今天完成律师函起草` 这类编号编辑优先于文书工作识别。服务器验证：定向 Agent2 测试 73/73、随机多轮回放 60 组 326 轮 0 mismatch/0 risk、历史 60 天回放 168 组 1375 轮 0 mismatch，`fallback_to_legacy` 由 76 降至 73；Agent2 harness 29/29、线上 system smoke 7/7、线上 issue ledger 104/104、product quick 137/137、progress gate 12/12。剩余风险：历史回放仍有 73 个 `fallback_to_legacy`，不能视作最终灰测完成。 |
| 2026-07-03-011 | 庞浩/Agent2 点号序号编辑 | 历史真实语料中 `把5.去掉`、`明日计划2.3.去掉` 这类点号序号口语表达会 fallback 到旧日报链路；Agent2 只稳定覆盖了 `第5条`、`2、3`、`5到7条` 等形式。 | fixed | 已修：`_loose_item_indices_from_text` 支持单个裸点号序号和点号分隔序号在结构化编辑词附近解析；`把5.去掉` 可按全局序号换算到具体栏目。服务器验证：定向 Agent2 测试 59/59、随机多轮回放 60 组 326 轮 0 mismatch/0 risk、历史 60 天回放 168 组 1375 轮 0 mismatch，`fallback_to_legacy` 由 73 降至 70；Agent2 harness 29/29、线上 system smoke 7/7、线上 issue ledger 104/104、product quick 137/137、progress gate 12/12。剩余风险：历史回放仍有 70 个 `fallback_to_legacy`，不能视作最终灰测完成。 |
| 2026-07-03-012 | 庞浩/Agent2 逗号编号编辑与未解析回落 | 历史真实语料中 `合并今日工作的 2，3，4，5` 会被 action-first 按逗号拆成多段，导致首段被当作普通内容写入，后续编辑又 fallback 到旧日报链路；另有 `删除第4条`、`第七条改成...`、`把第一条和第五条合并` 等引用超出当前草稿范围的编辑，定位失败后不应交给 legacy 继续猜。 | fixed | 已修：`_split_segments` 保留逗号分隔编号和范围引用，不再拆碎结构化编辑句；Agent2 一旦识别为日报编辑，即使编号/文本未定位成功，也返回明确 no-change 提示，不再 fallback legacy。服务器验证：目标单测 86/86、随机多轮回放 60 组 326 轮 0 mismatch/0 risk、历史 60 天回放 168 组 1375 轮 0 mismatch/0 risk 且 `fallback_to_legacy=0`、Agent2 harness 29/29、线上 system smoke 7/7、线上 issue ledger 104/104、product quick 137/137、progress gate 12/12。 |
| 2026-07-03-013 | 庞浩/Agent2 action-first 入口词表化风险 | 用户质疑“是不是又在枚举做 pending，又搞了个 Agent1”；真实 LLM 裁判抽样暴露入口层仍容易围绕短语补丁来挡误写，同时离线回放的活跃日报上下文可能和线上真实入口不一致。 | fixed | 已修：入口继续坚持 action-first，不把 `pending` 当意图分类；写日报必须来自显式日报字段、日报启动、正向工作证据、负向问题证据或同轮字段继承。撤掉“游泳”等具体词黑名单，改为“明日计划/今日工作”等写入锚点优先，`不是，我其实想说...` 这类元纠错不写日报；新增 Agent2 计划驱动的 replay daily context，让历史回放能承接上一轮日报效果；`日志/目前` 纳入只读日报查询；助手反馈、生活闲聊、系统格式反馈仍阻断写日报。验证：本地目标 149/149、服务器目标 149/149；随机多轮 60 组 326 轮 0 mismatch/0 risk；60 天全量历史 179 组 1446 轮 0 mismatch/0 risk；Agent2 harness 29/29；线上 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12；真实 LLM 抽样 240 条 `gate_allow_llm_block=0`，`gate_block_llm_allow=27`。剩余 27 条主要是 LLM 偏宽或无真实上下文的保守拦截（如“明天不活了/测试测试/格式反馈/裸清空”），不通过继续放宽词表刷分处理，后续只在真实上下文包明确时优化体验。 |
| 2026-07-03-014 | 庞浩/Agent2 灰测真实链路 | 活跃日报上下文下，`想说个案件进展`、`我想聊个案子`、裸 `出差`、机器人反馈、法律后果问答被第一层拦截后，`coordination_plan` 又独立抽取 `daily_entry`，绕过 action-first 并写入 2026-07-03 日报。 | fixed | 根因是 `app/agent2/coordination_plan.py` 与 `app/agent2/daily_commands.py` 没有把第一层 workflow 归属作为执行授权。已修复为计划层只能细化第一层允许的 workflow，命令层在无日报授权时拒绝 coordination daily entry。验证：本地目标 174/174；服务器目标 174/174；生产隔离用户真实链路 6 个坏样本无 action/无 command/DB 未变、2 个好样本正常写入；online system smoke 7/7；issue ledger smoke 104/104；product quick 137/137；progress gate 12/12；随机 60 组 326 轮 0 mismatch/0 risk；历史 60 天 175 组 1493 轮 0 mismatch/0 risk；真实 LLM 抽样 240 条 0 error，剩余 2 条 gate 放行分歧为“确认/暂无计划”，不属于本次误写风险。 |

## 2026-07-04 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-04-001 | 庞浩/Agent2 明日出差计划重复 | 灰测中多次表达“明天出差三亚/明天出差三亚沟通案件/明天出差三亚办理海花岛案件开庭”，日报明日计划出现多条同目的地出差计划。 | fixed | 已修：执行层对 `tomorrow_plan` 增加同一天同目的地出差计划的语义去重；后续更具体表述覆盖泛表述，后续泛表述不覆盖具体表述，不同目的地仍保留。回归：`test_tomorrow_trip_plan_replaces_generic_same_destination_with_specific_item`、`test_tomorrow_trip_plan_ignores_generic_same_destination_after_specific_item`、`test_tomorrow_trip_plan_deduplicates_same_destination_within_one_fill_command`、`test_tomorrow_trip_plan_keeps_different_destinations`。服务器验证：执行层 43/43、入口/协调/影子 74/74、线上 system smoke 7/7、线上 issue ledger smoke 104/104、product quick 137/137、progress gate 12/12；生产库隔离用户连续三次写“三亚”相关明日计划，最终只保留最具体的一条。 |
| 2026-07-04-002 | 庞浩/Agent2 昨天内容省略句 | 实际 stream 测试中，`复制昨天日报 -> 昨天的计划都完成了 -> 今天还是做了昨天那些事 -> 还是昨天那些事` 会把后两句原文追加进今日工作，造成垃圾内容和重复膨胀。 | fixed | 已修：命令层新增“沿用昨日工作内容”协议，`今天/还是/照着 + 昨天/昨日 + 那些事/一样/同样内容` 编译为 `copy_previous(today_work)`；执行层支持字段级复制，只合并昨天的今日工作，不覆盖已展开的“昨天计划完成”结果，不写入省略句原文。回归：`test_active_daily_repeat_previous_work_compiles_to_today_work_copy_command`、`test_apply_copy_previous_today_work_merges_without_overwriting_current_items`。线上 stream 隔离实测 5/5 通过，标准线上 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12。 |
| 2026-07-04-003 | 庞浩/Agent2 法律文书撤回动作 | 跑相关回归时发现 `撤回XX案起诉状` 会被 action-first 误判为内部问答，导致没有进入日报业务内容；该句不是撤回日报，而是处理法律文书。 | fixed | 已修：action-first 在内部问答前识别法律文书业务动作，出现起诉状/上诉状/答辩状/律师函等文书且含撤回/撤诉/修改/起草/审核/完成/处理等动作时，作为日报工作内容；`明天撤回XX案起诉状` 归入明日计划。回归：`test_withdrawing_lawsuit_document_is_daily_content_not_report_revoke`、`test_future_lawsuit_document_withdrawal_targets_tomorrow_plan`；服务器相关单测 120/120、协调/影子 25/25 通过。 |

## 2026-07-05 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-05-001 | 庞浩/Agent2 问题风险识别枚举化 | 用户追问“什么样的话会被识别成风险与困难”“卡点词还是在枚举吗”；旧入口把“问题/风险/困难/卡点”和“没反馈/缺失/逾期”等判断散落在 `action_intake`、`coordination_plan` 中，容易继续按关键词补丁退回 Agent1。 | fixed | 已修：新增 `app/workflows/problem_evidence.py`，统一抽取 `ProblemEvidence`：无问题、显式问题、依赖阻滞、质量缺口、进度异常、流程阻滞；`action_intake` 与 `coordination_plan` 改为消费问题证据对象，而不是各自命中词表。补充边界：`业务部门材料一直没反馈`、`客户资料不完整`、`回款节点逾期`、`合同审批流程卡住了` 进问题/风险；`破产债权申报逾期有什么后果？` 走问答；`明天处理资料缺失问题` 进明日计划。验证：本地目标 129/129、服务器目标 129/129、线上重启后 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12、近 7 天 workflow gate 回放 200 条无脚本失败。回放中另发现 3 条短句/错别字式编辑需后续专项处理，未混入本项修复。 |
| 2026-07-05-002 | 刘文娟/陆玉婷/Agent2 短句与错别字编辑 | 近 7 天回放暴露：`明日计划里面的飞速收款飞速写错了，是非的非诉讼的诉` 被 Agent2 当 unknown，不能按当前草稿把“飞速”纠为“非诉”；`括号里面的内容删掉` 曾因含“删掉”升级为清空；`用印，合同归档，月度收款计划，旬计划调整，法务小群案件汇报` 曾被案件进展抢占或只保留末段。 | fixed | 已修：新增 `app/agent2/daily_edit_intent.py` 统一识别口语编辑；action-first 先让带上下文的“写错/括号内容删掉”进入日报编辑，普通短反馈仍不写；命令层把局部删除编译为 `edit` 而非 `clear`；执行层支持唯一定位后的括号内容删除、拼写式纠错（如“非的非/诉讼的诉”=>“非诉”）；泛化 `案件汇报/法务小群案件` 不再生成案件进展；纯工作清单保留完整句。验证：服务器目标测试 147/147，追加清单分割目标 101/101；线上最终 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12；近 7 天真实回放 300 条 `write_review` 由 5 降至 3。剩余 3 条为安全保留：`第一`、`上海鸡仔` 无法唯一定位不写，另 1 条 Markdown 月报回复不应落入日报。 |
| 2026-07-05-003 | 庞浩/Agent2 候选式澄清体验 | 用户指出“你是要改今日工作第几条，还是补充新内容？”这种开放式追问仍像 Agent1，把定位负担丢给用户；短句 `第一`、`上海鸡仔` 这类安全阻断后，回复需要利用当前草稿给出候选，而不是泛泛让用户说明。 | fixed | 已修：新增 `app/agent2/daily_clarification.py`，在 stream blocked reply 中使用当前 `Agent2ContextPack.daily_draft` 生成只读候选回复；`上海鸡仔` 会提示最接近的草稿项如【今日工作2】进行上海机载...，`第一` 会定位当前草稿第 1 项；回复不出现“第几条/你是要”开放式问法，不邀请用户回“确定”，而是让用户直接说具体改法。候选不唯一时不猜。验证：本地/服务器目标 14/14；线上 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12；近 7 天 workflow gate 回放 300 条统计不变，说明未改变写入授权。剩余风险：本轮未做“确认候选后持久化执行”，后续如要支持“对/就这个/确认改”需单独做 pending candidate 状态机。 |
| 2026-07-05-004 | 庞浩/Agent2 候选状态机 | 用户继续指出测试样例太少、候选追问后多轮上下文仍可能丢失：`上海鸡仔` 定位候选后，下一句 `括号删掉/改成.../把里面的XX改成YY` 应该作用于该候选；但 `对/确定/就这个` 不能误提交日报或误改正文。 | fixed | 已修：新增 `_agent2_pending_daily_candidate` 短期焦点状态，stream blocked 候选回复会把候选字段、编号、item_id、原句和相似度写入 `section_status`；Context Pack、active daily pending keys、执行器均消费该状态。候选后支持括号内容删除、整条改写、局部替换；纯“对/嗯/就这个/确定”只回复“已定位但未提交/未修改”，不生成日报命令；任意成功写入后自动清除候选。补充随机生成器边界：`今天和昨天一样` 只视为沿用昨日今日工作，不等同整份日报覆盖。验证：服务器候选/路由/上下文目标单测 254/254；候选相关新增表达覆盖 31 个执行变体、8 个确认阻断变体；随机生成 60 组 326 轮 0 mismatch；生产隔离 DB smoke 验证候选落库、纯确认不提交、候选括号删除成功、候选清除成功；重启后线上 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12。 |
| 2026-07-05-005 | 庞浩/Agent2 状态层收口与真实 LLM 回归 | 候选状态、确认状态、草稿 item id、最后修改焦点散落在 `daily_execution/context_pack/daily_context/stream_runner` 多处，后续容易继续修成 Agent1 式 key 补丁；真实 LLM 回归同时暴露两点：编辑流后裸“确认”会被解释为提交日报，9 点后“昨天审核了合同”会误写入今日工作。 | fixed | 已修：新增 `app/agent2/daily_state.py` 作为日报状态深模块，统一 pending keys、候选焦点、最后修改焦点、草稿 item id 常量和引用解析；迁移 Context Pack、active task、stream 候选落库、执行器、回放和 capability 到统一模块。旧服务补两条结构性门禁：当前编辑流中裸“确认”只保留修改并提示“确认提交”，不提交；`昨天 + 具体工作动词` 按补昨日报截止判断，9 点前归上一填报日，9 点后阻断且不写今日。验证：新增/相关小测 3/3；真实 LLM 日报回归 51/51、LLM 调用 15 次；Agent2 目标 185/185；随机多轮 60 组 326 轮 0 mismatch；py_compile 通过；重启后线上 system smoke 7/7、issue ledger smoke 104/104、product quick 137/137、progress gate 12/12；真实 LLM workflow 裁判 20/20 judged，0 gate mismatch。 |
| 2026-07-05-006 | 庞浩/Agent2 泛案件工作与产品建设边界 | 真实 workflow LLM 抽样暴露：`用印，合同归档，月度收款计划，旬计划调整，拟诉案件录入跟进`、`构建原告案件进展系统/自动关联案件进展` 这类日报工作或产品建设语句会被抬成 `case_progress` 主流程或 sidecar；同时 `哈哈哈` no-write 回复偶发不说明“未写入日报/复盘”，导致体感不清楚。 | fixed | 已修：`workflow intake`、`action_intake`、`coordination_plan` 三层统一收紧案件进展门槛，只有具体案名/案号/法院/开庭/执行进展等 matter hint 才进入案件进展候选；`拟诉案件/待诉案件/案件进展系统/自动关联/固定节点询问/系统模块工具平台` 等泛案件管理或产品建设语义压回日报。`app.agent2.__init__` 改为 lazy export，消除直接 import `WorkflowRouter` 的循环依赖隐患。旧 executor no-write 文案补“这句不写入日报或复盘”。验证：本地目标 145/145；服务器目标 145/145、AgentCore 50/50、产品快测 140/140、真实 LLM 主回归 51/51、语料回放 16/16、progress gate 12/12；线上重启后 system smoke 7/7、issue ledger smoke 104/104、performance smoke 1/1、department performance smoke 1/1；workflow replay 1896 条，workflow LLM 抽样 20/20 judged，`gate_allow_llm_block=0`，剩余 2 条为 `取消/无语` 被 Agent2 保守阻断而 LLM 偏宽放行，未按刷分放宽。 |
| 2026-07-05-007 | 庞浩/Agent2 真实灰测聊天与执行反馈 | 真实 stream 测试中，`可以和我聊聊吗？` 在月报/日报活跃任务存在时被 `active_monthly_task_guard` 压成“请说明要记录日报还是咨询”；`妈的` 也被追问意图；`呃帮我清空` 成功后却回复“已记录到日报”；`今天的工作和昨天一样` 在昨天今日工作为空时只说“没有变更”，用户不知道原因。 | fixed | 已修：action-first 层补足聊天请求和短句情绪的 no-write 识别，`WorkflowRouter.plan/route` 在活跃月报护栏前先进入 `chat`；`_small_talk_signal` 同步增强，避免 active daily/monthly 抢上下文；聊天回复改为可读对话语气但仍不授权写入；执行层对 `clear` 生成“已清空”文案，对 `copy_previous(today_work)` 空源给出“昨天日报的【今日工作】是空的”并提示可说“昨天的计划都完成了”。验证：本地目标 201/201；服务器目标 201/201；线上重启后 product quick 140/140、progress gate 12/12、system smoke 7/7、issue ledger smoke 104/104；专项 HTTP smoke 4/4 覆盖聊天、吐槽、清空、空昨日今日工作复用，DB 未误写。 |

## 2026-07-06 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-06-001 | 庞浩/Agent2 第一层意图与闲聊体感 | 真实灰测中 `明天穿啥出门` 被活跃日报上下文写入 2026-07-06 明日计划；进一步压力测试发现 `日报 + 闲聊/问答` 的单句多意图能识别 segment，但整句汇总层可能丢失日报 effect；同时 chat 回复仍主要是静态模板，体感不像有 LLM 与上下文。 | fixed | 已修：第一层新增生活问题 no-write 负信号，动作层把生活闲聊置于内部问答之前；`action-first` 增加“日报写入 + 只读侧意图”汇总分支，保留日报 effect，chat/internal_qa/legal_research 只作为 side；chat 工具层允许调用 LLM 生成只读回复，但提示词和返回前缀明确“不写入日报”，失败回静态个人记忆回复。回归：新增 `test_agent2_first_layer_stress.py`，覆盖 20 条生活问题、20 条真实日报/协同、5 条单句多意图、1 组长上下文执行回放；本地与服务器 Agent2 核心 196/196 通过。线上验证：重启后 product quick 140/140、progress gate 12/12、online system smoke 7/7、issue ledger smoke 104/104；专项 Agent2 HTTP smoke 覆盖 5 条生活问题不写 DB、真实明日计划写入、`明天去南京开庭` 保留日报明日计划；已清理庞浩 2026-07-06 草稿中的误写正文、raw_input、input_fragments 与 stale item_id。 |
| 2026-07-06-002 | 庞浩/Agent2 闲聊后续、短删除与协同反馈 | 真实灰测中 `陪我聊会` 后续的 `好无聊` 被活跃日报上下文写入今日工作；追问 `好无聊为什么计入了今日工作呢` 时机器人错误归因给用户；随后 `删掉吧` 清空了整份 2026-07-06 草稿；`周三出差三亚处理海花岛案件` 的出差候选反馈显示“时间待确认”。 | fixed | 已修：短句情绪与系统不好用反馈在活跃日报任务下仍优先进入 chat/assistant_feedback，不写日报；短句 `删掉吧/删除吧` 先在 action-first 层归属为日报编辑，不再被 internal_qa 抢占，再编译为 edit；执行层只在存在最近修改焦点时删除该焦点，没有焦点则不猜最后一条、不清空草稿；协同候选日期补 `future_weekday`/`past_weekday` 标签，`周三` 不再显示“时间待确认”。回归：`test_action_intake_treats_short_vent_as_chat_but_keeps_business_problem`、`test_action_intake_routes_short_delete_reference_to_daily_edit_when_daily_is_active`、`test_agent2_plan_keeps_active_daily_vent_and_system_feedback_in_chat`、`test_agent2_plan_routes_active_daily_short_delete_to_daily_edit`、`test_short_delete_reply_compiles_to_edit_not_clear_when_daily_is_active`、`test_apply_short_delete_reply_deletes_last_modified_item_only`、`test_agent2_stream_candidate_feedback_labels_future_weekday_as_known_time`。验证：本地目标 247/247、服务器目标 247/247；强制 Agent2 线上专项 4/4 覆盖闲聊不写、系统反馈不写、短删除只删最近焦点、未来工作日标签；最终线上 system smoke 7/7、issue ledger smoke 104/104、product quick 140/140、progress gate 12/12。 |
| 2026-07-06-003 | 庞浩/Agent2 案件指标数据请求 | 真实灰测中 `总体被告存量发我` 没有稳定进入案件数据答疑：旧链路可能被 `发我/看下` 抢成当前日报查询，RAG 层也未把“总体/整体”识别为全量范围。 | fixed | 已修：第一层新增案件指标数据请求优先入口，不让活跃日报的“当前草稿查询”先抢；案件 RAG 支持 `总体/整体/总量/总数` 等全量范围，并剥离 `发我/给我/看下/统计下` 等请求动词，避免误当负责人。回归：`test_action_intake_routes_defendant_metric_requests_to_internal_qa_not_daily_current`、`test_case_table_rag_counts_total_defendant_metrics_with_request_verbs`。 |
| 2026-07-06-004 | 庞浩/Agent2 案件季度指标请求 | `发我被告二季度新增同比数据` 回复成 2026-06 单月新增 0 件，且“二季度数”疑似被当成范围/负责人。 | fixed | DR-054；线上专项 smoke 返回 Q2 新增 88 件、同比下降 33.83%，且不写日报。 |
| 2026-07-06-005 | 庞浩/Agent2 历史日报复用 | `今天和前天一样` 被写入今日工作原文。 | fixed | DR-055；线上专项 smoke 复制前天今日工作，不写原话，不误取昨天内容。 |
| 2026-07-06-006 | 庞浩/Agent2 日报写入资格 | `这个机器人有点傻`、`今天太累了` 这类无日报结构的话，在活跃日报上下文里仍可能被写入今日工作，且即使被命令层拦住也没有闲聊回复。 | fixed | 已修：第一层补“机器人/系统/你 + 负向体验反馈”结构识别，直接进入 chat；命令层保留写入资格门禁作为兜底；`这个机器人有点傻` 现在无 command、有 chat 回复。回归：`test_assistant_reply_handles_bot_feedback_as_chat_without_daily_write`；服务器 Agent2 核心 259/259、辅助/RAG/记忆 40/40、online system 7/7、issue ledger 104/104、product quick 140/140、progress 12/12、Agent2 专项 API 6/6 通过。 |
| 2026-07-06-007 | 庞浩/Agent2 混合句与月报状态查询 | `今天我吃了小番茄 评审了法务合同` 被整句写入今日工作；`大家月报填的怎样了` 被写入今日工作，说明活跃日报上下文仍在抢只读查询。 | fixed | 已修：生活闲聊补足食物/生活语义；日报命令层对“生活 + 业务”混合句做子句净化，只保留有业务动作和业务对象的内容；月报状态查询新增 `monthly_status_query` 只读动作，不生成日报 effect。回归：`test_active_daily_context_cleans_mixed_lifestyle_and_business_content`、`test_agent2_plan_routes_monthly_status_query_read_only_not_daily_context`、`test_assistant_reply_handles_monthly_status_query_without_daily_write`；服务器相关 176/176、扩展 259/259、辅助/RAG/记忆 40/40、Agent2 专项 API 6/6 通过。 |
| 2026-07-06-008 | 庞浩/Agent2 入口一致性与否定修正 | 真实 LLM 回放暴露：`/reports/manual` 非 Agent2 source 仍可能绕回 legacy；活跃日报中 `明天不是去南京，是去上海开庭` 曾被追加而不是替换；`明日计划改成...` 后回复“确认”没有提交；“刚补的那条删掉”被问澄清。 | fixed | 已修：manual endpoint 默认先进 Agent2 protective gate，仅保留显式 legacy escape hatch；action-first 分句器保留 `不是 X，是 Y` 修正句；旧服务兜底补齐 `direct_negative_replacement`、`direct_not_replace_but_add`、`direct_delete_last_modified_item`，并让确定性编辑动作先于合成编辑 pending 执行；`确认` 可提交完整 pending 草稿。回归：服务器 Agent2/旧编辑状态机 268/268 通过；真实 LLM wild 回放 50/50 通过，LLM calls 14。 |
| 2026-07-06-009 | 庞浩/线上第一层保护门未生效 | 真实钉钉测试中，`大家月报填的怎样了`、`今天我吃了小番茄 评审了法务合同` 等已在 Agent2 测试覆盖的场景仍被旧日报链路写入；表现为本地/专项测试通过，但线上体感继续像 Agent1。 | fixed | 根因不是单条意图规则缺失，而是生产 `.env` 未设置 `WORKFLOW_INTAKE_MODE=protective_gate`，导致第一层只 observe 不拦截；同时 `AGENT2_DAILY_ENABLED=false` 关闭执行层时，用户误以为保护门也已上线。已修：线上开启 `WORKFLOW_INTAKE_MODE=protective_gate`，保持 `AGENT2_DAILY_ENABLED=false`，即“第一层保护门常开，Agent2 写入执行层关闭”。验证：服务器 gate smoke 显示月报查询/测试话/穿衣闲聊 block，混合小番茄句只保留“评审了法务合同”；线上 system smoke 7/7、issue ledger smoke 104/104。 |

## 2026-07-07 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-07-001 | 庞浩/Agent2 长句多意图与时间锚点 | `今天完成了...，明天计划...`、`今天完成合同审核。顺便问下印章流程是什么？` 这类单句多意图在部分链路中会被后半句时间锚点或只读意图影响，导致今日工作缺写、明日计划误归属，或只读问答丢失。 | fixed | 已修：`action_intake` 与 `coordination_plan` 增加 segment frame，显式字段/时间锚点只在本段继承；`assistant_responder` 保留日报写入同时追加 internal_qa/legal_research side reply。验证：本地 Agent2 核心 240/240、执行相关 99/99；服务器同组 240/240、99/99；服务器对话回放 842 组/5562 轮 0 mismatch，执行回放 gray_ready=True。 |
| 2026-07-07-002 | 庞浩/Agent2 非明日出差/案件候选 | `保利案件估计下周要去开庭`、`后天去南京出差` 等非明日计划被活跃日报上下文误写入今日或明日计划。 | fixed | 已修：相对日期识别补 `后天/大后天/未来周几`；日报字段判定遇到非 tomorrow 的 future/past anchor 时不写日报，仅进入出差/案件候选。回归：`test_action_intake_future_trip_stays_out_of_daily_but_creates_candidates`、`test_coordination_plan_future_trip_is_candidate_only_not_daily_plan`、`test_daily_shadow_blocks_day_after_tomorrow_trip_from_legacy_daily_fallback`。 |
| 2026-07-07-003 | 庞浩/Agent2 活跃日报短答“没啥问题” | 多轮日报中第二句 `没啥问题` 应进入问题/风险栏，但命令层把 `啥` 当问句标记，最终 `no_write`，导致问题栏缺写。 | fixed | 已修：`DailyCommand` 写入资格判断中，`ProblemEvidence` 的无问题/问题证据优先于问句/反馈拦截。回归：`test_active_daily_no_problem_reply_with_sha_is_not_treated_as_question`；服务器对话回放 842 组/5562 轮 0 mismatch。 |
| 2026-07-07-004 | 庞浩/Agent2 复制昨天后多轮编辑错位 | `今天和昨天一样` 被正确识别为 `copy_previous(today_work)`，但执行层把昨天今日工作追加到当前今日工作，导致后续 `把第二条改成...`、`把第三条移到问题风险`、`合并第一条和第二条` 全部按错误编号执行。 | fixed | 已修：字段级复制语义改为“若当前栏已包含源内容则不动，否则用源栏替换当前栏”；保留完整整篇复制 `copy_previous(all)` 语义。回归：`test_apply_copy_previous_today_work_replaces_different_current_items`、`test_apply_copy_previous_today_work_merges_without_overwriting_current_items`；执行回放 842 组/5562 轮 0 mismatch、gray_ready=True。 |
| 2026-07-07-005 | 庞浩/Agent2 生活闲聊、测试话、月报查询误写 | `让我测试下`、`今天我吃了小番茄 评审了法务合同`、`大家月报填的怎样了` 等容易被活跃日报上下文吞成今日工作。 | fixed | 已修：meta-test、生活对象、短情绪、月报状态查询继续在第一层归为 chat/internal_qa/monthly_status_query；混合生活+业务内容进入日报前清洗，只保留业务片段。回归：`tests/test_agent2_generated_realistic_regression.py` 覆盖 23 条非日报、22 条清晰日报、混合句和复制昨天场景；线上 issue ledger smoke 104/104。 |
| 2026-07-07-006 | 庞浩/Agent2 评测金标滞后 | 架构把闲聊从 `unknown_or_help/small_talk` 收口为独立 `chat` workflow 后，历史回放仍按旧命名判失败；`今天和昨天一样` 的历史金标仍期望 `target_field=all`，与产品语义不一致。 | fixed | 已迁移本地与服务器 `evals/agent2/dialogues` 金标：chat 仍必须 no-write，但 workflow/reply_type 改为 `chat`；`今天和昨天一样` 改为 `today_work`，`复制昨天日报/把昨天的带过来` 仍为 `all`。验证：本地 161 组/760 轮 0 mismatch；服务器保留更多历史语料，842 组/5562 轮 0 mismatch。 |
| 2026-07-07-007 | 庞浩/Agent2 昨日日报编辑入口 | 钉钉中说 `我要改昨天日报`，系统识别到 2026-07-06 目标日报但编译为 `no_write`，回复“这次没有产生新的日报变更”，既不像人，也没有进入可继续编辑的上下文。 | fixed | 已修：新增 `begin_edit` 只读日报命令；泛泛“我要改/修改/调整某天日报/日志”只打开编辑上下文并提示可直接说栏目/编号/修改内容，不写库；具体“第 N 条删掉/改成...”仍按编辑执行。stream 入口补 `received_at`，命令层增加 09:00 前无显式日期默认昨天的日期锚点。回归：`test_dated_daily_edit_entry_compiles_to_begin_edit_without_writing`、`test_before_nine_bare_daily_edit_entry_defaults_to_yesterday`、`test_before_nine_daily_fill_defaults_report_date_to_yesterday`、`test_apply_begin_edit_is_read_only_and_has_edit_prompt`；服务器目标 230/230；线上 Agent2 专项 smoke 返回 `agent2_read_only`、目标日期 2026-07-06、DB 前后一致；线上 system smoke 7/7、issue ledger smoke 104/104、product quick 140/140、progress gate 12/12；Agent2 对话回放 1010 组/6937 轮 0 mismatch，执行回放 0 mismatch/0 risk/gray_ready=True。 |
| 2026-07-07-008 | 庞浩/Agent2 闲聊与安全阻断回复体感 | `哎`、`哈哦`、`明天吃屎` 等明显闲聊/玩笑/粗口短句虽然没有写库，但回复仍可能走“请说明要记录日报还是咨询/讨论”，或被候选澄清层拿当前草稿做“最接近的是【明日计划1】...”，体感像硬猜日报编辑。 | fixed | 已修：第一层把短叹词、粗口玩笑、生活闲聊统一收成 `chat/no_write`；同轮存在真实日报写入时抑制开头语气词，不影响 `呃，要修改一下，今天工作是...` 这类正常日报；候选澄清层遇到非日报闲聊不再匹配草稿；chat 工具层不再调用 LLM，只返回本地轮换的自然短回执，统一明确“不写入日报”并把用户带回日报/月报/案件/出差/查询动作。回归：`test_action_context_treats_non_business_short_chatter_as_chat`、`test_action_intake_treats_short_chatter_and_vulgar_jokes_as_no_write_chat`、`test_assistant_reply_handles_short_chatter_without_generic_clarification`、`test_daily_clarification_does_not_match_non_daily_chatter_to_draft_items`、`test_tool_reply_uses_static_reply_for_small_talk_without_llm`；服务器定向与入口回归通过；线上定向 smoke `chatter_does_not_match_daily_candidate` 通过。 |
| 2026-07-07-009 | 庞浩/Agent2 日报元话语误写正文 | 用户在机器人提示“还差问题/风险”后回复 `写日报了`，系统把这句追加为今日工作第 5 条；该句是用户状态/元话语，不是日报正文。 | fixed | 已修：新增“日报元话语”判定，`写日报了/我写日报了/我在写日报/已经填日报了` 等只进入 `chat/no_write`，不生成日报命令；`帮我写日报吧/写日报` 仍可启动日报流程；真实日报内容如 `今天完成合同审核` 不受影响。回归：`test_action_context_treats_daily_meta_status_as_chat`、`test_action_intake_treats_daily_meta_status_as_no_write_chat`、`test_assistant_reply_handles_daily_meta_status_without_daily_write`；服务器定向与入口回归通过；线上定向 smoke `daily_meta_status_is_no_write` 通过。 |
| 2026-07-07-010 | 庞浩/Agent2 9点后历史日报误清空 | 15:32 裸说 `清空日报`，系统沿用活跃上下文里的 2026-07-06 日报并清空；但 9点后昨天及更早日报应只支持查看/复制到今天，不允许直接编辑、清空或撤回，同时也不能影响今天日报的正常编辑。 | fixed | 已修：`DailyCommand` 编译层新增 09:00 截止门禁；9点后若用户明确把目标指向昨天/更早，`fill/edit/begin_edit/clear/revoke/confirm` 降级为 `no_write`；若历史日报只是 active context，则不再提供写入目标，`今天完成...`、`清空日报` 等当前日报操作回到今天；`query_history`、`copy_previous`、`complete_previous_plan` 仍允许；9点前补昨天/改昨天仍允许。回归：`test_after_nine_blocks_historical_daily_edit_even_when_explicit`、`test_after_nine_bare_clear_targets_today_not_historical_active_context`、`test_after_nine_today_write_does_not_use_historical_active_context`、`test_after_nine_still_allows_historical_daily_query_and_copy_sources`、`test_before_nine_still_allows_yesterday_edit`、`test_report_date_today_hint_ignores_historical_active_report_date`、`test_no_change_message_explains_historical_after_cutoff_block`；服务器定向 147/147、入口与回归 187/187 通过；线上定向 smoke `today_write_ignores_historical_context`、`bare_clear_targets_today_not_historical_context`、`explicit_yesterday_edit_is_blocked_after_cutoff` 通过。 |

## 2026-07-08 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-08-001 | 庞浩/Agent2 V4-PRO 大规模生成回放边界 | DeepSeek V4-PRO 30 组 smoke 暴露 9 组失败：`今天还行`、`明天计划还是那些`、`今天工作：……算了没啥，帮我查一下劳动法第39条` 等空泛/闲聊/只读问答仍可能写库；`今天做了啥呢……哦对了，上午整理档案，下午开了个评审会` 被前半句阻断；`把今天的工作事项再补充一个：下午参加了法务部例会，讨论了新规`、`证据清单发给法院`、`恒大碰进度` 等真实业务碎片覆盖不足；评测生成器还把 9 点后历史日报编辑、无历史上下文的“昨天计划已完成”期望成直接写入。 | verifying | 已修本地结构：入口/命令层区分空泛状态、日报元话语、模糊复用、历史日报编辑截止与真实业务证据；补充办公室业务对象/动作与日报内容净化；生成器校准历史编辑和无上下文完成语义，不再靠刷分放宽线上写库。新增/更新回归覆盖空泛 no-write、业务碎片写入、明早/明天上午、模糊复用阻断、历史编辑期望校准。当前本地核心回归 `264 passed`；服务器同步、历史回放和 V4-PRO 复测待完成。 |
| 2026-07-08-002 | 庞浩/Agent2 V4-PRO round5 语义边界 | V4-PRO smoke 复测继续暴露：`日报都不想写了`、`今天写日报了没？帮我把今天的活儿记一下` 这类元话语不能写库；`昨天我说今天要去见客户，已经见完了` 应写入“见客户”而不是保留整句或清洗成残字；`那个合同里的争议解决条款是不是改过？我记得之前写的是仲裁` 是无日期业务问句，应只读问答；`今天上午跟保利案...下午把恒大项目合同审完...` 不能被案件候选整体吞掉。 | verifying | 已修本地结构：日报命令清洗改为非贪婪抽取昨天意图完成事项；第一层新增业务引用问句只读边界，并修正“句中问号/是不是”问句形态；兜底问句不再抢月报状态和生活闲聊；生成器同步校准元话语、业务引用问句、昨天意图完成语义。新增回归覆盖上述样本。本地相关 `294 passed`，本地核心 `316 passed`；服务器同步、历史回放和 V4-PRO round6 复测待完成。 |

## 2026-07-10 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-10-001 | Agent2/模糊单条删除 | 无 focus、无 pending、今日工作为 `[alpha, beta]` 时，`那条删掉` 沿 `_recent_item_target_from_text()` 的 `preferred_field -> 单栏 -> today_work` 三层回退默认删除末条 `beta`；与 2026-07-06 台账“无焦点不猜最后一条”的结论漂移。 | fixed | 红测先复现实际删除末条；已删除代词目标的末条回退，返回 `ambiguous_target/clarify_target`。服务器 P0 真实 API/DB smoke 中 `[alpha,beta]` 前后完全一致；P0 Gold、三入口一致性和历史台账 smoke 104/104 通过。 |
| 2026-07-10-002 | Agent2/裸确认与多个 pending | 活跃 collecting 日报下，`是的/确认/对` 可被编译为 `confirm_daily_report`；日报和月报同时 pending 时，路由按代码顺序默认选择月报。 | fixed | 裸肯定语只允许唯一 awaiting pending；无 pending 返回 `orphan_confirmation`，多个返回 `ambiguous_confirmation`，均零写。显式 `提交日报` 仍直接提交且不创建 pending。服务器 P0 smoke、cutoff 5/5、Gold 39/39 通过。 |
| 2026-07-10-003 | Agent2/日报执行契约与并发幂等 | 生产 Agent2 日报 executor 接收 `raw_input + DailyCommand.content` 并在执行阶段重新解析目标；缺少统一的 item-id typed command、报告版本和命令级审计闭环。 | fixed | 新增五类 `TypedDailyCommand`、fail-closed validator、legacy compiler adapter、section-status 版本/幂等键、完整最小审计；Stream/Webhook/Manual 接入同一 orchestrator；高影响 `clear all/revoke` 在没有绑定目标和版本的 pending 前 fail-closed。服务器定向 385/385；P0 真实 API/DB smoke 5/5 覆盖三次重放一次写、旧版本冲突零写、回复失败后同消息重试零写；online system 7/7、product 140/140、progress 12/12、issue ledger 104/104 通过。 |
| 2026-07-10-004 | Agent2/回放携带上下文缺少 CognitiveDecision action | 服务器 842 组/5562 轮执行 replay 中有 6 个 contract invariant risk（3 条历史对话在两份语料重复）：`啊，处理了关于离职人员的手续对接工作`、`是日常运营审核`、`帮我整合优化` 被 replay-carried daily effect 写入，但 `CognitiveDecision.actions=[]`，违反 `allow_write_requires_write_action`。 | fixed | 2026-07-10：移除 active-daily 无 action 时生成 legacy write effect 的兜底；具体离职手续工作由 action-first 产生 `daily_write`；无明确目标的纠正/整合由显式 `disambiguation_required` action 进入澄清，零写入。最终本地 747 组/1727 轮、服务器 842 组/5562 轮 execution replay 均为 0 mismatch、0 invariant violation、0 unexpected write、`gray_ready=true`；定向回归本地/服务器均 457/457；线上真实 API/DB P0 smoke 6/6、product 140/140、progress 12/12、system 7/7、历史问题台账 104/104、cutoff 5/5 均通过。 |
| 2026-07-10-005 | Agent2 Runtime Phase 1/replay 误写意图 | baseline-derived Runtime replay 将 corpus 已标注 `should_enter_daily=false` 的案件进展、纯出差和案件结案继续编译成 `capture_daily_event`，形成 42 个 unexpected write intent。 | unverified | 687 组/1407 轮复跑的诊断数字为 42→0，但审查确认 adapter 读取 baseline expected/workflow 生成 semantic decision，再用同一 baseline 评分，属于 oracle-assisted diagnostic，不能作为关闭证明。台账已统一标记 `closure_claim_allowed=false`；需由只读 raw text/state/resources 的独立 interpreter 重跑。 |
| 2026-07-10-006 | Agent2 Runtime Phase 1/copy_previous typed contract | 最终 replay 有 7 个明确 `copy_previous` 期望写入，但 Phase 1 typed daily contract 没有 copy command；过去其中 2 条曾被错误降格为 edit/append 后碰巧写入。 | legacy-gap | 现统一生成 `copy_previous_daily_report` action，并由 Planner 返回 `unsupported_phase1_copy_previous`，零写且有稳定 stage/error code；不在本轮扩展 copy 执行能力。生产 Shadow 前必须新增正式 typed copy contract/executor 或经 ADR 正式裁决。 |
| 2026-07-10-007 | Agent2 Runtime Phase 1/完整 corpus 缺失 | 验收目标为服务器 842 组/5562 轮，本地可复现选择只有 687 组/1407 轮；虽发现 747/1727、915/3102 和 168/1375 等历史结果，但没有精确 raw bundle、选择 manifest 和哈希。 | unverified | 已生成 machine-readable corpus manifest，并记录服务器恢复命令与所需外部文件；不得以本地较小或不同选择替代完整 corpus 宣称 parity_ready。 |
| 2026-07-10-008 | Agent2 Runtime Phase 1/独立 semantic Gold | 当前正式 replay 仍为 `baseline_derived_planner_executor_replay`，不能证明 Cognitive Core 独立准确率。 | unverified | 已建立 14 条独立于当前 Runtime 输出的高风险 semantic tape 候选，覆盖跨域、多意图、active context、短回复、pending、危险操作；全部标记 `pending_human_review`，人工签核前 semantic/write-intent/executable/clarification/ownership/segmentation 指标均为 unavailable。 |
| 2026-07-10-009 | Agent2 Runtime Phase 1/写能力与 audit fail-closed | trusted composition 过去仅按 `load_daily_snapshot/execute` 方法名接受 adapter，误配 executor 可在 receipt 校验前真写；audit sink 首次异常还会被重试并可能伪称 `audit_recorded`。 | fixed-local | composition root 现在只接受仓库内精确 `InMemoryDailyDomainExecutor`，任意结构相同 adapter 在执行前拒绝；audit 首次异常稳定返回 `audit_failed`、只调用一次且无 `audit_recorded`。定向红绿测试 2/2，Runtime contract 集合 55/55；线上 Runtime 未部署，Operational Gate 仍失败。 |
| 2026-07-10-010 | Agent2 Runtime/Oracle 泄漏 | 旧 replay 让 expected/baseline/report delta 进入 semantic adapter，candidate 与 scorer 共用同一 oracle。 | fixed-local | 新增 Blind Input、completion-hashed Actual、Sealed Labels 三文件与独立进程顺序；Runtime/CLI 无 scorer/label interface，递归拒绝 12 类 oracle key。旧 0/7 只保留 diagnostic，不能再作 closure evidence。Blind/anti-oracle 回归通过，独立人工 labels 仍 pending。 |
| 2026-07-10-011 | Agent2 Runtime/版本指纹漏项 | prompt 与 closed semantic contract 改动后，旧 v1/v2 Actual 仍出现相同 Runtime hash/run_id，模型配置也未进入指纹。 | fixed-local | Runtime version material 显式覆盖 prompt、semantic contract、interpreter、planner、state、typed validator 和 Blind execution modules；LLM adapter 暴露非密钥 model/thinking identity。内容或模型变化均有回归证明 hash 改变。 |
| 2026-07-10-012 | Agent2 Runtime/内部知识查询缺少 contract | `公司印章借用流程是什么？` 曾在 Cognitive Core 因未知 entity/action fail-closed。 | fixed-local | 新增 `knowledge_query/search_enterprise_knowledge` closed contract、Planner typed business command 与 contract-only Knowledge Domain receipt；无知识 adapter 时明确 blocked/unavailable，零写且不伪装成功。 |
| 2026-07-10-013 | Agent2 typed command/嵌套 payload 被字符串化 | structural fuzz 发现 `append_item.patch.items=[{"nested":"executable"}]` 会被 `str()` 化后当日报正文进入 simulated write。 | fixed-local | 最终 typed validator 新增 runtime 类型闭包：UUID/version/tuple item ids/dict patch/string idempotency，append items 与 replacement 必须为非空纯字符串。160 schema/owner seeds、100 transaction rollback seeds 与 typed 回归全绿。 |
| 2026-07-10-014 | Agent2 Runtime/超长输入依赖超时 | 约 2500 字 adversarial 输入依赖模型超时，Actual 只能得到泛化 `runtime_dependency_failure`。 | fixed-local | 认知入口增加 2000 字 reviewed limit，在模型调用前 fail-closed 为稳定 `input_limit_exceeded`，不截断后继续执行；回归证明模型调用次数为 0。 |
| 2026-07-10-015 | Agent2 Runtime/Blind 42/29 重裁 | 新 Blind Runtime 对当前 73-row ledger 完成 68 对话/145 前缀轮 replay；ledger 实际由原始 42+29 加 2 条 post-fix surfaced 组成。 | watching | 机器候选结果：原始 unexpected 42→0；原始 expected-write miss 29→6（5 条 copy 缺合法 previous snapshot，1 条 merge 目标已在先前 turn 删除）；新增 2 条 copy 仍 open。73 条 actual_write=0、legacy=0、failed-closed=0。human approved=0，因此 independent closure count 固定 0。 |
| 2026-07-10-016 | Agent2 Runtime/Shadow Candidate 隔离 | Phase 1 Harness 本身没有 sampling、timeout、circuit、PII 最小化和“不向用户返回 Shadow reply”的入口 contract。 | fixed-local | 新增未接线的 `ProductionShadowCandidateAdapter`：默认 disabled+kill switch、deterministic sampling、timeout/circuit、exact offline evaluator、ephemeral simulator、hash-only identity/text logs、无 reply 字段。只完成离线 contract/test，未连接生产 Shadow。 |
| 2026-07-10-017 | Agent2 Runtime/隔离数据库环境 | 本机无 Docker，localhost:5432 不可达，无法运行真实 PostgreSQL transaction/lock smoke。 | evidence-gap | 已完成 repository-owned transaction simulator 7/7：typed-only、raw text 拒绝、owner/field/version、batch rollback、idempotency、receipt/effect；真实 PostgreSQL rollback/advisory-lock/DB audit 仍需外部隔离环境。 |

## 2026-07-11 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-11-001 | Agent2 Runtime/Oracle 全字段与密封对象 | Oracle 黑名单只列举少数字段，`expected_action_class` 等可藏入 runtime config；Blind/Sealed 对象只做浅冻结，嵌套对象可在 hash 后被修改。 | fixed-local | Oracle guard 统一拒绝全部 `expected_*` 及 review/label/seal metadata；新增递归 JSON freeze/thaw，Blind Input、Actual 和 Sealed Labels 均深冻结且导出深拷贝。49 项聚焦测试与 65 项最终 acceptance 测试通过。 |
| 2026-07-11-002 | Agent2 Runtime/Shadow fail-closed 与日志隔离 | `failed_closed` 曾被 Shadow adapter 当成 observed 并清空失败计数；日志 sink 异常会逃逸；actor 字符串契约与 Runtime UUID 不一致。 | fixed-local | `failed_closed` 和 log failure 均计入 circuit；日志异常被隔离；actor/snapshot owner 全链路 UUID。新增连续失败、日志失败和真实 UUID 红绿测试。仍未接生产 Shadow。 |
| 2026-07-11-003 | Agent2 Runtime/模拟 receipt 审计自相矛盾 | 模拟 receipt 顶层 `actual_write=false`，但嵌套 legacy audit 保留 `actual_write=true`，旧 v6 有 71 条矛盾记录。 | fixed-local | 模拟审计统一为 `actual_write=false/would_write=true/simulated=true`；Domain registry 增加 receipt/audit 对齐 invariant。最终 A/B 各自 actual/domain/receipt/audit 写入计数和 flag mismatch 均为 0。 |
| 2026-07-11-004 | Agent2 Runtime/真实模型确定性 | 同一 Blind input digest、Runtime hash 和 run id 的真实模型重复运行产生不同 artifact。 | no-go | 最终 hash `3aecf502...` 下 A/B 145 轮仅 56 轮完全相同，89 轮不同，含 decision 81、typed command 35、clarification 4、status 7；两次安全包络均为零实际写、零 fallback、零 receipt/audit 冲突。Determinism Gate 明确 FAIL，不以 scripted interpreter 结果替代。 |
| 2026-07-11-005 | Agent2 Runtime/29 漏写候选资源一致性 | 旧 closure 把 `target_not_found`、completed report 和 Phase-1 copy gap 与真正 Runtime miss 混为 open；其中合并第二目标已被前序合法删除。 | classified-pending-review | 最终 A 为 22/29 可模拟执行、7/29 合法不可执行；B 为 23/29 与 6/29。5 copy gap 和 1 deleted target 在 A/B 稳定；另一 completed-state candidate 因前序模型状态轨迹不同仅 A 阻断。未知本地漏写为 0，independent closure 仍为 0。 |
| 2026-07-11-006 | Agent2 Runtime/最终 Shadow 验收 | 工程安全子项可通过，但真实模型 nondeterminism、0/1407 人工审核、2 条 adversarial active-goal 写候选、842/5562 corpus 缺失、隔离 DB 缺失及线上 Runtime 未部署。 | no-go | 最终报告 `outputs/AGENT2_RUNTIME_PHASE1_FINAL_ACCEPTANCE_REPORT.md`；machine summary 的 Evidence identity PASS，Safety/Semantic/Parity/Operational/Determinism 均 FAIL，Production Shadow 与 Live 均禁止。 |

## 2026-07-13 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-13-001 | 庞浩/Agent2 日报复数指代 | 用户在当前日报已有两项今日工作后说“明天继续做这俩件事情”，系统将代词原样写为唯一明日计划，未展开为两项具体工作。服务器 PostgreSQL 证据：2026-07-13 日报 typed command v2→v3 执行 `append_item(tomorrow_plan=[明天继续做这俩件事情])`；截图与 DB 一致。 | fixed | 根因是 Cognitive Semantic Interpreter 未把复数指代绑定当前 `daily_draft.items`，Planner 已有 `copy_current_work_to_tomorrow` typed contract 但未被调用。新增正式 deictic projection protocol：只有 collecting 当前日报、无其他 Pending、被指代数量与今日工作完整集合唯一一致时才生成 `copy_current_work_to_tomorrow`；不唯一时 clarification + 零 action。覆盖“这两件事情/这俩件事情/这俩项工作/这两个任务”及三项中裸指“两项”零写。服务器真实 API + PostgreSQL 隔离 smoke：两项场景把今日工作投影为“继续优化…/继续搭建…”，placeholder 缺失，v3，PASS；三项中裸指两项返回 `cognitive_v3_clarification`，明日计划保持空，PASS。庞浩现有日报已通过 typed command 纠正为两条具体计划，最终 v8。 |
| 2026-07-13-002 | Legal Ops/日报写操作 Outcome | 中台日报删除/新增已经提交并使版本递增，但 API 读取不存在的 `execution_result` 字段，把成功 receipt 误判为 blocked 并返回 HTTP 409/`actual_write=false`。 | fixed | `write_service` 改为只读取 typed executor 的正式 receipt `status` 和 `actual_write`；新增 executed/duplicate/blocked 映射回归。服务器 14 项中台写服务与产品 UI 测试通过；实际庞浩日报后续 edit 请求均返回 HTTP 200、`status=executed`、`actual_write=true`，与数据库 v6→v8 一致。 |
| 2026-07-13-003 | 庞浩/“我有哪些案件” | 2026-07-13 17:59 连续两次询问“我有哪些案件？/我有什么案件”，Semantic 识别为 `case_query`，但 Planner 固定生成 `query_case_risk`；Party resolver 随后返回“需要选择具体对象”，没有列出权限内 40 件案件。 | fixed | 新增 typed `list_assigned_cases`、actor/tenant/allowed-case 限定 SQL receipt/outcome/reply；自有清单 scope 与具体主体风险 target 分离。生产 LLM + 真实 PostgreSQL 最终两次完整回放均 PASS：庞浩、刘聪各返回权限内 40 件，查询 `actual_write=false`，英文 case type/stage 已映射为已确认的中文阶段。 |
| 2026-07-13-004 | 庞浩/日报上下文劫持出差 | 2026-07-13 18:00 “明天计划出差昆明”在 collecting 日报上下文中只生成 `append_item(tomorrow_plan)`；日报 receipt `actual_write=true`，但 `TravelIntent=0`。随后“有没有人和我协同？”退化为 Chat，未查询匹配状态。 | fixed | 显式未来出差事实通过闭式 grammar 补齐 Travel 主域 typed command；新增 `query_operation_status(domain=travel)`。Compiler 同时接受模型规范化的 ISO 日期提示。生产回放中庞浩“明日上午出差去昆明”、刘聪“我后天去南京出差”均在真实事务生成 `create_travel_intent actual_write=true`，状态查询读取当前候选/通知事实；事务回滚后持久化测试行 0。 |
| 2026-07-13-005 | 庞浩/日报上下文劫持案件进展 | 2026-07-13 18:02 “人民西路8号院 正在和业主沟通调解”可在权限内唯一关联“淄博市人民西路8号危房改造案”，但只写入日报，`CaseProgress=0`。机器人自由追问后没有创建 SelectionPending，用户补充“人民西路”又被写成第 4 条日报。 | fixed | `visible_cases` 仅从当前 tenant/actor permission scope 注入认知资源；唯一确认简称补齐 Case 主域 action，非唯一进入 SelectionPending，label fragment 只允许唯一候选继续。两用户生产回放各在真实事务为本人案件生成 `create_case_progress actual_write=true`，用对方案件简称均 `case_target_not_found + actual_write=false`；事务回滚后 CaseProgress/receipt 测试行 0。 |
| 2026-07-13-006 | 庞浩/业务状态追问与案件幻觉 | 2026-07-13 18:07 用户问“进入案件进展了吗”，系统未读取上一轮 receipt，转为 Chat 并编造“君瑞国际”“绿地城铂骊”两个案件；两者既不在庞浩 40 件权限集合，也不在当前 tenant 83 件 Case 中。 | fixed | 新增 persisted OperationOutcome 状态查询，严格按 tenant/user/conversation 读取上一 source turn receipt；Phase2 主路不再加载未限定旧 CaseTable RAG。生产回放“刚才那条有没有记录成案件进展/上一条记入案件进展没有”均返回“没有。上一条没有创建案件进展”，只读 receipt、零写入、无授权外案件名。 |
| 2026-07-13-007 | 庞浩/内部路由标签泄露 | Chat 回复固定输出“这部分按【闲聊】处理，不写入日报”，将内部工作流分类直接暴露给用户，且现有测试把该文案当成正确行为。 | fixed | 移除用户回复中的内部路由标签；路由只保留 audit。专项回归断言不出现“按【闲聊】处理/不写入日报”，现有旧错误期望已改为事实型回复，未删除或 xfail。 |
| 2026-07-13-008 | 两用户生产 LLM/自有案件清单 matter hint 漂移 | 同一“我手上有哪些案子？”在不同真实运行中，模型有时不填 `matter_hint`，有时把整句或“我负责的案件”填入，导致一次复跑又退化为 `query_case_risk`。 | fixed | 引入自有清单 scope 正式归一化：仅接受“我/本人 + 手上/手头/名下/负责/经办/承办 + 哪些/全部/所有案件”作为非主体 hint；任何具体企业、项目、案号或风险目标仍走风险/主体查询。修改后生产完整回放连续两次 12/12 PASS，不挑选有利运行。 |
| 2026-07-13-009 | 两用户生产 LLM/出差日期提示形态 | 模型把“明日上午”输出为 `date_hint=2026-07-14 上午`；旧 Compiler 只理解 `tomorrow/明天`，导致语义正确但执行返回 `travel_time_needs_clarification`。 | fixed | TravelWindow typed compiler 支持 `YYYY-MM-DD` 规范日期，同时保留自然相对日期和非法日期 fail-closed；生产回放昆明/南京两种表达连续通过。 |
| 2026-07-13-010 | 两用户生产 LLM/Case evidence span 结构 | 案件事实提取时模型返回非闭集 `evidence_spans` 形态，连续三次 contract repair 仍失败，Runtime 安全零写但用户只能收到失败回复；Prompt 的旧 `case_ref` 字段列表与后续 extraction contract 互相矛盾。 | fixed | Prompt 合并为唯一 `case_ref` 闭集并明确 `evidence_spans=[[start,end]]`。Interpreter 仅将精确 `{start:int,end:int}` 对象规范为整数二元数组；字符串、布尔、额外字段和其他非法形态继续 fail-closed。生产两用户案件写入回放连续通过，正文、案件名和“法院表示下周重新查控”均由 Outcome 原样复述。 |

## 2026-07-15 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-15-001 | 刘聪/跨日催报回复被旧月报上下文劫持 | 2026-07-14 22:00 收到日报催报后，刘聪于 2026-07-15 06:42 回复完整三栏日报：`【今日完成】...【明日计划】...【风险与问题】无`。生产 `WebhookEvent` 已处理，但 `report_id=null`；Route audit 为 `agent2_primary/canary_user`。Cognitive Core 将消息判成 `monthly_report`，三个 action 全部因 `periodic_report_snapshot_required` 阻断，Agent2 daily receipt 为 0、Legacy report interaction 无写入，用户收到“未形成 typed command”。 | fixed | 根因是同一会话遗留 `current_goal=monthly_report`，且旧催报没有持久化“日报类型 + 归属日期 + 用户”的回复绑定。现新增显式三栏日报文档协议，按用户原文确定性生成三栏 typed action，并优先于陈旧月报目标；Scheduler 在真实发送成功后持久化 `daily_report_reminder_sent`，Stream/Webhook 只在同 tenant/user 且 16 小时内读取该绑定以确定目标日期。线上 PostgreSQL 回滚验证中提醒回复被绑定到 2026-07-14、回滚后残留 0；生产真实模型只读连续 3 次均生成 7 个 `capture_daily_event`（4 今日完成、2 明日计划、1 风险），无失败。历史失败日报未补写，需用户重发才会产生真实业务写入。证据：`/home/ai_review_tunnel/deployments/agent2-dialogue-context-20260715T094248/postgres_rollback_evidence.json`、`live_semantics_evidence.json`；本地专项总集 1738 passed/1 skipped，服务器部署包验证 211 passed。 |
| 2026-07-15-002 | 庞浩/出差、案件进展和日报计划复合消息漏案件进展 | `明天出差去南京沟通鑫瑞达回款事宜` 当时只写入日报明日计划和出差，生产 PostgreSQL 对该 source message 的 `CaseProgress` 记录数为 0；页面回复却没有说明案件进展未写入。 | fixed | 根因有两层：自然简称生成器要求至少 4 个汉字，唯一可见案件“鑫瑞达”只有 3 个字，首次语义因此只得到出差；补充案件语义时模型又重复输出原出差动作并回显内部案件标识，旧合并校验将整个补充结果拒绝。现允许经权限范围验证的 3 字唯一自然简称并阻断“房地产/有限公司/合同纠纷”等泛称；补充语义只保留合法案件动作，剥离重复 sibling action 和模型回显的数据库标识；案件+未来出差在同一来源片段时，由 typed policy 确定性补充日报明日计划。生产真实模型只读连续 3 次均得到 `record_travel_event + record_case_progress + capture_daily_event`，案件唯一解析为 `SSGL-2505-0022`；数据库回滚验证证明庞浩有该案写权限且无残留。历史截图消息未回填，修复只对新发或重发消息生效。证据同上。 |
| 2026-07-15-003 | 两用户/完整日报中夹带一个或多个案件进展 | 完整三栏日报中包含“鑫瑞达今天与对方沟通回款……”时，结构化日报协议能生成日报 action，案件补判也能识别案件事实，但模型把整份日报作为 Case segment；合并后触发“semantic segments must be ordered exact substrings”，整轮安全失败。多个不同案件分列在同一日报时，旧逻辑还会把它们当成一个歧义目标，而不是逐条处理。 | fixed | 根因是案件补判以整轮日报为单位，未遵守 ADR 0019 的精确 segment 边界。现先由可信日报文档解析器切分原文条目，再仅对包含权限内案件引用的条目逐条执行 Case 语义补判；每个补判使用条目级 prompt、独立命名空间和原文 offset，日报 action 与案件 action 合并在同一个原文条目上，案件正文固定为该条原文，其他日报内容不会进入 CaseProgress。确定性红测先复现整轮失败，再验证单案件和双案件日报；本地 Agent2/日报/调度总集 `1742 passed, 1 skipped`。生产 `deepseek-v4-flash` 只读验证：单案件日报得到 6 个日报 action + 1 个案件 action；双案件日报得到 6 个日报 action + 2 个案件 action，两个 normalized fact 均为各自条目，0 DB write、0 外部消息。当前两人各 80 件可见案件均有 confirmed alias；另修复“精确正式别名被其他案件的模糊子序列抢成歧义”，精确正式别名优先，同一正式别名确实绑定多案时仍保持澄清。生产全量扫描两人均为 80/80 案件编号、完整案名和正式别名唯一正确解析，failure=0。证据：`/home/ai_review_tunnel/deployments/agent2-embedded-daily-20260715T104000/embedded_daily_live_evidence.json`、`embedded_multi_case_daily_live_evidence.json`、`case_reference_coverage_final.json`。 |

## 2026-07-16 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-16-001 | 近两周 Agent1→Agent2 回放/错误的零问题结论 | 旧报告宣称 747/747 轮零异常，但回放实际 `live_model_calls=0`，复用了缓存 decision；评分器只看运行、写入标志和少量固定文案，没有核验日期、栏目、正文事实、连续上下文和回复—变更一致性。 | fixed-harness | 已建立真实在线模型连续回放和独立评分器：747 轮全部调用 `deepseek-v4-flash`；按真实来源会话跨日期保持 Agent2 状态；后续日期使用 Agent2 自身快照；judge 不读取 Agent1 回复或 expected label。新结果为至少 70 个去重确认失败、1 个确认 P0，旧“零问题”结论撤销。证据：`docs/evidence/AGENT2_REAL_LIVE_REPLAY_AUDIT_20260716.md`。 |
| 2026-07-16-002 | Agent2/运行失败与内部实现泄漏 | 真模型 747 轮出现 17 次运行失败；另有 40 轮回复用户“未形成 Agent2 typed command / 未回退 Agent1”等内部文案。旧评分器因失效的文本匹配统计为 0。 | open-no-go | 失败包括 daily event/report/item target 绑定缺失、输入超限、periodic version contract 违规和远端断连。需把 contract/admission 失败转换为自然、事实一致且可继续的用户回复，同时为合法语义补齐 typed binding；修复前不得扩大 Canary。 |
| 2026-07-16-003 | Agent2/长段多栏目与修改语义 | 多条包含“今日完成、风险、明日计划”的长段输入被整体写入“今日工作”；12 个确定性栏目候选人工确认 11 个为真。另有“去掉完成两个字”被改成“今天了……”以及实际已追加、回复却声称未修改。 | open-no-go | 需要先形成确定性多栏目分段合同，再执行 typed command；edit/replace 必须做原文保护，Reply Composer 只能读取 committed Outcome。已新增评分器回归测试 6 项，但业务修复尚未完成。 |
| 2026-07-16-004 | Agent2/旧日期日报污染当前日报 | 用户明确处理 7 月 6 日日报时，Agent2 把旧 Agent1 回复摘要作为正文写入 7 月 7 日“问题与风险”，并把 7 月 6 日计划追加到 7 月 7 日。Admission 为 shadow，产生 review item 但仍继续假设写入，回复“记下了”。 | open-p0-no-go | 必须对日期、用户、报告和版本建立强绑定；禁止 assistant/history/resource 文本进入业务正文；命中高风险 review item 时 fail-closed 并零写入。来源轮次 `f06affd93b2fc640`，生产未写入（只读回放）。 |
| 2026-07-16-005 | 回放 Harness/跨日状态与自动事件覆盖 | 旧 harness 按日重置部分状态并读取 Agent1 历史快照；自动事件统计把有对话的 21 次催报与库存全部 35 次催报混在同一“已回放”口径。 | fixed-harness | 已改为按来源会话跨日连续状态、Agent2 前序快照覆盖 Agent1 快照；只读库存单独统计 35 次催报，语义工件明确只含有对话用户的 21 次。最后的 automatic-only 覆盖补丁 `py_compile` 通过，未部署生产。 |
| 2026-07-16-006 | 刘聪/单字碎片写入日报 | 日报收集中用户只发送一个“交”字，生产 Agent2 将其作为一条今日工作展示；该文本无法独立表达业务事实。 | fixed-local-verifying | 新增 `ReportFactContract` 完整度门禁：少于 2 个有效文字/数字单元的碎片不得形成日报写入，返回自然的补充提示且零写入；`test_agent2_real_dialogue_regressions.py` 已覆盖。尚未部署，待真实在线 smoke。 |
| 2026-07-16-007 | 747 轮真模型回放/时间字段序列化 | 首次新鲜在线回放显示 199 个运行失败，其中 193 个为 replay helper 将 `submitted_at` 的 `datetime` 直接放入 JSON，生产资源适配器实际使用 ISO 字符串。 | fixed-harness-verifying | replay helper 已与生产 `_daily_snapshot_resource` 对齐为 ISO 8601，并新增序列化测试；剔除该工具错误后，真实语义/契约失败为 6 个。必须用修复后 harness 完整重跑，旧 199 不能作为产品失败数，也不能把旧 747/747 缓存结果当在线证据。 |
| 2026-07-16-008 | Agent2/分栏日报标题与无关 evidence 字段 | `今日工作完成情况/明日工作计划/碰到问题与风险` 未进入正式文档解析，三次模型修复仍可能缺少 Daily action 绑定；另有模型给 `daily_event` 附带不参与执行的 `evidence_spans`，封闭契约直接令整轮失败。 | fixed-local-verifying | 正式日报文档协议新增业务分区标题和无冒号整行标题，逐条保留原文并过滤尾部助手式引导语；语义接缝仅丢弃 Daily 合同不消费的 `evidence_spans`，不改变正文、栏目或动作。目标红绿测试通过，待服务器真实模型复现。 |
| 2026-07-16-009 | Agent2/长段多栏目日报与跨历史日期复制 | 长段落同时包含今日完成、当前问题和明日/后续计划时，模型可能整体塞入今日工作或遗漏动作绑定；`复制前天的汇报内容到昨天的里面` 属于尚无 source-target 写入合同的跨历史日期复制，却以 Runtime 异常结束。 | fixed-local-verifying | 新增 Report Domain 专门拆分复核：仅接受按原文顺序、非重叠、唯一出现的精确子串，一项一实体一动作，并至少区分今日工作与后续计划；复核失败则零写澄清。跨历史日期复制沿用既有产品边界，在模型调用前返回明确的“不支持且没有改动”，不生成动作。目标红绿测试通过，待服务器真模型与全量 747 重跑。 |
| 2026-07-16-010 | Agent2/完整日报同时包含歧义案件与其他案件 | 完整三栏日报中同时出现“海西高新……”和“鑫瑞达……”时，前者需要案件选择、后者可唯一解析；旧歧义合同却强制整轮只能有一个 `record_case_progress`，导致整轮 Runtime 失败，日报、出差和两个案件均无法继续。 | fixed-local-verifying | 将歧义合同改为逐案件动作验证：至少保留一个歧义案件提案，且每个案件动作仍必须唯一绑定一个 `case_ref` 和一段原文；Trusted Admission 继续逐动作独立判定，歧义项创建 Selection，唯一项可继续，不允许模型选择数据库 ID。新增多案件合同红绿测试，相关专项 95/95 通过；待服务器真模型复现。 |
| 2026-07-16-011 | 在线只读回放/业务候选全部被误判上下文缺失 | 16 条暴露问题集在线回放中，模型已正确生成案件、出差或日报 Admission ticket，但 replay 漏传 Admission 所依据的会话版本、按历史时间签发 ticket 却用当前墙钟校验有效期、把收集中的日报错误表示为没有 active task，且没有镜像生产的权威 Ticket Store 与 Daily execution scope，导致候选依次被 `admission_state_version_required`、`admission_ticket_expired`、`report_context_not_uniquely_authorized`、`admission_ticket_repository_required` 或 `admission_scope_missing` 阻断。生产 Runtime 已有正式绑定，本项是验收工具失真。 | fixed-harness-verifying | replay 现在显式绑定同一轮 pre-turn `ConversationState.version` 和模拟轮次时间，按生产资源形状为 collecting 日报暴露唯一 active task，并在内存权威 Ticket Store 中签发/单次消费每张业务票据、向日报执行器传入同源 Admission scope；仍保持外部 `actual_write=false`。新增资源镜像测试并通过专项 106/106；待重新打包后以真实在线模型复跑 17 条，确认 `would_write`、阻断原因和回复均与真实候选一致。 |
| 2026-07-16-012 | 庞浩/“今天做了滨海医院拟诉评估”只进日报未进案件 | 2026-07-16 14:20 真实消息写入当天日报成功，但案件进展被阻断，机器人先展示日报再追加“需要补充信息”。数据库证据：Daily receipt `actual_write=true`、日报 v0→v1；Case Outcome `actual_write=false`、`blocking_reason=case_target_needs_clarification`；Admission 原因是 `case_reference_not_grounded_in_segment`。 | fixed-local-verifying | 生产仍是旧 HEAD `96cecec...`，尚未包含本地已完成的自然案件简称解析：三字以上简称必须在原文出现、对全部可见案件匹配，唯一才准入，歧义则 Selection，泛称永不获得写权限。当前隔离代码已能把“滨海医院”唯一解析到 `SSGL-2603-0007`；新增这条真实原文进入在线回放并在部署前验证“日报 + 案件”双 Outcome。历史消息不手工补写。 |
| 2026-07-16-013 | 案件—日报/同一事实半成功 | 同一原文同时生成 Case 与 Daily 独立命令时，Case 目标解析失败仍可能让 Daily 先成功，形成“日报已记、案件没记”的矛盾事实；反向粗暴阻断又会误伤用户明确要求“只写日报”的场景。 | fixed-local-verifying | 新增统一语义协调层：具体案件的实质工作默认 Case 为主，只有 committed/duplicate Case receipt 才能通过正式 Projection Policy 进入日报；Case 失败时该事实的日报写入为 0。独立日报事项继续执行；明确 daily_only 时只保留 Daily，明确 case_only 时不投影日报。自然简称仍必须权限内唯一。结构/投影/真实对话专项 90/90 通过，待全量回归、服务器在线模型和 PostgreSQL 限定写验证。 |
| 2026-07-16-014 | Agent2/案件相关工作默认漏记案件进展 | “整理案件资料、与分公司核对材料、寻找当地资源、联系法院/对方/律师、拟诉评估、回款判断”等表达不一定包含“进展”二字；旧模型可只生成日报动作，或给已生成的案件动作错误标成 question/hypothetical，导致有明确案件的实质工作未进入 Case Progress。 | fixed-local-verifying | 统一为“具体案件 + 该案工作/状态/计划默认 Case 主写”：模型已提出案件动作且原文、权限内可见简称和证据片段精确绑定时，再由独立 statement assessor 校正错误 mode；真实问题、假设、引用、服务请求和显式“只写日报”仍 fail-closed。无具体案件的“整理案件清单”等泛化工作只进日报。新增资料、分公司、资源、状态、未来计划、显式 opt-out 与泛化工作回归；在线 8/8 策略语料 P0/P1/P2 均为 0，待 PostgreSQL 限定写验证。 |
| 2026-07-16-015 | 验收 Harness/多行案件事实与待选择回复误报 | 案件事实改为 Case 主写后，旧回放评分仍只统计直接 Daily append，错误报告“多行未拆分”；存在 Selection Pending 时，回放回复顺序又先落入通用规划失败文案，没有展示真实候选列表。生产 Stream/Webhook 的候选优先顺序本来正确，属于验收工具与生产漂移。 | fixed-harness-verifying | 回放评分现在同时统计直接日报条目、独立业务原文片段和 Selection 请求；回复顺序镜像生产，Selection/Information Pending 优先于通用 planning block。新增多行评分测试，真实模型暴露集 17/17、策略集 8/8 均为 0 失败、0 写入、0 fallback、0 内部术语泄漏；全量 747 新鲜模型回放正在运行。 |
| 2026-07-16-016 | Manual API/案件成功后漏掉日报智能投影 | Stream 与钉钉 Webhook 已统一为“案件 receipt 成功后再投影日报”，但 Legal Ops/Manual API 只组合了 Case、直接 Daily 和周期报告 Outcome，没有调用同一 `project_committed_case_facts`；同一自然语言从不同入口进入会得到不同结果。 | fixed-local-verifying | Manual API 已接入同一个 receipt-backed Projection Runtime：只在 Case `executed/duplicate` 且命中两用户/tenant/高置信度策略时执行，Case 失败不会创建 Report 命令；投影 Outcome 与 Case Outcome 一起持久化并进入同一回复。新增三入口结构门禁和 Manual 运行时回归，17/17 通过；因此旧 v12 运行哈希不再是最终部署候选，待 v13 全量回放和数据库验证。 |
| 2026-07-16-017 | 公网 Manual API/可伪造钉钉身份写日报或案件 | 生产服务直接暴露 8000 端口，`POST /reports/manual` 仅接收 `dingtalk_user_id` 和正文，没有任何认证；知道两用户钉钉 ID 的外部调用者可冒充其身份进入 Agent1/Agent2 写链。Legal Ops 前端不依赖该接口，它只用于内部调试/限定 smoke。 | fixed-local-verifying | Manual API 现在在查询用户之前强制校验现有 `X-Admin-Token`，生产未启用 Admin 或未配置 token 时返回 404，缺失/错误 token 返回 401；外部 `source` 字段仍不能强制切换路由。新增“未认证请求不得到达身份查询”和合法管理员请求回归，相关 37/37 通过；待服务器部署后用无 token/错 token/正确 token 三档 smoke。 |
| 2026-07-16-018 | 庞浩/“明天出差去南京沟通鑫瑞达回款事宜”确认投影断链 | 2026-07-16 17:05 真实消息成功写入“鑫瑞达”案件进展，但当日没有生成 TravelIntent、ReportProjectionRequest 或 Projection Confirmation Pending；模型生成的 Chat 旁路却随口追问“需要记录到日报明日计划吗”。用户回复“要”时，确认处理器把实际 0 条 Pending 错报成“有多条日报投影”，造成案件已写、出差和日报未写且上下文无法承接。 | fixed-local-verifying | 按三层修复：同一 segment 已绑定业务动作时禁止 Chat 旁路再发起无状态业务追问；Confirmation Resolver 明确区分 0 条与多条 Pending，0 条不得谎报歧义；Enforce 模式对明确“日期+出差+地点+事由”补齐正式 Travel 动作，并由 Case committed receipt 后的 Report Projection Policy 处理明日计划。数据库证据已确认 Case receipt `actual_write=true`，Travel/Projection/Pending 均无本轮记录；待专项回归、全量在线模型回放和限定部署 smoke。 |
| 2026-07-16-019 | Semantic Admission/安全澄清被擦除 | 日报为空时用户说“删掉第一条”，确定性协议已生成“当前日报没有已保存条目”的零写澄清，但 Enforced Admission 在没有 action 时只保留 `ambiguous_case_alias`，把其他安全澄清清空。 | fixed-candidate-validated | Admission 现在在 action 集为空时保留原始 clarification；空栏目、编号不存在等均保持零写且返回可继续的自然提示。新增强制 Admission 回归，v16c 真实模型 747 轮中该样本正确零写；候选尚未部署。 |
| 2026-07-16-020 | Semantic Interpreter/线上超时没有进入既定重试 | 生产 LLM 客户端把 `httpx` 超时转换为 `LLMTimeoutError`，旧重试层只捕获 `httpx.TransportError`，导致一次可恢复超时直接变成整轮失败。 | fixed-candidate-validated | 重试层同时捕获两种传输异常且最多重试一次；测试先复现一次超时、第二次返回合法语义，随后通过。v16c 747 轮 `failed_replay_turns=0`；候选尚未部署。 |
| 2026-07-16-021 | 日报/明确“确认提交”仍依赖模型绑定 | 完整日报处于 collecting 时，精确“确认提交”偶发被模型绑定到错误 segment 或产生无效 payload，同一候选定向回放出现非确定性失败。 | fixed-candidate-validated | 将精确“确认提交/提交日报”提升为可信文档合同：唯一 collecting 日报确定性提交，已完成日报明确无需重复提交，无唯一活动日报则零写澄清。v16c 定向 66/66、暴露集 17/17、全量 747/747 均无失败；候选尚未部署。 |
| 2026-07-16-022 | 回放评分器/否定语境误报成功 | Agent2 正确回复“当前日报没有已保存的条目，无法删除”，旧评分器仅匹配“已保存”三个字，误报 P0 `false_success`。 | fixed-harness | 新增否定分句识别和不可覆盖原工件的确定性复评分工具；红测确认旧逻辑误报，修复后相关 15/15。原始 v16c 工件保持不变，复评分工件通过 `reclassified_from_sha256` 绑定原始 SHA-256，结果 P0/P1/P2 和人工未决均为 0。 |

## 2026-07-23 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-23-001 | 庞浩/Agent2 日报采集上下文误写控制消息 | 进入日报采集状态后，用户依次发送“你是agent1还是agent2”“撤回”“清空”“清空日报”，四条均被当作今日工作写入并回复“记下了”。这证明 Agent1 回滚只能处理明确异常，不能识别 Agent2 的错误成功。 | mitigated-fix-verifying | 已立即将庞浩、刘聪从 Agent2 Canary 切回 Agent1（route v10，rollback=true），未扩大用户范围。根因是 active Daily 的确定性 plain-content fallback 把绝大多数短句直接提升为 `capture_daily_event`，绕过模型意图和控制命令协议。候选修复新增另类问句识别、日报控制命令与正文分离、Admission 二次拒绝误分类 capture；专项 585/585、真实故障链路回归 90/90、Agent2 2341 passed/2 skipped。服务器已部署代码但两名用户仍保持 Agent1，等待隔离在线 API/数据库 smoke 后再决定是否恢复灰测。 |

## 2026-07-27 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-27-001 | 庞浩/个人记忆称呼与可信姓名重复 | 真实回复为“庞总，早啊，庞浩！……”，同一回复先使用个人记忆“庞总”，随后又使用真实姓名“庞浩”。数据库确认 `response.preferred_salutation` 只有一条有效值“庞总”，不是记忆串线。 | fixed | 根因是模型基于可信身份生成“早啊，庞浩”，服务端称呼渲染器随后再前置“庞总”。修复限定在最终回复渲染接缝：精确使用可信姓名和个人称呼消除开场呼语重复，正文中的姓名事实保持不变；未增加姓名特例、意图规则或修改可信身份。真实原句线上只读验证输出为“庞总，早啊！……”。本地与服务器候选均为 349 passed / 3 skipped，相关配置回归 60 passed；部署前后日报与对话状态哈希一致、消息事件数无新增，三项服务健康，原 11 人 Agent2 范围已完整恢复。 |

## 2026-07-28 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-28-001 | 刘聪、张圆圆/Agent2 模型已有回复但用户端静默 | 刘聪的“帮我提醒他们快点填写”“风险加一条，我拉肚子了”以及张圆圆的“确认提交”出现模型或运行链已有结果、钉钉却未回复；失败记录只保留错误类别，无法还原模型当轮的完整可见回复。 | fixed-deployed | 根因有两类：非标准结束标记把已有正常文字判为失败；运行失败在消息发送开启时仍抛出异常，事件一直停在 `processing`。修复后，非空的普通文字可原样返回并记录协议告警，原生工具调用仍保持严格校验；失败时返回事实一致的用户提示并关闭事件；结构化审计保存完整可见回复、原始 Tool Call 和响应元数据，不保存隐藏思考全文。共 187 项相关测试通过；服务器只读验证确认文字与审计均完整、工具调用为 0；部署前后 `daily_reports`、Tool-Call Pending/Receipt、Agent2 Conversation State 的数量与哈希完全一致。4 条历史卡单已标记失败并禁止重放，未补发旧消息；API、Stream 服务健康。候选提交：`e292cea54828201c832e38447ed8ba59a1af1971`。 |
| 2026-07-28-002 | 庞浩/跨用户查询“昨天谁没交日报”误报20人 | 机器人把 2026-07-27 的 20 名管理花名册成员全部称为“未交”，并错误包含已有日报的庞浩、刘聪等人。生产只读证据确认当天实际有 7 份日报、5 份已完成；故障 Tool Receipt 的真实分组是 `overdue=0`、`responsibility_unknown=20`，模型却把“提交责任未知”改写成“未交”。另确认 23:00 后补填未被次日 09:00 再次自动提交，且 Tool-Call 新链路在 09:00 后仍可修改昨日既有条目。 | fixed-deployed | 两个结构根因已关闭：日报读取不再要求采集团队与管理分组团队相同，而是按用户在日期上的唯一有效归属关联；服务器每日应交范围来自正式催报名单，最终回复只把服务器确认逾期者称为“未交”。业务口径统一为：非空日报夜间自动提交、无需人工确认；次日09:00后仍允许新增条目，但禁止修改、删除、移动和清空已有内容。生产兼容候选通过69项新口径定向测试；真实 PostgreSQL 回滚预演证明历史新增成功但不落库，改/删/移/清空均被拒绝。上线后补建7月27/28日共38条责任记录，并将7月27日2份有内容的迟填日报自动提交；正文未改，消息、Conversation State、Pending、Receipt和交互事件数量均未变化。在线复测确认刘聪7月27日日报为已提交（今日工作6条、问题2条、明日计划3条），未交名单收敛为12名服务器确认逾期者，另1名责任未知者未被列为未交；API、Stream、Scheduler均健康。 |
| 2026-07-28-003 | 团队/部门负责人晨报版式与数据范围 | 当前团队负责人会连续收到“总览+全员明细”两条长消息，栏目拥挤且人员缺少序号；部门总览线上只读生成结果包含59名用户、31个团队及大量测试账号/测试团队，风险统计为59人、其中高风险55人，正式部门负责人收件人为0，赵卫中则通过团队抄送收到重复消息。 | fixed-deployed | 已替换为正式管理数据投影：团队负责人只收本团队一条简报，部门仅发一条总览，自动明细消息为0；快捷查询统一为“看具体人员日报”，取消固定条数截断，人员编号、内容分行并保留全部事项。7月27日线上只读验收为7个正式团队、应交19、已交7、未交12、责任待核1；综合管理部收件人为刘聪，法务五部为曹俊，部门主收件人为赵卫中并抄送朱佳佳，其余5个尚未装载名册的团队明确显示“成员名单待同步”。定向测试107 passed，本次新增生产候选测试8 passed；生产旧测试原有7项失败，候选未新增失败。部署后API/Stream/Scheduler均active、health正常，数据库业务表/Conversation State/Pending/Receipt/Outbox before/after哈希一致，历史消息发送0。回滚备份：`/home/ai_review_tunnel/codex_backups/management_daily_briefing_pre_20260728T151900`、`/home/ai_review_tunnel/codex_backups/management_daily_briefing_truth_pre_20260728T152300`、`/home/ai_review_tunnel/codex_backups/management_daily_briefing_cc_pre_20260728T154300`。 |
| 2026-07-28-004 | 庞浩/查询综合管理部当日填写情况 | 陆健当天已经填写并完成，机器人却回复“目前没有可以确认的未交人员，另有1人缺少明确的提交责任数据”，同时暴露内部标识 `monthly-admin`；用户无法从回复中直接看出谁已完成、谁只填了一部分、谁完全没填。 | fixed-deployed | 根因不在模型选工具：真实日志显示模型已直接选择正确的只读工具。问题在服务端结果只突出旧的“逾期/责任未知”口径，没有把填写中日报单列，确定性回复又把内部数据术语展示给用户。现统一为三个用户可理解的维度：`已完成`（已完成或待最终确认）、`部分填写`（已有正文但尚未完成）、`未填写`（明确应填但没有正文）；提交责任不明确者继续留在内部，不进入这三个名单，团队内部代码不再出现在回复。新增回归先复现旧字段把“部分填写”重复并入“未填写”的问题，再修为新三分类存在时只认新分类。相关测试96项通过；生产只读核验为已完成4人（吴翔、姚菁华、庞浩、陆健）、部分填写2人（翁亚兰、胡晓云）、未填写5人（刘聪、张圆圆、曹扬眉、陆玉婷、马民），回复中无“责任待核”、无责任数据术语、无 `monthly-admin`，API/Stream/Scheduler均健康。候选提交：`e4bb02caa4a1a9fb14f1eb89f44de0de7b95c720`。 |

## 2026-07-29 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 回归 |
| --- | --- | --- | --- | --- |
| 2026-07-29-001 | 庞浩/Agent2 被告绩效查询误入日报查询 | 2026-07-29 09:32:58 发送“法务二部被告绩效发我”，机器人连续把“被告绩效”当作成员姓名或日报查询条件，最后要求确认成员和团队，没有返回已经发布的被告绩效结果。 | fixed-deployed | 生产日志确认同一消息连续三次调用跨成员日报查询，均被可信服务以“目标不存在”阻断，没有写入日报。根因是 11 人 Tool-Call 主链只有日报查询工具，旧绩效问答虽有真实数据，但在进入它之前已被主链截获。现已受控部署独立、只读的“被告绩效查询”工具，读取已发布的确定性结果；日报查询明确不处理绩效，服务端结果优先于模型自行改写。定向测试 133 项通过；全仓 3,101 passed、65 failed、26 skipped，候选新增失败 0。上线后真实模型四轮无发送回放严格得到“绩效查询—日报新增—绩效查询—日报编辑”，失败和各类写入均为 0；线上原句只读烟测返回法务二部月度结果，生产日报数前后均为 307。原 11 人全部恢复且集合哈希未变，API/Stream/Scheduler 均健康，两个日报受保护文件哈希未变。候选提交：`d0467353ddd2dd32cf8b1759a0cda92dc0029dc2`、`720e2daa4ac4b8c52be931b38519d02d922e8161`。 |
| 2026-07-29-002 | 庞浩/被告绩效目标明确却显示“数据不完整，目标待确认” | 法务二部的存量同比实际下降32.90%、新增同比实际下降39.57%，目标均明确为下降10.00%，机器人仍因3条已结案记录缺少结案日期而隐藏目标完成结论，并向普通用户展示内部数据维护说明、Skill和规则版本。 | fixed-deployed | 生产只读证据确认目标值、比较方向和实际值均完整；根因是报表层把“团队归属未确认”和“缺结案日期”统一视为目标阻断错误。现已拆分：只有会改变团队归属的错误才阻断目标；缺结案日期仍按Skill既定口径处理并保留后台审计，但不再阻断目标。线上原句现返回存量、新增两个“达到目标（目标下降10.00%）”，不再展示缺日期明细、Skill、规则版本或技术口径行。真实模型无发送烟测 `actual_write=false`，日报总数前后均为307，回执回滚后为0。绩效相关96项测试通过；完整候选3,101 passed、65 failed、26 skipped，新增失败0；进展检查12项通过。原11人范围、两个日报受保护文件及API/Stream/Scheduler均未变化。候选提交：`2486ceb1`。 |
| 2026-07-29-003 | 庞浩/绩效回复反复暴露“内部问答、不写入日报” | 每次绩效查询都先回复“庞总，这句我按【内部问答】处理，不写入日报”，给用户感觉像系统在解释内部路由，打断正常业务阅读。 | fixed-deployed | 这句话原本用于提醒绩效查询是只读操作，但安全边界已经由只读工具、权限和事务策略保证，不应由用户承担理解成本。现已从所有正常绩效结果、个人暂不可算和范围无权限回复中移除该内部分类前缀；线上原句回复直接进入“庞总，法务二部｜2026年7月……”和业务指标。真实模型无发送烟测确认不含“内部问答”及“不写入日报”，`actual_write=false`，日报数前后均为307，回执回滚后为0。绩效相关96项、进展检查12项通过；完整候选3,101 passed、65 failed、26 skipped，新增失败0。原11人范围、受保护日报文件及API/Stream/Scheduler均未变化。候选提交：`a785c52e`。 |

## 2026-08-04 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 根因、证据与待回归说法 |
| --- | --- | --- | --- | --- |
| 2026-08-04-001 | 庞浩、刘聪/晨报收件人错误 | 8月3日晨报汇总没有发给刘聪，却发给庞浩。生产只读重建显示“综合管理部（team-01）”晨报收件人为0，部门总览收件人只有庞浩。 | fixed-deployed | 已把刘聪的 `team_lead` 归属从旧分组 `monthly-admin` 校正到正式分组 `team-01`，并清空庞浩的额外部门抄送配置。发布后按8月3日数据重建收件人：刘聪（55264）恰好1条、角色为 `team_lead`、团队为正式综合管理部；庞浩（40842）为0条。未处理其余8条既有责任归属告警。 |
| 2026-08-04-002 | 刘聪/9点前补填后晨报仍显示未交 | 刘聪8:35补写8月3日日报，9:00数据库最终为“自动提交”，但9:00发出的晨报仍显示他未交。 | fixed-deployed | 定时任务现在会在自动提交后先保存，再读取晨报数据。生产库回滚演练确认：保存前原始查询为 `collecting`，执行新增保存点后为 `completed`，演练结束恢复原状态；定向回归确认自动提交、保存、晨报读取的顺序固定。 |
| 2026-08-04-003 | 刘聪/补填昨日日报后无法按上下文提交 | 机器人刚展示8月3日日报，刘聪回复“可以，提交”，机器人又追问日期；刘聪再答“昨天的”，机器人错误回复“确认提交功能目前仅支持当天日报”，没有按用户要求提交。 | fixed-deployed | 服务端现在从最近对话确定唯一日报日期，并把该日期作为可信上下文交给确认工具；“可以，提交”和追问后回答“昨天的”均在真实模型、生产库回滚演练中命中8月3日，未改动8月4日。原报告实际缺少“问题/风险”，因此线上真实行为会提示补齐，而不会谎称只支持当天或已经提交。 |
| 2026-08-04-004 | 庞浩/追问晨报时日期上下文丢失 | 庞浩先问“刘聪没交吗？他说交了”，补充“8月3日的”后得到正确结果；随后问“那你的晨报怎么说他没交”，机器人再次跳回8月4日并重复“11人未填写”，没有解释8月3日晨报。 | fixed-deployed | 无日期的连续追问现在沿用服务端确定的对话日期，只读工具不会自行退回今天。发布后真实模型回滚复测中，“8月3日的”→“那你的晨报怎么说他没交”的全部查询回执日期均为 `2026-08-03`，回复为8月3日11人已完成。 |
| 2026-08-04-005 | 刘聪/机器人错误否认自己发出的晨报 | 刘聪粘贴晨报并问“这是你发的吧”，机器人先混淆其他回复，随后称这份晨报“并不是我生成或发出的”，实际该晨报由同一系统定时任务发送。 | fixed-deployed（下一次真实晨报继续观察） | 晨报经钉钉接受后，会按收件人保存完整正文、报告日期、范围、平台回执和发送通道；最近一次定时外发会保留在对话上下文。发布后使用假发送端、真实生产库与真实模型做回滚演练，机器人能识别为系统定时管理晨报且不再否认。8月4日修复前的旧发送没有可靠回执，未伪造补录。 |
| 2026-08-04-006 | 全员/8点、10点催报未使用个人称谓 | 8月3日20:00和22:00的线上催报均直接使用姓名，例如“刘聪，到了今天的复盘时间了”“庞浩，你的今天的复盘还有问题/风险没有填写”；个人记忆中对应称谓分别为“四哥”“庞总”，其余已设置称谓的人员也同样未生效。 | fixed-deployed | 每轮催报先批量读取仍有效且不冲突的个人称谓，再生成文案；无效、过期或冲突时继续使用姓名。发布后生产库不发送演练确认刘聪开头为“四哥”、庞浩开头为“庞总”，正文保持不变；定向测试覆盖称谓、回退和冲突三类情况。 |
| 2026-08-04-007 | 刘聪/历史日报缺项时错误提示“已提交” | 修复日期承接后进一步复现：8月3日报告实际未填“问题/风险”，确认动作会被完整性规则拒绝；旧错误归类会把所有 `invalid_report_state` 都说成“已经提交”。 | fixed-deployed | 保留原有完整性校验，只把“草稿缺项”和“已提交不可改”拆成两个用户提示。真实模型、生产库回滚演练确认缺项时不写入，并明确提示补充“问题/风险”；填写“无”后可提交唯一的8月3日报告。 |
| 2026-08-04-008 | 法务一至六部/团队人员表仅录入、尚未正式启用 | 生产只读核对显示7个正式团队共70人均已录入团队名单且都有钉钉ID、无人重复归属；但除综合管理部11人外，法务一至六部共59人的用户状态均未启用，当日应交责任记录均为0。 | open-待业务确认 | 各队负责人名字已配置（法一杨弟桦、法二丁益明、法三刘波、法四薛旭、法五曹俊、法六王睿），但6名团队负责人和2名部门负责人均无法解析为已启用的钉钉收件人，当前有8条收件人告警。因此名单可查询，但这59人不会被催报、不会计入明确应交，也不会向相应负责人发送团队晨报。启用会扩大真实消息和填报范围，需业务确认后再分批处理。 |
| 2026-08-04-009 | 庞浩/查询综合管理部日报时出现两个同名团队 | 2026-08-04 10:43:21，庞浩问“查看昨天综合部的人的日报”，机器人要求在“综合管理部（team-01，已提交11人）”与“综合管理部（monthly-admin，0人）”之间二选一。 | fixed-deployed | 生产证据确认 `monthly-admin` 的12条旧成员归属均已于2026-08-02结束，正式 `team-01` 的11条归属自2026-08-03生效；根因是团队候选只检查团队启用状态，没有按所查询日期过滤成员归属有效期。现查询和晨报均按报告日期筛选有效团队，保留历史关系但不再把过期团队列为当前候选；同时支持“综合部”这类唯一简称，若简称会命中多个团队仍要求澄清，并从用户可见结果中移除内部团队代码。上线后真实模型回滚演练覆盖“查看昨天综合部的人的日报”“看一下昨天综合管理部所有人的日报”“昨天综合部成员的日报发我看看”，3/3均直接返回正式综合管理部11人日报，无二选一、无 `monthly-admin`、无 `team-01`，工具回执全部成功；日报、对话状态、Webhook和工具回执数量前后一致。晨报按8月3日重建仅含7个有效团队，刘聪仍恰好收到1条团队晨报、庞浩0条。 |

### 2026-08-04 发布与验证结果

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-daily-triage-20260804-v1`；API、Stream、Scheduler 均从该版本运行，`/health` 正常。
- 定向回归：17/17 通过；真实模型多轮回滚演练覆盖“可以，提交”“昨天的”“就刚才那份”等近义说法并全部通过，所有临时日报、消息、发送记录和工具回执均已回滚，未真实发送钉钉消息。
- 线上系统 smoke：7/7 通过；进度检查：12/12 通过。
- 历史问题台账 smoke：103/104 通过；既有 `DR-012-01`（“合并第2到第3条”仍二次询问）保持红色，本次未改该旧入口。
- 产品快速检查：137通过、3失败；失败均为旧入口“工作日9点前无日期日报默认归到前一工作日”的既有测试，本次未改该路径。
- 检查工具暴露26条无对应消息的旧演练防重复标记，已仅删除这些孤立演练记录；真实用户数据未动。
- 回滚备份：`/home/ai_review_tunnel/codex_backups/daily_triage_20260804_before_v1`。

### 2026-08-04 团队有效期修复发布与验证结果

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-daily-triage-20260804-v2`；`current` 与 Stream/Scheduler 固定路径均指向该版本，API、Stream、Scheduler 三项服务运行目录均为该版本，`/health` 正常。
- 定向回归：21/21 通过；其中新增覆盖查询日期传递、成员与负责人归属有效期、唯一团队简称、非唯一简称不猜测、用户可见结果不泄露内部代码。
- 真实模型与生产库回滚演练：3/3 通过，正式综合管理部成员数为11；演练前后工具回执310、对话状态36、Webhook事件2884、日报347，数量完全一致，未发送钉钉消息。
- 线上系统 smoke：7/7 通过；进展检查：12/12 通过。系统 smoke 产生的26条孤立演练防重复标记已按测试前缀删除，复核演练用户、演练Webhook、孤立演练标记均为0。
- 历史问题台账 smoke：103/104 通过；唯一失败仍是既有 `DR-012-01`（“今日工作第2到第3条是一件事”没有真正合并），本次未改该入口。
- 产品快速检查：137通过、3失败；仍为既有的工作日9点前历史日报日期归属测试，本次未改该路径，候选未新增失败。
- 本次代码回滚：运行线上版本中的 `scripts/switch_team_effective_date_release.sh rollback`；修复前代码备份位于 `/home/ai_review_tunnel/codex_backups/team-effective-date-20260804-before-v2`。

## 2026-08-06 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 根因、修复与回归 |
| --- | --- | --- | --- | --- |
| 2026-08-06-001 | 管理层/日报统计、总结、重点与未闭环查询 | 需要直接询问“庞浩目前有多少份日报”“总结最近工作”“综合管理部本周/上周做了什么”“最近部门重点”“刘聪或综合部有哪些未闭环工作”；原入口不能稳定回答日报数量，也没有按后续日报是否再次提及来识别未闭环计划。 | fixed-deployed | 已上线只读日报分析入口，覆盖个人数量、个人近况、团队周总结、部门重点、个人与组织未闭环；未闭环口径为计划提出后，后续今日工作未再明确提及。线上用生产库复测7种问法全部命中，读取过程中日报内容与更新时间均未变化。 |
| 2026-08-06-002 | 法务合约中心/同名部门、层级与架构表人员 | 生产数据同时存在两个“综合管理部”、两个“法务五部”和一个以人员姓名命名的错误团队；要求法务合约中心只包含法务一至六部、综合管理部7个子部门，人员与架构表一致。 | fixed-deployed | 已在单一事务中迁移历史引用并删除3个错误团队；当前法务合约中心恰好7个子部门，当前架构表共70人，部门人数依次为14、11、9、11、8、6、11，无重复归属、无人员与团队错位。只读分析现在识别完整架构名单，包括尚未开启机器人的59名成员，但不会因此发送催报或晨报；是否启用这59人的真实消息范围仍按 `2026-08-04-008` 等待业务确认。 |
| 2026-08-06-003 | 管理层/查询与总结类长回复排版 | “刘聪有什么未闭环工作？”和“总结下综合部上周工作”的内容正确，但钉钉窗口直接显示 `##`、`---`、`**` 和表格竖线，阅读体验较差。 | fixed-deployed | 根因是长回复使用了 Markdown 排版，但钉钉实际按普通文本发送；同时线上存在通用只读入口、日报分析直达入口和更靠前的工具调用通道，前两版分别漏掉了直达入口及工具调用通道。V3 已在工具调用成功结果形成时统一转换，再交给缓存、落库和发送；原有 Stream/Webhook 两条后备路径也继续转换。标题改为短标题，表格改为编号及分行字段，列表改为圆点；事项、数字、结论和业务逻辑不变，日报填写、修改、确认、提交及催报通知不受影响。生产工具调用通道用原话“发我下综合部上周总结”只生成不发送并回滚复测通过；10:37 用户再次真实发送同一句，事件正常处理、钉钉接口受理、无 Markdown 残留、`report_id` 为空且未写日报。 |

### 2026-08-06 发布与验证结果

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-report-insights-roster-20260806-62653fb7-v2`；API、Stream、Scheduler 三项服务均正常，`/health` 正常。
- 候选全部测试：374/374 通过；线上系统 smoke：7/7 通过，测试账号、测试日报、测试 Webhook 与孤立防重复标记已清理。
- 线上日报分析 smoke：7/7 通过；另用架构表中未启用成员“丁帅”复核姓名解析成功并返回0份日报，未开启催报。
- 组织清理前完整受影响行备份：`/home/ai_review_tunnel/codex_backups/report_insights_20260806_before_legal_department_cleanup/run-20260806T010943Z-2480985/affected_rows.json`，SHA256 `454b877c9b9b865e2232f1ab1d246ff95026dcef27682e83ef7614fc43f08e30`。

### 2026-08-06 钉钉长回复排版修复发布与验证结果

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-dingtalk-layout-20260806-v3`；API、Stream、Scheduler 三项服务均从该版本运行，`/health` 正常。
- 候选全部测试：381/381 通过；线上系统 smoke：7/7 通过；进度检查：12/12 通过。产生的测试用户、日报与 Webhook 已清理，26 条孤立演练防重复标记已删除。
- 真实回复格式 smoke：2/2 通过，分别覆盖“刘聪未闭环工作”和“综合管理部上周工作汇总”；两条回复的关键事项、数字、状态和结论均保留，`##`、`###`、`---`、`**` 及表格分隔线均未残留。
- 首版后捕获真实问法“发我下综合部上周总结”，确认其命中日报分析直达入口并仍返回旧排版；V2 已将直达入口、通用只读入口以及 Stream/Webhook 两种接入方式全部纳入同一转换，避免同类漏接。
- V2 上线后 10:20 的真实重试仍为旧排版，线上日志与 Webhook 证明该消息实际被更靠前的工具调用通道接管（`tool_call_canary_processed`），并未进入上述两条路径。V3 已在该通道形成最终成功回复时统一转换；使用同一句原话、真实生产模型和生产数据库进行只生成不发送的回滚 smoke，通过工具调用主通道，`actual_write=false`，输出已是短标题、圆点列表及编号字段，无原始 Markdown 残留。
- V3 上线后 10:37 用户真实重试“发我下综合部上周总结”：事件状态 `processed`，钉钉发送接口返回 200，发送内容无 `##`、`###`、`---`、`**` 或表格分隔线，且事件无日报关联，未发生日报写入。V3 上线后截至 10:38 的真实事件共1条、失败0条。
- 历史问题台账线上回归：104/104 通过，覆盖碎片填报、清空、已提交修改、闲聊防误写、追加、替换、删除、撤销、合并、撤回、历史查询、整篇复制、编号展开、长文本、9点规则等；未发现本次排版转换引入连带失败。
- 当前候选包未包含 `scripts/run_product_gate.py`，因此本次没有把旧代码目录的 product quick gate 冒充为 V3 验证；该项保留为验证缺口，不影响已通过的候选全部测试、真实工具调用回滚 smoke、线上系统 smoke 与进度检查。
- 本次验证只读取历史回复并在本地转换，没有向真实钉钉用户补发消息，也没有改动真实日报。

### 2026-08-06 Agent2 模型主路整改与查询功能重新发布

| 编号 | 用户/场景 | 原话/现象 | 状态 | 根因、修复与回归 |
| --- | --- | --- | --- | --- |
| 2026-08-06-004 | 庞浩/自然对话被误写入日报 | “叫我庞总”“啥玩意”“有点无语”“今天周几”等短句在日报采集状态下被直接追加到今日工作，回复速度也明显表明没有经过模型判断。 | fixed-deployed | 根因是新增查询与日报采集前置判断放在模型之前，短句被程序直接判成日报内容，同时绕过了 Agent2 的记忆和自然回复。已先回滚到稳定 Agent2，再彻底移除 Stream 与 Webhook 中的模型前直达判断；误写内容通过正式 Agent2 日报命令清理。V5 上线后“有点无语”由模型自然回复并使用“庞总”称呼，日报零写入。 |
| 2026-08-06-005 | 全项目/查询功能走回 Agent1 式关键词对抗 | 日报数量、工作总结、部门重点和未闭环查询虽然能回答，但由模型前固定问法分流，破坏了 Agent2“模型理解、工具执行、结果回给模型组织回复”的架构。 | fixed-deployed | 新增统一只读工具 `query_report_insights`，模型先理解用户原话并选择查询类型，服务端只负责精确解析人员、部门、日期和权限，再把事实交回同一个模型作答；查询工具本身不写日报。Stream 与 Webhook 已确认不存在日报分析直达分支。 |
| 2026-08-06-006 | 管理层/本周与上周组合查询 | 同一句同时询问“综合管理部本周和上周做了什么”时，早期版本可能只返回一个周期；模型也曾误选只能查单日填写情况的工具。 | fixed-deployed | 明确区分“单个日期填写情况”和“周、最近、历史、重点、未闭环分析”两类工具；模型策略要求同时问两个周期时分别调用两次分析工具。真实模型发布前连续3次周查询及1次本周+上周组合查询均正确，发布后周查询再次通过且全程零写入。 |
| 2026-08-06-007 | 庞浩/个人记忆和称呼体验退化 | 回归期间机器人不再称呼“庞总”，并把“叫我庞总”写进日报，看起来像记忆能力消失。 | fixed-deployed | 数据库中的“庞总”长期记忆始终存在，问题是模型前直达逻辑没有读取记忆。恢复 Agent2 模型主路后，记忆工具与最终回复重新生效；线上回滚演练和发布后自然对话均返回“庞总”，没有新增日报内容。 |
| 2026-08-06-008 | 庞浩/当日日报为空的发布后核对 | 发布后只读核对发现庞浩 2026-08-06 日报三栏为空，需要确认是否由发布或演练造成。 | verified-user-action | 用户明确确认该日报由本人主动清空，不是发布、测试或查询功能造成；保持现状，未擅自恢复内容。 |

- 当前线上版本：`/home/ai_review_tunnel/releases/ai-review-system-agent2-model-query-20260806-v5`；API、Stream、Scheduler 正常，`/health` 正常，发布后无新增服务告警。
- 当前生效范围为综合管理部现有11名 Agent2 用户；没有擅自把法务一至六部尚未启用的59人纳入真实催报或消息范围。
- 候选回归除已废弃的“模型前查询直达”旧断言外为194项通过；服务器定向测试26项通过。真实模型发布前11类对话全部通过，覆盖自然聊天、称呼记忆、日期问答、7类日报分析及本周+上周组合查询，日报均未变化且事务全部回滚。
- V5 发布后再次验证自然聊天与周查询；日报填写、历史日报确认、日期承接、“刚才那份”承接、缺项阻断和定时晨报来源等原有流程均通过回滚演练。

### 2026-08-06 未闭环口径、近期重点与全员上线准备

| 编号 | 用户/场景 | 原话/现象 | 状态 | 根因、修复与回归 |
| --- | --- | --- | --- | --- |
| 2026-08-06-009 | 庞浩/翁亚兰上周未闭环统计 | 同一项绩效表收集和15楼资料整理被按不同日期、不同说法重复计数；已经在后续日报写明完成的事项仍被算作未闭环，模型回复还出现重复编号层级。 | fixed-deployed | 未闭环统一为“计划提出后，后续今日工作中再也没有提及同一事项”；同一事项跨日重复计划会合并，阿拉伯数字与中文数字、动作词和语气词差异会归到同一工作流。后续写明仍在推进属于“已有跟进”，计划后没有任何后续日报属于“证据不足”，均不冒充未闭环。模型只接收结构化事实并负责最终表达，明确限制为一层可见编号。生产真实数据复测翁亚兰上周未闭环由11项校正为3项：沟通汇报演讲比赛、参加部门周会、对接领导报销；日报零写入、事务回滚。 |
| 2026-08-06-010 | 70人全员上线准备/运行租户识别 | 首次只读上线计划错误地按管理后台租户查 Agent2 控制记录，得到“现有控制为0”，与综合管理部已使用多日的事实不符。 | fixed-prepared | 管理后台组织租户与 Agent2 运行租户本来就是两套标识；上线工具现从现有11名用户的控制记录反查唯一运行租户，并在出现0个或多个运行租户时直接中止。修正后的只读计划确认：架构70人、当前启用11人、待启用59人、现有控制11个、待创建/开启59个；当前上限仍为11，因此 `ready_to_apply=false`，没有执行全员启用。 |
| 2026-08-06-011 | 次日晨报/团队与中心收件人 | 全员上线前需要确认7个团队晨报能分别发给团队负责人，中心整体情况能发给赵卫中、朱佳佳；同时晨报标题曾固定写成“法务部”，与正式架构“法务合约中心”不一致。 | fixed-deployed（下一次真实晨报继续观察） | 晨报标题改为读取正式中心名称；只生成不发送的70人模拟确认7个团队负责人分别为杨弟桦、丁益明、刘波、薛旭、曹俊、王睿、刘聪，中心收件人恰好为赵卫中、朱佳佳。模拟生成7条团队晨报和1条中心晨报，报告日2026-08-07、发送日2026-08-08、每天09:00调度日期计算正确；数据库写入0、钉钉发送0。 |
| 2026-08-06-012 | 管理层/近期重点包含已解决旧问题 | “最近综合管理部有什么重点需要关注”仍把翁亚兰7月31日“绩效表尚未收集完成”列为当前风险，但8月3日已写明催收完毕，8月4日至5日又完成扫描归档。 | fixed-deployed | 近期重点不再简单汇总近7天所有问题和计划，而是按同一事项核对后续今日工作：后续明确完成的历史问题从当前风险中移除；仍在推进、没有后续记录或当天新计划继续保留。生产真实模型发布后复测仅列当前3项问题和12项需跟进事项，翁亚兰已完成的绩效表事项不再作为当前风险；全程日报未变化、事务回滚、未发送钉钉消息。 |

#### 本轮发布与全员上线模拟结果

- 当前线上版本：`/home/ai_review_tunnel/releases/ai-review-system-agent2-unclosed-followup-20260806-v8`；API、Stream、Scheduler 均为 `active`，`/health` 正常。
- 当前仍只启用综合管理部11人，Agent2现有11个控制均启用且配置一致；其余59人没有被激活，没有扩大催报、晨报或机器人消息范围。
- 架构复核为法务一至六部、综合管理部共7个子部门，70人均有唯一钉钉身份、唯一当前归属；只读上线计划数据库写入0、钉钉发送0。
- 全员模拟生成8条晨报（7条团队、1条中心），核对填报概览、负责人关注、关键进展、重点计划、填报质量提示及消息长度；数据库写入0、钉钉发送0。
- 本轮相关本地与服务器定向回归16/16通过；生产真实模型重点查询通过，`actual_write=false`，日报快照前后一致且回执事务已回滚。
- 全员真实开通脚本默认只展示计划；正式执行必须同时把用户上限设为70、明确确认人数70并生成回滚备份。当前条件未满足，故不会误触发真实上线。

## 2026-08-07 新增问题记录

| 编号 | 用户/场景 | 原话/现象 | 状态 | 根因、修复与回归 |
| --- | --- | --- | --- | --- |
| 2026-08-07-001 | 庞浩/上线通知送达 | 完成情况曾被记录为“已发钉钉”，但庞浩实际没有收到；旧发送结果只能证明钉钉受理请求，不能证明指定人员真正收到。 | fixed-deployed | 人工复核发现旧工作通知使用的应用身份不包含用户40842，钉钉返回的最终结果明确列入无效收件人。已改用生产机器人直接发送并查询最终送达状态，3条补发消息均确认送达庞浩；线上代码进一步要求催报、晨报和上线通知取得“指定收件人已送达”的最终结果后才能记为成功，受理后状态不明时不切换通道重复发送。普通聊天回复不等待送达轮询，保持原有速度。定向回归31项通过，既有已送达消息只读复核通过，复核发送次数为0。 |
| 2026-08-07-002 | 法务合约中心/全员人数被少算为70人 | 庞浩指出“为什么只有70个人，我们应该是74个人”。 | fixed-deployed（全员仍未启用） | 70人只是7个子部门的当前成员；赵卫中、丁益明、朱佳佳、薛旭4人挂在“法务合约中心（中心层级）”，原上线脚本错误地只筛选启用的7个子部门，因而把4名中心直属人员排除。现上线名单改为“7个子部门70人 + 中心直属4人 = 74人”，同时继续只把法务一至六部、综合管理部认作7个子部门，不把中心层级伪装成第8个部门。生产数据只读模拟确认74人均有唯一钉钉身份、唯一当前归属；生成7条团队晨报和1条中心晨报，团队负责人7人齐全，中心晨报收件人仍恰好为赵卫中、朱佳佳；数据库写入0、钉钉发送0。当前仍只启用11人，另外63人未启用。 |
| 2026-08-07-003 | 胡晓云/补写昨日的日报未完成提交 | 08:33:43 询问“昨天的可以补吗？”，当时没有发送日报内容；09:49:51 至 09:50:24 连续补写4项今日工作。用户侧反馈“没成功”。 | watching | 生产库核对显示3次补写均已执行并写入2026-08-06日报，不是写入失败；但问题风险、明日计划仍为空，状态为 `collecting`，没有确认或提交时间。继续观察“已更新”是否容易被误解为“已提交”，本次未代用户补齐或提交。 |
| 2026-08-07-004 | 庞浩/全员首句预览标题显示为8个问号 | 08:46 收到的首句预览正文正常，但正文前出现单独的 `????????`。 | fixed-deployed | 8个问号与8字符标题“【全员首句预览】”一一对应；同一次人工发送的回执中，2字符姓名“庞浩”也变成 `??`，而由服务器文件生成的正文保持正常，证明乱码发生在人工脚本从 Windows 传到服务器的文字编码环节，不是 Agent2 模型回复，也没有进入日报。现所有钉钉文字、Markdown和工作通知在调用钉钉前统一检查：出现 Unicode 替换字符或整行4个以上问号时直接拦截，不向用户发送；普通中文、正常问句和现有上线通知均通过。线上专项回归48项通过，74人无发送演练通过，数据库写入0、钉钉发送0。 |
| 2026-08-07-005 | 法务合约中心/74人正式上线 | 庞浩明确要求“正式上线”，随后补充要求发送上线通知。 | fixed-deployed | 已将Agent2人数上限、正常日报提醒范围和数据库启用范围全部从11人扩至74人；63名新增成员已激活并创建63份控制记录，原11份控制同步到当前版本。上线后复核为74名活跃用户、74份可收发消息的Agent2控制、74人提醒范围，待开通人数0。7个团队及中心负责人关系保持不变。配置和数据库变更均有独立回滚备份。 |
| 2026-08-07-006 | 全员/上线通知疑似未收到 | 上线通知发送后，庞浩反馈“别人好像没收到”，并具体询问崔明明是否未读。 | watching | 未按接口受理结果直接下结论，而是逐批重新查询钉钉最终状态：74名全员通知收件人全部 `SUCCESS`，无无效、过滤或限流人员；首次复核时32人已读、42人未读。后续复核已升至62人已读、12人未读，管理团队通知为8人已读、1人未读。崔明明最初确为未读，之后已转为已读，并已与机器人交互3次，3次全部处理成功。上线以来已有17人主动对话、共43条消息全部成功、失败0条，7人开始填写或更新日报。说明消息已进入对应机器人会话，未读人员只是尚未打开；为避免重复打扰，未全员重发。 |
| 2026-08-07-005 | 胡晓云/加号分隔的日报事项被“删除”操作词误导 | 原话“印章盘点+删除测试问题汇总”完整进入服务器，但正式写入指令只包含“印章盘点”，机器人随后按已保存结果回复，第二项静默丢失。 | watching（接受风险） | 直接诱因是第二段以“删除”开头，模型偶发把它理解为删除已有日报事项，而不是第二项已完成工作；同一句线上只读回放两次，一次仅查询日报、一次正确写出两项，说明判断不稳定。改为“两项工作：一是印章盘点，二是删除测试问题汇总”或“印章盘点+测试问题删除情况汇总”时均完整保留。更深一层原因是写入校验只检查工具参数、日期、权限和版本，没有拿原句核对“每个明确事项是否都被保留”；把当时缺项指令原样回放时仍返回 `success`。业务判断为低概率边缘事件，当前不投入修复；仅当再次出现、形成同类聚集或影响提交完整率时重新升级处理。未修改线上代码或胡晓云的日报。 |

### 2026-08-07 人数口径更正

- 2026-08-06 台账中“70人全员”的表述，统一更正为“7个子部门70人”；法务合约中心全员口径为74人。
- 正式全员上线脚本现在必须确认74人，程序允许的全员上限也已支持74；生产当前上限仍保持11，避免在正式指令前误开通。如果名单再次只有70人，预检会直接失败，不能标记为全员准备完成。
- 本次仅完成代码修正、生产数据只读核对和不发送模拟；没有激活其余63人，没有发送测试通知，也没有执行真实全员上线。

### 2026-08-07 发布与验证结果

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-agent2-rollout-prep-20260807-v9`；API、Stream、Scheduler 三项服务均为运行状态，`/health` 正常，发布后近5分钟无服务警告。
- 定向回归31项通过；生产日报查询只读 smoke 7/7通过，日报更新时间前后一致。
- 74人只读上线计划确认：当前启用11人、待启用63人、现有控制11个、待创建63个；生产上限仍为11，因此不会误触发全员上线。
- 74人晨报模拟确认7个子部门、7名团队负责人、赵卫中与朱佳佳2名中心收件人；数据库写入0、钉钉发送0。
- 人数更正结果已通过生产机器人发给庞浩，最终送达状态为成功，收件人用户ID恰好为40842。

### 2026-08-07 上线通知乱码保护与负责人最终确认

- 线上版本：`/home/ai_review_tunnel/releases/ai-review-system-agent2-rollout-prep-20260807-v10`；API、Stream、Scheduler 三项服务均从该版本运行，`/health` 正常，发布后无服务告警。
- 丁益明、薛旭仍属于中心层级4人，同时分别承担法务二部、法务四部负责人职责；人员归属和负责人职责分开校验。74人上线演练确认二部晨报收件人为丁益明、四部为薛旭，中心总体晨报收件人为赵卫中、朱佳佳。
- 上线通知与晨报的文字在发出前新增乱码拦截；48项定向回归通过，正常全员首句、管理团队通知均可发送，模拟的8个问号会在钉钉调用前被拦截。
- 发布后再次执行74人只读演练：7个子部门、74人、7名团队负责人和2名中心总体晨报收件人全部正确；数据库写入0、钉钉发送0。
- 当前仍只启用11人，另外63人未启用；本次没有执行真实全员上线，也没有发送测试消息。

### 2026-08-07 74人正式上线与通知送达

- 已完成正式上线：活跃用户74人、Agent2可收发消息控制74份、正常日报提醒范围74人，待开通人数0。
- 当时正常调度保持开启：20:00首次提醒、22:00二次提醒、23:00自动提交、次日09:00晨报；钉钉应用身份和模型服务配置均已加载。2026-08-12产品口径已调整为次日08:00自动提交，见 `2026-08-12-001`。
- 全员上线通知分8批发送给74人，管理团队使用说明另发给9人；9批均取得钉钉最终 `SUCCESS`，无无效收件人、过滤或限流，共83人次确认送达。
- 收件状态首次复核：全员通知32人已读、42人未读；管理通知3人已读、6人未读。未读不等于未送达，暂不重复群发。
- 环境回滚备份：`/home/ai_review_tunnel/backups/full-rollout-74-20260807T101845-env-before`；数据库回滚备份：`/home/ai_review_tunnel/backups/full-rollout-74-20260807T101845-db-before.json`；通知送达回执：`/home/ai_review_tunnel/backups/full-rollout-notifications-20260807T103206.json`。三份文件权限均为仅部署账号可读写。

## 2026-08-09 全量排查与 Agent2 稳定性修复

本节只记录 2026-08-09 隔离候选的处理结论。除 `2026-08-09-003` 已包含在当前生产 `v21` 基线外，其余代码修复均尚未部署；真实模型验证全部使用事务回滚，真实钉钉发送为0。实施依据与逐批门槛见 `docs/decision-maps/agent2-stability-repair-20260809.md`。

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-09-001 | 日报整篇复制 | 模型表达了复制意图，但来源日报没有由服务端可靠绑定，可能出现来源缺失、日期错位或内部错误直接暴露。 | fixed-candidate | 来源日期、本人范围和可信版本改由服务端绑定；来源不存在或日期不清楚时只返回可说明的结构化事实。真实模型回滚覆盖3种复制/确认说法、来源缺失、日期含糊、目标已有内容和重复执行，日报全部恢复原状。 |
| 2026-08-09-002 | 晨报差异原因查询 | 用户追问“为什么晨报说我未交”时，旧答复可能依据当前日报状态猜测历史原因。 | fixed-candidate | 新增只读晨报事实工具，模型只能依据对应日期的快照、生成时间、自动提交、分段发送和事故记录作答；证据不足时明确不能确认。3种真实模型回滚均只读、无猜测、无写入。 |
| 2026-08-09-003 | Agent2处理监控 | 旧监控可能把“消息已消费”当成“业务成功”，也不能可靠证明是否实际进入模型。 | fixed | 当前生产 `v21` 基线已包含真实模型调用、工具结果和最终可见回复的分层监控；候选沿用该基线，没有恢复旧的单一 `processed` 判断。 |
| 2026-08-09-004 | 长语音、引号与编号内容 | 长内容可能在模型整理或工具参数转写时静默遗漏，尤其是中文/英文引号、编号事项和多栏目内容。 | fixed-candidate | 以当前消息序号和原文校验值绑定来源，不让程序理解业务语义，也不要求模型在工具参数中重复转写整段原话；写入结果以实际日报快照复核。7类真实模型回滚全部保留要求核验的日期、数字、部门、事项、风险和计划。 |
| 2026-08-09-005 | 机器人名字与用户称呼记忆 | 普通呼唤或提到别人给机器人的名字，可能被误当成“给机器人取名”；机器人名字和用户称呼也可能混淆。 | fixed-candidate | 记忆仍由Agent2理解，服务端只核验当前消息依据、保存值和记忆角色；两类记忆独立。王喜原有“兼爱”记忆保持不变；3轮明确双重纠正、12轮非赋名反例和19次模型对话回滚通过。 |
| 2026-08-09-006 | 休假与日报应交 | 聊天中的休假表达是否应自动改变应交或晨报口径，当前缺少可信考勤/审批依据。 | deferred | 产品决定本轮不做；不增加休假关键词、问句枚举或Agent1式旁路，不从聊天、语音或个人记忆改变应交。以后仅在接入正式考勤或请假审批数据后另行设计。 |
| 2026-08-09-007 | 写操作最终答复 | 多工具或延迟写入时，模型原计划与最终提交结果可能不一致，出现“数据已变却说失败”或“未执行却说成功”。 | fixed-candidate | 依赖写操作在同一事务内原子完成；最终答复只依据已提交的结构化回执，部分成功时逐项说明，内部错误码不直接展示。真实模型回滚同时覆盖成功、阻断、后续失败和未来条件请求不执行。 |
| 2026-08-09-008 | 历史日报内容修正 | 张圆圆重复项、翁亚兰原话含义等历史数据问题不能随代码修复自动改写。 | authorization-required | 本轮没有修改任何历史日报。后续只有取得明确授权并按原始消息时间线、Agent2审核、独立复核、备份和追加审计流程处理；不改变原提交状态。 |
| 2026-08-09-009 | 连续消息与语音后补充 | 收集器位于同会话执行锁之后，快速连续消息仍按单条排队，可能等待很久并削弱上下文体验。 | fixed-candidate | 同会话片段先在短窗口内按到达顺序合批，再由同一Agent2轮处理；后一批不能越过前一批，不同会话仍可并行。真实流式回滚中2条消息合为1批、只形成1条回复，事件、控制、绩效和回执全部回滚。 |
| 2026-08-09-010 | 未闭环查询范围 | 未指定时间范围时可能一次倾倒全部历史，既慢又难读；截断展示又可能被误解为总数。 | fixed-candidate | 工具仍统计全部可信事实，但结果过多时只给总数、起止日期和可选范围，由Agent2自然追问；明确最近30天时披露总数、展示数和剩余数。综合管理部、刘聪及最近30天三类真实模型回滚均只读通过。 |
| 2026-08-09-011 | 总体晨报分段续发 | 中间一段失败后可能整份重发，导致已送达段重复，或把平台受理误当最终送达。 | fixed-candidate | 为每段保存稳定编号、正文校验值和发送阶段；重试只从第一段缺口继续，已最终送达段不重发，正文变化必须新建批次。7项故障注入回归覆盖各分段位置、不同收件人和正文冲突；未进行真实发送。 |
| 2026-08-09-012 | Agent1遗留入口 | 不可达旧函数、误导命名或手工接口仍可能让后续开发误接Agent1。 | fixed-candidate | 生产入口、手工接口、日报执行和回放均增加Agent2唯一门禁；所有非主路配置明确失败，不静默降级。相关入口与日报回归已纳入162项专项验证。 |
| 2026-08-09-013 | 正式74人名单与组织口径 | 不同模块各自拼名单，容易再次出现70/74人不一致、中心被当第8个子部门或负责人职责混入人员归属。 | fixed-candidate | 建立唯一正式名单接口：七个子部门72人、中心直属赵卫中和朱佳佳2人，共74人；丁益明归法务二部、薛旭归法务四部并分别担任负责人。生产真实数据只读演练确认查询74人、团队晨报7份、总体收件人2人，数据库写入0。 |
| 2026-08-09-014 | 修复基线与发布安全 | 本地代码曾与线上稳定版不一致，直接修改会把已上线能力覆盖掉。 | fixed | 已从生产 `v21` 建立独立工作区、分支和隔离 `v22` 候选，生产没有切换、重启或发送消息。候选修改逐文件校验，并保留回退到上一版Agent2的边界。 |

## 2026-08-11 晨报提交明细展示调整

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-11-001 | 中心总体晨报/赵卫中提交明细 | 总体晨报的未交人员栏目直接展示“中心直属（1人）赵卫中”。产品口径要求赵卫中继续计入74人总数、中心直属2人分母及已交/未交数字，但不显示其个人提交明细。 | fixed | 仅在总体晨报的未交姓名展示层按赵卫中的稳定用户身份隐藏姓名；底层应交责任、提交分类、总数、分团队数字和审计快照均保持不变。同组其他未交成员仍显示姓名；正式名单配置缺失时直接停止生成，不允许旧版晨报绕过。已部署到 `/home/ai_review_tunnel/releases/ai-review-system-agent2-v22-briefing-detail-20260811-e8a60c5`。服务器晨报专项50项、Agent2既有能力95项、进度门禁12项及V22真实模型回滚12/12通过；2026-08-10生产数据只读核验保持74人、已交49、未交25、中心直属已交1/2且未交1，赵卫中审计快照仍为未交但姓名不显示，其余24名未交人员全部显示。三项服务和健康检查正常，数据库写入0、回滚残留0、钉钉发送0。 |
| 2026-08-11-002 | 旧线上隔离脚本/当前Agent2消息入口 | `run_online_system_smoke.py` 仍按旧的手工接口格式制造测试消息，未提供当前Agent2要求的唯一消息入口依据，导致6项被入口保护拒绝，其中重复消息与并发场景表现为500。 | legacy-gap | 部署前后该脚本、手工接口和消息入口代码的校验值完全一致，确认不是本次晨报改动引入，也不代表真实钉钉入口异常。测试结束后已清除26条仅由该隔离脚本产生、且不关联任何消息记录的入口占位，真实数据未变；本次不放宽Agent2入口保护、不恢复旧旁路，改用当前V22真实模型事务回滚验收并12/12通过。后续应单独升级隔离脚本，使其按正式Agent2入口合同构造测试消息。 |

## 2026-08-12 自动提交时间与已提交日报修改

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-12-001 | 庞浩及全员/自动提交时间 | 庞浩8月11日没有主动提交，23点后却被告知日报已提交并无法继续正常填写。 | fixed | 生产只读证据确认：庞浩当天只有17:46的 `append_item`，没有 `submit_report`；日报在23:00:00.013被定时任务改为 `completed + auto_submitted_timeout`，且 `confirmed_by_user=false`。同一毫秒共有26份日报被自动提交。现已把有效调度时间改为次日08:00，并把目标改为“前一个自然日且该日应填日报”，避免早上8点误提交当天日报。因此周二至周六早上提交周一至周五日报，周日及周一早上不处理周末日报；09:00晨报前保留同日期幂等兜底。22点提醒同步说明“明早8点前可补充”。自动提交与Agent2用户写入共用同一份日报锁，取得锁后重新读取最新状态，避免用户恰在08:00补写时相互覆盖。已部署到 `/home/ai_review_tunnel/releases/ai-review-system-agent2-v22-completed-edit-8am-20260812-b88d74c`；线上三个服务均运行该版本，scheduler进程有效值为8，健康检查正常。 |
| 2026-08-12-002 | Agent2/已提交日报修改 | 已提交后修改、删除、移动已有条目会被阻断，系统还会错误声称“提交后不能继续追加”。 | fixed | 本人现在可通过Agent2直接增补、确认空栏目、编辑、删除和移动已提交日报内容，修改后仍为 `completed`；非本人、旧版本、伪造条目、整篇清空及复制等保护不变。修改未注入的历史日报时，由Agent2先按用户语义调用指定日期读取工具，服务器返回该用户的可信报告、版本和条目；同一轮下一步再精确修改。程序不因看到“昨天”就主动加载日报正文，也不靠修改关键词判断意图。自动提交来源、本人确认标记及原提交时间原样保留；每次修改在 `agent2_daily_command_receipts` 中追加操作者、时间、目标条目及完整改前/改后快照。服务器候选与上线后真实模型回滚均覆盖补充、修改、删除、移动4/4，原V22日报真实模型12/12、相关回归167项及进度检查12项通过；所有回滚残留0、钉钉发送0。已部署到上述版本。 |
| 2026-08-12-003 | Agent2/自动提交原因与时间查询 | 庞浩问“几点提交的”，机器人称数据中没有提交时间；追问为何自动提交时，又猜测可能来自其他入口或他人操作。 | open | 数据库实际保存了精确的 `submitted_at=23:00:00`、`confirmation_type=auto_submitted_timeout` 和 `confirmed_by_user=false`，但当前查询工具快照没有把提交时间、提交方式和是否本人确认提供给模型，导致模型无法依据真实事实回答。本轮仅记录，未放宽查询权限或程序拼答案。 |
| 2026-08-12-004 | Agent2/“要”的连续确认 | 机器人主动问是否把“优化日报提交后无法修改的限制”加入明日计划，用户回答“要”后，系统仍再次追问是否写入8月12日，未完成写入。 | open | 上一轮只是自然语言询问，没有建立可执行的结构化待确认状态；下一轮模型无法把“要”绑定为同一写入动作。这是Agent2上下文与工具状态衔接缺口，不是用户表达不清。本轮未修改日报。 |

## 2026-08-14 周总结与完整日报提醒

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-14-001 | 庞浩/“综合部本周都做了什么” | 回复只展示8月14日和8月13日的部分事项，8月10日至12日没有出现，用户感觉少了几天。 | fixed-deployed | 已上线 `f585056`。生产只读复测仍读取8月10日至14日、11人、42份日报和148项今日工作；新版有限预览覆盖5个有记录日期，明确展示16项、未展开132项，不再把早几天静默省略。上线后三服务正常、Agent2控制74/74对齐；未发送测试消息、未修改日报。 |
| 2026-08-14-002 | 张世祥等24人/完整日报仍收到缺项提醒 | 张世祥8月13日21:57一次写齐5项今日工作、明确暂无风险、4项明日计划，三栏完整度为1，但状态仍为collecting；22点收到“未完成部分没有填写”。8月4日至13日同类错误提醒47次、涉及24人，其中43次有最终送达记录。 | fixed-deployed | 已上线 `f585056`。内容写入、状态、完整度和提醒筛选现共用同一判断：写齐后进入待确认但不代用户确认；完整遗留稿不再收到20点或22点缺项提醒；不完整稿只指出具体缺栏。生产只读重放47次、24人的历史形状，新版“未完成部分”通用输出为0。没有批量改历史日报、没有代用户提交、没有发送测试消息。 |

## 2026-08-15 日报写入失败续接

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-15-001 | 庞浩/“今天做了日报的基础功能优化”及“再试试” | 8月14日21:36完整原句写入被拦；21:41和22:39两次要求“再试试”仍被拦。23:48重新发送完整原句后才成功，最终日报仅有1项今日工作、问题风险和明日计划均为空，完整度0.34，并于8月15日08:00自动提交，非本人确认。 | fixed-deployed | 生产只读证据排除服务停机、身份权限、日期截止和数据库故障。当前轮原文写入修复先随 `28487703` 上线；跨轮安全续接现已随 `8c008ee6` 上线到 `/home/ai_review_tunnel/releases/ai-review-system-daily-retry-file-voice-20260816-8c008ee6`。只有同一本人、同一单聊、紧邻上一条入站、同一日期且原失败轮确实零写入的单个可恢复日报动作才会形成候选；仍由 Agent2 理解用户是否明确要求重试，服务端再从原事件复制用户原话，并锁定原日期、版本和目标。真模型重试验证4/4通过；同版本生产数据库外层事务四类回滚全部进入真实链路且残留为0。上线后三服务和健康检查正常、74/74 Agent2控制对齐；无数据库迁移或环境配置变化，真实消息发送0、历史日报修改0。历史受影响用户没有逐人重放，因此本项只证明新版本链路和隔离验收通过，不宣称旧失败消息已自动补写。 |
| 2026-08-15-002 | 全员/日报新增内容被大面积写前拦截 | 8月14日18:00至8月15日09:46，共35人出现126个写前阻断回合；125个拟调用新增日报内容，1个拟纠正日报日期。既有完整三栏日报、普通单项、长短文字、语音和“确认/再试试”等续接消息，部分人员连续重发多次仍无法写入。 | fixed-deployed | 根因是8月14日14:06上线的周计划补丁把逐字顺序校验误放到全员日报入口；修复已随 `28487703` 上线。模型继续负责理解、分栏和拆项，执行前独立复核一次；服务端只从本轮用户原话复制每项连续原文，并拒绝伪造、重复或相互重叠的原文片段，复核不能改变日期、目标、版本和提交开关。发布后三项服务正常，Agent2控制74/74对齐；隔离回滚 smoke 验证候选代码实际进入写入路径、服务器以用户原话覆盖模型改写、阻断规则生效，事务回滚后测试数据残留0、真实消息发送0。本次只证明已部署版本及隔离链路通过，不代表35名真实用户流量已逐人验证；未批量重放真实消息，未修改用户历史日报。短句“再试试”的跨轮续接仍归001处理。 |

## 2026-08-16 日报完整度与确认回复

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-16-001 | 8月14日日报提交/`REPORT_INCOMPLETE`缺栏提示 | 生产记录中有16次提交被标记为 `REPORT_INCOMPLETE`；多份日报实际仅缺问题/风险一栏，回复却笼统要求检查今日工作、问题/风险和明日计划三栏。另有预览已显示“问题/风险暂无”后确认仍称缺栏、完整或已完成日报仍被追问、不同日期日报状态串用，以及同轮含其他业务时日报确认结果不一致的风险。 | fixed-deployed | 已上线 `e98cb786`。状态、完整度、提交校验和用户回复现统一读取同一份可信日报快照：只列出服务器实际缺少的栏目；明确无风险计入完整度；完整或已完成日报不再追问补栏；多日期操作绑定各自报告、版本和日期，不能串用；日报确认复核不会压掉同轮其他业务的成功或澄清。发布前 Agent2 相关回归195项全绿、代码复审无高风险项；真实模型完成态5/5、单日报缺栏2/2、多日期缺栏2/2、跨流程协议3/3。发布后三服务均运行新版本、健康检查正常、74/74 Agent2控制对齐且无需修改；生产数据库外层事务回滚 smoke 覆盖5项，typed/tool/conversation残留均为0，钉钉发送0。未修改任何用户日报，未发送测试消息；真实用户后续自然流量仍继续观察。 |
| 2026-08-16-002 | 全员/Word 文件静默丢失与语音 ASR 404 复发 | 一份 Word 文件曾被当成空消息，既没有进入 Agent2，也没有正常保存；用户随后追问4轮，机器人仍只称没有收到内容。语音识别接口返回404的情况也再次暴露出 HTTP 与 Stream 入口失败留痕和提示不一致。 | fixed-deployed | 已随 `8c008ee6` 上线到 `/home/ai_review_tunnel/releases/ai-review-system-daily-retry-file-voice-20260816-8c008ee6`。Word 等当前不能处理的文件，以及语音缺 downloadCode、ASR 异常或识别为空，都会先保存失败事件并给出与实际失败原因一致的提示；失败事件保存本身失败时也不得声称“已收到”。HTTP 与 Stream 入口使用同一处理结果，正常识别成功的语音仍只进入 Agent2。Word/ASR 隔离适配器回归10/10通过；这证明失败分支和入口衔接，不是假称完成了真实钉钉文件传输或真实 ASR 网络调用。同版本生产数据库外层事务四类回滚全部进入真实链路且零残留。上线后三服务和健康检查正常、74/74 Agent2控制对齐；无数据库迁移或环境配置变化，真实消息发送0、历史日报修改0。 |

## 2026-08-17 日报长短文本、提交与查询失败

全量对账：8月17日最终共61次失败、40种不同输入。22:45后的两次新增失败是一份109字完整短日报和随后一次“提交”。精确分类为：15次长文本、8次可独立理解的短日报/补充、38次依赖上下文或提交跟进；提交连锁不再重复计算成独立根因。候选对10份长文本、全部可独立理解的短日报/补充、5类查询修改操作和常见提交跟进做了真实模型回放；脱离前文无法独立执行的跟进在无上下文测试中保持零写入，并由带可信今日草稿/上一轮预览的提交回放覆盖。

逐条脱敏映射见 `docs/evidence/agent2_aug17_failure_ledger_20260818.json`：61条事件逐条记录匿名事件哈希、上海时区时间、输入哈希/长度、原模型状态、根因类别、对应回放和候选结果，不保存原文或人员身份。生产原文、修复方案、写入清单和回读结果仅保存在服务器受限备份目录；三份主要证据哈希均写入脱敏映射。

| 编号 | 用户/场景 | 问题现象 | 状态 | 根因、处理与验证 |
| --- | --- | --- | --- | --- |
| 2026-08-17-001 | 全员/长短日报新增与整篇拆分 | 8月17日最终61次失败、40种不同输入；15次长文本失败去重为10份，包含董理258字日报。原模型轮最长约395秒且零写入，整篇日报还可能漏栏、漏项、误拆或合并。 | fixed-deployed | 统一版本先以 `5d23c6c` 上线，最终补丁 `480722d` 现运行于 `/home/ai_review_tunnel/releases/ai-review-system-unified-daily-20260818-480722d`。所有新增日报统一由Flash高推理规划并独立审核，不按长短文本分两套路径；服务器只从原文绑定内容并整笔执行。线上真实回滚先后暴露并修复了“内容已写但最后一句回复校验让整轮回滚”以及独立复核偶尔把“填写/保存”误认成“立即提交”两处遗漏。相关服务器回归401项通过；全仓3658通过、220项与基线一致、无新增失败；部署后三服务健康。最终候选整套真实模型回滚11/11通过；曾偶发失败的12项长清单修正后连续3/3通过；上线后长清单、上午359字口述、带日期完整短日报和单独提交4/4通过，钉钉发送0、数据残留0。生产61次失败逐条复盘涉及14人：8份日报修正（新建3、修正5），6份独立复核后确认无需改动；14条追加审计和最终回读全部通过。 |
| 2026-08-17-002 | 全员/填写后立即提交及连续“提交” | 日报新增失败后，大量“提交”“按这个提交”“没有变化直接提交”等后续消息连锁失败；同轮包含内容和立即提交时，旧复核强制要求三栏都有内容或明确为空，连续模型复核后仍失败。 | fixed-deployed | 统一规划审核三栏后，用户明确提交一份自包含日报时，未提到的风险或计划可由规划与独立复核共同确认留空，不再被旧完整性规则拦截；只有提交意图、没有新增内容时，交回完整Agent2并使用`confirm_report`，不得生成空新增。线上真实测试确认缺风险同轮提交直接完成，单独提交只调用确认工具。历史修复中原有5份日报的提交状态、确认方式和提交时间全部保持不变；缺失的3份按当晚已经过自动提交时点补建为自动提交，不冒充用户亲自确认。 |
| 2026-08-17-003 | 全员/查看今日日报 | 候选回归中“查看我今天的日报内容”直接使用注入快照回答，没有调用可信查询工具，耗时约50秒。 | fixed-deployed | 用户明确查看当前日报时，即使已有上下文快照，也必须调用一次`query_today_report`读取最新状态；确认或修改且无需展示时不做预备查询。真实Flash加绑定复测10.7秒，部署后回滚测试再次确认只调用一次今日查询且零写入。 |
| 2026-08-18-001 | 全员/8月18日0点至12点失败输入恢复 | 上午窗口内共21次失败、19种不同输入，涉及9人；均发生在最终版本上线前。部分是昨日日报整份重发，部分是连续提交，另有一份较长口述用后一次完整重述纠正前稿。 | fixed-deployed | 生产只读盘点和受限备份后，当前 `deepseek-v4-flash` 按9人的完整时间线生成最终内容，另一轮Flash独立复核；内容方案9/9和提交状态方案9/9通过。最终恢复3份内容（新建2、修正1），另将1份明确“提交昨天的日报”但原为自动提交的记录纠正为用户确认；其余5份确认已被后续成功重试覆盖，不重复改写。单一事务写入后9条追加审计与逐份回读全部通过，钉钉发送0。证据哈希：备份`634c908b...beaa0`、内容方案`39371293...6541e`、提交方案`2c4e80c4...92306`、写入清单`5a6c328c...0849e`、回读`c2849929...f774`。 |
