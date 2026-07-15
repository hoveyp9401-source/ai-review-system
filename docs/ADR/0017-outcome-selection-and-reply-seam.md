# ADR 0017: Receipt-backed outcomes and typed selection before reply composition

Status: accepted for Agent2 Dialogue Safety Phase 1.

Agent2 will cross one deep dialogue-safety seam: executors and repositories produce receipt-backed `Operation Outcome` values, conversation ambiguity is represented by a distinct tenant/user/conversation-scoped `Selection Pending`, and reply strategies may express only those outcomes. We reject executor-authored final prose, model-selected database IDs, and reply-time success inference because each permits natural language to outrun committed database or external-message evidence. A Selection Pending is consumed only after its bound operation returns a successful or duplicate receipt; conflicts, permission changes, expiry, and missing candidates invalidate it with audit evidence and zero writes. Provider acceptance and delivery confirmation remain separate message states.
