# Agent1 to Agent2 daily capability matrix

Statuses are evidence-oriented: `migrated` means an Agent2 typed path exists; `compatible` means an Agent2-owned adapter compiles the action to a typed command; `pending` means replacement cannot yet be claimed.

| Capability | Status | Agent2 evidence | Notes |
|---|---|---|---|
| Add one daily item | migrated | `append_item` | Cognitive v3 planner and typed executor |
| Add multiple items | migrated | `append_item.patch.items` | One typed command can carry multiple items |
| View current daily | migrated | Cognitive v3 `query_daily_report` -> `query_report` | Read-only executor; no advisory write lock or report upsert |
| View historical daily | migrated | Trusted `daily_reports` resource -> `query_report` | Relative/explicit dates are resolved before retrieval; result includes report content and status |
| Edit exact item | migrated | `edit_item` with item ID | Ambiguous targets block |
| Delete exact item | migrated | `delete_item` with item ID | No “last item” default |
| Merge items | migrated | `merge_items` | Requires at least two IDs in one field |
| Copy specified/previous daily | migrated | Cognitive v3 `copy_previous_daily_report` -> `copy_report` | Source report ID/date and typed sections come from permission-scoped persisted snapshots |
| Submit daily | migrated | `submit_report` | Completeness and version checks |
| Query submission/status | migrated | `query_report` | User-visible result includes collecting/completed status |
| Supplement explanation | compatible | Agent2 daily planning plus typed append/edit | Needs full scenario replay before cutover |
| Explicit short reply with one pending | migrated | Agent2 conversation state/pending binding | Exact pending ID/action/entity binding; consumed once |
| Short reply with no pending | migrated | fail-closed/no write | Covered by Agent2 regression corpus |
| Multi-turn context | migrated | Cognitive v3 state store | Optimistic state versioning |
| Daily plus chat | migrated | Agent2 semantic chat segment -> `build_cognitive_side_reply_v3` plus independent typed daily result | Only the chat segment enters the read-only reply composer; no Shadow/Agent1 reply generation |
| Daily plus case progress | migrated | typed daily + Phase 2 business composer | Separate receipt/result; separate transaction |
| Daily plus travel | migrated | typed daily + `CreateTravelIntent` | Separate receipt/result |
| Begin exact edit session | compatible | `query_report` + Agent2 context | Read-only setup |
| Clear whole report | migrated | Confirmed bound pending -> `clear_report(field=all)` | Direct model clear is rejected; exact report/version and idempotency required |
| Clear one section | migrated | Cognitive v3 `clear_daily_section` -> `clear_report(field=<section>)` | Exact report/version/section; no whole-report side effect |
| Revoke submitted report | migrated | Cognitive v3 `reopen_daily_report` -> `reopen_report` | Only `completed -> collecting`; exact persisted report/date/version |
| Copy current work to tomorrow | migrated | `copy_current_work_to_tomorrow` -> `copy_report` | Exact current snapshot; target is `tomorrow_plan`; deduplicated |
| Complete previous plan | migrated | `complete_previous_daily_plan` -> `copy_report` | Exact trusted source snapshot; previous plan projects to `today_work` |

Current conclusion: the core daily CRUD/query/copy/submit/reopen, section/projection paths, and daily-plus-chat composition are Agent2-owned. Full Agent1 parity is **not yet proven** until the parity replay and real test-tenant smoke pass. Phase 2 primary bypasses the Shadow/Agent1 semantic and reply chain; unsupported capabilities fail closed instead of falling back.
