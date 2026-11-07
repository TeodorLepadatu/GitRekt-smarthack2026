import json

from goals import GoalParser, GoalStore
from llm.ollama_client import ChatResult


class FakeClient:
    model = "fake"

    def __init__(self, reply: dict):
        self.reply = reply
        self.calls = 0

    def chat_json(self, messages, schema, *, tag=""):
        self.calls += 1
        return ChatResult(json.dumps(self.reply), 0.0, self.model)


def parser(reply):
    return GoalParser(client=FakeClient(reply))


def test_parses_contract_example():
    result = parser({
        "objective": "max_profit",
        "goals": [
            {"type": "reserve", "source_text": "Store 6 MWh by 18:00", "mwh": 6, "by": "18:00"},
            {"type": "min_sell_price", "source_text": "Never sell below 50 EUR/MWh", "eur_per_mwh": 50},
        ],
        "questions": [],
    }).parse(["Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh"])

    assert result.ok
    assert result.config.model_dump() == {
        "objective": "max_profit",
        "goals": [
            {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": True,
             "source_text": "Store 6 MWh by 18:00"},
            {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50.0, "hard": True,
             "source_text": "Never sell below 50 EUR/MWh"},
        ],
    }


def test_vague_goal_asks_back():
    result = parser({"goals": [{"type": "reserve", "source_text": "Keep some energy", "mwh": None}]}).parse(
        ["Keep some energy"]
    )
    assert not result.ok and result.questions


def test_reserve_above_capacity_asks_back():
    result = parser({"goals": [{"type": "reserve", "source_text": "Store 50 MWh by 18:00", "mwh": 50, "by": "18:00"}]}).parse(
        ["Store 50 MWh by 18:00"]
    )
    assert not result.ok and "capacity" in result.questions[0]


def test_bad_time_asks_back():
    result = parser({"goals": [{"type": "reserve", "source_text": "x", "mwh": 2, "by": "6pm"}]}).parse(["x"])
    assert not result.ok and "HH:MM" in result.questions[0]


def test_store_parses_only_on_change_and_keeps_old_config_on_questions(tmp_path):
    client = FakeClient({"objective": "self_consumption", "goals": [], "questions": []})
    store = GoalStore(GoalParser(client=client), tmp_path / "goals.json")

    assert store.set(["Use my own energy first"]).ok
    store.set(["Use my own energy first"])
    assert client.calls == 1

    client.reply = {"goals": [], "questions": ["How much?"]}
    assert not store.add("Keep some energy").ok
    assert store.config.objective == "self_consumption"
    assert store.sentences == ["Use my own energy first"]

    reloaded = GoalStore(GoalParser(client=client), tmp_path / "goals.json")
    assert reloaded.config.objective == "self_consumption"
