from scripts.check_dingtalk_robot_identity_alignment import evaluate_robot_identity


def test_identity_gate_fails_when_explicit_robot_code_is_missing() -> None:
    result = evaluate_robot_identity(
        configured_robot_code="",
        app_key="active-robot",
        app_secret_present=True,
        active_robots=(
            {"robot_code": "active-robot", "events": 1250, "users": 71},
        ),
    )

    assert result["status"] == "FAIL"
    assert result["outbound_matches_active_robot"] is False


def test_identity_gate_passes_only_for_active_robot_with_credentials() -> None:
    result = evaluate_robot_identity(
        configured_robot_code="active-robot",
        app_key="active-robot",
        app_secret_present=True,
        active_robots=(
            {"robot_code": "active-robot", "events": 1250, "users": 71},
        ),
    )

    assert result["status"] == "PASS"
    assert result["outbound_matches_active_robot"] is True
    assert result["app_key_present"] is True
    assert result["app_secret_present"] is True


def test_identity_gate_rejects_multiple_active_robots() -> None:
    result = evaluate_robot_identity(
        configured_robot_code="robot-a",
        app_key="robot-a",
        app_secret_present=True,
        active_robots=(
            {"robot_code": "robot-a", "events": 100, "users": 10},
            {"robot_code": "robot-b", "events": 20, "users": 2},
        ),
    )

    assert result["status"] == "FAIL"
    assert result["active_inbound_robot_count"] == 2
