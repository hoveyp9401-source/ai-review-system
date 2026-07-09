# Agent2 Context Pack Design

## Why

Agent2 cannot become a better Agent by carrying more raw chat lines. Raw history is noisy, lossy, and unsafe: it does not reliably say what task is active, what item was just edited, or whether a fact came from a trustworthy business source.

The stable direction is a small Context Pack interface with deeper implementation behind it.

## External Interface

`build_agent2_context_pack(envelope, daily_report=None, knowledge=(), recent_assistant_replies=())`

Callers should not assemble prompt context by hand. They pass the current message, optional business artifacts, and optional retrieved evidence. The module returns one structured payload.

## Layers

1. Conversation frame
   - Current user message
   - Source, message id, conversation id
   - Recent assistant replies when available

2. Task frame
   - Active workflows
   - Task id
   - Status
   - Whether the task is awaiting confirmation

3. Artifact frame
   - Current daily draft
   - Field index and global index
   - Stable item id
   - Pending action keys

4. Recent action frame
   - Last modified item
   - Last deleted item
   - Pending action

5. Knowledge evidence frame
   - Source type
   - Source id
   - Summary
   - Facts
   - Confidence
   - Freshness

## RAG Rule

RAG results are evidence, not context authority.

For questions like "我手底下有几个案子？":

1. Query structured case registry first.
2. Use RAG over reports/documents only as supplementary evidence.
3. Put only summarized evidence into the Context Pack.
4. If there is no evidence, the assistant must say it has not retrieved a reliable source, instead of guessing.

## Knowledge Resolver

`resolve_knowledge(query, adapters)` is the retrieval boundary before Context Pack assembly.

Adapters return `KnowledgeEvidenceFrame` objects. The resolver:

1. Calls only the requested source types when the query narrows them.
2. Prefers structured business sources over vector/RAG snippets.
3. De-duplicates by source type and source id.
4. Returns `no_reliable_evidence` instead of allowing the assistant to guess.

The first adapter is `InMemoryCaseRegistryAdapter`, used by tests to model the future case registry connector. It can answer questions like "我手底下有几个案子？" from structured case rows without mixing that answer into daily-report writing.

`InMemoryOrgDirectoryAdapter` is the first organization-directory adapter. It reads normalized `OrgUserRecord` and `OrgTeamRecord` rows, and accepts ORM-like `User` / `Team` objects. It can answer identity and organization questions such as:

- "我属于哪个团队？"
- "法务二部负责人是谁？"
- "部门负责人是谁？"

`load_live_org_directory_adapter(session)` is a read-only loader over existing `users` and `teams` tables. It is intentionally not wired into DingTalk stream processing yet; gray testing should call it explicitly from harness or smoke scripts first.

`InMemoryDailyReportHistoryAdapter` is the first daily-history adapter. It reads normalized `DailyReportHistoryRecord` rows, and accepts ORM-like `DailyReport` objects. It can provide evidence for:

- "复制昨天的日报"
- "昨天明日计划是什么？"
- "昨天的计划都完成了"
- "看看我最近几天日报"

For previous-plan completion, the adapter returns evidence with `suggested_daily_operation=complete_previous_plan_items` and `suggested_target_field=today_work`. It does not execute that operation. DailyCommand compilation and execution must make the write decision separately.

`load_live_daily_history_adapter(session, user, settings, current_date=..., lookback_days=...)` is a read-only loader over the existing `daily_reports` table. It excludes the current date by default so current draft context remains owned by the artifact frame.

## Non-Goals For This First Slice

- No vector database yet.
- No automatic live answer generation change yet.
- No Agent2 gray enablement.

This first slice creates the seam so later retrieval and answer tools do not leak prompt assembly logic into every workflow.
