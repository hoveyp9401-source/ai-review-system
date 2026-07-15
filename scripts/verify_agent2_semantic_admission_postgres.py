#!/usr/bin/env python3
"""Run the Agent2 semantic-admission migration in an isolated PostgreSQL schema.

The verifier is deliberately self-cleaning and production-safe:

* the caller must supply a unique, allow-listed temporary schema name;
* only synthetic gate records are created;
* the configured ``public`` schema is inspected as metadata only;
* the temporary schema is dropped in ``finally``;
* database connection secrets never enter command arguments or evidence.

This is an independent migration/database gate.  It does not start application
services and it does not prove a user-facing runtime closure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import dotenv_values


EXPECTED_TABLES = {
    "agent2_semantic_admission_traces",
    "agent2_semantic_admission_decisions",
    "agent2_semantic_admission_tickets",
    "agent2_semantic_review_items",
    "agent2_deferred_semantic_events",
    "agent2_information_pendings",
}

REQUIRED_CONSTRAINTS = {
    "agent2_semantic_admission_trace_mode_check",
    "agent2_semantic_admission_trace_uuid_v5_check",
    "agent2_semantic_admission_trace_idempotency_key",
    "agent2_semantic_admission_decision_uuid_v5_check",
    "agent2_semantic_admission_decision_idempotency_key",
    "agent2_semantic_admission_decision_trace_fk",
    "agent2_semantic_admission_decision_object_check",
    "agent2_semantic_admission_decision_artifact_check",
    "agent2_semantic_admission_decision_ticket_fk",
    "agent2_semantic_admission_decision_pending_fk",
    "agent2_semantic_admission_ticket_uuid_v5_check",
    "agent2_semantic_admission_ticket_idempotency_key",
    "agent2_semantic_admission_ticket_trace_fk",
    "agent2_semantic_admission_ticket_decision_fk",
    "agent2_semantic_admission_ticket_ttl_check",
    "agent2_semantic_admission_ticket_consumption_check",
    "agent2_semantic_review_uuid_v5_check",
    "agent2_semantic_review_idempotency_key",
    "agent2_semantic_review_trace_fk",
    "agent2_semantic_review_decision_fk",
    "agent2_deferred_semantic_event_uuid_v5_check",
    "agent2_deferred_semantic_event_idempotency_key",
    "agent2_deferred_semantic_event_trace_fk",
    "agent2_deferred_semantic_event_decision_fk",
    "agent2_deferred_semantic_event_window_check",
    "agent2_information_pending_uuid_v5_check",
    "agent2_information_pending_idempotency_key",
    "agent2_information_pending_trace_fk",
    "agent2_information_pending_decision_fk",
    "agent2_information_pending_consumed_trace_fk",
    "agent2_information_pending_ttl_check",
    "agent2_information_pending_consumption_check",
}

REQUIRED_INDEXES = {
    "agent2_semantic_admission_trace_scope_idx",
    "agent2_semantic_admission_trace_source_idx",
    "agent2_semantic_admission_decision_scope_idx",
    "agent2_semantic_admission_decision_trace_idx",
    "agent2_semantic_admission_ticket_decision_key",
    "agent2_semantic_admission_ticket_scope_idx",
    "agent2_semantic_review_scope_idx",
    "agent2_deferred_semantic_event_scope_idx",
    "agent2_information_pending_decision_key",
    "agent2_information_pending_scope_idx",
    "agent2_information_pending_one_active_action",
}

SCHEMA_RE = re.compile(r"^agent2_admission_gate_[a-z0-9_]{8,80}$")
URL_RE = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s]+", re.IGNORECASE)


class GateFailure(RuntimeError):
    """A deterministic verifier assertion failed."""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_tsv(stdout: str, width: int | None = None) -> list[list[str]]:
    rows = [line.split("\t") for line in stdout.splitlines() if line.strip()]
    if width is not None and any(len(row) != width for row in rows):
        raise GateFailure(f"unexpected psql row width; expected={width}")
    return rows


@dataclass(frozen=True)
class PgEnvironment:
    values: dict[str, str]
    secrets: tuple[str, ...]

    @classmethod
    def from_database_url(cls, database_url: str) -> "PgEnvironment":
        normalized = re.sub(
            r"^postgresql\+[a-z0-9_]+://",
            "postgresql://",
            database_url.strip(),
            count=1,
            flags=re.IGNORECASE,
        )
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise GateFailure("configured DATABASE_URL is not PostgreSQL")
        if not parsed.hostname or not parsed.username or not parsed.path.lstrip("/"):
            raise GateFailure("configured DATABASE_URL is incomplete")

        values = os.environ.copy()
        values.update(
            {
                "PGHOST": parsed.hostname,
                "PGPORT": str(parsed.port or 5432),
                "PGUSER": unquote(parsed.username),
                "PGDATABASE": unquote(parsed.path.lstrip("/")),
                "PGCONNECT_TIMEOUT": "10",
                "PGAPPNAME": "agent2_semantic_admission_isolated_gate",
            }
        )
        password = unquote(parsed.password or "")
        if password:
            values["PGPASSWORD"] = password

        query = parse_qs(parsed.query)
        ssl_mode = (query.get("sslmode") or query.get("ssl") or [""])[0]
        if ssl_mode:
            values["PGSSLMODE"] = {
                "true": "require",
                "false": "disable",
            }.get(ssl_mode.lower(), ssl_mode)

        secrets = tuple(secret for secret in (database_url, normalized, password) if secret)
        return cls(values=values, secrets=secrets)


class PostgresGate:
    def __init__(self, *, pg: PgEnvironment, schema: str, migration: Path) -> None:
        self.pg = pg
        self.schema = schema
        self.migration = migration

    def sanitize(self, text: str, limit: int = 2000) -> str:
        cleaned = text
        for secret in self.pg.secrets:
            cleaned = cleaned.replace(secret, "<redacted>")
        cleaned = URL_RE.sub("<redacted-postgresql-url>", cleaned)
        cleaned = cleaned.replace("\x00", "")
        return cleaned[-limit:]

    def environment(self, *, scoped: bool) -> dict[str, str]:
        env = self.pg.values.copy()
        if scoped:
            env["PGOPTIONS"] = f"-c search_path={self.schema},pg_catalog"
        else:
            env.pop("PGOPTIONS", None)
        return env

    @staticmethod
    def psql_args(*, tuples_only: bool = True) -> list[str]:
        args = [
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "VERBOSITY=verbose",
        ]
        if tuples_only:
            args.extend(["-A", "-t", "-F", "\t"])
        return args

    def run(
        self,
        *,
        sql: str | None = None,
        file: Path | None = None,
        scoped: bool = True,
        expect_success: bool = True,
        expected_sqlstate: str | None = None,
        timeout: int = 45,
    ) -> subprocess.CompletedProcess[str]:
        if (sql is None) == (file is None):
            raise ValueError("provide exactly one of sql or file")
        args = self.psql_args()
        if sql is not None:
            args.extend(["-c", sql])
        else:
            args.extend(["-f", str(file)])
        completed = subprocess.run(
            args,
            env=self.environment(scoped=scoped),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if expect_success and completed.returncode != 0:
            raise GateFailure(
                "psql command failed: " + self.sanitize(completed.stderr or completed.stdout)
            )
        if not expect_success:
            if completed.returncode == 0:
                raise GateFailure("invalid artifact unexpectedly committed")
            if expected_sqlstate and expected_sqlstate not in completed.stderr:
                raise GateFailure(
                    f"invalid artifact returned wrong SQLSTATE; expected={expected_sqlstate}; "
                    + self.sanitize(completed.stderr)
                )
        return completed

    def scalar(self, sql: str, *, scoped: bool = True) -> str:
        rows = [line.strip() for line in self.run(sql=sql, scoped=scoped).stdout.splitlines()]
        rows = [row for row in rows if row]
        if len(rows) != 1:
            raise GateFailure(f"expected one scalar row, got {len(rows)}")
        return rows[0]

    def catalog_snapshot(self, schema: str) -> dict[str, object]:
        schema_lit = sql_literal(schema)
        relations = parse_tsv(
            self.run(
                scoped=False,
                sql=f"""
                    SELECT c.relname, c.relkind
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = {schema_lit}
                      AND c.relkind IN ('r','p','v','m','S')
                    ORDER BY c.relname, c.relkind
                """,
            ).stdout,
            2,
        )
        columns = parse_tsv(
            self.run(
                scoped=False,
                sql=f"""
                    SELECT table_name, column_name, ordinal_position::text,
                           data_type, is_nullable, COALESCE(column_default, '')
                    FROM information_schema.columns
                    WHERE table_schema = {schema_lit}
                    ORDER BY table_name, ordinal_position
                """,
            ).stdout,
            6,
        )
        constraints = parse_tsv(
            self.run(
                scoped=False,
                sql=f"""
                    SELECT rel.relname, con.conname, con.contype,
                           COALESCE(ref_ns.nspname, ''), COALESCE(ref.relname, ''),
                           pg_get_constraintdef(con.oid, true)
                    FROM pg_constraint con
                    JOIN pg_class rel ON rel.oid = con.conrelid
                    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                    LEFT JOIN pg_class ref ON ref.oid = con.confrelid
                    LEFT JOIN pg_namespace ref_ns ON ref_ns.oid = ref.relnamespace
                    WHERE ns.nspname = {schema_lit}
                    ORDER BY rel.relname, con.conname
                """,
            ).stdout,
            6,
        )
        indexes = parse_tsv(
            self.run(
                scoped=False,
                sql=f"""
                    SELECT tablename, indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = {schema_lit}
                    ORDER BY tablename, indexname
                """,
            ).stdout,
            3,
        )
        comments = parse_tsv(
            self.run(
                scoped=False,
                sql=f"""
                    SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '')
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = {schema_lit} AND c.relkind IN ('r','p')
                    ORDER BY c.relname
                """,
            ).stdout,
            2,
        )
        canonical = {
            "relations": relations,
            "columns": columns,
            "constraints": constraints,
            "indexes": indexes,
            "comments": comments,
        }
        return {
            **canonical,
            "sha256": canonical_hash(canonical),
            "counts": {
                "relations": len(relations),
                "columns": len(columns),
                "constraints": len(constraints),
                "indexes": len(indexes),
            },
        }

    def assert_schema_contract(self, snapshot: dict[str, object]) -> dict[str, object]:
        relations = snapshot["relations"]
        constraints = snapshot["constraints"]
        indexes = snapshot["indexes"]
        assert isinstance(relations, list)
        assert isinstance(constraints, list)
        assert isinstance(indexes, list)

        table_names = {row[0] for row in relations if row[1] in {"r", "p"}}
        if table_names != EXPECTED_TABLES:
            raise GateFailure(
                f"six-table contract mismatch; expected={sorted(EXPECTED_TABLES)}; "
                f"actual={sorted(table_names)}"
            )

        constraint_names = {row[1] for row in constraints}
        missing_constraints = sorted(REQUIRED_CONSTRAINTS - constraint_names)
        if missing_constraints:
            raise GateFailure(f"missing required constraints: {missing_constraints}")

        primary_key_tables = {row[0] for row in constraints if row[2] == "p"}
        if primary_key_tables != EXPECTED_TABLES:
            raise GateFailure("every semantic-admission table must have a primary key")

        fk_rows = [row for row in constraints if row[2] == "f"]
        if len(fk_rows) != 12:
            raise GateFailure(f"expected 12 foreign keys, got {len(fk_rows)}")
        if any(row[3] != self.schema for row in fk_rows):
            raise GateFailure("a semantic-admission foreign key escapes the temporary schema")

        index_names = {row[1] for row in indexes}
        missing_indexes = sorted(REQUIRED_INDEXES - index_names)
        if missing_indexes:
            raise GateFailure(f"missing required indexes: {missing_indexes}")

        index_defs = {row[1]: row[2].lower() for row in indexes}
        active_index = index_defs["agent2_information_pending_one_active_action"]
        for fragment in ("unique", "pending_status", "active", "awaiting_input"):
            if fragment not in active_index:
                raise GateFailure("one-active InformationPending index is incomplete")

        definitions_by_table: dict[str, str] = {}
        for table, _name, _kind, _ref_schema, _ref_table, definition in constraints:
            definitions_by_table[table] = definitions_by_table.get(table, "") + " " + definition.lower()
        required_definition_fragments = {
            "agent2_semantic_admission_tickets": (
                "executor_revalidation_required",
                "not proves_business_write",
            ),
            "agent2_semantic_review_items": ("audit_only", "not business_write_allowed"),
            "agent2_deferred_semantic_events": (
                "audit_only",
                "not business_write_allowed",
                "requires_fresh_admission",
            ),
            "agent2_information_pendings": (
                "not business_write_allowed",
                "jsonb_array_length(missing_fields_json) > 0",
            ),
        }
        for table, fragments in required_definition_fragments.items():
            definition = definitions_by_table.get(table, "")
            if any(fragment not in definition for fragment in fragments):
                raise GateFailure(f"fail-closed check missing from {table}")

        return {
            "passed": True,
            "table_count": len(table_names),
            "constraint_count": len(constraints),
            "foreign_key_count": len(fk_rows),
            "index_count": len(indexes),
            "all_foreign_keys_scoped_to_temp_schema": True,
            "critical_fail_closed_checks_present": True,
        }

    def _id(self, label: str) -> uuid.UUID:
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"agent2-admission-gate:{self.schema}")
        return uuid.uuid5(namespace, label)

    def legal_roundtrip(self) -> dict[str, object]:
        tenant = f"gate_tenant_{sha256_text(self.schema)[:10]}"
        user = "synthetic_gate_user"
        conversation = "synthetic_gate_conversation"
        trace_ticket = self._id("trace-ticket")
        decision_ticket = self._id("decision-ticket")
        ticket = self._id("ticket")
        trace_pending = self._id("trace-pending")
        decision_pending = self._id("decision-pending")
        pending = self._id("pending")
        trace_audit = self._id("trace-audit")
        decision_audit = self._id("decision-audit")
        review = self._id("review")
        deferred = self._id("deferred")
        digest = sha256_text("synthetic-gate-fact")
        policy = "agent2.gate.policy.v1"
        issued_at = "2026-07-14 08:00:00+08"

        sql = f"""
        BEGIN;
        SET CONSTRAINTS ALL DEFERRED;

        INSERT INTO agent2_semantic_admission_traces
          (trace_id, tenant_id, user_id, conversation_id, source_turn_id,
           source_message_id, expected_conversation_state_version, proposal_sha256,
           policy_version, admission_mode, trace_status, admission_summary,
           failure_reason, idempotency_key)
        VALUES
          ('{trace_ticket}', {sql_literal(tenant)}, {sql_literal(user)}, {sql_literal(conversation)},
           'turn-ticket', 'message-ticket', 3, '{digest}', {sql_literal(policy)},
           'enforced', 'evaluated', 'admitted', '', 'legal:trace:ticket'),
          ('{trace_pending}', {sql_literal(tenant)}, {sql_literal(user)}, {sql_literal(conversation)},
           'turn-pending', 'message-pending', 3, '{digest}', {sql_literal(policy)},
           'enforced', 'evaluated', 'information_required', '', 'legal:trace:pending'),
          ('{trace_audit}', {sql_literal(tenant)}, {sql_literal(user)}, {sql_literal(conversation)},
           'turn-audit', 'message-audit', 3, '{digest}', {sql_literal(policy)},
           'shadow', 'evaluated', 'review_only', '', 'legal:trace:audit');

        INSERT INTO agent2_semantic_admission_decisions
          (decision_id, trace_id, tenant_id, user_id, conversation_id, source_turn_id,
           source_message_id, action_id, segment_id, segment_text_sha256,
           segment_start_offset, segment_end_offset, domain, operation,
           object_type, object_stable_id, object_version, object_label,
           expected_conversation_state_version, verdict, reason_code,
           evidence_refs_json, ticket_id, pending_id, idempotency_key)
        VALUES
          ('{decision_ticket}', '{trace_ticket}', {sql_literal(tenant)}, {sql_literal(user)},
           {sql_literal(conversation)}, 'turn-ticket', 'message-ticket', 'action-ticket',
           'segment-ticket', '{digest}', 0, 12, 'case', 'add_case_progress',
           'case', 'synthetic-case-1', 7, 'Synthetic case', 3, 'admitted',
           'policy_admitted', '[{{"kind":"synthetic"}}]'::jsonb, '{ticket}', NULL,
           'legal:decision:ticket'),
          ('{decision_pending}', '{trace_pending}', {sql_literal(tenant)}, {sql_literal(user)},
           {sql_literal(conversation)}, 'turn-pending', 'message-pending', 'action-pending',
           'segment-pending', '{digest}', 0, 12, 'travel', 'register_travel_intent',
           'travel_intent', 'synthetic-travel-1', 1, 'Synthetic travel', 3,
           'information_required', 'travel_date_required', '[]'::jsonb, NULL, '{pending}',
           'legal:decision:pending'),
          ('{decision_audit}', '{trace_audit}', {sql_literal(tenant)}, {sql_literal(user)},
           {sql_literal(conversation)}, 'turn-audit', 'message-audit', 'action-audit',
           'segment-audit', '{digest}', 0, 12, 'runtime', 'audit_ambiguous_segment',
           NULL, NULL, NULL, NULL, 3, 'review_only', 'human_review_required',
           '[]'::jsonb, NULL, NULL, 'legal:decision:audit');

        INSERT INTO agent2_semantic_admission_tickets
          (ticket_id, trace_id, decision_id, tenant_id, user_id, conversation_id,
           source_turn_id, source_message_id, action_id, segment_id, segment_text_sha256,
           segment_start_offset, segment_end_offset, domain, operation, object_type,
           object_stable_id, object_version, object_label,
           expected_conversation_state_version, authority_scope_json,
           allowed_changed_fields_json, fact_claims_sha256, authorized_command_sha256,
           policy_version, ticket_status, issued_at, expires_at, ttl_seconds,
           executor_revalidation_required, proves_business_write, idempotency_key)
        VALUES
          ('{ticket}', '{trace_ticket}', '{decision_ticket}', {sql_literal(tenant)},
           {sql_literal(user)}, {sql_literal(conversation)}, 'turn-ticket', 'message-ticket',
           'action-ticket', 'segment-ticket', '{digest}', 0, 12, 'case',
           'add_case_progress', 'case', 'synthetic-case-1', 7, 'Synthetic case', 3,
           '{{"tenant":"synthetic"}}'::jsonb, '["content"]'::jsonb, '{digest}', '{digest}',
           {sql_literal(policy)}, 'issued', TIMESTAMPTZ '{issued_at}',
           TIMESTAMPTZ '{issued_at}' + INTERVAL '300 seconds', 300, TRUE, FALSE,
           'legal:ticket');

        INSERT INTO agent2_information_pendings
          (pending_id, pending_type, trace_id, decision_id, tenant_id, user_id,
           conversation_id, source_turn_id, source_message_id, segment_id,
           segment_text_sha256, segment_start_offset, segment_end_offset, domain,
           operation, object_type, object_stable_id, object_version, object_label,
           expected_conversation_state_version, missing_fields_json,
           question_snapshot_json, acceptable_answer_forms_json, pending_status,
           created_at, expires_at, ttl_seconds, business_write_allowed, idempotency_key)
        VALUES
          ('{pending}', 'information', '{trace_pending}', '{decision_pending}',
           {sql_literal(tenant)}, {sql_literal(user)}, {sql_literal(conversation)},
           'turn-pending', 'message-pending', 'segment-pending', '{digest}', 0, 12,
           'travel', 'register_travel_intent', 'travel_intent', 'synthetic-travel-1',
           1, 'Synthetic travel', 3, '["travel_date"]'::jsonb,
           '{{"question":"synthetic"}}'::jsonb, '{{"forms":["date"]}}'::jsonb,
           'active', TIMESTAMPTZ '{issued_at}',
           TIMESTAMPTZ '{issued_at}' + INTERVAL '86400 seconds', 86400, FALSE,
           'legal:pending');

        INSERT INTO agent2_semantic_review_items
          (review_id, trace_id, decision_id, tenant_id, user_id, conversation_id,
           source_turn_id, source_message_id, segment_id, segment_text_sha256,
           segment_start_offset, segment_end_offset, domain, operation, reason_code,
           candidate_snapshot_json, resolution_json, audit_only,
           business_write_allowed, idempotency_key)
        VALUES
          ('{review}', '{trace_audit}', '{decision_audit}', {sql_literal(tenant)},
           {sql_literal(user)}, {sql_literal(conversation)}, 'turn-audit', 'message-audit',
           'segment-audit', '{digest}', 0, 12, 'runtime', 'audit_ambiguous_segment',
           'human_review_required', '{{"synthetic":true}}'::jsonb, '{{}}'::jsonb,
           TRUE, FALSE, 'legal:review');

        INSERT INTO agent2_deferred_semantic_events
          (deferred_event_id, trace_id, decision_id, tenant_id, user_id,
           conversation_id, source_turn_id, source_message_id, segment_id,
           segment_text_sha256, segment_start_offset, segment_end_offset, domain,
           operation, reason_code, event_status, payload_json, audit_only,
           business_write_allowed, requires_fresh_admission, idempotency_key)
        VALUES
          ('{deferred}', '{trace_audit}', '{decision_audit}', {sql_literal(tenant)},
           {sql_literal(user)}, {sql_literal(conversation)}, 'turn-audit', 'message-audit',
           'segment-audit', '{digest}', 0, 12, 'runtime', 'defer_ambiguous_segment',
           'shadow_only', 'recorded', '{{"synthetic":true}}'::jsonb, TRUE, FALSE,
           TRUE, 'legal:deferred');
        COMMIT;
        """
        self.run(sql=sql)

        table_union = " UNION ALL ".join(
            f"SELECT {sql_literal(table)}, count(*)::text FROM {table} "
            f"WHERE tenant_id = {sql_literal(tenant)}"
            for table in sorted(EXPECTED_TABLES)
        )
        counts = {row[0]: int(row[1]) for row in parse_tsv(self.run(sql=table_union).stdout, 2)}
        expected_counts = {
            "agent2_semantic_admission_traces": 3,
            "agent2_semantic_admission_decisions": 3,
            "agent2_semantic_admission_tickets": 1,
            "agent2_semantic_review_items": 1,
            "agent2_deferred_semantic_events": 1,
            "agent2_information_pendings": 1,
        }
        if counts != expected_counts:
            raise GateFailure(f"legal artifact roundtrip count mismatch: {counts}")

        invariant_count = int(
            self.scalar(
                f"""
                SELECT (
                    (SELECT count(*) FROM agent2_semantic_admission_decisions d
                     JOIN agent2_semantic_admission_tickets t
                       ON t.tenant_id=d.tenant_id AND t.decision_id=d.decision_id
                      AND t.ticket_id=d.ticket_id
                     WHERE d.tenant_id={sql_literal(tenant)}
                       AND d.verdict='admitted' AND t.ticket_status='issued'
                       AND t.executor_revalidation_required AND NOT t.proves_business_write)
                  + (SELECT count(*) FROM agent2_semantic_admission_decisions d
                     JOIN agent2_information_pendings p
                       ON p.tenant_id=d.tenant_id AND p.decision_id=d.decision_id
                      AND p.pending_id=d.pending_id
                     WHERE d.tenant_id={sql_literal(tenant)}
                       AND d.verdict='information_required' AND p.pending_status='active'
                       AND NOT p.business_write_allowed
                       AND p.missing_fields_json='["travel_date"]'::jsonb)
                  + (SELECT count(*) FROM agent2_semantic_review_items
                     WHERE tenant_id={sql_literal(tenant)} AND audit_only
                       AND NOT business_write_allowed
                       AND candidate_snapshot_json='{{"synthetic":true}}'::jsonb)
                  + (SELECT count(*) FROM agent2_deferred_semantic_events
                     WHERE tenant_id={sql_literal(tenant)} AND audit_only
                       AND NOT business_write_allowed AND requires_fresh_admission
                       AND payload_json='{{"synthetic":true}}'::jsonb)
                )::text
                """
            )
        )
        if invariant_count != 4:
            raise GateFailure("legal artifact roundtrip lost a contract field")

        return {
            "passed": True,
            "synthetic_only": True,
            "table_record_counts": counts,
            "decision_artifact_links_roundtripped": 2,
            "contract_invariant_groups_roundtripped": invariant_count,
            "uuid_versions": sorted(
                {
                    value.version
                    for value in (
                        trace_ticket,
                        decision_ticket,
                        ticket,
                        trace_pending,
                        decision_pending,
                        pending,
                        trace_audit,
                        decision_audit,
                        review,
                        deferred,
                    )
                }
            ),
            "tenant_ref_sha256": sha256_text(tenant),
        }

    def invalid_artifacts(self) -> dict[str, object]:
        tenant = f"gate_tenant_{sha256_text(self.schema)[:10]}"
        user = "synthetic_gate_user"
        conversation = "synthetic_gate_conversation"
        digest = sha256_text("synthetic-gate-fact")
        trace_ticket = self._id("trace-ticket")
        ticket = self._id("ticket")
        review = self._id("review")
        deferred = self._id("deferred")
        pending = self._id("pending")
        cases: list[tuple[str, str, str]] = []

        cases.append(
            (
                "uuid_v4_rejected",
                f"""
                INSERT INTO agent2_semantic_admission_traces
                  (trace_id, tenant_id, user_id, conversation_id, source_turn_id,
                   source_message_id, expected_conversation_state_version,
                   proposal_sha256, policy_version, admission_mode,
                   admission_summary, idempotency_key)
                VALUES ('00000000-0000-4000-8000-000000000001', {sql_literal(tenant)},
                        {sql_literal(user)}, {sql_literal(conversation)}, 'invalid-turn-uuid',
                        'invalid-message-uuid', 0, '{digest}', 'gate-policy', 'enforced',
                        'blocked', 'invalid:uuid');
                """,
                "23514",
            )
        )
        cases.extend(
            [
                (
                    "ticket_cannot_prove_business_write",
                    f"UPDATE agent2_semantic_admission_tickets SET proves_business_write=TRUE "
                    f"WHERE tenant_id={sql_literal(tenant)} AND ticket_id='{ticket}';",
                    "23514",
                ),
                (
                    "review_cannot_authorize_business_write",
                    f"UPDATE agent2_semantic_review_items SET business_write_allowed=TRUE "
                    f"WHERE tenant_id={sql_literal(tenant)} AND review_id='{review}';",
                    "23514",
                ),
                (
                    "deferred_requires_fresh_admission",
                    f"UPDATE agent2_deferred_semantic_events SET requires_fresh_admission=FALSE "
                    f"WHERE tenant_id={sql_literal(tenant)} AND deferred_event_id='{deferred}';",
                    "23514",
                ),
                (
                    "pending_missing_fields_cannot_be_empty",
                    f"UPDATE agent2_information_pendings SET missing_fields_json='[]'::jsonb "
                    f"WHERE tenant_id={sql_literal(tenant)} AND pending_id='{pending}';",
                    "23514",
                ),
            ]
        )

        wrong_tenant_decision = self._id("invalid-cross-tenant-decision")
        cases.append(
            (
                "cross_tenant_trace_reference_rejected",
                f"""
                INSERT INTO agent2_semantic_admission_decisions
                  (decision_id, trace_id, tenant_id, user_id, conversation_id,
                   source_turn_id, source_message_id, action_id, segment_id,
                   segment_text_sha256, segment_start_offset, segment_end_offset,
                   domain, operation, expected_conversation_state_version, verdict,
                   reason_code, idempotency_key)
                VALUES ('{wrong_tenant_decision}', '{trace_ticket}', 'synthetic_other_tenant',
                        {sql_literal(user)}, {sql_literal(conversation)}, 'invalid-turn-tenant',
                        'invalid-message-tenant', 'invalid-action', 'invalid-segment',
                        '{digest}', 0, 1, 'runtime', 'blocked_probe', 0, 'blocked',
                        'cross_tenant_probe', 'invalid:cross-tenant');
                """,
                "23503",
            )
        )

        missing_ticket_decision = self._id("invalid-missing-ticket-decision")
        cases.append(
            (
                "admitted_write_without_ticket_rejected",
                f"""
                INSERT INTO agent2_semantic_admission_decisions
                  (decision_id, trace_id, tenant_id, user_id, conversation_id,
                   source_turn_id, source_message_id, action_id, segment_id,
                   segment_text_sha256, segment_start_offset, segment_end_offset,
                   domain, operation, object_type, object_stable_id,
                   expected_conversation_state_version, verdict, reason_code,
                   idempotency_key)
                VALUES ('{missing_ticket_decision}', '{trace_ticket}', {sql_literal(tenant)},
                        {sql_literal(user)}, {sql_literal(conversation)},
                        'invalid-turn-ticket', 'invalid-message-ticket', 'invalid-action',
                        'invalid-segment', '{digest}', 0, 1, 'case', 'add_case_progress',
                        'case', 'synthetic-case-1', 0, 'admitted', 'missing_ticket_probe',
                        'invalid:missing-ticket');
                """,
                "23514",
            )
        )

        duplicate_trace = self._id("invalid-duplicate-active-trace")
        duplicate_decision = self._id("invalid-duplicate-active-decision")
        duplicate_pending = self._id("invalid-duplicate-active-pending")
        cases.append(
            (
                "second_active_pending_for_same_action_rejected",
                f"""
                BEGIN;
                INSERT INTO agent2_semantic_admission_traces
                  (trace_id, tenant_id, user_id, conversation_id, source_turn_id,
                   source_message_id, expected_conversation_state_version,
                   proposal_sha256, policy_version, admission_mode,
                   admission_summary, idempotency_key)
                VALUES ('{duplicate_trace}', {sql_literal(tenant)}, {sql_literal(user)},
                        {sql_literal(conversation)}, 'invalid-turn-duplicate',
                        'invalid-message-duplicate', 3, '{digest}', 'gate-policy', 'enforced',
                        'blocked', 'invalid:duplicate:trace');
                INSERT INTO agent2_semantic_admission_decisions
                  (decision_id, trace_id, tenant_id, user_id, conversation_id,
                   source_turn_id, source_message_id, action_id, segment_id,
                   segment_text_sha256, segment_start_offset, segment_end_offset,
                   domain, operation, object_type, object_stable_id, object_version,
                   object_label, expected_conversation_state_version, verdict,
                   reason_code, idempotency_key)
                VALUES ('{duplicate_decision}', '{duplicate_trace}', {sql_literal(tenant)},
                        {sql_literal(user)}, {sql_literal(conversation)},
                        'invalid-turn-duplicate', 'invalid-message-duplicate',
                        'invalid-action-duplicate', 'invalid-segment-duplicate', '{digest}',
                        0, 1, 'travel', 'register_travel_intent', 'travel_intent',
                        'synthetic-travel-1', 1, 'Synthetic travel', 3, 'blocked',
                        'duplicate_probe', 'invalid:duplicate:decision');
                INSERT INTO agent2_information_pendings
                  (pending_id, trace_id, decision_id, tenant_id, user_id,
                   conversation_id, source_turn_id, source_message_id, segment_id,
                   segment_text_sha256, segment_start_offset, segment_end_offset,
                   domain, operation, object_type, object_stable_id, object_version,
                   object_label, expected_conversation_state_version,
                   missing_fields_json, question_snapshot_json,
                   acceptable_answer_forms_json, pending_status, created_at,
                   expires_at, ttl_seconds, idempotency_key)
                VALUES ('{duplicate_pending}', '{duplicate_trace}', '{duplicate_decision}',
                        {sql_literal(tenant)}, {sql_literal(user)}, {sql_literal(conversation)},
                        'invalid-turn-duplicate', 'invalid-message-duplicate',
                        'invalid-segment-duplicate', '{digest}', 0, 1, 'travel',
                        'register_travel_intent', 'travel_intent', 'synthetic-travel-1',
                        1, 'Synthetic travel', 3, '["travel_date"]'::jsonb, '{{}}'::jsonb,
                        '{{}}'::jsonb, 'active', TIMESTAMPTZ '2026-07-14 08:00:00+08',
                        TIMESTAMPTZ '2026-07-15 08:00:00+08', 86400,
                        'invalid:duplicate:pending');
                COMMIT;
                """,
                "23505",
            )
        )

        results = []
        for name, statement, sqlstate in cases:
            completed = self.run(
                sql=statement,
                expect_success=False,
                expected_sqlstate=sqlstate,
            )
            results.append(
                {
                    "name": name,
                    "passed": True,
                    "sqlstate": sqlstate,
                    "psql_exit_code": completed.returncode,
                    "actual_write": False,
                }
            )

        residue = int(
            self.scalar(
                f"""
                SELECT (
                  (SELECT count(*) FROM agent2_semantic_admission_traces
                   WHERE tenant_id={sql_literal(tenant)} AND idempotency_key LIKE 'invalid:%')
                + (SELECT count(*) FROM agent2_semantic_admission_decisions
                   WHERE tenant_id={sql_literal(tenant)} AND idempotency_key LIKE 'invalid:%')
                + (SELECT count(*) FROM agent2_information_pendings
                   WHERE tenant_id={sql_literal(tenant)} AND idempotency_key LIKE 'invalid:%')
                )::text
                """
            )
        )
        guards_intact = self.scalar(
            f"""
            SELECT (
                NOT (SELECT proves_business_write FROM agent2_semantic_admission_tickets
                     WHERE tenant_id={sql_literal(tenant)} AND ticket_id='{ticket}')
                AND NOT (SELECT business_write_allowed FROM agent2_semantic_review_items
                         WHERE tenant_id={sql_literal(tenant)} AND review_id='{review}')
                AND (SELECT requires_fresh_admission FROM agent2_deferred_semantic_events
                     WHERE tenant_id={sql_literal(tenant)} AND deferred_event_id='{deferred}')
                AND (SELECT jsonb_array_length(missing_fields_json)=1
                     FROM agent2_information_pendings
                     WHERE tenant_id={sql_literal(tenant)} AND pending_id='{pending}')
            )::text
            """
        )
        if residue != 0 or guards_intact != "true":
            raise GateFailure("an invalid artifact left residue or changed a legal artifact")
        return {
            "passed": True,
            "case_count": len(results),
            "cases": results,
            "invalid_row_residue": residue,
            "existing_artifact_guards_unchanged": True,
        }

    def transaction_rollback(self) -> dict[str, object]:
        tenant = f"gate_tenant_{sha256_text(self.schema)[:10]}"
        trace_id = self._id("rollback-trace")
        digest = sha256_text("rollback-probe")
        self.run(
            sql=f"""
            BEGIN;
            INSERT INTO agent2_semantic_admission_traces
              (trace_id, tenant_id, user_id, conversation_id, source_turn_id,
               source_message_id, expected_conversation_state_version,
               proposal_sha256, policy_version, admission_mode, admission_summary,
               idempotency_key)
            VALUES ('{trace_id}', {sql_literal(tenant)}, 'synthetic_gate_user',
                    'synthetic_gate_conversation', 'rollback-turn', 'rollback-message',
                    0, '{digest}', 'gate-policy', 'enforced', 'blocked', 'rollback:trace');
            ROLLBACK;
            """
        )
        count = int(
            self.scalar(
                f"SELECT count(*)::text FROM agent2_semantic_admission_traces "
                f"WHERE tenant_id={sql_literal(tenant)} AND idempotency_key='rollback:trace'"
            )
        )
        if count != 0:
            raise GateFailure("explicit transaction rollback left a row")
        return {"passed": True, "actual_write_after_rollback": count}

    def concurrent_idempotency(self) -> dict[str, object]:
        tenant = f"gate_tenant_{sha256_text(self.schema)[:10]}"
        digest = sha256_text("concurrent-idempotency-probe")
        ids = [self._id("concurrent-trace-a"), self._id("concurrent-trace-b")]

        def statement(trace_id: uuid.UUID, suffix: str) -> str:
            return f"""
            BEGIN;
            SELECT pg_sleep(0.25);
            WITH inserted AS (
              INSERT INTO agent2_semantic_admission_traces
                (trace_id, tenant_id, user_id, conversation_id, source_turn_id,
                 source_message_id, expected_conversation_state_version,
                 proposal_sha256, policy_version, admission_mode, admission_summary,
                 idempotency_key)
              VALUES ('{trace_id}', {sql_literal(tenant)}, 'synthetic_gate_user',
                      'synthetic_gate_conversation', 'concurrent-turn-{suffix}',
                      'concurrent-message-{suffix}', 0, '{digest}', 'gate-policy',
                      'enforced', 'blocked', 'concurrent:same-idempotency-key')
              ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
              RETURNING 1
            )
            SELECT COALESCE((SELECT sum(1) FROM inserted), 0);
            SELECT pg_sleep(0.50);
            COMMIT;
            """

        processes: list[subprocess.Popen[str]] = []
        for trace_id, suffix in zip(ids, ("a", "b"), strict=True):
            processes.append(
                subprocess.Popen(
                    self.psql_args(),
                    env=self.environment(scoped=True),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        outputs: list[int] = []
        for process, trace_id, suffix in zip(processes, ids, ("a", "b"), strict=True):
            stdout, stderr = process.communicate(statement(trace_id, suffix), timeout=30)
            if process.returncode != 0:
                raise GateFailure("concurrent psql failed: " + self.sanitize(stderr or stdout))
            numeric_lines = [int(line) for line in stdout.splitlines() if line.strip() in {"0", "1"}]
            if len(numeric_lines) != 1:
                raise GateFailure("concurrent psql did not return one insert indicator")
            outputs.append(numeric_lines[0])

        final_count = int(
            self.scalar(
                f"SELECT count(*)::text FROM agent2_semantic_admission_traces "
                f"WHERE tenant_id={sql_literal(tenant)} "
                "AND idempotency_key='concurrent:same-idempotency-key'"
            )
        )
        if sorted(outputs) != [0, 1] or final_count != 1:
            raise GateFailure(
                f"concurrent idempotency failed; insert_indicators={outputs}; rows={final_count}"
            )
        return {
            "passed": True,
            "parallel_connection_count": 2,
            "insert_indicators": sorted(outputs),
            "committed_row_count": final_count,
            "duplicate_business_artifacts": 0,
        }


def public_summary(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "sha256": snapshot["sha256"],
        "counts": snapshot["counts"],
    }


def render_markdown(result: dict[str, object]) -> str:
    checks = result.get("checks", {})
    assert isinstance(checks, dict)
    migration = checks.get("migration", {})
    schema_contract = checks.get("schema_contract", {})
    legal = checks.get("legal_artifact_roundtrip", {})
    invalid = checks.get("invalid_artifacts_fail_closed", {})
    rollback = checks.get("transaction_rollback", {})
    concurrent = checks.get("concurrent_idempotency", {})
    cleanup = result.get("cleanup", {})
    public = checks.get("public_catalog_unchanged", {})

    def passed(value: object) -> str:
        return "PASS" if isinstance(value, dict) and value.get("passed") else "FAIL"

    lines = [
        "# Agent2 Semantic Admission PostgreSQL Isolated Gate",
        "",
        f"- Final status: `{result.get('status', 'FAIL')}`",
        f"- Run ID: `{result.get('run_id', '')}`",
        f"- Started (UTC): `{result.get('started_at', '')}`",
        f"- Finished (UTC): `{result.get('finished_at', '')}`",
        f"- Migration SHA-256: `{result.get('migration_sha256', '')}`",
        f"- Verifier SHA-256: `{result.get('verifier_sha256', '')}`",
        f"- Execution transport: `{result.get('execution_transport', '')}`",
        f"- Temporary schema: `{result.get('temporary_schema', '')}`",
        f"- Cleanup confirmed: `{bool(isinstance(cleanup, dict) and cleanup.get('confirmed'))}`",
        "",
        "## Reproducible gate results",
        "",
        "| Gate | Result | Reproducible evidence |",
        "|---|---:|---|",
        f"| Migration first apply | {passed(migration.get('first_apply') if isinstance(migration, dict) else {})} | transaction completed |",
        f"| Migration idempotent re-apply | {passed(migration.get('second_apply') if isinstance(migration, dict) else {})} | canonical catalog hashes equal |",
        f"| Six tables and constraints | {passed(schema_contract)} | tables={schema_contract.get('table_count', 0) if isinstance(schema_contract, dict) else 0}; FKs={schema_contract.get('foreign_key_count', 0) if isinstance(schema_contract, dict) else 0} |",
        f"| Legal artifact roundtrip | {passed(legal)} | synthetic records across all six tables |",
        f"| Illegal artifacts fail closed | {passed(invalid)} | cases={invalid.get('case_count', 0) if isinstance(invalid, dict) else 0}; residue={invalid.get('invalid_row_residue', 'n/a') if isinstance(invalid, dict) else 'n/a'} |",
        f"| Transaction rollback | {passed(rollback)} | remaining rows={rollback.get('actual_write_after_rollback', 'n/a') if isinstance(rollback, dict) else 'n/a'} |",
        f"| Concurrent idempotency | {passed(concurrent)} | committed rows={concurrent.get('committed_row_count', 'n/a') if isinstance(concurrent, dict) else 'n/a'} |",
        f"| Public catalog unchanged | {passed(public)} | metadata fingerprint before/after equal |",
        f"| Exact temporary-schema cleanup | {passed(cleanup)} | schema absent after finally |",
        "",
        "## Safety boundary",
        "",
        "- The verifier used only synthetic identifiers and digests.",
        "- No business row, production user identity, message, configuration, or service was written.",
        "- `public` was read only for catalog metadata fingerprinting; the migration search path was the unique temporary schema followed by `pg_catalog`.",
        "- This database gate does not claim a user-facing Agent2 end-to-end pass.",
        "",
        "## Reproduce",
        "",
        "Run this verifier with a new allow-listed `agent2_admission_gate_<unique>` schema name, the unchanged migration, and the server `.env`; pass `--execution-transport ssh` when it is invoked through the isolated SSH session. The verifier refuses a pre-existing schema and always performs exact cleanup in `finally`.",
    ]
    if result.get("failure"):
        lines.extend(["", "## Failure", "", f"`{result['failure']}`"])
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict[str, object], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(render_markdown(result), encoding="utf-8")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument(
        "--execution-transport",
        choices=("local", "ssh", "unspecified"),
        default="unspecified",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not SCHEMA_RE.fullmatch(args.schema):
        raise SystemExit("temporary schema name does not match the safety allow-list")
    if not args.migration.is_file():
        raise SystemExit("migration file not found")
    if not args.env_file.is_file():
        raise SystemExit("environment file not found")

    values = dotenv_values(args.env_file)
    database_url = values.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")
    pg = PgEnvironment.from_database_url(database_url)
    gate = PostgresGate(pg=pg, schema=args.schema, migration=args.migration)
    run_id = args.schema.removeprefix("agent2_admission_gate_")
    result: dict[str, object] = {
        "artifact_version": "agent2.semantic_admission.postgres_gate.v1",
        "run_id": run_id,
        "status": "FAIL",
        "started_at": utc_now(),
        "finished_at": None,
        "database_target": "remote_configured_postgresql_redacted",
        "execution_transport": args.execution_transport,
        "migration_sha256": sha256_bytes(args.migration.read_bytes()),
        "verifier_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "temporary_schema": args.schema,
        "checks": {},
        "cleanup": {"passed": False, "confirmed": False},
    }
    checks = result["checks"]
    assert isinstance(checks, dict)
    schema_created = False
    public_before: dict[str, object] | None = None

    try:
        public_before = gate.catalog_snapshot("public")
        existing = int(
            gate.scalar(
                f"SELECT count(*)::text FROM pg_namespace WHERE nspname={sql_literal(args.schema)}",
                scoped=False,
            )
        )
        if existing:
            raise GateFailure("refusing to reuse or drop a pre-existing schema")

        gate.run(sql=f"CREATE SCHEMA {quote_ident(args.schema)}", scoped=False)
        schema_created = True
        search_path = gate.scalar("SHOW search_path")
        if search_path.replace(" ", "") != f"{args.schema},pg_catalog":
            raise GateFailure(f"unexpected migration search_path: {search_path}")

        gate.run(file=args.migration)
        first_snapshot = gate.catalog_snapshot(args.schema)
        gate.assert_schema_contract(first_snapshot)
        gate.run(file=args.migration)
        second_snapshot = gate.catalog_snapshot(args.schema)
        second_contract = gate.assert_schema_contract(second_snapshot)
        same_catalog = first_snapshot["sha256"] == second_snapshot["sha256"]
        if not same_catalog:
            raise GateFailure("second migration apply changed the canonical catalog")
        checks["migration"] = {
            "passed": True,
            "first_apply": {"passed": True, "catalog_sha256": first_snapshot["sha256"]},
            "second_apply": {"passed": True, "catalog_sha256": second_snapshot["sha256"]},
            "idempotent_catalog": same_catalog,
        }
        checks["schema_contract"] = second_contract
        checks["legal_artifact_roundtrip"] = gate.legal_roundtrip()
        checks["invalid_artifacts_fail_closed"] = gate.invalid_artifacts()
        checks["transaction_rollback"] = gate.transaction_rollback()
        checks["concurrent_idempotency"] = gate.concurrent_idempotency()
        result["status"] = "PASS"
    except Exception as exc:  # evidence must survive every deterministic gate failure
        result["failure"] = gate.sanitize(f"{type(exc).__name__}: {exc}")
        result["status"] = "FAIL"
    finally:
        cleanup = result["cleanup"]
        assert isinstance(cleanup, dict)
        if schema_created:
            try:
                gate.run(
                    sql=f"DROP SCHEMA {quote_ident(args.schema)} CASCADE",
                    scoped=False,
                )
                remains = int(
                    gate.scalar(
                        f"SELECT count(*)::text FROM pg_namespace "
                        f"WHERE nspname={sql_literal(args.schema)}",
                        scoped=False,
                    )
                )
                cleanup.update(
                    {
                        "passed": remains == 0,
                        "confirmed": remains == 0,
                        "remaining_schema_count": remains,
                    }
                )
                if remains:
                    result["status"] = "FAIL"
            except Exception as cleanup_exc:
                cleanup.update(
                    {
                        "passed": False,
                        "confirmed": False,
                        "failure": gate.sanitize(str(cleanup_exc)),
                    }
                )
                result["status"] = "FAIL"
        else:
            cleanup.update(
                {
                    "passed": True,
                    "confirmed": True,
                    "remaining_schema_count": 0,
                    "note": "schema was never created",
                }
            )

        if public_before is not None:
            try:
                public_after = gate.catalog_snapshot("public")
                unchanged = public_before["sha256"] == public_after["sha256"]
                checks["public_catalog_unchanged"] = {
                    "passed": unchanged,
                    "before": public_summary(public_before),
                    "after": public_summary(public_after),
                }
                if not unchanged:
                    result["status"] = "FAIL"
                    result.setdefault(
                        "failure",
                        "public catalog metadata fingerprint changed during isolated gate",
                    )
            except Exception as public_exc:
                checks["public_catalog_unchanged"] = {
                    "passed": False,
                    "failure": gate.sanitize(str(public_exc)),
                }
                result["status"] = "FAIL"

        result["finished_at"] = utc_now()
        write_outputs(result, args.json_output, args.markdown_output)

    print(
        json.dumps(
            {
                "status": result["status"],
                "run_id": run_id,
                "cleanup_confirmed": result["cleanup"].get("confirmed"),
                "json_output": str(args.json_output),
                "markdown_output": str(args.markdown_output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
