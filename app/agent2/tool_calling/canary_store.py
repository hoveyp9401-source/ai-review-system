from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class ToolCallCanaryControl(Base):
    """Independent Tool-Call Core switch; it does not reuse old Agent2 state."""

    __tablename__ = "agent2_tool_call_canary_controls"
    __table_args__ = (
        CheckConstraint(
            "runtime = 'canary_execute'",
            name="agent2_tool_call_canary_runtime_mode_check",
        ),
        CheckConstraint(
            "version >= 1",
            name="agent2_tool_call_canary_version_check",
        ),
        UniqueConstraint(
            "control_key",
            name="agent2_tool_call_canary_control_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "user_id",
            name="agent2_tool_call_canary_tenant_user_key",
        ),
        Index(
            "agent2_tool_call_canary_route_idx",
            "enabled",
            "tenant_id",
            "user_id",
        ),
    )

    control_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    control_key: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    runtime: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="canary_execute",
    )
    messages_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    registry_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ToolCallCanaryControlAudit(Base):
    __tablename__ = "agent2_tool_call_canary_control_audits"
    __table_args__ = (
        UniqueConstraint(
            "source_change_id",
            name="agent2_tool_call_canary_change_id_key",
        ),
        Index(
            "agent2_tool_call_canary_audit_key_idx",
            "control_key",
            "created_at",
        ),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    control_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_change_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    after_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ToolCallCanaryControlRepository:
    """Audited control changes; runtime requests cannot call this interface."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def enable_after_human_confirmation(
        self,
        *,
        control_key: str,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
        expected_registry_digest: str,
        expected_prompt_sha256: str,
        expected_model_name: str,
        max_active_controls: int = 1,
    ) -> ToolCallCanaryControl:
        _validate_active_control_limit(max_active_controls)
        control = await self._locked_control(control_key)
        self._validate_change_request(
            control=control,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
            expected_version=expected_version,
        )
        if (
            control.registry_digest != expected_registry_digest
            or control.prompt_sha256 != expected_prompt_sha256
            or control.model_name != expected_model_name
            or control.runtime != "canary_execute"
        ):
            raise ValueError("tool_call_canary_frozen_contract_mismatch")
        active_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ToolCallCanaryControl)
                .where(
                    ToolCallCanaryControl.enabled.is_(True),
                    ToolCallCanaryControl.control_key != control.control_key,
                )
            )
            or 0
        )
        if active_count >= max_active_controls:
            raise ValueError("tool_call_canary_cohort_limit_reached")
        before = _control_mapping(control)
        control.enabled = True
        control.messages_enabled = False
        control.version += 1
        control.changed_by = actor_user_id
        control.change_reason = reason
        await self._session.flush()
        self._add_audit(
            control=control,
            before=before,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
        )
        await self._session.flush()
        return control

    async def disable_runtime_fail_closed(
        self,
        *,
        control_key: str,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
    ) -> ToolCallCanaryControl:
        control = await self._locked_control(control_key)
        self._validate_change_request(
            control=control,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
            expected_version=expected_version,
        )

        before = _control_mapping(control)
        control.enabled = False
        control.messages_enabled = False
        control.version += 1
        control.changed_by = actor_user_id
        control.change_reason = reason
        await self._session.flush()
        self._add_audit(
            control=control,
            before=before,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
        )
        await self._session.flush()
        return control

    async def upgrade_prompt_while_disabled(
        self,
        *,
        control_key: str,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
        expected_registry_digest: str,
        expected_prompt_sha256: str,
        expected_model_name: str,
        new_prompt_sha256: str,
    ) -> ToolCallCanaryControl:
        control = await self._locked_control(control_key)
        self._validate_change_request(
            control=control,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
            expected_version=expected_version,
        )
        if control.enabled or control.messages_enabled:
            raise ValueError("tool_call_canary_prompt_upgrade_requires_closed_control")
        if (
            control.runtime != "canary_execute"
            or control.registry_digest != expected_registry_digest
            or control.prompt_sha256 != expected_prompt_sha256
            or control.model_name != expected_model_name
        ):
            raise ValueError("tool_call_canary_frozen_contract_mismatch")
        if (
            len(new_prompt_sha256) != 64
            or new_prompt_sha256 == expected_prompt_sha256
        ):
            raise ValueError("tool_call_canary_new_prompt_digest_required")

        before = _control_mapping(control)
        control.prompt_sha256 = new_prompt_sha256
        control.version += 1
        control.changed_by = actor_user_id
        control.change_reason = reason
        await self._session.flush()
        self._add_audit(
            control=control,
            before=before,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
        )
        await self._session.flush()
        return control

    async def upgrade_registry_while_disabled(
        self,
        *,
        control_key: str,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
        expected_registry_digest: str,
        expected_prompt_sha256: str,
        expected_model_name: str,
        new_registry_digest: str,
    ) -> ToolCallCanaryControl:
        control = await self._locked_control(control_key)
        self._validate_change_request(
            control=control,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
            expected_version=expected_version,
        )
        if control.enabled or control.messages_enabled:
            raise ValueError(
                "tool_call_canary_registry_upgrade_requires_closed_control"
            )
        if (
            control.runtime != "canary_execute"
            or control.registry_digest != expected_registry_digest
            or control.prompt_sha256 != expected_prompt_sha256
            or control.model_name != expected_model_name
        ):
            raise ValueError("tool_call_canary_frozen_contract_mismatch")
        try:
            digest_is_hex = int(new_registry_digest, 16) >= 0
        except ValueError:
            digest_is_hex = False
        if (
            len(new_registry_digest) != 64
            or not digest_is_hex
            or new_registry_digest == expected_registry_digest
        ):
            raise ValueError(
                "tool_call_canary_new_registry_digest_required"
            )

        before = _control_mapping(control)
        control.registry_digest = new_registry_digest
        control.version += 1
        control.changed_by = actor_user_id
        control.change_reason = reason
        await self._session.flush()
        self._add_audit(
            control=control,
            before=before,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
        )
        await self._session.flush()
        return control

    async def enable_message_delivery_after_human_confirmation(
        self,
        *,
        control_key: str,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
        expected_registry_digest: str,
        expected_prompt_sha256: str,
        expected_model_name: str,
        max_active_controls: int = 1,
    ) -> ToolCallCanaryControl:
        """Open replies for one member of a bounded frozen test cohort."""

        _validate_active_control_limit(max_active_controls)
        control = await self._locked_control(control_key)
        self._validate_change_request(
            control=control,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
            expected_version=expected_version,
        )
        if (
            not control.enabled
            or control.runtime != "canary_execute"
            or control.registry_digest != expected_registry_digest
            or control.prompt_sha256 != expected_prompt_sha256
            or control.model_name != expected_model_name
        ):
            raise ValueError("tool_call_canary_frozen_contract_mismatch")
        if control.messages_enabled:
            raise ValueError("tool_call_canary_messages_already_enabled")
        other_active_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ToolCallCanaryControl)
                .where(
                    ToolCallCanaryControl.enabled.is_(True),
                    ToolCallCanaryControl.control_key != control.control_key,
                )
            )
            or 0
        )
        if other_active_count + 1 > max_active_controls:
            raise ValueError("tool_call_canary_cohort_limit_exceeded")

        before = _control_mapping(control)
        control.messages_enabled = True
        control.version += 1
        control.changed_by = actor_user_id
        control.change_reason = reason
        await self._session.flush()
        self._add_audit(
            control=control,
            before=before,
            actor_user_id=actor_user_id,
            source_change_id=source_change_id,
            reason=reason,
        )
        await self._session.flush()
        return control

    async def _locked_control(
        self,
        control_key: str,
    ) -> ToolCallCanaryControl:
        control = await self._session.scalar(
            select(ToolCallCanaryControl)
            .where(ToolCallCanaryControl.control_key == control_key)
            .with_for_update()
        )
        if control is None:
            raise ValueError("tool_call_canary_control_missing")
        return control

    @staticmethod
    def _validate_change_request(
        *,
        control: ToolCallCanaryControl,
        actor_user_id: str,
        source_change_id: str,
        reason: str,
        expected_version: int,
    ) -> None:
        if control.version != expected_version:
            raise ValueError("tool_call_canary_control_version_conflict")
        if not (
            actor_user_id.strip()
            and source_change_id.strip()
            and reason.strip()
        ):
            raise ValueError(
                "tool_call_canary_change_audit_fields_required"
            )

    def _add_audit(
        self,
        *,
        control: ToolCallCanaryControl,
        before: dict[str, object],
        actor_user_id: str,
        source_change_id: str,
        reason: str,
    ) -> None:
        self._session.add(
            ToolCallCanaryControlAudit(
                control_key=control.control_key,
                actor_user_id=actor_user_id,
                source_change_id=source_change_id,
                before_json=before,
                after_json=_control_mapping(control),
                reason=reason,
            )
        )


def _control_mapping(control: ToolCallCanaryControl) -> dict[str, object]:
    return {
        "control_key": control.control_key,
        "tenant_id": control.tenant_id,
        "user_id": control.user_id,
        "enabled": control.enabled,
        "runtime": control.runtime,
        "messages_enabled": control.messages_enabled,
        "registry_digest": control.registry_digest,
        "prompt_sha256": control.prompt_sha256,
        "model_name": control.model_name,
        "version": control.version,
        "changed_by": control.changed_by,
        "change_reason": control.change_reason,
    }


def _validate_active_control_limit(value: int) -> None:
    if not 1 <= value <= 70:
        raise ValueError("tool_call_canary_cohort_limit_invalid")
