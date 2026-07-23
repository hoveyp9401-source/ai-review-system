from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.agent2.business.contracts import (
    BusinessCommandContext,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    RespondTravelCollaboration,
    UpdateCaseProgress,
    UpdateTravelIntent,
)
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.business.party import (
    PartyAliasRecord,
    PartyCaseRoleRecord,
    PartyEntityRecord,
    PartyIdentifierRecord,
    PartyKnowledgeBase,
)
from app.agent2.business.case_progress import CaseRecord, resolve_case_target
from app.agent2.business.travel import LocationRegistry, TravelMatcher, resolve_travel_window


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)


def _context(*, message_id: str = "message-1", tenant_id: str = "tenant-a") -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id=tenant_id,
        company_id="company-a",
        department_id="legal",
        team_id="dispute",
        actor_user_id="user-1",
        actor_role_ids=("legal_member",),
        allowed_case_ids=("case-1", "case-2"),
        source_message_id=message_id,
        source_channel="dingtalk_stream",
        occurred_at=NOW,
    )


def test_business_executor_rejects_raw_text():
    executor = InMemoryBusinessExecutor()

    with pytest.raises(TypeError, match="typed business command only"):
        executor.execute("明天去南京", _context())


def test_party_identifier_and_alias_resolution_are_tenant_scoped():
    knowledge = PartyKnowledgeBase(
        entities=(
            PartyEntityRecord("party-a", "tenant-a", "company", "南京华东建设有限公司"),
            PartyEntityRecord("party-b", "tenant-b", "company", "南京华东建设有限公司"),
        ),
        aliases=(PartyAliasRecord("alias-a", "tenant-a", "party-a", "华东建设", "human_record"),),
        identifiers=(PartyIdentifierRecord("id-a", "tenant-a", "party-a", "uscc", "91320100TEST000001"),),
        case_roles=(PartyCaseRoleRecord("role-a", "tenant-a", "party-a", "case-1", "defendant"),),
    )

    by_identifier = knowledge.resolve("91320100TEST000001", tenant_id="tenant-a", allowed_case_ids={"case-1"})
    by_alias = knowledge.resolve("华东建设", tenant_id="tenant-a", allowed_case_ids={"case-1"})

    assert by_identifier.status == "resolved"
    assert by_identifier.party_id == "party-a"
    assert by_identifier.match_basis == "exact_identifier"
    assert by_alias.status == "resolved"
    assert by_alias.party_id == "party-a"
    assert knowledge.resolve("华东建设", tenant_id="tenant-b", allowed_case_ids={"case-1"}).status == "not_found"


def test_fuzzy_party_match_never_auto_merges_or_becomes_fact():
    knowledge = PartyKnowledgeBase(
        entities=(
            PartyEntityRecord("party-1", "tenant-a", "company", "南京华东建设有限公司"),
            PartyEntityRecord("party-2", "tenant-a", "company", "南京华东建设置业有限公司"),
        )
    )

    result = knowledge.resolve("南京华东建设", tenant_id="tenant-a", allowed_case_ids=set())

    assert result.status == "needs_clarification"
    assert len(result.candidates) == 2
    assert all(candidate.confirmed is False for candidate in result.candidates)


def test_party_roles_are_per_case_not_permanent_labels():
    knowledge = PartyKnowledgeBase(
        entities=(PartyEntityRecord("party-1", "tenant-a", "company", "华东建设有限公司"),),
        case_roles=(
            PartyCaseRoleRecord("role-1", "tenant-a", "party-1", "case-1", "plaintiff"),
            PartyCaseRoleRecord("role-2", "tenant-a", "party-1", "case-2", "defendant"),
        ),
    )

    result = knowledge.party_cases("party-1", tenant_id="tenant-a", allowed_case_ids={"case-1", "case-2"})

    assert {(item.case_id, item.role_type) for item in result} == {
        ("case-1", "plaintiff"),
        ("case-2", "defendant"),
    }


def test_location_and_time_normalization_are_deterministic_and_conservative():
    registry = LocationRegistry.default()

    nanjing = registry.resolve("江苏南京")
    province_only = registry.resolve("去江苏出差")
    window = resolve_travel_window("两天后去三天", reference_date=date(2026, 7, 11))

    assert nanjing.status == "resolved"
    assert nanjing.city_code == "320100"
    assert province_only.status == "needs_clarification"
    assert window.start_date == date(2026, 7, 13)
    assert window.end_date == date(2026, 7, 15)


@pytest.mark.parametrize(
    ("destination", "city_code", "province_code"),
    (
        ("浙江杭州", "330100", "330000"),
        ("广东广州", "440100", "440000"),
        ("湖北武汉", "420100", "420000"),
        ("四川成都", "510100", "510000"),
        ("山东青岛", "370200", "370000"),
        ("福建厦门", "350200", "350000"),
    ),
)
def test_national_core_business_destinations_resolve_from_data_catalog(
    destination: str,
    city_code: str,
    province_code: str,
):
    resolution = LocationRegistry.default().resolve(destination)

    assert resolution.status == "resolved"
    assert resolution.city_code == city_code
    assert resolution.province_code == province_code


def test_travel_match_requires_same_tenant_different_users_same_city_and_overlap():
    executor = InMemoryBusinessExecutor()
    first = executor.execute(
        CreateTravelIntent(
            command_id="travel-command-1",
            destination_raw="南京",
            destination_normalized="南京市",
            city_code="320100",
            province_code="320000",
            start_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            time_precision="day",
            purpose_summary="处理案件",
            related_case_ids=("case-1",),
            confidence=0.98,
        ),
        _context(message_id="travel-message-1"),
    )
    second_context = BusinessCommandContext(
        **{**_context(message_id="travel-message-2").as_dict(), "actor_user_id": "user-2"}
    )
    second = executor.execute(
        CreateTravelIntent(
            command_id="travel-command-2",
            destination_raw="南京市",
            destination_normalized="南京市",
            city_code="320100",
            province_code="320000",
            start_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            time_precision="day",
            purpose_summary="",
            related_case_ids=(),
            confidence=0.95,
        ),
        second_context,
    )

    candidates = TravelMatcher().match(executor.travel_intents)

    assert first.status == second.status == "executed"
    assert len(candidates) == 1
    assert candidates[0].participant_ids == ("user-1", "user-2")
    assert candidates[0].overlap_start.date() == date(2026, 7, 13)


def test_same_user_resending_same_travel_fact_does_not_create_second_intent():
    executor = InMemoryBusinessExecutor()
    first = CreateTravelIntent(
        command_id="travel-resend-action-1",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc),
        time_precision="day",
        purpose_summary="沟通鑫瑞达回款事宜",
        related_case_ids=("case-1",),
        confidence=1.0,
    )
    resent = CreateTravelIntent(
        **{
            **first.__dict__,
            "command_id": "travel-resend-action-2",
        }
    )

    created = executor.execute(first, _context(message_id="provider-message-a"))
    duplicate = executor.execute(
        resent,
        _context(message_id="provider-message-b"),
    )

    assert created.status == "executed"
    assert duplicate.status == "duplicate"
    assert duplicate.receipt_id != created.receipt_id
    assert duplicate.resource_id == created.resource_id
    assert duplicate.actual_write is False
    assert len(executor.travel_intents) == 1


def test_cancelled_travel_fact_can_be_registered_again_without_reactivating_old_row():
    executor = InMemoryBusinessExecutor()
    command = CreateTravelIntent(
        command_id="travel-create-before-cancel",
        destination_raw="Nanjing",
        destination_normalized="Nanjing",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc),
        time_precision="day",
        purpose_summary="cancel and recreate regression",
        related_case_ids=(),
        confidence=1.0,
    )
    created = executor.execute(command, _context(message_id="travel-create-a"))
    cancelled = executor.execute(
        UpdateTravelIntent(
            command_id="travel-cancel",
            travel_intent_id=created.resource_id,
            expected_version=1,
            status="cancelled",
        ),
        _context(message_id="travel-cancel-b"),
    )
    recreated = executor.execute(
        CreateTravelIntent(
            **{
                **command.__dict__,
                "command_id": "travel-create-after-cancel",
            }
        ),
        _context(message_id="travel-create-c"),
    )

    assert cancelled.status == "executed"
    assert recreated.status == "executed"
    assert recreated.resource_id != created.resource_id
    assert len(executor.travel_intents) == 2
    assert sum(item.status != "cancelled" for item in executor.travel_intents) == 1


def test_three_people_same_city_and_window_produce_one_group_candidate_not_pairwise_storm():
    executor = InMemoryBusinessExecutor()
    for index, user_id in enumerate(("user-1", "user-2", "user-3"), start=1):
        context = BusinessCommandContext(
            **{
                **_context(message_id=f"travel-group-message-{index}").as_dict(),
                "actor_user_id": user_id,
            }
        )
        executor.execute(
            CreateTravelIntent(
                command_id=f"travel-group-command-{index}",
                destination_raw="南京",
                destination_normalized="南京市",
                city_code="320100",
                province_code="320000",
                start_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                time_precision="day",
                purpose_summary="出差",
                related_case_ids=(),
                confidence=0.99,
            ),
            context,
        )

    candidates = TravelMatcher().match(executor.travel_intents)

    assert len(candidates) == 1
    assert candidates[0].participant_ids == ("user-1", "user-2", "user-3")
    assert len(candidates[0].travel_intent_ids) == 3


def test_travel_match_does_not_cross_company_or_department_boundary():
    executor = InMemoryBusinessExecutor()
    first_context = _context(message_id="org-travel-1")
    second_context = BusinessCommandContext(
        **{
            **_context(message_id="org-travel-2").as_dict(),
            "actor_user_id": "user-2",
            "company_id": "company-b",
        }
    )
    for command_id, context in (("org-1", first_context), ("org-2", second_context)):
        executor.execute(
            CreateTravelIntent(
                command_id=command_id,
                destination_raw="南京",
                destination_normalized="南京市",
                city_code="320100",
                province_code="320000",
                start_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                time_precision="day",
                purpose_summary="出差",
                related_case_ids=(),
                confidence=0.99,
            ),
            context,
        )

    assert TravelMatcher().match(executor.travel_intents) == ()


def test_travel_match_does_not_cross_team_visibility_boundary():
    executor = InMemoryBusinessExecutor()
    contexts = (
        _context(message_id="team-travel-1"),
        BusinessCommandContext(
            **{
                **_context(message_id="team-travel-2").as_dict(),
                "actor_user_id": "user-2",
                "team_id": "advisory",
            }
        ),
    )
    for index, context in enumerate(contexts, start=1):
        executor.execute(
            CreateTravelIntent(
                command_id=f"team-travel-command-{index}",
                destination_raw="Nanjing",
                destination_normalized="Nanjing",
                city_code="320100",
                province_code="320000",
                start_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                time_precision="day",
                purpose_summary="travel",
                related_case_ids=(),
                confidence=0.99,
            ),
            context,
        )

    assert TravelMatcher().match(executor.travel_intents) == ()


def test_travel_notification_is_idempotent_and_discloses_no_case_details():
    executor = InMemoryBusinessExecutor()
    candidate = executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-1",
        travel_intent_ids=("intent-1", "intent-2"),
        participant_ids=("user-1", "user-2"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    first = executor.dispatch_travel_notifications(candidate.candidate_id)
    second = executor.dispatch_travel_notifications(candidate.candidate_id)

    assert len(first) == 2
    assert second == first
    assert all("案件" not in item.message_text and "金额" not in item.message_text for item in first)
    assert len({item.idempotency_key for item in first}) == 2


def test_travel_candidate_becomes_accepted_only_after_both_accept():
    executor = InMemoryBusinessExecutor()
    executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-1",
        travel_intent_ids=("intent-1", "intent-2"),
        participant_ids=("user-1", "user-2"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    one = executor.execute(
        RespondTravelCollaboration("response-1", "candidate-1", "accept"),
        _context(message_id="response-message-1"),
    )
    two = executor.execute(
        RespondTravelCollaboration("response-2", "candidate-1", "accept"),
        BusinessCommandContext(**{**_context(message_id="response-message-2").as_dict(), "actor_user_id": "user-2"}),
    )

    assert one.after["status"] == "accepted_by_one"
    assert two.after["status"] == "accepted"


def test_cancel_response_cancels_only_actor_intent_and_all_related_pending_notifications():
    executor = InMemoryBusinessExecutor()
    user_1 = _context(message_id="travel-cancel-intent-1")
    user_2 = BusinessCommandContext(
        **{**_context(message_id="travel-cancel-intent-2").as_dict(), "actor_user_id": "user-2"}
    )
    receipts = []
    for index, context in enumerate((user_1, user_2), start=1):
        receipts.append(
            executor.execute(
                CreateTravelIntent(
                    command_id=f"travel-cancel-command-{index}",
                    destination_raw="南京",
                    destination_normalized="南京市",
                    city_code="320100",
                    province_code="320000",
                    start_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
                    end_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                    time_precision="day",
                    purpose_summary="出差",
                    related_case_ids=(),
                    confidence=0.99,
                ),
                context,
            )
        )
    intent_1, intent_2 = (item.resource_id for item in receipts)
    executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-current",
        travel_intent_ids=(intent_1, intent_2),
        participant_ids=("user-1", "user-2"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-other",
        travel_intent_ids=(intent_1, "intent-3"),
        participant_ids=("user-1", "user-3"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    executor.dispatch_travel_notifications("candidate-current")
    executor.dispatch_travel_notifications("candidate-other")

    receipt = executor.execute(
        RespondTravelCollaboration("response-cancel", "candidate-current", "cancel"),
        _context(message_id="travel-cancel-response"),
    )

    assert receipt.after["status"] == "cancelled"
    assert executor.travel_intents_by_id[intent_1].status == "cancelled"
    assert executor.travel_intents_by_id[intent_2].status == "planned"
    assert executor.travel_candidates["candidate-current"].status == "cancelled"
    assert executor.travel_candidates["candidate-other"].status == "cancelled"
    assert {item.status for item in executor.notifications.values()} == {"cancelled"}
    assert TravelMatcher().match(executor.travel_intents) == ()


def test_decline_closes_candidate_without_cancelling_either_trip():
    executor = InMemoryBusinessExecutor()
    executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-decline",
        travel_intent_ids=("intent-1", "intent-2"),
        participant_ids=("user-1", "user-2"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    executor.dispatch_travel_notifications("candidate-decline")

    receipt = executor.execute(
        RespondTravelCollaboration("response-decline", "candidate-decline", "decline"),
        _context(message_id="travel-decline-response"),
    )

    assert receipt.after["status"] == "declined"
    assert executor.travel_intents == ()
    assert {item.status for item in executor.notifications.values()} == {"cancelled"}


def test_terminal_travel_candidate_rejects_a_new_response():
    executor = InMemoryBusinessExecutor()
    candidate = executor.seed_travel_candidate(
        tenant_id="tenant-a",
        candidate_id="candidate-closed",
        travel_intent_ids=("intent-1", "intent-2"),
        participant_ids=("user-1", "user-2"),
        destination="南京市",
        overlap_start=datetime(2026, 7, 12, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    candidate.status = "cancelled"

    receipt = executor.execute(
        RespondTravelCollaboration("response-after-close", "candidate-closed", "accept"),
        _context(message_id="travel-after-close"),
    )

    assert receipt.status == "blocked"
    assert receipt.error_code == "travel_candidate_closed"


def test_case_target_resolution_requires_unique_authorized_case():
    cases = (
        CaseRecord("case-1", "tenant-a", "（2026）苏01执1号", "华东公司执行案", ("华东公司",)),
        CaseRecord("case-2", "tenant-a", "（2026）苏01执2号", "华东公司合同案", ("华东公司",)),
        CaseRecord("case-3", "tenant-b", "（2026）苏01执3号", "华东公司外租户案件", ("华东公司",)),
    )

    exact = resolve_case_target("（2026）苏01执1号", cases, _context())
    ambiguous = resolve_case_target("华东公司", cases, _context())

    assert exact.status == "resolved" and exact.case_id == "case-1"
    assert ambiguous.status == "needs_clarification"
    assert set(ambiguous.candidate_case_ids) == {"case-1", "case-2"}


def test_case_target_resolution_supports_external_id_and_confirmed_alias_only():
    cases = (
        CaseRecord(
            "case-1",
            "tenant-a",
            "（2026）苏01执1号",
            "杭州设计院南京江心洲大区精装修及公共区域室内设计合同纠纷",
            ("南京市启洲房地产开发有限公司",),
            external_case_id="SSGL-2407-0010",
            confirmed_aliases=("江心洲设计案",),
        ),
    )

    external = resolve_case_target("SSGL 2407 0010", cases, _context())
    alias = resolve_case_target("江心洲设计案", cases, _context())

    assert external.status == "resolved"
    assert external.case_id == "case-1"
    assert external.match_basis == "exact_external_case_id"
    assert alias.status == "resolved"
    assert alias.case_id == "case-1"
    assert alias.match_basis == "confirmed_case_alias"


def test_case_target_resolution_never_auto_selects_unconfirmed_fuzzy_shorthand():
    cases = (
        CaseRecord(
            "case-1",
            "tenant-a",
            "（2026）苏01执1号",
            "杭州设计院南京江心洲大区精装修及公共区域室内设计合同纠纷",
            ("南京市启洲房地产开发有限公司",),
        ),
    )

    result = resolve_case_target("江心洲", cases, _context())

    assert result.status == "needs_clarification"
    assert result.candidate_case_ids == ("case-1",)


def test_case_target_resolution_accepts_unique_grounded_natural_abbreviation():
    cases = (
        CaseRecord(
            "case-1",
            "tenant-a",
            "SSGL-2603-0007",
            (
                "股份三分（天津）天津市滨海新区妇女儿童医院生态城院区工程"
                "精装修工程2标段施工合同纠纷"
            ),
            (),
            confirmed_aliases=("天津市滨海新区妇女儿童医院生态城院区工程案",),
        ),
    )

    result = resolve_case_target(
        "滨海医院",
        cases,
        _context(),
        source_text="滨海医院预计下周拜访法官沟通回款线索",
    )

    assert result.status == "resolved"
    assert result.case_id == "case-1"


def test_case_target_resolution_keeps_shared_natural_abbreviation_ambiguous():
    cases = (
        CaseRecord("case-1", "tenant-a", "A-1", "海西高新三期装修工程合同纠纷", ()),
        CaseRecord("case-2", "tenant-a", "A-2", "海西高新五期装修工程合同纠纷", ()),
    )

    result = resolve_case_target(
        "海西高新",
        cases,
        _context(),
        source_text="海西高新今日与原告沟通，对方坚持诉状金额，暂未答应",
    )

    assert result.status == "needs_clarification"
    assert set(result.candidate_case_ids) == {"case-1", "case-2"}


def test_case_target_resolution_requires_clarification_for_duplicate_exact_identifiers():
    cases = (
        CaseRecord("case-1", "tenant-a", "（2026）苏01执1号", "案件一", ()),
        CaseRecord("case-2", "tenant-a", "（2026）苏01执1号", "案件二", ()),
    )

    result = resolve_case_target("（2026）苏01执1号", cases, _context())

    assert result.status == "needs_clarification"
    assert set(result.candidate_case_ids) == {"case-1", "case-2"}


def test_case_target_resolution_does_not_prefer_exact_name_when_it_is_another_case_shorthand():
    cases = (
        CaseRecord("case-1", "tenant-a", "A-1", "河北儿童医院装修工程", ()),
        CaseRecord("case-2", "tenant-a", "A-2", "六分公司河北儿童医院装修工程合同纠纷", ()),
    )

    result = resolve_case_target("河北儿童医院装修工程", cases, _context())

    assert result.status == "needs_clarification"
    assert set(result.candidate_case_ids) == {"case-1", "case-2"}


def test_case_progress_create_update_soft_delete_are_versioned_audited_and_idempotent():
    executor = InMemoryBusinessExecutor(cases=(CaseRecord("case-1", "tenant-a", "A-1", "案件一", ()),))
    create = CreateCaseProgress(
        command_id="progress-create-1",
        case_id="case-1",
        occurred_at=NOW,
        progress_type="court_communication",
        summary="法院预计下周重新查控",
        details="与法院沟通所得，尚非正式法院文书",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
    )

    first = executor.execute(create, _context(message_id="progress-message-1"))
    duplicate = executor.execute(create, _context(message_id="progress-message-1"))
    updated = executor.execute(
        UpdateCaseProgress(
            command_id="progress-update-1",
            progress_id=first.resource_id,
            expected_version=1,
            summary="法院预计本周五反馈",
            details=None,
        ),
        _context(message_id="progress-message-2"),
    )
    deleted = executor.execute(
        DeleteCaseProgress("progress-delete-1", first.resource_id, expected_version=2, reason="用户说明为误记"),
        _context(message_id="progress-message-3"),
    )

    assert first.status == "executed"
    assert duplicate.status == "duplicate"
    assert duplicate.receipt_id == first.receipt_id
    assert updated.after["version"] == 2
    assert deleted.after["deleted_at"] is not None
    assert executor.case_progress[first.resource_id].content_origin == "human_record"
    assert len(executor.audit_log) == 3
    assert all(item.actor_user_id == "user-1" and item.tenant_id == "tenant-a" for item in executor.audit_log)


def test_in_memory_executor_allows_only_explicit_cross_owner_progress_create():
    case = CaseRecord(
        "case-1",
        "tenant-a",
        "A-1",
        "协办案件",
        (),
        owner_user_id="user-2",
    )
    executor = InMemoryBusinessExecutor(cases=(case,))
    command = CreateCaseProgress(
        command_id="progress-collaborator-1",
        case_id="case-1",
        occurred_at=NOW,
        progress_type="manual_update",
        summary="与项目公司核对材料",
        details="",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
    )
    legacy = _context(message_id="progress-collaborator-legacy")
    blocked = executor.execute(command, legacy)
    explicit = BusinessCommandContext(
        **{
            **_context(message_id="progress-collaborator-explicit").as_dict(),
            "writable_case_ids": ("case-1",),
        }
    )
    created = executor.execute(command, explicit)

    assert blocked.status == "blocked"
    assert blocked.error_code == "case_not_writable"
    assert created.status == "executed"
    assert created.after["reporter_id"] == "user-1"


def test_same_source_and_semantic_business_command_deduplicates_even_if_model_action_id_changes():
    executor = InMemoryBusinessExecutor(cases=(CaseRecord("case-1", "tenant-a", "A-1", "案件一", ()),))
    first_command = CreateCaseProgress(
        "model-action-id-a",
        "case-1",
        NOW,
        "work_update",
        "法院预计下周重新查控",
        "",
        (),
        (),
        (),
        0.99,
    )
    second_command = CreateCaseProgress(
        "model-action-id-b",
        "case-1",
        NOW,
        "work_update",
        "法院预计下周重新查控",
        "",
        (),
        (),
        (),
        0.99,
    )

    first = executor.execute(first_command, _context(message_id="semantic-replay"))
    replay = executor.execute(second_command, _context(message_id="semantic-replay"))

    assert first.status == "executed"
    assert replay.status == "duplicate"
    assert replay.receipt_id == first.receipt_id
    assert len(executor.case_progress) == 1
    assert len(executor.audit_log) == 1


def test_cross_domain_commands_share_source_but_have_independent_receipts():
    executor = InMemoryBusinessExecutor(cases=(CaseRecord("case-1", "tenant-a", "A-1", "案件一", ()),))
    context = _context(message_id="mixed-message")
    progress = executor.execute(
        CreateCaseProgress(
            "mixed-progress",
            "case-1",
            NOW,
            "work_update",
            "联系法院推进执行",
            "",
            (),
            (),
            (),
            1.0,
        ),
        context,
    )
    travel = executor.execute(
        CreateTravelIntent(
            "mixed-travel",
            "南京",
            "南京市",
            "320100",
            "320000",
            datetime(2026, 7, 12, tzinfo=timezone.utc),
            datetime(2026, 7, 12, 23, 59, tzinfo=timezone.utc),
            "day",
            "",
            ("case-1",),
            0.99,
        ),
        context,
    )

    assert progress.status == travel.status == "executed"
    assert progress.receipt_id != travel.receipt_id
    assert {item.source_message_id for item in executor.audit_log} == {"mixed-message"}
