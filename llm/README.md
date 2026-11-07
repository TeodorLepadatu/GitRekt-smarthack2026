# llm — the shared Ollama client and the battery decision step

Every 15 minutes the small LLM chooses one battery action. It **compares numbers that are
already computed and never calculates**. It never touches hardware either: what it returns goes
to the validator and then the guard layer.

```
prompt assembly ──► validator.decide(prompt, features) ──► Decision ──► guard
                          │  llm_call (default)
                    DecisionAgent(prompt) ──► raw text
                          │
                    OllamaClient.chat_json()  (temp 0, JSON schema, num_ctx 8192)
```

## Files

| File | Contents |
|---|---|
| `ollama_client.py` | `OllamaClient`, `ChatResult`, `LLMError`, the `ChatClient` protocol. The **only** Ollama client in the project: the goal parser and the decision step both use it |
| `decision.py` | `DecisionAgent`, the `LLMDecision` contract and its `DECISION_SCHEMA` |

## Decision contract

What the LLM must return:

```json
{
  "reasoning": "The price is EUR 53 below break-even ... so I charge at the goal pace.",
  "battery_fraction": 0.29,
  "curtail": false,
  "confidence": "medium",
  "explanation_for_operator": "Charging 0.87 MW to reach the 6 MWh goal by 18:00."
}
```

| Field | Type | Meaning |
|---|---|---|
| `reasoning` | str | Short comparison: price vs break-even, timing, SoC, uncertainty, goals |
| `battery_fraction` | float in [-1, 1] | Share of max power. `+` charge, `−` discharge, `0` idle |
| `curtail` | bool | Curtail surplus production (the guard allows it only when price < 0) |
| `confidence` | `low` / `medium` / `high` | Model's own confidence |
| `explanation_for_operator` | str | One sentence for the dashboard |

`reasoning` comes first on purpose. The model writes its explanation before it picks numbers,
because fields are generated in schema order.

## Usage

Normally you don't call the agent yourself: `pipeline.run_cycle()` (or `validator.decide()`) uses
it by default. To call it directly:

```python
from llm import DecisionAgent
from validator import decide

agent = DecisionAgent()                     # model from config.Settings.decision_model
raw = agent(prompt_text)                    # raw JSON text; raises LLMError if Ollama fails
decision = decide(prompt_text, features, llm_call=agent)   # parsed, checked, fallback on failure
```

The agent **does not parse or validate**. That is the validator's job (`validator/validator.py`),
so there is one definition of a valid decision. `LLMDecision` exists only to produce
`DECISION_SCHEMA`, which Ollama uses to constrain decoding. Ollama errors are raised as `LLMError`
so the validator records them as `llm_error` in the reliability metrics.

The assembled prompt already contains the role, rules and output format, so no system message is
sent by default. `DecisionAgent(system_prompt=...)` adds one if needed.

## Ollama client

`OllamaClient(model).chat_json(messages, schema, tag="...")` posts to `/api/chat` with:

- `temperature: 0` and `seed: 0` for repeatable runs
- `num_ctx: 8192`. Ollama's default context can be about 4k tokens, which is too small for the
  roughly 2.5k-token prompt plus its output
- `format: <JSON schema>` (Ollama structured outputs), which limits generation to the contract
- `think: false` by default. Qwen3-family models think by default, which adds latency. If a model
  rejects the flag, the client retries without it

Timeouts and HTTP errors raise `LLMError`.

### Logging

Every call, failed ones included, is appended to a JSONL file. Each record holds the timestamp, tag
(`decision` or `goal_parser`), model, messages, response, thinking, latency and error. Logging
failures are ignored so they can never break a control cycle.

### Testing without Ollama

Anything with a `model` attribute and a `chat_json(messages, schema, *, tag="")` method that
returns a `ChatResult` can be passed as `client=`. See the `FakeClient` in `tests/test_decision.py` or `tests/test_pipeline.py`.

## Configuration

| Variable | Default | |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | |
| `ENERGY_AGENT_MODEL` | `qwen3.5:4b` | decision model (must be pulled: `ollama pull qwen3.5:4b`) |
| `ENERGY_AGENT_GOAL_MODEL` | `ENERGY_AGENT_MODEL` | goal parser model |
| `ENERGY_AGENT_TIMEOUT_S` | `60` | per request |
| `ENERGY_AGENT_NUM_CTX` | `8192` | |
| `ENERGY_AGENT_THINK` | `false` | `true` to enable thinking; empty to not send the flag |
| `ENERGY_AGENT_LLM_LOG` | `logs/llm_calls.jsonl` | empty string disables logging |

## Not in this folder

- **Validator + fallback:** parsing, range clamping and the rule-based policy that runs when the LLM fails
- **Guard layer:** MW conversion, SoC and power limits, hard-goal enforcement
- **Prompt assembly:** building the ~2.5k-token prompt that `decide()` receives
