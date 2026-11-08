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


def test_each_sentence_is_parsed_once_so_edits_never_reparse_old_goals(tmp_path):
    client = FakeClient(SELF)
    store = GoalStore(GoalParser(client), tmp_path / "goals.json")
    store.set(["Use my own energy first"])
    client.reply = {"objective": None, "clarifications": [], "goals": [
        {"source_text": "Store 6 MWh by 18:00", "type": "reserve", "amount": 6, "unit": "MWh",
         "by": "18:00", "until": None}]}
    assert not store.add("Store 6 MWh by 18:00").needs_clarification
    assert client.calls == 2                                     # only the new sentence

    # the LLM now answers everything with a question: removing still works, without a call
    client.reply = {"objective": None, "goals": [],
                    "clarifications": [{"source_text": "x", "question": "What?"}]}
    assert not store.remove(0).needs_clarification and client.calls == 2
    assert store.config.objective == "max_profit"                # the objective sentence is gone
    assert [g.id for g in store.config.goals] == ["g1"] and store.sentence_of("g1") == 0

    reloaded = GoalStore(GoalParser(client), tmp_path / "goals.json")
    assert reloaded.sentence_of("g1") == 0 and client.calls == 2  # the parses are saved too


def test_later_sentence_wins_for_the_same_goal():
    store = GoalStore(GoalParser())
    store.set(["Never sell below 50 EUR/MWh", "Store 6 MWh by 18:00", "Never sell below 60 EUR/MWh"])
    assert [(g.id, g.type) for g in store.config.goals] == [("g1", "min_sell_price"), ("g2", "reserve")]
    assert store.config.goals[0].eur_per_mwh == 60 and store.sentence_of("g1") == 2


def test_old_file_without_parts_still_loads(tmp_path):
    path = tmp_path / "goals.json"
    path.write_text(json.dumps({"sentences": ["Store 6 MWh by 18:00"], "config": {"objective": "max_profit",
                    "goals": [{"id": "g1", "type": "reserve", "mwh": 6, "by": "18:00", "until": "18:00",
                               "hard": True, "source_text": "Store 6 MWh by 18:00"}]}}), encoding="utf-8")
    store = GoalStore(GoalParser(), path)
    assert store.sentence_of("g1") == 0 and not store.remove(0).needs_clarification
    assert store.config.goals == []
