import pytest

from llm import DECISION_SCHEMA, ChatResult, DecisionAgent, LLMError
from validator import decide

VALID = (
    '{"reasoning": "Price is below break-even.", "battery_fraction": 0.29, "curtail": false,'
    ' "confidence": "medium", "explanation_for_operator": "Charging at the goal pace."}'
)
FEATURES = {"price_now": 95, "break_even": 148, "net_mw": 0.4, "matching_fraction": 0.13, "max_charge_mw": 3.0}


class FakeClient:
    model = "fake"

    def __init__(self, reply=VALID, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def chat_json(self, messages, schema, *, tag=""):
        self.calls.append((messages, schema, tag))
        if self.error:
            raise self.error
        return ChatResult(self.reply, 0.0, self.model)


def test_agent_sends_only_the_assembled_prompt_with_the_contract_schema():
    client = FakeClient()
    assert DecisionAgent(client=client)("PROMPT") == VALID
    messages, schema, tag = client.calls[0]
    assert messages == [{"role": "user", "content": "PROMPT"}]
    assert schema == DECISION_SCHEMA and tag == "decision"


def test_agent_raises_when_ollama_fails():
    with pytest.raises(LLMError):
        DecisionAgent(client=FakeClient(error=LLMError("connection refused")))("PROMPT")


def test_validator_uses_agent_output():
    d = decide("PROMPT", FEATURES, llm_call=DecisionAgent(client=FakeClient()))
    assert d.source == "llm" and d.battery_fraction == 0.29


def test_ollama_down_is_reported_as_llm_error():
    agent = DecisionAgent(client=FakeClient(error=LLMError("connection refused")))
    d = decide("PROMPT", FEATURES, llm_call=agent)
    assert d.source == "fallback" and d.fallback_reason == "llm_error"
