from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    ValidationInfo,
    field_validator,
)


PersonalMemoryType = Literal[
    "response_preference",
    "saved_view",
    "terminology_alias",
]
PersonalMemorySource = Literal["explicit_user", "server_verified"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class VerbosityPreferenceValue(_FrozenModel):
    level: Literal["concise", "balanced", "detailed"]


class TogglePreferenceValue(_FrozenModel):
    enabled: StrictBool


class OutputFormatPreferenceValue(_FrozenModel):
    format: Literal["plain_text", "bullet_list", "table"]


class PreferredSalutationValue(_FrozenModel):
    salutation: str = Field(min_length=1, max_length=24)

    @field_validator("salutation")
    @classmethod
    def salutation_must_be_a_single_safe_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("preferred salutation is invalid")
        if not all(
            character.isalnum()
            or character in {" ", "-", "_", "·", "・"}
            for character in value
        ):
            raise ValueError("preferred salutation is invalid")
        return value


class AssistantPreferredNameValue(_FrozenModel):
    name: str = Field(min_length=1, max_length=24)

    @field_validator("name")
    @classmethod
    def name_must_be_a_single_safe_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("assistant preferred name is invalid")
        if not all(
            character.isalnum()
            or character in {" ", "-", "_", "·", "・"}
            for character in value
        ):
            raise ValueError("assistant preferred name is invalid")
        return value


class SavedViewValue(_FrozenModel):
    view_code: str = Field(min_length=1, max_length=128)
    scope: Literal["self"]
    display_format: Literal["plain_text", "bullet_list", "table"]

    @field_validator("view_code")
    @classmethod
    def view_code_must_be_safe(cls, value: str) -> str:
        if not _is_safe_code(value):
            raise ValueError("saved view code is invalid")
        return value


class TerminologyAliasValue(_FrozenModel):
    alias: str = Field(min_length=1, max_length=128)
    view_code: str = Field(min_length=1, max_length=128)

    @field_validator("view_code")
    @classmethod
    def view_code_must_be_safe(cls, value: str) -> str:
        if not _is_safe_code(value):
            raise ValueError("terminology view code is invalid")
        return value


PersonalMemoryValue = (
    VerbosityPreferenceValue
    | TogglePreferenceValue
    | OutputFormatPreferenceValue
    | PreferredSalutationValue
    | AssistantPreferredNameValue
    | SavedViewValue
    | TerminologyAliasValue
)


def validate_personal_memory_value(
    memory_type: PersonalMemoryType,
    memory_key: str,
    value: Any,
) -> PersonalMemoryValue:
    """Validate one value against the server-owned key/type contract."""

    value_model, error_label = _memory_value_model(
        memory_type,
        memory_key,
    )
    try:
        return value_model.model_validate(value)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError(
            f"{error_label} value violates its contract"
        ) from exc


def model_visible_personal_memory_value(
    memory_type: PersonalMemoryType,
    memory_key: str,
    value: Any,
) -> dict[str, Any]:
    validated = validate_personal_memory_value(
        memory_type,
        memory_key,
        value,
    )
    if memory_key == "response.preferred_salutation":
        return {
            "configured": True,
            "server_rendered": True,
        }
    return validated.model_dump(mode="json")


class PersonalMemoryScope(_FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    now: datetime

    @field_validator("now")
    @classmethod
    def now_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("personal memory time must be timezone-aware")
        return value


class TrustedPersonalMemory(_FrozenModel):
    memory_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    memory_type: PersonalMemoryType
    memory_key: str = Field(min_length=1, max_length=128)
    value: PersonalMemoryValue
    source_kind: PersonalMemorySource
    source_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
    )
    version: int = Field(ge=1)
    updated_at: datetime
    expires_at: datetime | None = None
    provenance: Literal["server_personal_memory"] = "server_personal_memory"

    @field_validator("updated_at", "expires_at")
    @classmethod
    def timestamps_must_be_timezone_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("personal memory timestamps must be timezone-aware")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def value_matches_memory_contract(
        cls,
        value: Any,
        info: ValidationInfo,
    ) -> PersonalMemoryValue:
        memory_type = info.data.get("memory_type")
        memory_key = info.data.get("memory_key")
        try:
            return validate_personal_memory_value(
                memory_type,
                memory_key,
                value,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def model_payload(self) -> dict[str, Any]:
        return {
            "memory_type": self.memory_type,
            "memory_key": self.memory_key,
            "value": model_visible_personal_memory_value(
                self.memory_type,
                self.memory_key,
                self.value,
            ),
            "provenance": self.provenance,
        }


class TrustedPersonalMemoryContext(_FrozenModel):
    entries: tuple[TrustedPersonalMemory, ...] = ()

    def model_payload(self) -> dict[str, Any]:
        return {
            "entries": [entry.model_payload() for entry in self.entries],
            "authority": {
                "preference_only": True,
                "may_authorize_writes": False,
                "may_override_server_facts": False,
                "may_supply_internal_ids": False,
            },
        }


class PersonalMemoryReadPort(Protocol):
    async def load_active(
        self,
        scope: PersonalMemoryScope,
        *,
        limit: int,
    ) -> tuple[TrustedPersonalMemory, ...]: ...


class PersonalMemoryViewCatalog(Protocol):
    async def contains_view(
        self,
        scope: PersonalMemoryScope,
        *,
        view_code: str,
    ) -> bool: ...


class PersonalMemoryModule:
    def __init__(
        self,
        *,
        read_port: PersonalMemoryReadPort,
        view_catalog: PersonalMemoryViewCatalog | None = None,
        max_entries: int = 20,
    ) -> None:
        if max_entries < 0 or max_entries > 20:
            raise ValueError("personal memory limit must be between 0 and 20")
        self._read_port = read_port
        self._view_catalog = view_catalog
        self._max_entries = max_entries

    async def read_for_turn(
        self,
        scope: PersonalMemoryScope,
    ) -> TrustedPersonalMemoryContext:
        if self._max_entries == 0:
            return TrustedPersonalMemoryContext()
        entries = await self._read_port.load_active(
            scope,
            limit=self._max_entries,
        )
        if any(
            entry.tenant_id != scope.tenant_id
            or entry.user_id != scope.user_id
            for entry in entries
        ):
            raise ValueError(
                "personal memory read returned data outside authenticated scope"
            )
        if len(entries) > self._max_entries:
            raise ValueError(
                "personal memory read returned more than configured maximum"
            )
        current_entries = tuple(
            entry
            for entry in entries
            if entry.expires_at is None or entry.expires_at > scope.now
        )
        active_keys = [entry.memory_key for entry in current_entries]
        if len(active_keys) != len(set(active_keys)):
            raise ValueError("personal memory active keys must be unique")
        trusted_entries: list[TrustedPersonalMemory] = []
        for entry in current_entries:
            if entry.memory_type == "response_preference":
                trusted_entries.append(entry)
                continue
            if self._view_catalog is None:
                continue
            view_code = entry.value.view_code
            if await self._view_catalog.contains_view(
                scope,
                view_code=view_code,
            ):
                trusted_entries.append(entry)
        return TrustedPersonalMemoryContext(entries=tuple(trusted_entries))


def _memory_value_model(
    memory_type: Any,
    memory_key: Any,
) -> tuple[type[_FrozenModel], str]:
    value_model: type[_FrozenModel] | None = None
    if memory_key == "response.verbosity":
        value_model = VerbosityPreferenceValue
    elif memory_key in {
        "report.daily_reminders_enabled",
        "report.show_updated_snapshot",
        "report.show_item_numbers",
    }:
        value_model = TogglePreferenceValue
    elif memory_key == "response.output_format":
        value_model = OutputFormatPreferenceValue
    elif memory_key == "response.preferred_salutation":
        value_model = PreferredSalutationValue
    elif memory_key == "assistant.preferred_name":
        value_model = AssistantPreferredNameValue
    if memory_type == "response_preference":
        if value_model is None:
            raise ValueError(
                "response preference value violates its contract"
            )
        return value_model, "response preference"
    if memory_type == "saved_view":
        if (
            not isinstance(memory_key, str)
            or not memory_key.startswith("saved_view.")
            or len(memory_key) <= len("saved_view.")
        ):
            raise ValueError("saved view value violates its contract")
        return SavedViewValue, "saved view"
    if memory_type == "terminology_alias":
        if (
            not isinstance(memory_key, str)
            or not memory_key.startswith("terminology.")
            or len(memory_key) <= len("terminology.")
        ):
            raise ValueError(
                "terminology alias value violates its contract"
            )
        return TerminologyAliasValue, "terminology alias"
    if value_model is not None:
        raise ValueError("response preference value violates its contract")
    raise ValueError("personal memory value violates its contract")


def _is_safe_code(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character in "._-" for character in value)
    )
