# Agent2 Information Pending production continuation evidence (2026-07-14)

## Scope

This slice closes the first `InformationPending` continuation for a missing
travel date. It does not add a new business domain. The only supported field is
`travel_date`, and the only accepted first-phase answers are a standalone
relative date (`今天`, `明天`, `后天`) or a valid ISO calendar date.

## Runtime flow

1. `Agent2TurnRuntime` verifies tenant, actor, conversation, source message, and
   current conversation-state version.
2. `SqlInformationPendingRepository` loads only `active`/`awaiting_input` rows
   within that exact scope.
3. Multiple live Pendings, expiry, version drift, permission/object changes,
   and duplicate source messages produce a typed zero-write block.
4. The exact answer parser preserves source offsets and produces a non-writable
   `InformationContinuationRequest`.
5. `InformationContinuationAdmissionEngine` creates a fresh trace, decision,
   and Ticket. It does not call the LLM, reuse an old Ticket, select a recent
   object, or fabricate a combined source sentence.
6. The current answer remains the current source segment. The old destination
   and preallocated travel-intent id remain cross-turn authority claims from the
   authoritative Pending.
7. The fresh Ticket binds the original `pending_id`, current source message,
   normalized/raw answer values, evidence offsets, object ref, and state
   version. `Agent2TurnRuntime` independently rechecks that binding.
8. The normal typed planner/compiler/executor creates the travel command.
9. After the database receipt is staged as `executed`,
   `SqlAdmissionTicketStore.consume` consumes the Ticket and Information
   Pending in the same nested transaction as the domain effect and receipt.
10. A Pending CAS failure rolls back the travel write, successful receipt state,
    and Ticket consumption. Duplicate ingress returns the existing receipt
    before a second Ticket/Pending consume.

## Fail-closed cases covered

- no scoped Pending;
- multiple live Pendings;
- cross-tenant/user/conversation lookup;
- expired Pending;
- conversation-state version drift;
- permission revocation;
- object contract change or preallocated-id reuse;
- duplicate source message;
- non-exact/negated/invalid date answer;
- fresh Ticket not bound to original Pending;
- modified answer evidence;
- Pending status/version drift during commit;
- duplicate receipt after an earlier execution;
- failed business outcome (Pending is not consumed as success).

## Test evidence

Focused command:

```text
venv\Scripts\python.exe -m pytest -q
  tests/test_agent2_information_pending.py
  tests/test_agent2_information_pending_runtime.py
  tests/test_agent2_information_pending_sql.py
  tests/test_agent2_information_pending_fresh_admission.py
  tests/test_agent2_information_pending_runtime_wiring.py
  tests/test_agent2_turn_runtime.py
  tests/test_agent2_sql_admission_artifact_sink.py
  tests/test_agent2_sql_admission_ticket_store.py
  tests/test_agent2_authoritative_ticket_wiring.py
  tests/test_agent2_business_compiler.py
  tests/test_agent2_cognitive_admission.py
  tests/test_agent2_domain_admission.py
```

Result: `125 passed in 2.24s`.

After merging the remaining case-domain Admission contracts, the combined
Information Pending + Domain Admission suite completed with
`139 passed in 2.47s`.

Python compilation and `git diff --check` also passed for the modified slice.

## Files

- `app/agent2/information_pending_runtime.py`
- `app/agent2/information_pending_sql.py`
- `app/agent2/information_pending_admission.py`
- `app/agent2/turn_runtime.py`
- `app/agent2/cognitive_runtime_v3.py`
- `app/agent2/admission_store_sql.py`
- `tests/test_agent2_information_pending_runtime.py`
- `tests/test_agent2_information_pending_sql.py`
- `tests/test_agent2_information_pending_fresh_admission.py`
- `tests/test_agent2_information_pending_runtime_wiring.py`
- `tests/test_agent2_sql_admission_ticket_store.py`

## Evidence boundary

This report proves deterministic and SQL-contract behavior in the local test
harness. It is not evidence of a server migration, a real PostgreSQL smoke, a
deployed runtime hash, or a real DingTalk user continuation. Those remain part
of the parent Goal's deployment and Canary gates.
