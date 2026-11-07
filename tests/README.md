# tests

Unit tests for `goals/` and `llm/`. They run **without Ollama**: every LLM call is replaced by a
fake client that returns a fixed reply.

## Running

From the repository root:

```bash
python -m pytest -q
```

Use `python -m pytest`, not bare `pytest`. Running it as a module adds the repository root to
`sys.path`, so `import goals` and `import llm` resolve. Bare `pytest` currently fails with
`ModuleNotFoundError`. To fix that, add `pythonpath = .` to a `pytest.ini`, or move `conftest.py`
to the repository root.

## Files

| File | Covers |
|---|---|
| `test_goals.py` | Goal parser post-processing and `GoalStore` |
| `test_decision.py` | Parsing of the decision LLM's output (`parse_decision`) |
| `conftest.py` | Empty; place for shared fixtures |

### `test_goals.py`

| Test | Checks |
|---|---|
| `test_parses_contract_example` | Output matches the plan's goal-config contract exactly. Checks ids, `until = by` default and hard flags |
| `test_vague_goal_asks_back` | Missing amount or time produces a question, not a guess |
| `test_reserve_above_capacity_asks_back` | A reserve above battery capacity is rejected with a question |
| `test_bad_time_asks_back` | Times that aren't `HH:MM` (e.g. `6pm`) produce a question |
| `test_store_parses_only_on_change_and_keeps_old_config_on_questions` | Same sentences don't call the LLM again. A vague edit leaves the previous config active. The config is saved and reloaded |

### `test_decision.py`

| Test | Checks |
|---|---|
| `test_valid_decision` | A contract-conforming reply parses into `LLMDecision` |
| `test_invalid_json` | Non-JSON → `ok` false, `data` None |
| `test_out_of_range_keeps_data_for_validator` | `battery_fraction` 3.15 fails the contract, but `data` and `errors` are kept for the validator |

## The fake client

```python
class FakeClient:
    model = "fake"
    def __init__(self, reply: dict):
        self.reply, self.calls = reply, 0
    def chat_json(self, messages, schema, *, tag=""):
        self.calls += 1
        return ChatResult(json.dumps(self.reply), 0.0, self.model)
```

Pass it as `GoalParser(client=FakeClient(...))` or `DecisionAgent(client=...)`. The reply is
**what the LLM would have returned**, so the tests exercise the deterministic code around the
model, not the model itself. You can change `client.reply` in the middle of a test to simulate a
later parse.

## Not covered yet

- **Live model behaviour.** Nothing checks that a real model (e.g. `qwen3.5:4b`) extracts goals
  correctly or returns valid decisions. The plan's target is at least 99% valid JSON over a
  simulated week. Live runs with Mistral dropped goals, so a live test marked to skip when Ollama is
  unavailable is the next one to add.
- **`OllamaClient` transport.** Timeouts, the `think`-flag retry and JSONL logging are not tested.
  `httpx.MockTransport` would cover them.
