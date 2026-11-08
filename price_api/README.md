# Price API (mock)

A small FastAPI service that answers "what does energy cost in this 15-minute interval": the
day-ahead price and the two imbalance prices, in EUR/MWh, for Romania (Europe/Bucharest).

```bash
python -m price_api                      # http://127.0.0.1:8100, interactive docs at /docs
```

## Where the numbers come from

| Price | Real source | When the file does not cover the interval |
|---|---|---|
| day-ahead | `data/energy_price.csv` (ENTSO-E, from `energy_price.py`) | generated |
| imbalance short / long | `synthetic_out/prices.csv` (`synth_market.py`) | generated from the day-ahead price |

A generated day-ahead curve has the real file's own shape: the mean price per quarter-hour slot for
that calendar month (or the nearest month the file covers), scaled by a day-level factor, with a
15 % weekend discount and some AR(1) noise. It is **seeded by the date**, so a date always returns
the same curve. A generated imbalance pair is `day-ahead ± |day-ahead| × (0.2–0.8)`, the same
range `synth_market.py` uses, and it stays a penalty when prices are negative.

Each interval says which source each of its prices came from (`entsoe`, `synth_market` or
`synthetic`), and the response gives a summary for the whole day (`mixed` when a day contains both).

## Routes

| Route | Returns |
|---|---|
| `GET /health` | `status`, and `real_coverage`: the first and last real timestamp of each file |
| `GET /prices/day/{YYYY-MM-DD}` | the 96 intervals of that local day, built the way `loop.real_day.day_timestamps` builds them |
| `GET /prices?start=&end=` | any range up to 31 days: start inclusive, end exclusive, as unix seconds or ISO-8601 (a time without a zone is read as UTC) |

Both price routes take `synthetic=false` for real data only. A range with any interval the files
don't cover then returns 404, with the coverage in the error body.

```json
{
  "date": "2026-09-28", "timezone": "Europe/Bucharest", "interval_minutes": 15,
  "currency": "EUR", "unit": "MWh",
  "source": {"day_ahead": "entsoe", "imbalance": "synth_market"},
  "intervals": [
    {"timestamp": 1790542800, "time": "2026-09-28T00:00+03:00",
     "day_ahead_eur_mwh": 174.04, "imbalance_short_eur_mwh": 150.89, "imbalance_long_eur_mwh": 41.37,
     "source": {"day_ahead": "entsoe", "imbalance": "synth_market"}}
  ]
}
```

## Used by the loop

```python
from price_api import fetch_day
day = fetch_day("2026-09-28", "http://127.0.0.1:8100")   # day.day_ahead, day.imbalance (96 each)
```

When `PRICE_API_URL` is set, `python -m backend --date ...` and `python -m loop --date ...` take the
day's prices from this API (`real_day(price_api=...)`). Because missing days are generated, they can
run days the price file does not have; the ML models still limit how far ahead that can go. If the
API cannot be reached, a warning is logged and the CSVs are read instead. In `docker-compose.yml`
the `prices` service runs it and the `api` service points at it.

Tests: `pytest tests/test_price_api.py`.
