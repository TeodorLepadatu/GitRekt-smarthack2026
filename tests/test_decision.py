from llm import parse_decision

VALID = (
    '{"reasoning": "Price is below break-even.", "battery_fraction": 0.29, "curtail": false,'
    ' "confidence": "medium", "explanation_for_operator": "Charging 0.87 MW."}'
)


def test_valid_decision():
    r = parse_decision(VALID)
    assert r.ok and r.decision.battery_fraction == 0.29


def test_invalid_json():
    r = parse_decision("not json")
    assert not r.ok and r.data is None


def test_out_of_range_keeps_data_for_validator():
    r = parse_decision(VALID.replace("0.29", "3.15"))
    assert not r.ok and r.data["battery_fraction"] == 3.15 and r.errors
