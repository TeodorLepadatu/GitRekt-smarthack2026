import json

from goal_parser import GoalConfig, GoalParser, GoalStore
from llm import ChatResult


class FakeClient:
    model = "fake"

    def __init__(self, reply: dict):
        self.reply = reply
        self.calls = 0

    def chat_json(self, messages, schema, *, tag=""):
        self.calls += 1
        return ChatResult(json.dumps(self.reply), 0.0, self.model)


SELF = {"objective": "self_consumption", "goals": [], "clarifications": []}


def test_parses_only_on_change_and_keeps_old_config_on_questions(tmp_path):
    client = FakeClient(SELF)
    store = GoalStore(GoalParser(client), tmp_path / "goals.json")

    assert not store.set(["Use my own energy first"]).needs_clarification
    assert store.set(["Use my own energy first"]).source == "stored"
    assert client.calls == 1

    client.reply = {"objective": None, "goals": [],
                    "clarifications": [{"source_text": "Keep some energy", "question": "How much?"}]}
    assert store.add("Keep some energy").needs_clarification
    assert store.config.objective == "self_consumption"
    assert store.sentences == ["Use my own energy first"]

    reloaded = GoalStore(GoalParser(client), tmp_path / "goals.json")
    assert reloaded.config.objective == "self_consumption"


def test_saved_config_round_trips(tmp_path):
    store = GoalStore(GoalParser(), tmp_path / "goals.json")  # rule parser only
    store.set(["Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh"])
    assert len(store.config.goals) == 2

    reloaded = GoalStore(GoalParser(), tmp_path / "goals.json")
    assert reloaded.config.to_dict() == store.config.to_dict()
    assert GoalConfig.from_dict(store.config.to_dict()).to_dict() == store.config.to_dict()


def test_remove_reparses(tmp_path):
    store = GoalStore(GoalParser())
    store.set(["Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh"])
    store.remove(0)
    assert [g.type for g in store.config.goals] == ["min_sell_price"]
