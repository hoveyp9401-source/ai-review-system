from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_RESPONSE_STYLE = "warm_concise"


@dataclass(frozen=True)
class HabitMemoryFrame:
    habit_type: str
    trigger_text: str
    meaning: str
    confidence: float = 0.0
    evidence_count: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "habit_type": self.habit_type,
            "trigger_text": self.trigger_text,
            "meaning": self.meaning,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True)
class PersonalMemoryProfile:
    """Per-user memory visible to Agent2 reply composition.

    This profile is deliberately read-only. It can help the assistant speak with
    continuity, but it must not authorize writes or change workflow ownership.
    """

    user_id: str
    dingtalk_user_id: str = ""
    display_name: str = ""
    response_style: str = DEFAULT_RESPONSE_STYLE
    active_habits: tuple[HabitMemoryFrame, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self, *, max_habits: int = 8) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "dingtalk_user_id": self.dingtalk_user_id,
            "display_name": self.display_name,
            "response_style": self.response_style,
            "active_habits": [habit.as_payload() for habit in self.active_habits[:max_habits]],
            "notes": list(self.notes),
            "is_user_scoped": True,
        }


def build_personal_memory_profile(
    *,
    user: Any,
    user_habits: list[Any] | tuple[Any, ...] = (),
    response_style: str = DEFAULT_RESPONSE_STYLE,
) -> PersonalMemoryProfile:
    """Build a user-scoped memory profile from existing persistent records."""

    return PersonalMemoryProfile(
        user_id=str(getattr(user, "id", "") or getattr(user, "user_id", "") or ""),
        dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
        display_name=str(getattr(user, "name", "") or ""),
        response_style=response_style or DEFAULT_RESPONSE_STYLE,
        active_habits=tuple(_habit_frame(habit) for habit in user_habits or ()),
        notes=_profile_notes(user_habits or ()),
    )


def _habit_frame(habit: Any) -> HabitMemoryFrame:
    return HabitMemoryFrame(
        habit_type=str(getattr(habit, "habit_type", "") or ""),
        trigger_text=str(getattr(habit, "trigger_text", "") or ""),
        meaning=str(getattr(habit, "meaning", "") or ""),
        confidence=_float_or_zero(getattr(habit, "confidence", 0)),
        evidence_count=_int_or_zero(getattr(habit, "evidence_count", 0)),
    )


def _profile_notes(user_habits: list[Any] | tuple[Any, ...]) -> tuple[str, ...]:
    notes: list[str] = []
    habit_types = {str(getattr(habit, "habit_type", "") or "") for habit in user_habits or ()}
    if "previous_plan_rollover" in habit_types:
        notes.append("user_has_previous_plan_rollover_habit")
    if "asr_correction" in habit_types:
        notes.append("user_has_asr_correction_habit")
    if "phrase_meaning" in habit_types:
        notes.append("user_has_phrase_meaning_habit")
    return tuple(notes)


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
