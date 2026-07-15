from __future__ import annotations

from app.agent2.business.case_reference import (
    discover_visible_case_references,
    match_grounded_visible_cases,
)


BINHAI_CASE = {
    "case_id": "case-binhai",
    "case_number": "SSGL-2603-0007",
    "case_name": (
        "股份三分（天津）天津市滨海新区妇女儿童医院生态城院区工程"
        "精装修工程2标段施工合同纠纷"
    ),
    "external_case_id": "plaintiff:SSGL-2603-0007",
    "confirmed_aliases": ["天津市滨海新区妇女儿童医院生态城院区工程案"],
    "version": 1,
}


def test_grounded_natural_abbreviation_resolves_only_visible_unique_case() -> None:
    matches = match_grounded_visible_cases(
        "滨海医院",
        segment_text="滨海医院预计下周拜访法官沟通回款线索",
        raw_cases=[BINHAI_CASE],
    )

    assert [item.case["case_id"] for item in matches] == ["case-binhai"]


def test_natural_abbreviation_must_be_grounded_in_exact_source_segment() -> None:
    matches = match_grounded_visible_cases(
        "滨海医院",
        segment_text="预计下周拜访法官沟通回款线索",
        raw_cases=[BINHAI_CASE],
    )

    assert matches == ()


def test_generic_short_reference_never_uses_derived_abbreviation() -> None:
    matches = match_grounded_visible_cases(
        "医院",
        segment_text="医院预计下周能有执行回款",
        raw_cases=[BINHAI_CASE],
    )

    assert matches == ()


def test_shared_abbreviation_returns_every_candidate_without_guessing() -> None:
    cases = [
        {
            "case_id": "case-1",
            "case_number": "SSGL-2603-0013",
            "case_name": "海西高新五期装修工程合同纠纷",
            "confirmed_aliases": [],
            "version": 1,
        },
        {
            "case_id": "case-2",
            "case_number": "SSGL-2603-0014",
            "case_name": "海西高新三期装修工程合同纠纷",
            "confirmed_aliases": [],
            "version": 1,
        },
    ]

    matches = match_grounded_visible_cases(
        "海西高新",
        segment_text="海西高新今日与原告沟通，对方坚持诉状金额，暂未答应",
        raw_cases=cases,
    )

    assert {item.case["case_id"] for item in matches} == {"case-1", "case-2"}


def test_exact_confirmed_alias_outranks_another_cases_fuzzy_alias_match() -> None:
    cases = [
        {
            "case_id": "case-exact",
            "case_number": "BGGL-2512-0024",
            "case_name": "荣和五象学府北区工程李兆山劳务合同纠纷",
            "confirmed_aliases": ["荣和五象学府北区1、2、3、6、8、10案"],
            "version": 1,
        },
        {
            "case_id": "case-fuzzy",
            "case_number": "BGGL-2512-0025",
            "case_name": "荣和五象学府北区工程李兆法劳务合同纠纷",
            "confirmed_aliases": ["荣和五象学府北区1、2、3、6、8、10号楼李兆法案"],
            "version": 1,
        },
    ]
    reference = "荣和五象学府北区1、2、3、6、8、10案"

    matches = match_grounded_visible_cases(
        reference,
        segment_text=f"{reference}今天与当事人沟通",
        raw_cases=cases,
    )

    assert [item.case["case_id"] for item in matches] == ["case-exact"]


def test_same_exact_confirmed_alias_on_multiple_cases_remains_ambiguous() -> None:
    cases = [
        {
            "case_id": "case-1",
            "case_number": "A-1",
            "case_name": "甲项目合同纠纷",
            "confirmed_aliases": ["共同项目案"],
            "version": 1,
        },
        {
            "case_id": "case-2",
            "case_number": "A-2",
            "case_name": "乙项目合同纠纷",
            "confirmed_aliases": ["共同项目案"],
            "version": 1,
        },
    ]

    matches = match_grounded_visible_cases(
        "共同项目案",
        segment_text="共同项目案今天与法院沟通",
        raw_cases=cases,
    )

    assert {item.case["case_id"] for item in matches} == {"case-1", "case-2"}


def test_action_free_discovery_supports_unique_natural_abbreviation() -> None:
    matches = discover_visible_case_references(
        "滨海医院预计下周能有执行回款",
        [BINHAI_CASE],
    )

    assert [item.case["case_id"] for item in matches] == ["case-binhai"]
    assert matches[0].reference == "滨海医院"


def test_action_free_discovery_keeps_shared_project_ambiguous() -> None:
    cases = [
        {
            "case_id": "case-1",
            "case_number": "A-1",
            "case_name": "海西高新三期装修工程合同纠纷",
            "confirmed_aliases": [],
        },
        {
            "case_id": "case-2",
            "case_number": "A-2",
            "case_name": "海西高新五期装修工程合同纠纷",
            "confirmed_aliases": [],
        },
    ]

    matches = discover_visible_case_references(
        "·海西高新今日与原告沟通，对方坚持诉状金额，暂未答应",
        cases,
    )

    assert {item.case["case_id"] for item in matches} == {"case-1", "case-2"}
    assert {item.reference for item in matches} == {"海西高新"}


def test_trusted_two_character_alias_can_trigger_reassessment_but_not_fuzzy_inference() -> None:
    case = {
        "case_id": "case-junrui",
        "case_number": "A-3",
        "case_name": "君瑞国际财富中心建设工程合同纠纷",
        "confirmed_aliases": ["君瑞"],
    }

    discovered = discover_visible_case_references("君瑞明天沟通调解", [case])
    inferred = match_grounded_visible_cases(
        "财富",
        segment_text="财富明天沟通调解",
        raw_cases=[case],
    )

    assert [item.case["case_id"] for item in discovered] == ["case-junrui"]
    assert inferred == ()


def test_two_different_case_references_never_drop_the_shorter_match() -> None:
    cases = [
        {
            "case_id": "case-long",
            "case_number": "A-1",
            "case_name": "滨海新区医院建设工程合同纠纷",
            "confirmed_aliases": ["滨海新区医院"],
        },
        {
            "case_id": "case-short",
            "case_number": "A-2",
            "case_name": "君瑞国际财富中心合同纠纷",
            "confirmed_aliases": ["君瑞"],
        },
    ]

    matches = discover_visible_case_references(
        "滨海新区医院预计回款，君瑞明天沟通调解",
        cases,
    )

    assert {item.case["case_id"] for item in matches} == {
        "case-long",
        "case-short",
    }
