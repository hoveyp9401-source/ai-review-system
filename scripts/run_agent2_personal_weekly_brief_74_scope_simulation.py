#!/usr/bin/env python3
"""Simulate all 74 formal recipients without database or transport access."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from app.agent2.personal_weekly_brief_delivery import (
    PersonalWeeklyBriefDispatcher,
    PersonalWeeklyBriefRecipient,
)
from app.agent2.personal_weekly_brief_scope import (
    validate_personal_weekly_brief_targets,
)
from app.agent2.personal_weekly_brief_sources import (
    build_personal_weekly_brief_snapshot,
)
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord
from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.config import Settings
from app.legal_daily_roster import (
    FORMAL_CENTER_TEAM_CODE,
    FORMAL_CHILD_TEAM_NAMES,
    FORMAL_PARENT_DEPARTMENT,
    FormalLegalDailyRoster,
    FormalRosterMember,
)


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
WEEK_START = date(2026, 8, 17)
WEEK_END = date(2026, 8, 21)
SCHEMA_VERSION = "agent2.personal_weekly_brief.scope_simulation.v1"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _formal_roster() -> FormalLegalDailyRoster:
    team_names = tuple(sorted(FORMAL_CHILD_TEAM_NAMES))
    counts = (10, 10, 10, 10, 10, 10, 10)
    members: list[FormalRosterMember] = []
    index = 0
    for team_index, (team_name, count) in enumerate(zip(team_names, counts, strict=True)):
        for _ in range(count):
            user_id = str(uuid5(NAMESPACE_URL, f"pwb-formal-user-{index}"))
            members.append(
                FormalRosterMember(
                    user_id=user_id,
                    user_name=f"脱敏成员{index:02d}",
                    dingtalk_user_id=f"ding-redacted-{index:02d}",
                    team_id=f"team-redacted-{team_index}",
                    team_code=f"legal-{team_index + 1}",
                    team_name=team_name,
                    department_name=FORMAL_PARENT_DEPARTMENT,
                    team_active=True,
                    data_complete=True,
                )
            )
            index += 1
    for center_index, name in enumerate(
        ("中心直属甲", "中心直属乙", "中心直属丙", "中心直属丁")
    ):
        user_id = str(uuid5(NAMESPACE_URL, f"pwb-formal-center-{center_index}"))
        members.append(
            FormalRosterMember(
                user_id=user_id,
                user_name=name,
                dingtalk_user_id=f"ding-redacted-center-{center_index}",
                team_id="team-redacted-center",
                team_code=FORMAL_CENTER_TEAM_CODE,
                team_name=FORMAL_PARENT_DEPARTMENT,
                department_name=FORMAL_PARENT_DEPARTMENT,
                team_active=False,
                data_complete=True,
            )
        )
    return FormalLegalDailyRoster(
        tenant_id="tenant-redacted",
        on_date=NOW.date(),
        members=tuple(members),
    )


def _scope_rows(roster: FormalLegalDailyRoster):
    bindings = tuple(
        SimpleNamespace(
            tenant_id=roster.tenant_id,
            user_id=member.user_id,
            dingtalk_user_id=member.dingtalk_user_id,
            display_name=member.user_name,
            active=True,
        )
        for member in roster.members
    )
    controls = tuple(
        SimpleNamespace(
            tenant_id=roster.tenant_id,
            user_id=member.user_id,
            enabled=True,
            messages_enabled=True,
            runtime="canary_execute",
            model_name=CANARY_MODEL_NAME,
        )
        for member in roster.members
    )
    states = tuple(
        SimpleNamespace(
            user_key=f"{roster.tenant_id}:{member.user_id}",
            conversation_id=f"conversation-redacted-{index:02d}",
        )
        for index, member in enumerate(roster.members)
    )
    return bindings, controls, states


def _report(owner_user_id: str, index: int):
    return SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"pwb-report-{owner_user_id}"),
        user_id=UUID(owner_user_id),
        report_date=date(2026, 8, 18),
        today_work=[f"脱敏成员{index:02d}本人的工作事项"],
        problems=[],
        tomorrow_plan=[],
        status="completed",
    )


def _record(target, index: int) -> PersonalWeeklyBriefRecord:
    return PersonalWeeklyBriefRecord(
        brief_id=str(uuid5(NAMESPACE_URL, f"pwb-brief-{index}")),
        tenant_id=target.tenant_id,
        owner_user_id=target.internal_user_id,
        conversation_id=target.conversation_id,
        week_start=WEEK_START,
        week_end=WEEK_END,
        snapshot_at=NOW,
        source_snapshot={"sources": []},
        source_fingerprint="a" * 64,
        content_json={"trace": {}},
        message_text="脱敏简报",
        llm_model=CANARY_MODEL_NAME,
        status="delivered",
        idempotency_key=f"isolated-{index}",
        created_at=NOW,
        updated_at=NOW,
        provider_message_id=f"provider-{index}",
        provider_accepted_at=NOW,
        delivered_at=NOW,
        delivery_receipt_json={"delivery_verified": True},
    )


class _NoSendTransport:
    calls = 0

    async def send_private_text_verified(self, **_kwargs):
        self.calls += 1
        raise AssertionError("scope simulation must not send")

    async def query_private_delivery(self, **_kwargs):
        self.calls += 1
        raise AssertionError("scope simulation must not query delivery")


class _NoWriteStore:
    def __getattr__(self, _name):
        raise AssertionError("scope simulation must not write")


async def run_simulation() -> dict[str, Any]:
    roster = _formal_roster()
    bindings, controls, states = _scope_rows(roster)
    targets = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )
    if len(targets) != 74:
        raise AssertionError("formal scope did not resolve to exactly 74 targets")
    allowed = frozenset(target.internal_user_id for target in targets)
    transport = _NoSendTransport()
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=_NoWriteStore(),
        transport=transport,
        tenant_id=roster.tenant_id,
        allowed_user_ids=allowed,
    )
    own_scope_passed = 0
    cross_scope_rejected = 0
    source_isolation_passed = 0
    for index, target in enumerate(targets):
        other = targets[(index + 1) % len(targets)]
        snapshot = build_personal_weekly_brief_snapshot(
            tenant_id=target.tenant_id,
            owner_user_id=target.internal_user_id,
            week_start=WEEK_START,
            snapshot_at=NOW,
            daily_reports=(
                _report(target.internal_user_id, index),
                _report(other.internal_user_id, (index + 1) % len(targets)),
            ),
            weekly_plan=None,
        )
        if (
            snapshot.owner_user_id == target.internal_user_id
            and len(snapshot.sources) == 1
            and f"脱敏成员{index:02d}" in snapshot.sources[0].original_text
        ):
            source_isolation_passed += 1
        row = _record(target, index)
        recipient = PersonalWeeklyBriefRecipient(
            tenant_id=target.tenant_id,
            internal_user_id=target.internal_user_id,
            dingtalk_user_id=target.dingtalk_user_id,
            conversation_id=target.conversation_id,
        )
        returned = await dispatcher.dispatch(
            row=row,
            recipient=recipient,
            changed_at=NOW,
            claim_token="unused-delivered-row",
        )
        if returned == row:
            own_scope_passed += 1
        wrong_recipient = PersonalWeeklyBriefRecipient(
            tenant_id=other.tenant_id,
            internal_user_id=other.internal_user_id,
            dingtalk_user_id=other.dingtalk_user_id,
            conversation_id=other.conversation_id,
        )
        try:
            await dispatcher.dispatch(
                row=row,
                recipient=wrong_recipient,
                changed_at=NOW,
                claim_token="must-not-send",
            )
        except ValueError as exc:
            if str(exc) == "personal_weekly_brief_scope_invalid":
                cross_scope_rejected += 1
                continue
        raise AssertionError("cross-owner recipient was not rejected")

    outsider_rejected = False
    outsider = PersonalWeeklyBriefRecipient(
        tenant_id=roster.tenant_id,
        internal_user_id="outsider-not-in-roster",
        dingtalk_user_id="ding-outsider",
        conversation_id="conversation-outsider",
    )
    try:
        await dispatcher.dispatch(
            row=_record(targets[0], 0),
            recipient=outsider,
            changed_at=NOW,
            claim_token="must-not-send",
        )
    except ValueError as exc:
        outsider_rejected = str(exc) == "personal_weekly_brief_scope_invalid"

    ambiguous_identity_rejected = False
    try:
        validate_personal_weekly_brief_targets(
            roster=roster,
            bindings=(*bindings, bindings[0]),
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )
    except RuntimeError:
        ambiguous_identity_rejected = True
    repeated_context_targets = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=(*states, states[0]),
        expected_model_name=CANARY_MODEL_NAME,
    )
    prior_context_independent = all(
        target.conversation_id
        == f"agent2-direct:{target.internal_user_id}"
        for target in repeated_context_targets
    )

    settings = Settings(_env_file=None)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "formal_target_count": len(targets),
        "child_member_count": len(roster.child_members),
        "center_member_count": len(roster.center_members),
        "child_department_count": len({member.team_id for member in roster.child_members}),
        "own_scope_passed": own_scope_passed,
        "cross_scope_rejected": cross_scope_rejected,
        "source_isolation_passed": source_isolation_passed,
        "outsider_rejected": outsider_rejected,
        "ambiguous_identity_rejected": ambiguous_identity_rejected,
        "prior_context_independent": prior_context_independent,
        "generation_switch_enabled": settings.agent2_personal_weekly_brief_enabled,
        "send_switch_enabled": settings.agent2_personal_weekly_brief_send_enabled,
        "transport_calls": transport.calls,
        "database_accessed": False,
        "real_user_data_used": False,
    }
    expected = {
        "own_scope_passed": 74,
        "cross_scope_rejected": 74,
        "source_isolation_passed": 74,
        "outsider_rejected": True,
        "ambiguous_identity_rejected": True,
        "prior_context_independent": True,
        "generation_switch_enabled": False,
        "send_switch_enabled": False,
        "transport_calls": 0,
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise AssertionError("74-person scope simulation did not meet its contract")
    return payload


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    args = _args()
    payload = asyncio.run(run_simulation())
    _write_json_atomically(args.output, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "formal_target_count": payload["formal_target_count"],
                "transport_calls": payload["transport_calls"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
