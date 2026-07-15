# ADR 0022: Two-User Shared Case Progress Scope

- Status: Accepted
- Date: 2026-07-15
- Scope: Agent2 Sandbox tenant, Pang Hao and Liu Cong Canary identities, 80 imported cases, Legal Ops

## Context

The July 13 Legal Ops acceptance described each Canary user as seeing the 40
cases assigned to that user. On July 14 the business requirement changed: Pang
Hao and Liu Cong must be able to record work performed on a teammate's case.
The deployed identity bindings were therefore expanded to 80 allowed and 80
writable cases with `case_progress_collaboration_mode=explicit_shared_scope`.

The database continued to assign 40 cases to each user, but the Legal Ops case
workspace displayed all 80 as an undifferentiated list. That made the intended
collaboration scope look like a tenant or user-permission leak. The page also
rendered a progress form without explaining whether the current credential was
actually allowed to write the case.

## Decision

1. `owner_user_id` represents responsibility and workload assignment. It is not
   the case-progress write boundary in the current two-user Canary.
2. The server-resolved identity binding is authoritative:
   - `allowed_case_ids` controls visibility;
   - `writable_case_ids` controls creation of a progress record;
   - tenant and credential fencing remain mandatory;
   - URL parameters, request headers and frontend state cannot expand either
     set.
3. Only the Agent2 Sandbox tenant, Pang Hao, Liu Cong and the current 80 imported
   cases use `explicit_shared_scope`. This decision does not authorize another
   user, tenant or case.
4. Pang Hao and Liu Cong may each create a new progress record on any case in
   their 80-case writable set. The UI labels a case as either `本人负责` or
   `团队协作` and separately displays whether progress can be recorded.
5. A user may modify or soft-delete only a progress record whose non-null
   `reporter_id` equals the current server-resolved user. A missing reporter is
   read-only. The existing explicit administrative role remains a separate
   server-side exception and is not granted to either case-owner credential.
6. Reports remain user-scoped. Shared case visibility and case-progress creation
   do not grant access to another user's daily, weekly or monthly reports.
7. Default business pages exclude acceptance-smoke and fixture records. The
   travel page displays only travel notifications; case follow-up notifications
   remain in their own domain view.
8. The UI is a projection of server authorization, not an authorization source.
   A hidden or visible button never changes the repository or executor checks.

## Consequences

- Each Canary user sees 80 cases: 40 labelled as personally assigned and 40 as
  team collaboration cases.
- Each may add progress to all 80, while edit and delete controls are shown only
  for progress created by the current user.
- Team case collaboration is explicit and auditable instead of being inferred
  from ownership or a recent conversation.
- Older acceptance evidence that says each user can see only 40 cases is stale
  and must not be reused as current production evidence.
- This decision does not enable Semantic Admission enforcement, proactive
  Follow-up sending, automatic report projection or a broader Canary.

## Verification

The release gate must verify both credentials independently:

- 80 visible cases, 40 `本人负责`, 40 `团队协作`, 80 writable;
- an assigned and a collaboration case both expose truthful add-progress state;
- another user's progress has no edit or delete action;
- report counts remain different where the underlying user records differ;
- default travel results contain no `server_acceptance_smoke` record and no
  `case_progress_followup` notification;
- missing or invalid credentials return 401.

All production writes used for real-user acceptance must flow through the
natural-language or Legal Ops command path and produce committed receipt and
audit evidence. Handwritten SQL is not acceptance evidence.

## Rollback

Restore the previous application revision and identity-binding snapshot
together. If shared scope must be withdrawn, first disable the two-user write
path, then atomically restore each binding's 40-case allowed and writable set.
Do not leave the UI claiming team collaboration while the server has already
returned to owner-only scope. Keep all P0 safety switches closed throughout the
rollback.
