from app.utils.json import extract_json_object


def test_extract_json_object_from_plain_json():
    assert extract_json_object('{"today_work": [], "completeness": 0}')["today_work"] == []


def test_extract_json_object_from_fenced_json():
    data = extract_json_object('```json\n{"problems": ["x"]}\n```')
    assert data["problems"] == ["x"]
