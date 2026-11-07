# llm — Ollama client and battery decision step

Every 15 minutes the small LLM chooses one battery action. It **compares numbers that are
already computed and never calculates**. It never touches hardware either: what it returns goes
to the validator and then the guard layer.

```
prompt assembly ──► DecisionAgent.decide(prompt) ──► DecisionResult ──► validator + fallback
                          │
                    OllamaClient.chat_json()  (temp 0, JSON schema, num_ctx 8192)
```

## Files

| File | Contents |
|---|---|
| `ollama_client.py` | `OllamaClient`, `ChatResult`, `LLMError`, the `ChatClient` protocol |
| `decision.py` | `DecisionAgent`, the `LLMDecision` contract, `DecisionResult`, `parse_decision()` |

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

```python
from llm import DecisionAgent

agent = DecisionAgent()
result = agent.decide(prompt_text)   # full prompt from prompt assembly

if result.ok:
    d = result.decision              # LLMDecision
    print(d.battery_fraction, d.explanation_for_operator)
else:
    print(result.error, result.errors)   # hand to the fallback policy
```

### `DecisionResult`

`decide()` **never raises**. Every failure ends up in the result:

| Field | Set when |
|---|---|
| `decision` | Output is valid JSON **and** matches `LLMDecision` |
| `raw` | Always, if the model answered (exact text) |
| `data` | The output was a JSON object, even one that breaks the contract. The validator can inspect it |
| `error` | Timeout, HTTP failure, invalid JSON, or a contract mismatch |
| `errors` | Per-field validation messages, e.g. `battery_fraction: Input should be less than or equal to 1` |
| `latency_s`, `model` | For the latency and reliability metrics |

`parse_decision(raw)` is the same parsing step as a standalone function. It also strips Markdown
code fences around the JSON. Use it to replay logged responses.

### Custom system prompt

`DecisionAgent(system_prompt=...)` replaces the short default system message. The main
instructions (rules, solved examples, current situation, USER GOALS) belong in the user prompt,
which prompt assembly builds.

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
returns a `ChatResult` can be passed as `client=`. See the `FakeClient` in `tests/test_goals.py`.

## Configuration

| Variable | Default | |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | |
| `ENERGY_AGENT_MODEL` | `qwen3.5:4b` | decision model (must be pulled: `ollama pull qwen3.5:4b`) |
| `ENERGY_AGENT_TIMEOUT_S` | `60` | per request |
| `ENERGY_AGENT_NUM_CTX` | `8192` | |
| `ENERGY_AGENT_THINK` | `false` | `true` to enable thinking; empty to not send the flag |
| `ENERGY_AGENT_LLM_LOG` | `logs/llm_calls.jsonl` | empty string disables logging |

## Not in this folder

- **Validator + fallback:** range clamping and the rule-based policy that runs when `result.ok` is false
- **Guard layer:** MW conversion, SoC and power limits, hard-goal enforcement
- **Prompt assembly:** building the ~2.5k-token prompt that `decide()` receives
