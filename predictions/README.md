# predictions — demand and production forecasting (ML 1 + ML 2)

Two models forecast Romania's national electricity demand and production for **any 15-minute
timestamp up to 7 days ahead**, each with a 10–90 % range:

| Model | Class | Saved as | Predicts |
|---|---|---|---|
| ML 1, demand | `DemandForecaster` | `models/demand_model.pkl` | `actual_total_load` |
| ML 2, production | `ProductionForecaster` | `models/production_model.pkl` | solar, wind, total production |

```python
from predictions import DemandForecaster, ProductionForecaster

demand = DemandForecaster.load()
demand.predict([1794160800, 1794247200])             # unix seconds, any order
demand.predict(timestamps, issued_at=some_ts)         # forecast "as of" a given moment
```

Or over HTTP (run from the repo root):

```
uvicorn predictions.api:app --port 8000               # docs at http://localhost:8000/docs
```

## Files

| File | Contents |
|---|---|
| `demand_model.py` | `DemandForecaster`: one series, demand |
| `production_model.py` | `ProductionForecaster`: solar, wind and the rest of the mix; total = sum |
| `core.py` | Shared code: features, training, range calibration, `predict`, save/load |
| `api.py` | FastAPI app exposing both models |
| `train_and_evaluate.ipynb` | Trains both models, backtests them on 2026, saves the final models |
| `build_dataset.py` | Downloads weather from Open-Meteo and merges it into `dataset.csv` |
| `dataset.csv` | Training data: energy, weather and calendar per 15 min |
| `models/` | Trained models (created by the notebook) |
| `reports/` | Backtest results: `eval_2026.csv` and plots |
| `data/weather/` | Saved weather downloads, so `build_dataset.py` doesn't download again |
| `live.py` | The live loop: scrapes every 15 min, adds the new intervals, retrains (see Live data) |
| `live/` | The live loop's output, not in git: growing `dataset.csv`, retrained `models/`, `status.json` |

## Data

`dataset.csv` has one row per 15 minutes from 2022-11-07 to 2026-11-07, 140,302 rows with
no gaps. It holds national figures from ENTSO-E for Romania:

- **Energy:** `actual_total_load` and 9 production columns (biomass, storage, lignite, gas,
  2 × hydro, nuclear, solar, wind).
- **Added targets:** `total_production` (the sum of the 9 columns) and `valid` (0 on 48 missing
  records, where the load or every always-on source reads 0; these rows are left out).
- **Weather**, from Open-Meteo's
  [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api) at
  15 min. Each value is a weighted average over sites chosen for what it explains:

  | Column | Explains | Sites (weight) |
  |---|---|---|
  | `temperature_2m`, `precipitation` | demand, hydro | 7 largest cities, weighted by population (Bucharest about half) |
  | `shortwave_radiation`, `cloud_cover` | solar | Olt, Giurgiu (1.0), Banat (0.5) |
  | `wind_speed_80m` | wind | Tulcea, Cogealac (1.0), Galați (0.5) in Dobrogea |

  Nuclear, gas, lignite and biomass are run by operators, not by the weather, so no weather
  was pulled for them.
- **Calendar**, in Romanian local time: hour, weekday, month, weekend, public holidays.

To rebuild it, run `python predictions/build_dataset.py` (add `--refresh` to re-download).

## The models

### What is modelled

The two models cover four series, each with its own LightGBM models:

| Series | Model | Main drivers | Divided by (`scale`) | Long-lead base |
|---|---|---|---|---|
| `actual_total_load` | demand | time of day, weekday, holidays, temperature | mean of the last 7 days | same time last week |
| `solar` | production | sun angle, irradiance, cloud | max of the last 30 days | same time yesterday |
| `wind_onshore` | production | wind speed | max of the last 365 days | mean of the last 24 h |
| `other_production` | production | follows demand, the market and exports | mean of the last 7 days | same time last week |

`other_production` is total − solar − wind: nuclear, gas, lignite, hydro, biomass and storage.
Total production is the sum solar + wind + other.

### Any timestamp: one model for every lead time

Every forecast is a pair: **target time `T`** and **issue time `issued_at`** (the "now" of the
forecast). Features only use data from before `issued_at`, so the same code is used for
training, backtesting and the live app, and it can never use future data.

| Feature | Meaning |
|---|---|
| `lead_hours` | how far ahead `T` is: `(T − issued_at)` + 15 min |
| `last_known`, `ramp_15m`, `ramp_1h` | the latest value and how fast it is changing |
| `lag_day`, `lag_week` | value at `T` minus whole days or weeks, choosing the most recent one already known |
| `mean_1d` | mean of the 24 h before `issued_at` |
| calendar of `T` | quarter of the day, weekday, month, season (sin/cos), weekend, holiday |
| weather at `T` | the 5 weather columns, plus the sun's elevation |
| `base` | the base forecast below |

Because `lead_hours` is a feature, a single model answers "15 minutes ahead" and "6 days
ahead". Timestamps can be irregular and unsorted.

### What the model learns: a correction to a simple forecast

A tree model can't copy one of its inputs exactly, yet for the next 15 minutes the last known
value is very hard to beat. So the model does not predict the value directly. It predicts a
**correction** to a simple base forecast:

```
base   = w · last_known + (1 − w) · seasonal        w = exp(−(lead_hours − 0.25) / 3)
target = (y / scale − base) / σ(lead)               σ = sqrt(min(lead_hours, 24) / 24)
```

- **`base`** is exactly the last value for the next interval. Further ahead it slides towards
  a seasonal value: last week, yesterday, or the 24 h mean, depending on the series.
- **Dividing by `scale`** makes the model learn ratios instead of MW. Tree models can't output
  values bigger than anything in training, and solar's 2026 peak (3,395 MW) is almost double
  2025's (1,845 MW). With ratios, forecasts follow the growth in installed capacity.
- **Dividing by `σ`** puts every lead time on the same footing. Long-lead errors are about
  10 times larger and would otherwise dominate training, so short-lead corrections stay
  small.

Each series has three LightGBM models with quantile loss, for the **10 %, 50 % and 90 %**
quantiles. The 50 % model gives the forecast; the other two give the range.

## Training

`DemandForecaster.fit(data, train_end)` and `ProductionForecaster.fit(...)` do the following:

1. **Make samples.** Each interval before `train_end` is paired with 4 random issue times
   between 15 min and 7 days earlier. Half are drawn log-uniformly (many short leads) and
   half uniformly, giving about 440k samples per series.
2. **Early stopping.** Train on everything up to 3 months before `train_end`. Stop adding
   trees when the error on those last 3 months stops improving. LightGBM settings: 31 leaves,
   at least 1,000 samples per leaf, learning rate 0.05, row and feature subsampling.
3. **Refit** on everything before `train_end`, using the number of trees found in step 2.
4. **Calibrate the ranges.** The early-stopped models never saw the held-out 3 months, so
   their errors there are honest. For each output series and lead bucket (0–1 h, 1–6 h,
   6–24 h, 1–3 d, 3–7 d), find the factor `k` that makes `forecast ± k · (half-width)` cover
   80 % of actuals. Quantile models are usually too confident on new data, and this corrects
   it.
5. **Totals.** `total_production` and `renewables` (solar + wind) add up their parts' ranges
   assuming independent errors: `half-width = sqrt(sum of the parts' half-widths²)`. They are
   then calibrated like the other series.

The notebook trains twice:
- **For evaluation:** `train_end = 2026-01-01`, so 2026 is never seen in training.
- **The final models:** trained on all data and saved to `models/` for the app and the API.

Both runs together take about 5 minutes on a laptop CPU.

## Evaluation

The test set is **every 15-minute interval of 2026**: Jan 1 – Nov 7, 29,806 intervals. For
each lead time (15 min, 1 h, 3 h, 6 h, 12 h, 1 d, 2 d, 3 d, 5 d, 7 d), every interval is
forecast as if it had been issued that long before.

Baselines to beat:
- **Persistence:** the last known value.
- **Weekly naive:** the same quarter-hour one or more weeks earlier.

Metrics:
- **MAE** in MW.
- **nMAE:** MAE as a % of the mean actual value.
- **Skill:** how much lower the MAE is than the better baseline, in %.
- **Bias:** mean forecast − actual.
- **Coverage:** the share of actuals inside the 10–90 % range (target ≈ 80 %).

## Results (2026 backtest)

Full tables: `reports/eval_2026.csv`. Each plot is explained under it in section 3b of the
notebook:

| Plot | What it answers |
|---|---|
| `reports/skill_heatmap.png` | How much better than the simple rules, per series and lead time? |
| `reports/error_pct_by_lead.png` | How big is the error, as % of the typical value? |
| `reports/range_coverage.png` | Can the 10–90 % ranges be trusted (target 80 %)? |
| `reports/error_by_hour.png` | When in the day is the day-ahead forecast wrong? |
| `reports/error_by_month.png` | Which months are hard? |
| `reports/forecast_vs_actual.png` | Over- or under-forecasting? (day-ahead, every 2026 interval) |
| `reports/forecast_sharpening.png` | How does one day's forecast improve as the day approaches? |
| `reports/mae_by_lead.png` | Error in MW vs both simple rules, by lead time |
| `reports/week_*.png` | Full 7-day forecasts vs actuals (one winter, one summer week) |

**Error, MAE in MW (nMAE in brackets):**

| Lead time | 15 min | 1 h | 6 h | 1 day | 3 days | 7 days |
|---|---|---|---|---|---|---|
| Demand | 49 (0.8 %) | 109 (1.9 %) | 218 (3.8 %) | 237 (4.1 %) | 263 (4.5 %) | 291 (5.0 %) |
| Solar | 18 | 44 | 89 | 87 | 90 | 92 |
| Wind | 22 | 75 | 202 | 200 | 198 | 201 |
| Total production | 74 (1.4 %) | 189 (3.6 %) | 358 (6.9 %) | 354 (6.8 %) | 380 (7.3 %) | 394 (7.6 %) |

**Skill vs the better baseline** (higher is better; above 0 means the model wins):

| Lead time | 15 min | 1 h | 6 h | 1 day | 3 days | 7 days |
|---|---|---|---|---|---|---|
| Demand | 36 % | 58 % | 48 % | 44 % | 38 % | 31 % |
| Solar | 57 % | 71 % | 50 % | 41 % | 49 % | 48 % |
| Wind | 19 % | 17 % | 40 % | 65 % | 69 % | 71 % |
| Total production | 6 % | 12 % | 49 % | 46 % | 46 % | 44 % |

**Coverage of the 10–90 % range** (target 80 %): demand 72–84 %, solar 81–86 %,
wind 74–82 %, total production 69–76 %.

Both "done when" criteria from the project plan are met: the MAE beats persistence at every
lead time for every series, and the ranges cover roughly 80 %.

### What the results mean

- **Next 15 min to 1 h,** used by the battery agent every cycle: very accurate. Demand is
  within about 1–2 %, and solar and wind are within about 1–3 % of installed capacity.
- **Day ahead,** used for planning against the 96 day-ahead prices: demand within about 4 %
  is close to grid-operator quality (about 2–3 %). Solar (about 3 % of capacity) and wind
  (about 7 % of capacity) are in the normal range for day-ahead renewable forecasts.
- **3–7 days:** the error grows only slowly, but read the limitations below.

### Limitations

1. **Accuracy depends on the lead time, not on the timestamp.** The default `issued_at` is the
   end of the data in `dataset.csv`. If that file isn't updated, every request is effectively
   further ahead than it looks, and after 7 days every request is rejected. In the live app,
   pass new readings in with `model.set_data(df)`. `GET /health` shows `data_age_hours`.
2. **Errors for days 3–7 are probably understated.** The backtest uses archived weather
   forecasts issued close to each target time. Real 5–7-day weather forecasts are worse, so
   expect solar and wind errors there to be noticeably higher in live use. Open-Meteo's
   *Previous Runs* API (forecasts made 1–7 days earlier) would make this measurement honest.
3. **The rest of the mix is the weakest part.** Nuclear, gas, hydro and lignite are run by
   operators to match the market and exports, so total production only just beats
   persistence under 1 h and can be off by about 800 MW for days (see
   `reports/week_2026-07-06.png`). Adding day-ahead prices would help most here.
4. **Steady bias in 2026:** beyond 1 day, demand is over-forecast by about 65 MW and wind
   under-forecast by about 60 MW. 2026 has lower demand and more wind than the training
   years. Retraining on recent data (the final models include 2026) reduces this.
5. **National, not site-level.** These are totals for all of Romania. They are good context
   for prices, but they are not one site's own load or PV output.

## API

`uvicorn predictions.api:app --port 8000` loads both saved models at startup. To serve models
from a different folder, set the environment variable `PREDICTIONS_MODELS_DIR`.

| Endpoint | Returns |
|---|---|
| `GET /health` | status, latest data time, `data_age_hours`, allowed forecast window |
| `POST /forecast/demand` | `demand_mw`, `demand_low_mw`, `demand_high_mw` |
| `POST /forecast/production` | `solar_*`, `wind_*`, `prod_low/high_mw` (solar + wind range), `total_*` |
| `POST /forecast` | both, merged per timestamp |
| `GET /csv` | the CSV files under `predictions/`: name, download URL, size, columns |
| `GET /csv/{name}` | downloads one file, e.g. `/csv/dataset.csv` or `/csv/reports/eval_2026.csv` |

**CSV downloads**
- **Forecasts:** add `"format": "csv"` to any forecast request to get the predictions table
  as a CSV file instead of JSON. The columns are the same as in the JSON.
- **Filtering:** `GET /csv/{name}?start=...&end=...` returns only rows with
  `start <= timestamp < end`, as unix seconds or ISO. This works for files with a `timestamp`
  column, e.g. `/csv/dataset.csv?start=2026-11-01T00:00:00Z`.
- **Which files:** only CSV files under `predictions/` can be downloaded. Any other name
  returns `404`.

Request body: give either a list of timestamps, or a start and end.

```json
{"timestamps": [1794160800, "2026-11-08T18:00:00Z"], "issued_at": null, "allow_fetch": true}
{"start": "2026-11-08T00:00:00Z", "end": "2026-11-09T00:00:00Z"}
```

- **Timestamps** are unix seconds, unix milliseconds (any integer above 10¹¹, e.g. from JS
  `Date.now()`) or ISO-8601 strings; times without a timezone count as UTC. Each one is
  snapped down to the 15-min grid. Order and duplicates are kept in the response.
- **`issued_at`** defaults to just after the latest data. It must lie between 30 days after
  the data starts and the end of the data. Later values are rejected rather than silently
  forecast without recent data: add the new data first.
- **`start` / `end`** expands to every 15-minute step: `start` is rounded up, `end` is
  exclusive, and at most 5,000 steps are allowed per request.
- **Errors:** timestamps outside `[issued_at, issued_at + 7 days)`, an empty request, or too
  many points return `422` with an explanation.
- **Live weather** is cached for 1 hour per model. A 7-day request takes about 0.1 s, or about
  2 s when it has to download new weather.

### Tests

`python -m pytest tests/test_predictions_api.py -q` runs 26 tests. They need no network and
use the saved models. They check:
- the API returns the same numbers as calling the classes directly;
- `/forecast` matches `/forecast/demand` + `/forecast/production`;
- unix seconds, milliseconds, ISO strings, UTC offsets and timezone-less times all give the
  same timestamp;
- 15-min snapping, and that order and duplicates are kept;
- `start`/`end` ranges;
- the edges of the 7-day window and the `issued_at` bounds;
- malformed requests are rejected;
- CSV output for forecasts, and the `/csv` list, download, filter and error cases;
- a full week of values is sane: no nulls, low ≤ mid ≤ high, no solar at night, realistic
  demand.

The serving path was also checked end to end against a running server, using models trained
only on data **before 2026-10-01**. A 7-day `/forecast` was issued every 6 h through October
2026: 121 requests, 81,312 forecasts, scored against actuals. The model beat the better
baseline in every lead bucket. Demand error was 0.9 % at 15 min and 3.1–3.6 % from 6 h to
7 days. Total production was 2.6 % at 15 min and 8–9 % from 6 h onward. Range coverage was
75–97 %; the exception is total production at 15 min, at 60 %.

Each prediction includes `weather_source`, which says where its weather came from: `archive`
(in `dataset.csv`), `forecast` (live Open-Meteo, up to 16 days ahead) or `climatology` (the
average for that month and time of day, used as a fallback).

## Retraining

Run `train_and_evaluate.ipynb` top to bottom. It rebuilds the 2026 backtest and overwrites
`models/*.pkl`. To add newer data first, extend `dataset.csv` with the new energy rows and run
`python predictions/build_dataset.py --refresh` to fill in their weather. `--refresh` is needed
because the saved download for the current year stops at the old end date.

## Live data

`live.py` keeps the models learning from new data. Every 15 minutes (5 minutes after each
quarter hour, once ENTSO-E has usually published the interval that just ended) it:

1. **Scrapes the energy data** for the last 2 days (more after downtime, up to 30), from the
   first of these that is available:
   - **ENTSO-E's official API** when `ENTSOE_API_TOKEN` is set (`entsoe_api.py` in the repo root).
   - **ENTSO-E's website export** with the browser token in `ENTSOE_TOKEN`, through the same code
     as the scrapers (`entsoe_export.py`, parsed by the root `build_dataset.py`). That token
     expires after about 15 minutes.
   - **Transelectrica** otherwise, with no login (`transelectrica.py`). Its public "Grafic SEN"
     chart exports the national load and production per type about every 10 minutes, averaged
     here into 15-minute intervals. ENTSO-E's Romanian data comes from Transelectrica.

   Against `dataset.csv` (725 intervals, 30 Oct – 7 Nov 2026), Transelectrica matches the
   training data closely:

   | Series | Correlation | Typical gap |
   |---|---|---|
   | Load | 1.000 | 0.3% |
   | Solar | 1.000 | 4.9% |
   | Wind | 0.995 | 1.2% |
   | Hydro | 1.000 | 1.1% |
   | Nuclear | 1.000 | 2 MW |

   Gas reads about 80 MW higher and coal about 120 MW higher: ENTSO-E's lignite has reporting
   gaps, and Transelectrica's coal includes hard coal. Total production is therefore about 4.6%
   higher. Two columns have to be derived:
   - **Hydro** comes as one total and is split into the dataset's two hydro columns at the last
     week's ratio.
   - **Storage** is what Transelectrica's total production holds beyond the listed types.

   Transelectrica only adds intervals after the end of the data. It never replaces ENTSO-E rows.
2. **Fetches the weather** from the Open-Meteo Historical Forecast API at the same 13 sites. It
   matches the weather already in `dataset.csv` to rounding.
3. **Builds the rows exactly like the training rows**: total production, the `valid` flag and the
   calendar columns. An interval is added only once its load and every production type are
   published. Recent intervals are read again, so ENTSO-E's corrections replace the old values.
4. **The models learn from every new interval**, whenever one was added or corrected:
   - **every tick, an update** (`BaseForecaster.update`, about 5 seconds). LightGBM's
     `Booster.refit` keeps every tree and moves its leaf values towards the latest 30 days.
   - **once a day, a full fit** on all the data, 2026 included (about 7 minutes). The trees are
     rebuilt, with early stopping and range recalibration.

   Training uses half the CPU cores by default (`--threads`). `refit()` (rebuild the trees from
   all the data, keeping the tree counts; about 3.5 minutes) is also available.
5. **Writes `live/status.json`**: what the tick fetched, added and corrected, where the data ends,
   the last training and any error. A failed fetch (offline, expired token) is recorded and tried
   again on the next tick. A failed training is retried on the next tick.

```powershell
python -m predictions.live               # run forever (Transelectrica, or ENTSO-E if a token is set)
python -m predictions.live --once        # one tick (Task Scheduler, cron)
python -m predictions.live --no-train    # collect data only
python transelectrica.py                 # check Transelectrica: prints the latest intervals
python entsoe_api.py                     # check an ENTSO-E API token: prints the latest rows
```

To get an API token (free), register on transparency.entsoe.eu and email
transparency@entsoe.eu with the subject "Restful API access". Once access is granted, generate the
token under My Account Settings.

**The original results stay as they are.** The loop writes only to `live/`, which starts as a
copy of `dataset.csv`. It never writes to `dataset.csv`, `models/`, `reports/` or the notebook,
so the 2026 backtest, the plots and the tables above are kept for the presentation. The notebook
and the tests read only the originals.

**The app uses the live files only with `PREDICTIONS_LIVE=1`.** `docker-compose.yml` sets it and
runs the loop as the `live` service; the token is passed with
`ENTSOE_API_TOKEN=... docker compose up --build`; without one it uses Transelectrica. With live mode on:
- the forecast API reloads the data and models whenever the loop writes new ones;
- the backend rebuilds its day on the new models at the next reset of the simulation.

Until the loop has trained once, the original models are used, on the live data.

### Live demo in the dashboard

A real-time run adds one interval every 15 minutes, which is too slow to show. With
`--live-demo` (on in `docker-compose.yml`), the dashboard's demo bar has a **Live data** control
that replays the live loop on the day being run. The day's measured data starts cut off at 08:00
(`--live-demo-start`). Each press of **Fetch the next hour** runs the real pipeline for the next
hour, in about 10–15 seconds:

1. **Scrape:** a real request to Transelectrica and Open-Meteo for that hour.
2. **Learn:** the models update from the new data.
3. **Re-forecast:** the rest of the day is forecast again from the new data. The intervals now
   measured settle on what really happened.

The running simulation keeps everything it has decided. The demo writes only to
`live/demo/`; **Back to the start** returns to 08:00.
