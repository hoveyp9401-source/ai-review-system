from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    CompletePreviousPlanArgs,
    ConfirmReportArgs,
    CopyPreviousToTodayArgs,
    DeleteDailyItemsArgs,
    EditDailyItemsArgs,
    MoveDailyItemsArgs,
    QueryReportByDateArgs,
    ReceiptStatus,
    RequestClearReportArgs,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.sandbox_contracts import (
    SandboxExecutionContext,
)
from app.agent2.tool_calling.sandbox_handlers import SandboxHandlerRequest
from app.agent2.tool_calling.sandbox_store import SandboxExecutionSession


class SandboxExecutionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SandboxDateResolverPort(Protocol):
    def resolve(
        self,
        *,
        expression: str,
        proposed_date: date,
        context: SandboxExecutionContext,
    ) -> date: ...


class SealedDateResolver:
    """Server-owned date bindings used by sealed Sandbox acceptance calls."""

    def __init__(self, bindings: Mapping[str, date]) -> None:
        self._bindings = dict(bindings)

    def resolve(
        self,
        *,
        expression: str,
        proposed_date: date,
        context: SandboxExecutionContext,
    ) -> date:
        del proposed_date, context
        resolved = self._bindings.get(expression)
        if resolved is None:
            raise SandboxExecutionError("DATE_EXPRESSION_UNRESOLVED")
        return resolved


@dataclass(frozen=True)
class SandboxHandlerOutcome:
    target_type: str
    target_id: str
    idempotency_key: str | None
    status_if_unchanged: ReceiptStatus = ReceiptStatus.NO_OP


class SandboxDailyExecutor:
    def __init__(
        self,
        *,
        session: SandboxExecutionSession,
        context: SandboxExecutionContext,
        date_resolver: SandboxDateResolverPort,
    ) -> None:
        self._session = session
        self._context = context
        self._date_resolver = date_resolver

    async def query_today_report(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        report_date = self._today()
        report_id = self._report_id(report_date)
        report = await self._owned_report(report_id, required=False)
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=report_id,
            idempotency_key=None,
            status_if_unchanged=(
                ReceiptStatus.SUCCESS if report is not None else ReceiptStatus.NO_OP
            ),
        )

    async def query_report_by_date(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, QueryReportByDateArgs)
        report_date = self._resolve(
            arguments.date_expression,
            arguments.proposed_date,
        )
        report_id = self._report_id(report_date)
        report = await self._owned_report(report_id, required=False)
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=report_id,
            idempotency_key=None,
            status_if_unchanged=(
                ReceiptStatus.SUCCESS if report is not None else ReceiptStatus.NO_OP
            ),
        )

    async def add_daily_items(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, AddDailyItemsArgs)
        report_date = self._resolve(
            arguments.date_expression,
            arguments.proposed_date,
        )
        report_id = self._report_id(report_date)
        report = await self._owned_report(report_id, required=False)
        before_version = int(report["version"]) if report is not None else 0
        key = self._write_key(
            request,
            target_object=f"daily_report:{report_id}",
            expected_version=before_version,
        )
        existing_items = await self._items_for_report(report_id)
        existing_content = {
            (str(item["field"]), str(item["content"])) for item in existing_items
        }
        additions = []
        seen_content = set(existing_content)
        for item in arguments.items:
            content_key = (item.field, item.content)
            if content_key in seen_content:
                continue
            additions.append(item)
            seen_content.add(content_key)
        if additions:
            if report is None:
                report = self._new_report(report_id, report_date)
            next_positions = self._next_positions(existing_items)
            for index, item in enumerate(additions):
                item_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        (
                            f"agent2-sandbox-item:{key}:{index}:"
                            f"{item.field}:{item.content}"
                        ),
                    )
                )
                await self._session.upsert_record(
                    "daily_items",
                    item_id,
                    {
                        "item_id": item_id,
                        "report_id": report_id,
                        "tenant_id": self._context.tenant_id,
                        "user_id": str(self._context.user_id),
                        "field": item.field,
                        "content": item.content,
                        "position": next_positions[item.field],
                        "provenance": {
                            "kind": "sandbox_direct_add",
                            "source_message_id": self._context.source_message_id,
                        },
                        "created_at": self._now(),
                        "updated_at": self._now(),
                    },
                    create_only=True,
                )
                next_positions[item.field] += 1
            await self._save_report(
                report,
                version=before_version + 1,
            )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=report_id,
            idempotency_key=key,
        )

    async def edit_daily_items(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, EditDailyItemsArgs)
        report = await self._versioned_report(
            str(arguments.report_id),
            arguments.expected_version,
        )
        targets = await self._bound_items(
            str(arguments.report_id),
            arguments.target_item_ids,
        )
        target_ids = {str(item["item_id"]) for item in targets}
        all_items = await self._items_for_report(str(arguments.report_id))
        if any(
            str(item["item_id"]) not in target_ids
            and str(item["content"]) == arguments.replacement
            and str(item["field"]) == str(targets[0]["field"])
            for item in all_items
        ):
            raise SandboxExecutionError("DUPLICATE_ITEM_CONTENT")
        key = self._write_key(
            request,
            target_object=f"daily_report:{arguments.report_id}",
            expected_version=arguments.expected_version,
        )
        changed = False
        for item in targets:
            if str(item["content"]) == arguments.replacement:
                continue
            updated = {
                **item,
                "content": arguments.replacement,
                "updated_at": self._now(),
            }
            await self._session.upsert_record(
                "daily_items",
                str(item["item_id"]),
                updated,
            )
            changed = True
        if changed:
            await self._save_report(
                report,
                version=arguments.expected_version + 1,
            )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=str(arguments.report_id),
            idempotency_key=key,
        )

    async def delete_daily_items(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, DeleteDailyItemsArgs)
        report = await self._versioned_report(
            str(arguments.report_id),
            arguments.expected_version,
        )
        targets = await self._bound_items(
            str(arguments.report_id),
            arguments.target_item_ids,
        )
        key = self._write_key(
            request,
            target_object=f"daily_report:{arguments.report_id}",
            expected_version=arguments.expected_version,
        )
        for item in targets:
            if not await self._session.delete_record(
                "daily_items",
                str(item["item_id"]),
            ):
                raise SandboxExecutionError("ITEM_DELETE_RACE")
        await self._save_report(
            report,
            version=arguments.expected_version + 1,
        )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=str(arguments.report_id),
            idempotency_key=key,
        )

    async def move_daily_items(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, MoveDailyItemsArgs)
        report = await self._versioned_report(
            str(arguments.report_id),
            arguments.expected_version,
        )
        targets = await self._bound_items(
            str(arguments.report_id),
            arguments.target_item_ids,
        )
        if any(str(item["field"]) != arguments.source_field for item in targets):
            raise SandboxExecutionError("ITEM_SOURCE_FIELD_MISMATCH")
        key = self._write_key(
            request,
            target_object=f"daily_report:{arguments.report_id}",
            expected_version=arguments.expected_version,
        )
        target_position = max(
            (
                int(item.get("position", 0))
                for item in await self._items_for_report(
                    str(arguments.report_id)
                )
                if item["field"] == arguments.target_field
            ),
            default=0,
        )
        for offset, item in enumerate(targets, start=1):
            await self._session.upsert_record(
                "daily_items",
                str(item["item_id"]),
                {
                    **item,
                    "field": arguments.target_field,
                    "position": target_position + offset,
                    "updated_at": self._now(),
                },
            )
        await self._save_report(
            report,
            version=arguments.expected_version + 1,
        )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=str(arguments.report_id),
            idempotency_key=key,
        )

    async def copy_previous_to_today(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, CopyPreviousToTodayArgs)
        source_date = self._resolve(
            arguments.source_date_expression,
            arguments.proposed_source_date,
        )
        source_id = self._report_id(source_date)
        source = await self._owned_report(source_id, required=True)
        source_version = int(source["version"])
        target_date = self._today()
        target_id = self._report_id(target_date)
        target = await self._owned_report(target_id, required=False)
        target_version = int(target["version"]) if target is not None else 0
        key = self._write_key(
            request,
            target_object=f"daily_report:{target_id}",
            expected_version=target_version,
        )
        source_items = await self._items_for_report(source_id)
        target_items = await self._items_for_report(target_id)
        existing = {
            (str(item["field"]), str(item["content"])) for item in target_items
        }
        additions = [
            item
            for item in source_items
            if (str(item["field"]), str(item["content"])) not in existing
        ]
        if additions:
            if target is None:
                target = self._new_report(target_id, target_date)
            next_positions = self._next_positions(target_items)
            for index, source_item in enumerate(additions):
                item_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"agent2-sandbox-copy:{key}:{index}:{source_item['item_id']}",
                    )
                )
                await self._session.upsert_record(
                    "daily_items",
                    item_id,
                    {
                        "item_id": item_id,
                        "report_id": target_id,
                        "tenant_id": self._context.tenant_id,
                        "user_id": str(self._context.user_id),
                        "field": source_item["field"],
                        "content": source_item["content"],
                        "position": next_positions[str(source_item["field"])],
                        "provenance": {
                            "kind": "sandbox_copy_previous",
                            "source_report_id": source_id,
                            "source_report_version": source_version,
                            "source_item_id": source_item["item_id"],
                        },
                        "created_at": self._now(),
                        "updated_at": self._now(),
                    },
                    create_only=True,
                )
                next_positions[str(source_item["field"])] += 1
            await self._save_report(target, version=target_version + 1)
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=target_id,
            idempotency_key=key,
        )

    async def complete_previous_plan(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, CompletePreviousPlanArgs)
        source_date = self._resolve(
            arguments.source_date_expression,
            arguments.proposed_source_date,
        )
        source_id = str(arguments.report_id)
        if source_id != self._report_id(source_date):
            raise SandboxExecutionError("SOURCE_REPORT_DATE_MISMATCH")
        await self._versioned_report(source_id, arguments.expected_version)
        source_items = await self._bound_items(
            source_id,
            arguments.target_item_ids,
        )
        if any(str(item["field"]) != "tomorrow_plan" for item in source_items):
            raise SandboxExecutionError("SOURCE_ITEM_NOT_PREVIOUS_PLAN")
        target_date = self._today()
        target_id = self._report_id(target_date)
        target = await self._owned_report(target_id, required=False)
        target_version = int(target["version"]) if target is not None else 0
        key = self._write_key(
            request,
            target_object=f"daily_report:{target_id}",
            expected_version=target_version,
        )
        existing = {
            str(item["content"]) for item in await self._items_for_report(target_id)
            if str(item["field"]) == "today_work"
        }
        additions = [
            item for item in source_items if str(item["content"]) not in existing
        ]
        if additions:
            if target is None:
                target = self._new_report(target_id, target_date)
            next_positions = self._next_positions(
                await self._items_for_report(target_id)
            )
            for index, source_item in enumerate(additions):
                item_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        (
                            f"agent2-sandbox-complete:{key}:{index}:"
                            f"{source_item['item_id']}"
                        ),
                    )
                )
                await self._session.upsert_record(
                    "daily_items",
                    item_id,
                    {
                        "item_id": item_id,
                        "report_id": target_id,
                        "tenant_id": self._context.tenant_id,
                        "user_id": str(self._context.user_id),
                        "field": "today_work",
                        "content": source_item["content"],
                        "position": next_positions["today_work"],
                        "provenance": {
                            "kind": "sandbox_complete_previous_plan",
                            "source_report_id": source_id,
                            "source_report_version": arguments.expected_version,
                            "source_item_id": source_item["item_id"],
                        },
                        "created_at": self._now(),
                        "updated_at": self._now(),
                    },
                    create_only=True,
                )
                next_positions["today_work"] += 1
            await self._save_report(target, version=target_version + 1)
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=target_id,
            idempotency_key=key,
        )

    async def confirm_report(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, ConfirmReportArgs)
        report = await self._versioned_report(
            str(arguments.report_id),
            arguments.expected_version,
        )
        key = self._write_key(
            request,
            target_object=f"daily_report:{arguments.report_id}",
            expected_version=arguments.expected_version,
        )
        if str(report["status"]) != "completed":
            await self._save_report(
                {**report, "status": "completed"},
                version=arguments.expected_version + 1,
            )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=str(arguments.report_id),
            idempotency_key=key,
        )

    async def request_clear_report(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(request, RequestClearReportArgs)
        report = await self._versioned_report(
            str(arguments.report_id),
            arguments.expected_version,
        )
        key = self._write_key(
            request,
            target_object=f"daily_report:{arguments.report_id}",
            expected_version=arguments.expected_version,
        )
        active = [
            pending
            for pending in await self._session.read_table_rows("sandbox_pending")
            if self._pending_matches_scope(pending)
            and pending.get("consumed_at") is None
            and str(pending.get("report_id")) == str(arguments.report_id)
            and self._pending_unexpired(pending)
        ]
        if active:
            if len(active) != 1:
                raise SandboxExecutionError("CLEAR_PENDING_NOT_UNIQUE")
            if int(active[0]["report_version"]) != arguments.expected_version:
                raise SandboxExecutionError("CLEAR_PENDING_VERSION_CONFLICT")
            pending_id = str(active[0]["pending_id"])
        else:
            if request.pending_ttl_seconds is None:
                raise SandboxExecutionError("PENDING_TTL_MISSING")
            pending_id = str(
                uuid5(NAMESPACE_URL, f"agent2-sandbox-pending:{key}")
            )
            await self._session.upsert_record(
                "sandbox_pending",
                pending_id,
                {
                    "pending_id": pending_id,
                    "namespace": "agent2.tool_calling.sandbox.v1",
                    "tenant_id": self._context.tenant_id,
                    "user_id": str(self._context.user_id),
                    "conversation_id": self._context.conversation_id,
                    "report_id": str(arguments.report_id),
                    "report_version": arguments.expected_version,
                    "target_date": report["report_date"],
                    "source_message_id": self._context.source_message_id,
                    "request_turn_id": self._context.turn_id,
                    "created_at": self._now(),
                    "expiry_time": (
                        self._context.now
                        + timedelta(seconds=request.pending_ttl_seconds)
                    ).isoformat(),
                    "consumed_at": None,
                    "consumed_by_message_id": None,
                },
                create_only=True,
            )
        return SandboxHandlerOutcome(
            target_type="sandbox_pending",
            target_id=pending_id,
            idempotency_key=key,
        )

    async def confirm_clear_report(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        active_scope = [
            pending
            for pending in await self._session.read_table_rows("sandbox_pending")
            if self._pending_matches_scope(pending)
            and pending.get("consumed_at") is None
        ]
        unexpired = [
            pending
            for pending in active_scope
            if self._pending_unexpired(pending)
        ]
        if not unexpired:
            if active_scope:
                raise SandboxExecutionError("CLEAR_PENDING_EXPIRED")
            raise SandboxExecutionError("CLEAR_PENDING_MISSING")
        if len(unexpired) != 1:
            raise SandboxExecutionError("CLEAR_PENDING_NOT_UNIQUE")
        pending = unexpired[0]
        if (
            str(pending["source_message_id"]) == self._context.source_message_id
            or str(pending["request_turn_id"]) == self._context.turn_id
        ):
            raise SandboxExecutionError("CLEAR_PENDING_SAME_TURN")
        report_id = str(pending["report_id"])
        expected_version = int(pending["report_version"])
        report = await self._versioned_report(report_id, expected_version)
        key = self._write_key(
            request,
            target_object=f"daily_report:{report_id}",
            expected_version=expected_version,
        )
        for item in await self._items_for_report(report_id):
            await self._session.delete_record(
                "daily_items",
                str(item["item_id"]),
            )
        await self._save_report(
            {**report, "status": "collecting"},
            version=expected_version + 1,
        )
        await self._session.upsert_record(
            "sandbox_pending",
            str(pending["pending_id"]),
            {
                **pending,
                "consumed_at": self._now(),
                "consumed_by_message_id": self._context.source_message_id,
                "consumed_by_turn_id": self._context.turn_id,
            },
        )
        return SandboxHandlerOutcome(
            target_type="daily_report",
            target_id=report_id,
            idempotency_key=key,
        )

    def _today(self) -> date:
        return self._context.now.astimezone(
            ZoneInfo(self._context.timezone)
        ).date()

    def _resolve(self, expression: str, proposed_date: date) -> date:
        return self._date_resolver.resolve(
            expression=expression,
            proposed_date=proposed_date,
            context=self._context,
        )

    def _report_id(self, report_date: date) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                (
                    "agent2-sandbox-report:"
                    f"{self._context.tenant_id}:{self._context.user_id}:"
                    f"{report_date.isoformat()}"
                ),
            )
        )

    async def _owned_report(
        self,
        report_id: str,
        *,
        required: bool,
    ) -> dict[str, object] | None:
        report = await self._session.read_record("daily_reports", report_id)
        if report is None:
            if required:
                raise SandboxExecutionError("REPORT_NOT_FOUND")
            return None
        if (
            report.get("tenant_id") != self._context.tenant_id
            or report.get("user_id") != str(self._context.user_id)
        ):
            raise SandboxExecutionError("REPORT_BINDING_REJECTED")
        return report

    async def _versioned_report(
        self,
        report_id: str,
        expected_version: int,
    ) -> dict[str, object]:
        report = await self._owned_report(report_id, required=True)
        assert report is not None
        if int(report["version"]) != expected_version:
            raise SandboxExecutionError("STALE_REPORT_VERSION")
        return report

    async def _items_for_report(
        self,
        report_id: str,
    ) -> list[dict[str, object]]:
        values = [
            item
            for item in await self._session.read_table_rows("daily_items")
            if item.get("report_id") == report_id
            and item.get("tenant_id") == self._context.tenant_id
            and item.get("user_id") == str(self._context.user_id)
        ]
        field_order = {"today_work": 0, "problems": 1, "tomorrow_plan": 2}
        return sorted(
            values,
            key=lambda item: (
                field_order[str(item["field"])],
                int(item.get("position", 0)),
                str(item["item_id"]),
            ),
        )

    async def _bound_items(
        self,
        report_id: str,
        item_ids: tuple[str, ...],
    ) -> list[dict[str, object]]:
        items = []
        for item_id in item_ids:
            item = await self._session.read_record("daily_items", item_id)
            if (
                item is None
                or item.get("report_id") != report_id
                or item.get("tenant_id") != self._context.tenant_id
                or item.get("user_id") != str(self._context.user_id)
            ):
                raise SandboxExecutionError("ITEM_BINDING_REJECTED")
            items.append(item)
        return items

    def _new_report(self, report_id: str, report_date: date) -> dict[str, object]:
        return {
            "report_id": report_id,
            "tenant_id": self._context.tenant_id,
            "user_id": str(self._context.user_id),
            "report_date": report_date.isoformat(),
            "version": 0,
            "status": "collecting",
            "created_at": self._now(),
            "updated_at": self._now(),
        }

    @staticmethod
    def _next_positions(
        items: list[dict[str, object]],
    ) -> dict[str, int]:
        positions = {
            "today_work": 1,
            "problems": 1,
            "tomorrow_plan": 1,
        }
        for item in items:
            field = str(item["field"])
            positions[field] = max(
                positions[field],
                int(item.get("position", 0)) + 1,
            )
        return positions

    async def _save_report(
        self,
        report: dict[str, object],
        *,
        version: int,
    ) -> None:
        report_id = str(report["report_id"])
        await self._session.upsert_record(
            "daily_reports",
            report_id,
            {
                **report,
                "version": version,
                "updated_at": self._now(),
            },
        )

    def _write_key(
        self,
        request: SandboxHandlerRequest,
        *,
        target_object: str,
        expected_version: int | None,
    ) -> str:
        return build_write_idempotency_key(
            tenant_id=self._context.tenant_id,
            user_id=str(self._context.user_id),
            conversation_id=self._context.conversation_id,
            source_message_id=self._context.source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments=request.arguments.model_dump(mode="json"),
            target_object=target_object,
            expected_version=expected_version,
        )

    def _pending_matches_scope(self, pending: Mapping[str, object]) -> bool:
        return (
            pending.get("namespace") == "agent2.tool_calling.sandbox.v1"
            and pending.get("tenant_id") == self._context.tenant_id
            and pending.get("user_id") == str(self._context.user_id)
            and pending.get("conversation_id") == self._context.conversation_id
        )

    def _pending_unexpired(self, pending: Mapping[str, object]) -> bool:
        raw_expiry = pending.get("expiry_time")
        if not isinstance(raw_expiry, str):
            raise SandboxExecutionError("CLEAR_PENDING_EXPIRY_INVALID")
        try:
            expiry = datetime.fromisoformat(raw_expiry)
        except ValueError as exc:
            raise SandboxExecutionError("CLEAR_PENDING_EXPIRY_INVALID") from exc
        if expiry.tzinfo is None:
            raise SandboxExecutionError("CLEAR_PENDING_EXPIRY_INVALID")
        return expiry > self._context.now

    def _now(self) -> str:
        return self._context.now.isoformat()

    @staticmethod
    def _arguments(
        request: SandboxHandlerRequest,
        expected_type: type,
    ):
        if not isinstance(request.arguments, expected_type):
            raise SandboxExecutionError("HANDLER_ARGUMENT_TYPE_MISMATCH")
        return request.arguments


def canonical_arguments_hash(arguments: Mapping[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_fingerprint(
    *,
    context: SandboxExecutionContext,
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, object],
) -> str:
    payload = {
        "tenant_id": context.tenant_id,
        "user_id": str(context.user_id),
        "conversation_id": context.conversation_id,
        "source_message_id": context.source_message_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "arguments": arguments,
    }
    return canonical_arguments_hash(payload)


def operation_fingerprint(
    *,
    context: SandboxExecutionContext,
    tool_name: str,
    arguments: Mapping[str, object],
) -> str:
    payload = {
        "tenant_id": context.tenant_id,
        "user_id": str(context.user_id),
        "conversation_id": context.conversation_id,
        "source_message_id": context.source_message_id,
        "tool_name": tool_name,
        "arguments": arguments,
    }
    return canonical_arguments_hash(payload)
