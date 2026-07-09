from scripts.generate_agent2_llm_dialogues import _normalize_generated_cases
from scripts.run_agent2_llm_replay_loop import build_notification_text


def test_llm_dialogue_generator_normalizes_non_daily_turns_with_forbidden_text():
    payload = {
        "cases": [
            {
                "dialogue_id": "sample",
                "turns": [
                    {
                        "text": "让我测试下",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "今天完成合同审核",
                        "expected": {"agent2_direct_write": True, "raw_text_written": True},
                    },
                ],
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=1)

    assert cases[0]["source"] == "test_source"
    assert cases[0]["turns"][0]["expected"]["fallback_to_legacy"] is False
    assert cases[0]["turns"][0]["expected"]["raw_text_written"] is False
    assert cases[0]["turns"][0]["expected"]["forbidden_today_work_contains"] == ["让我测试下"]
    assert cases[0]["turns"][1]["expected"]["agent2_direct_write"] is True


def test_llm_dialogue_generator_routes_future_non_daily_forbidden_text_to_tomorrow_field():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "明天穿啥出门",
                        "expected": {"agent2_direct_write": False},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=2)

    assert cases[0]["turns"][0]["expected"]["forbidden_tomorrow_plan_contains"] == ["明天穿啥出门"]


def test_llm_dialogue_generator_normalizes_v4_boundary_expectations():
    payload = {
        "cases": [
            {
                "turns": [
                    {"text": "把‘完成合同审核5份’改成‘完成合同审核6份’，我漏了一份。", "expected": {"agent2_direct_write": False}},
                    {"text": "删掉问题那行，问题已经解决了。", "expected": {"agent2_direct_write": False}},
                    {"text": "算了，写日报吧，今天没啥事。", "expected": {"agent2_direct_write": True}},
                    {"text": "今天地铁挤死了，还迟到了，烦", "expected": {"agent2_direct_write": True}},
                    {"text": "催一下，明天就截止了", "expected": {"agent2_direct_write": True}},
                    {"text": "对，就是那个保利案的开庭，需要带案卷材料。", "expected": {"agent2_direct_write": True}},
                    {"text": "明天把变更同步给开发", "expected": {"agent2_direct_write": False}},
                    {"text": "那明天我拟一份和解协议发你。", "expected": {"agent2_direct_write": False}},
                    {"text": "好，写日报了", "expected": {"agent2_direct_write": True}},
                    {"text": "我刚才说的保利案，明天是不是要交报告？", "expected": {"agent2_direct_write": True}},
                    {"text": "嗯，就是昨天那些，没变化。", "expected": {"agent2_direct_write": True}},
                    {"text": "后天去保利案件开庭但明天穿啥出门吃饭睡觉", "expected": {"agent2_direct_write": True}},
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=22)
    expected = [turn["expected"] for turn in cases[0]["turns"]]

    assert expected[0]["agent2_direct_write"] is True
    assert expected[1]["agent2_direct_write"] is True
    assert expected[2]["agent2_direct_write"] is False
    assert expected[3]["agent2_direct_write"] is False
    assert expected[4]["agent2_direct_write"] is False
    assert expected[5]["agent2_direct_write"] is False
    assert expected[6]["agent2_direct_write"] is True
    assert expected[7]["agent2_direct_write"] is True
    assert expected[8]["agent2_direct_write"] is False
    assert expected[9]["agent2_direct_write"] is False
    assert expected[10]["agent2_direct_write"] is False
    assert expected[11]["agent2_direct_write"] is False


def test_llm_dialogue_generator_normalizes_round9_smoke_boundaries():
    payload = {
        "cases": [
            {
                "turns": [
                    {"text": "我先下班了啊，日报明天再说吧", "expected": {"agent2_direct_write": True}},
                    {"text": "今天日报先不写了。", "expected": {"agent2_direct_write": True}},
                    {"text": "算了，今天热死了，不想动", "expected": {"agent2_direct_write": True}},
                    {"text": "明天去南京见客户", "expected": {"agent2_direct_write": False}},
                    {"text": "把今天的工作也加到昨天日报里", "expected": {"agent2_direct_write": True}},
                    {"text": "啊？我昨天写的什么？", "expected": {"agent2_direct_write": True}},
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=23)
    expected = [turn["expected"] for turn in cases[0]["turns"]]

    assert expected[0]["agent2_direct_write"] is False
    assert expected[1]["agent2_direct_write"] is False
    assert expected[2]["agent2_direct_write"] is False
    assert expected[3]["agent2_direct_write"] is True
    assert expected[4]["agent2_direct_write"] is False
    assert expected[5]["agent2_direct_write"] is False


def test_llm_dialogue_generator_drops_noisy_llm_exact_reply_expectations_by_default():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天完成合同审核",
                        "expected": {
                            "agent2_direct_write": True,
                            "assistant_reply_type": "report_preview",
                            "report_today_work": ["完成合同审核"],
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=3)

    expected = cases[0]["turns"][0]["expected"]
    assert "assistant_reply_type" not in expected
    assert "report_today_work" not in expected
    assert expected["agent2_direct_write"] is True


def test_llm_dialogue_generator_drops_noisy_gate_expectation_by_default():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天食堂红烧肉太咸了",
                        "expected": {
                            "agent2_direct_write": False,
                            "blocked_by_gate": False,
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=4)

    expected = cases[0]["turns"][0]["expected"]
    assert "blocked_by_gate" not in expected
    assert expected["agent2_direct_write"] is False


def test_llm_dialogue_generator_keeps_only_negative_raw_text_written_assertion():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天处理合同",
                        "expected": {
                            "agent2_direct_write": True,
                            "raw_text_written": True,
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=5)

    expected = cases[0]["turns"][0]["expected"]
    assert "raw_text_written" not in expected


def test_llm_dialogue_generator_overrides_false_negative_for_explicit_travel_plan():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "明天去南京出差，帮我订个酒店",
                        "expected": {
                            "agent2_direct_write": False,
                            "forbidden_tomorrow_plan_contains": ["明天去南京出差"],
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=6)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is True
    assert "forbidden_tomorrow_plan_contains" not in expected


def test_llm_dialogue_generator_keeps_bare_daily_start_as_no_direct_write():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "写日报",
                        "expected": {"agent2_direct_write": True},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=7)

    assert cases[0]["turns"][0]["expected"]["agent2_direct_write"] is False


def test_llm_dialogue_generator_does_not_force_day_after_tomorrow_trip_into_daily():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "后天去南京出差参加庭审",
                        "expected": {"agent2_direct_write": False},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=8)

    assert cases[0]["turns"][0]["expected"]["agent2_direct_write"] is False


def test_llm_dialogue_generator_allows_completed_yesterday_plan_by_product_semantics():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "昨天的明日计划是审合同，已经审完了。",
                        "expected": {
                            "agent2_direct_write": False,
                            "forbidden_today_work_contains": ["昨天"],
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=9)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is True
    assert "forbidden_today_work_contains" not in expected


def test_llm_dialogue_generator_allows_prior_intent_completion_by_product_semantics():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "昨天我说今天要去见客户，已经见完了",
                        "expected": {
                            "agent2_direct_write": False,
                            "forbidden_today_work_contains": ["昨天我说今天要去见客户，已经见完了"],
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=12)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is True
    assert "forbidden_today_work_contains" not in expected


def test_llm_dialogue_generator_blocks_daily_meta_request_without_work():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天写日报了没？帮我把今天的活儿记一下",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "今天忙成狗，日报都不想写了",
                        "expected": {"agent2_direct_write": True},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=13)

    for turn in cases[0]["turns"]:
        assert turn["expected"]["agent2_direct_write"] is False
        assert turn["expected"]["raw_text_written"] is False


def test_llm_dialogue_generator_blocks_business_reference_question_without_date():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "那个合同里的争议解决条款是不是改过？我记得之前写的是仲裁",
                        "expected": {"agent2_direct_write": True},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=14)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is False
    assert expected["raw_text_written"] is False


def test_llm_dialogue_generator_blocks_abandoned_daily_and_copy_without_history():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "算了不写了，明天写",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "明天的计划就复制昨天的明日计划吧",
                        "expected": {"agent2_direct_write": True},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=15)

    for turn in cases[0]["turns"]:
        assert turn["expected"]["agent2_direct_write"] is False
        assert turn["expected"]["raw_text_written"] is False


def test_llm_dialogue_generator_keeps_copy_previous_semantics_as_no_direct_write_without_history():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天和昨天一样",
                        "expected": {"agent2_direct_write": True},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=11)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is False
    assert expected["raw_text_written"] is False


def test_llm_dialogue_generator_allows_clear_business_work_by_product_semantics():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天下午跟保利案对方律师沟通了和解方案，他们的条件太苛刻，我们内部还要再讨论。",
                        "expected": {
                            "agent2_direct_write": False,
                            "forbidden_today_work_contains": ["保利案对方律师"],
                        },
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=10)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is True
    assert "forbidden_today_work_contains" not in expected


def test_llm_dialogue_generator_allows_mixed_daily_write_but_forbids_question_fragment():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u4eca\u5929\u5ba1\u4e86\u4fdd\u5229\u6848\u7684\u8865\u5145\u534f\u8bae\uff0c\u987a\u4fbf\u95ee\u4e0b\uff0c\u7528\u5370\u6d41\u7a0b\u8d70\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b\uff1f\u660e\u5929\u7ea6\u4e86\u5ba2\u6237\u8c08\u548c\u89e3\u65b9\u6848\u3002",
                        "expected": {"agent2_direct_write": False},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=16)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is True
    assert expected["forbidden_today_work_contains"] == ["\u7528\u5370\u6d41\u7a0b\u8d70\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b\uff1f"]


def test_llm_dialogue_generator_aligns_v4_followup_oracle_boundaries():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u55ef\uff0c\u5199\u65e5\u62a5\u5427\uff0c\u5c31\u6309\u521a\u624d\u8bf4\u7684\uff0c\u628a\u62d6\u5ef6\u90a3\u4e2a\u4f5c\u4e3a\u95ee\u9898\u8bb0\u4e0a",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "\u5c31\u5199\u6628\u5929\u7684\u65e5\u62a5\u91cc\uff0c\u4eca\u5929\u7684\u5de5\u4f5c\u8fd8\u6ca1\u5f00\u59cb",
                        "expected": {"agent2_direct_write": True},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=17)

    first, second = [turn["expected"] for turn in cases[0]["turns"]]
    assert first["agent2_direct_write"] is True
    assert first["raw_text_written"] is False
    assert second["agent2_direct_write"] is False
    assert second["raw_text_written"] is False


def test_llm_dialogue_generator_keeps_absurd_business_shaped_text_blocked():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "今天我变成了一只猫，抓了三个老鼠，明天计划去太空。",
                        "expected": {"agent2_direct_write": True},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=11)

    expected = cases[0]["turns"][0]["expected"]
    assert expected["agent2_direct_write"] is False
    assert expected["forbidden_tomorrow_plan_contains"] == ["今天我变成了一只猫，抓了三个老鼠，明天计划去太空。"]


def test_llm_dialogue_generator_allows_followup_business_fragments_by_product_semantics():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u5bf9\u4e86\uff0c\u8fd8\u6709\u5408\u540c\u4e5f\u5ba1\u6838\u4e863\u4efd",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "\u5c31\u662f\u63a5\u7740\u5ba1\u6838\u5408\u540c\uff0c\u6ca1\u5565\u7279\u522b\u7684",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "\u4e0b\u5348\u548c\u5ba2\u6237\u6c9f\u901a\u4e86\u9700\u6c42",
                        "expected": {"agent2_direct_write": False},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=12)

    for turn in cases[0]["turns"]:
        expected = turn["expected"]
        assert expected["agent2_direct_write"] is True
        assert "forbidden_today_work_contains" not in expected


def test_llm_dialogue_generator_blocks_historical_daily_mutation_and_no_history_completion():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u5e2e\u6211\u628a\u8fd9\u4e2a\u5199\u8fdb\u6628\u5929\u7684\u65e5\u62a5",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u7ecf\u641e\u5b9a\u4e86\uff0c\u53ef\u4ee5\u5220\u6389\u4eca\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5417\uff1f",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u660e\u5929\u522b\u5fd8\u4e86\u5e26\u6750\u6599\u3002",
                        "expected": {"agent2_direct_write": False},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=13)

    first, second, third = [turn["expected"] for turn in cases[0]["turns"]]
    assert first["agent2_direct_write"] is False
    assert second["agent2_direct_write"] is False
    assert third["agent2_direct_write"] is True


def test_llm_dialogue_generator_keeps_non_tomorrow_future_schedule_out_of_daily():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u540e\u5929\u53bb\u4fdd\u5229\u6848\u4ef6\u5f00\u5ead\uff0c\u987a\u4fbf\u5e26\u70b9\u96f6\u98df",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u4e0b\u5468\u53bb\u5357\u4eac\u51fa\u5dee\u5904\u7406\u6750\u6599",
                        "expected": {"agent2_direct_write": True},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=14)

    for turn in cases[0]["turns"]:
        expected = turn["expected"]
        assert expected["agent2_direct_write"] is False
        assert expected["raw_text_written"] is False


def test_llm_dialogue_generator_aligns_case_candidate_and_tomorrow_trip_semantics():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u6052\u5927\u6848\u6709\u65b0\u8fdb\u5c55\uff0c\u5224\u51b3\u4e0b\u4e86\uff0c\u5bf9\u65b9\u8fd8\u4e0d\u4e0a\u94b1",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u660e\u5929\u53bb\u5357\u4eac\u5206\u6240\u51fa\u5dee\uff0c\u4e0b\u53482\u70b9\u7684\u9ad8\u94c1\uff0c\u540e\u5929\u8ddf\u4fdd\u5229\u6848\u5bf9\u65b9\u5f8b\u5e08\u78b0\u9762\u3002",
                        "expected": {"agent2_direct_write": False},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=15)
    case_progress, tomorrow_trip = [turn["expected"] for turn in cases[0]["turns"]]

    assert case_progress["agent2_direct_write"] is False
    assert tomorrow_trip["agent2_direct_write"] is True


def test_llm_dialogue_generator_allows_today_case_progress_as_daily_work():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u4fdd\u5229\u6848\u4eca\u5929\u5f00\u5ead\u4e86\uff0c\u6211\u4eec\u8d62\u4e86\uff0c\u54c8\u54c8\uff0c\u665a\u4e0a\u5403\u987f\u597d\u7684\u5e86\u795d\u4e0b",
                        "expected": {"agent2_direct_write": False},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=16)

    assert cases[0]["turns"][0]["expected"]["agent2_direct_write"] is True


def test_llm_dialogue_generator_allows_current_work_inside_yesterday_plan_sentence():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u5bf9\uff0c\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210\uff0c\u4eca\u5929\u518d\u628a\u8865\u5145\u534f\u8bae\u5b9a\u7a3f",
                        "expected": {"agent2_direct_write": False, "forbidden_today_work_contains": "\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212"},
                    }
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=17)
    expected = cases[0]["turns"][0]["expected"]

    assert expected["agent2_direct_write"] is True
    assert "forbidden_today_work_contains" not in expected


def test_llm_dialogue_generator_normalizes_non_daily_service_and_confirmation_turns():
    payload = {
        "cases": [
            {
                "turns": [
                    {"text": "\u786e\u8ba4", "expected": {"agent2_direct_write": True}},
                    {
                        "text": "\u5bf9\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211\u3002",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u50ac\u4e00\u4e0b\u6ca1\u4ea4\u7684\u5427\uff0cdeadline\u5c31\u662f\u4eca\u5929\uff0c\u518d\u62d6\u5c31\u6263\u7ee9\u6548\u4e86",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u5bf9\u4e86\uff0c\u90a3\u4e2a\u5408\u540cD\u7684\u7528\u5370\u7533\u8bf7\u7cfb\u7edf\u600e\u4e48\u63d0\u554a\uff1f\u6211\u660e\u5929\u6025\u7740\u8981\u7528\u5370\uff0c\u6015\u641e\u9519\u3002",
                        "expected": {"agent2_direct_write": True},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=18)

    assert all(turn["expected"]["agent2_direct_write"] is False for turn in cases[0]["turns"])


def test_llm_dialogue_generator_normalizes_done_without_business_content_as_no_write():
    payload = {
        "cases": [
            {
                "dialogue_id": "done-without-content",
                "turns": [
                    {"text": "\u6ca1\u4e86\uff0c\u5c31\u8fd9\u4e9b", "expected": {"agent2_direct_write": True}},
                    {"text": "\u641e\u5b9a\u4e86\uff0c\u4eca\u5929\u5b8c\u4e8b\u3002", "expected": {"agent2_direct_write": True}},
                    {"text": "\u6ca1\u5565\u7279\u522b\u7684\uff0c\u8fd8\u662f\u90a3\u4e9b\u4e8b\u3002", "expected": {"agent2_direct_write": True}},
                    {"text": "\u54e6\uff0c\u5176\u5b9e\u4eca\u5929\u6478\u9c7c\u4e86\uff0c\u4f46\u522b\u5199\u4e0a\u53bb\u54c8\u3002", "expected": {"agent2_direct_write": True}},
                    {"text": "\u6d4b\u8bd5\u4e00\u4e0b\uff0c\u5199\u65e5\u62a5\uff1a\u4eca\u5929\u6ca1\u5565\u4e8b", "expected": {"agent2_direct_write": True}},
                ],
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=19)
    assert all(turn["expected"]["agent2_direct_write"] is False for turn in cases[0]["turns"])


def test_llm_dialogue_generator_allows_concrete_today_court_and_arbitration_work():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u90a3\u5e2e\u6211\u8bb0\u4e00\u4e0b\uff0c\u4e0b\u5348\u8ddf\u5f8b\u5e08\u786e\u8ba4\u4e86\u4fdd\u5168\u88c1\u5b9a\u4e66\u5df2\u7ecf\u6536\u5230\u4e86",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "\u54e6\u5bf9\u4e86\uff0c\u4e0b\u5348\u7ec8\u4e8e\u628a\u90a3\u4e2a\u6d89\u5916\u4ef2\u88c1\u7684\u6750\u6599\u5bc4\u51fa\u53bb\u4e86\uff0c\u987a\u4e30\u5355\u53f7SF123456",
                        "expected": {"agent2_direct_write": False},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=19)

    assert all(turn["expected"]["agent2_direct_write"] is True for turn in cases[0]["turns"])


def test_llm_dialogue_generator_distinguishes_lookup_request_from_research_work():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211",
                        "expected": {"agent2_direct_write": True},
                    },
                    {
                        "text": "\u4eca\u5929\u67e5\u9605\u4e86\u4fdd\u5229\u6848\u4ef6\u8d44\u6599",
                        "expected": {"agent2_direct_write": False},
                    },
                    {
                        "text": "\u4eca\u5929\u53bb\u67e5\u4e86XX\u6848\u4ef6\u8d44\u6599",
                        "expected": {"agent2_direct_write": False},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=21)
    expected = [turn["expected"]["agent2_direct_write"] for turn in cases[0]["turns"]]

    assert expected == [False, True, True]


def test_llm_dialogue_generator_allows_completed_yesterday_plan_when_concrete_current_content_exists():
    payload = {
        "cases": [
            {
                "turns": [
                    {
                        "text": "\u5bf9\u4e86\uff0c\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u91cc\u90a3\u4e2a\u5f85\u529e\uff0c\u8ddf\u5ba1\u8ba1\u5bf9\u63a5\u7684\u4e8b\u6211\u641e\u5b8c\u4e86",
                        "expected": {"agent2_direct_write": False, "forbidden_today_work_contains": "\u5f85\u529e"},
                    },
                    {
                        "text": "\u6628\u5929\u65e5\u62a5\u91cc\u7684\u660e\u65e5\u8ba1\u5212\uff1a\u8ddf\u6cd5\u52a1\u603b\u76d1\u6c47\u62a5\u5408\u540cB\u7684\u98ce\u9669\uff0c\u4eca\u5929\u4e0a\u5348\u5df2\u7ecf\u6c47\u62a5\u5b8c\u4e86\uff0c\u603b\u76d1\u540c\u610f\u6309\u539f\u65b9\u6848\u8d70\u3002",
                        "expected": {"agent2_direct_write": False, "forbidden_today_work_contains": "\u6628\u5929"},
                    },
                ]
            }
        ]
    }

    cases = _normalize_generated_cases(payload, source="test_source", batch_index=20)

    for turn in cases[0]["turns"]:
        assert turn["expected"]["agent2_direct_write"] is True
        assert "forbidden_today_work_contains" not in turn["expected"]


def test_llm_replay_loop_notification_summarizes_gray_readiness(tmp_path):
    text = build_notification_text(
        status="failed",
        generated_path=tmp_path / "cases.jsonl",
        reports_dir=tmp_path / "reports",
        summary={
            "total_dialogues": 20,
            "total_turns": 61,
            "failed_dialogues": 3,
            "mismatch_count": 4,
            "risk_turn_count": 2,
            "unexpected_direct_write_count": 1,
            "fallback_to_legacy_count": 0,
            "gray_ready": False,
        },
    )

    assert "状态：未通过，需要修复后重跑" in text
    assert "对话数：20" in text
    assert "灰测条件：不满足" in text
