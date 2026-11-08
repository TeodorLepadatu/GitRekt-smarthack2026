# tests

All the project's Python tests, in one folder. They run **without Ollama**: every LLM call is
replaced by a fake client that returns a fixed reply.

## Running

From the repository root:

```bash
pytest -q
```

`pytest.ini` puts the repository root on `sys.path` and collects `tests/`, so this one command
runs everything.

## Files

| File | Covers |
|---|---|
| `test_pipeline.py` | End to end: goal parser → prompt assembly → decision agent (fake) → validator → guard |
| `test_prompt_assembly.py` | Solved-example numbers recomputed from `battery.BatteryConfig`; prompt rendering |
| `test_goal_parser.py` | Rule parser, checks on the LLM's output, unit/time conversion, clarifications |
| `test_goal_parser_llm.py` | Goal parser against the real model; skipped unless Ollama has `Settings().goal_model` |
| `test_goal_store.py` | `GoalStore`: parse only on change, keep the old config while questions are open, save/reload |
| `test_decision.py` | `DecisionAgent` sends the prompt with the schema; Ollama errors reach the validator as `llm_error` |
| `test_validator.py` | Validator: JSON parsing, required fields, ranges, fallback decision |
| `test_guard.py` | Guard: battery limits and hard goals (reserve, minimum sell) override the decision |
| `test_settlement.py` | Settlement: money per interval, goal status, day totals |
| `test_simulator.py` | Site simulator: battery limits, grid trade, curtailment, energy balance, full cycles |
| `test_simulator_settlement.py` | Settlement uses exactly the simulator's physics; ledger and simulator stay in step over a day loop |
| `test_loop.py` | The clock: a whole day through feature builder → pipeline → simulator → settlement, failures, real-time pacing, CLI |
| `test_backend.py` | Dashboard API: every route and shape, run/pause/reset, goals, SSE on a real server |

### `test_pipeline.py`

| Test | Checks |
|---|---|
| `test_plan_example_runs_end_to_end_with_goal_parser_object` | Plan section 5 example: 0.29 → 0.87 MW, goals checked by the guard |
| `test_object_and_dict_goal_config_give_the_same_result` | `GoalConfig` and its `to_dict()` are interchangeable |
| `test_guard_enforces_goal_against_llm` | An LLM that sells before a reserve deadline is overridden (forced charge) |
| `test_ollama_down_falls_back_and_still_meets_goal_pace` | LLM failure → fallback, reason `llm_error`, still at goal pace |
| `test_broken_features_never_reach_the_llm` | Prompt assembly failure → fallback (`prompt_error`), guard still runs |
| `test_guard_uses_the_given_battery` | The battery passed to `run_cycle` is the one the guard uses |

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

It implements `llm.ChatClient`, so it works for both `GoalParser(FakeClient(...))` and
`DecisionAgent(client=FakeClient(...))`. The reply is **what the LLM would have returned**, so the
tests exercise the deterministic code around the model, not the model itself.

## Not covered yet

- **Live decision model.** Nothing checks that a real model returns valid decisions. The plan's
  target is at least 99% valid JSON over a simulated week.
- **`OllamaClient` transport.** Timeouts, the `think`-flag retry and JSONL logging are not tested.
  `httpx.MockTransport` would cover them.
