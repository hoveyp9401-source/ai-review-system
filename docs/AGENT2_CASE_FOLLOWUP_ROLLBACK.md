# Agent2 Case Follow-up rollback

1. Set `CASE_FOLLOWUP_SEND_ENABLED=false` and
   `CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED=false`; restart Scheduler, API and
   Stream. This is the immediate effect rollback.
2. If evaluation must also stop, set `CASE_FOLLOWUP_ENABLED=false` and restart
   Scheduler. Existing Case facts and Report items remain intact.
3. Verify the three services are single-instance and healthy. Verify no new
   `case_lifecycle_followup` outbox row is created or claimed after the cutoff.
4. Cancel only `scheduled` or safely `queued` Follow-up tasks through the typed
   Legal Ops cancellation endpoint. Never rewrite a `processing` or provider-
   accepted row by hand.
5. Restore the pre-deployment code backup and restart services if a code rollback
   is required. Do not drop the new tables during an incident; they are audit
   evidence and are backward-compatible while switches are closed.
6. Reconcile committed Case receipts, projection requests, Report receipts,
   message receipts and audits before re-enabling any effect.

Rollback does not route failed Agent2 work through Agent1 automatically and does
not create a second write path.
