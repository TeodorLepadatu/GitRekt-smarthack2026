"""Live-data demo: the live loop (predictions/live.py) replayed on a past day, one button press at a time.

A real-time run adds one interval every 15 minutes, too slow to show. Here the
day's measured data starts cut off at a morning hour, as if that were "now".
Each press of "Fetch the next hour" moves that clock one hour on and runs the
real pipeline for it:

  1. scrape: the live loop's tick for that moment, a real request to
     Transelectrica (or ENTSO-E with a token) and to Open-Meteo, adding the
     hour's intervals to a copy of the dataset;
  2. learn: both models learn from the new data (BaseForecaster.update: every
     tree kept, its leaf values moved towards the latest 30 days; seconds);
  3. re-forecast: the rest of the day forecast again from the new data with the
     retrained models; the intervals already measured settle on what happened.

The new day replaces the running simulation's between two intervals, keeping
everything already decided. The demo writes only to predictions/live/demo/;
the original dataset, models and the real-time loop's files are untouched.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from loop import DayInput, SiteScale, real_day
from loop.real_day import day_timestamps, site_actuals, site_forecasts

log = logging.getLogger("backend.live_demo")
STEP_S = 900


class DemoBusy(RuntimeError):
    pass


class DemoDone(RuntimeError):
    pass


def _local_hhmm(ts: int) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo("Europe/Bucharest")).strftime("%H:%M")


class LiveDemo:
    def __init__(
        self,
        date: str,
        on_day: Callable[[DayInput], None],
        *,
        start: str = "08:00",
        scale: SiteScale = SiteScale(),
        directory: Optional[Path] = None,
        tick: Optional[Callable[..., dict]] = None,
        train: Optional[Callable[..., dict]] = None,
    ):
        import os

        from predictions import core, live
        from predictions.core import live_dir

        # Train on half the cores: the dashboard and the LLM keep running meanwhile.
        core.LGB_PARAMS.setdefault("num_threads", max(1, (os.cpu_count() or 2) // 2))
        self.date = date
        self.on_day = on_day
        self.scale = scale
        self.directory = Path(directory or live_dir() / "demo")
        self.stamps = day_timestamps(date)
        h, m = (int(x) for x in start.split(":"))
        self.start_index = min(len(self.stamps) - 1, h * 4 + m // 15)
        self._tick = tick or live.tick
        self._train = train or live.update_models

        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self.phase = "starting"
        self.error: Optional[str] = None
        self.last: Optional[dict] = None
        self.steps = 0
        self.cut = self.stamps[self.start_index]       # first interval not measured yet
        self.day: Optional[DayInput] = None

    # ------------------------------------------------------------------ reads

    def status(self) -> dict:
        with self._lock:
            measured = sum(a is not None for a in self.day.actuals) if self.day and self.day.actuals else 0
            return {
                "date": self.date,
                "phase": self.phase,               # starting | idle | scraping | training | forecasting
                "busy": self.phase != "idle",
                "data_until": _local_hhmm(self.cut) if self.cut < self.stamps[-1] + STEP_S else "24:00",
                "data_until_utc": datetime.fromtimestamp(self.cut, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "measured_intervals": measured,
                "can_fetch": self.cut < self.stamps[-1] + STEP_S,
                "steps": self.steps,
                "last": self.last,
                "error": self.error,
            }

    # --------------------------------------------------------------- commands

    def start(self) -> None:
        """Seed the demo's data at the start hour and build the day (in the background)."""
        self._run(self._reset_work)

    def reset(self) -> dict:
        """Back to the start hour: the hours fetched so far are forgotten."""
        self._run(self._reset_work)
        return self.status()

    def fetch(self, hours: float = 1.0) -> dict:
        """Scrape, retrain and re-forecast the next `hours` (in the background)."""
        with self._lock:
            if self.cut >= self.stamps[-1] + STEP_S:
                raise DemoDone(f"{self.date} is already complete")
        self._run(lambda: self._fetch_work(hours))
        return self.status()

    def wait(self, timeout: float = 600) -> None:
        w = self._worker
        if w is not None:
            w.join(timeout)

    # ------------------------------------------------------------------ worker

    def _run(self, work: Callable[[], None]) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise DemoBusy("the demo is still working on the previous step")
            self.error = None
            self._worker = threading.Thread(target=self._guarded, args=(work,), name="live-demo", daemon=True)
            self._worker.start()

    def _guarded(self, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001  (shown on the dashboard; the demo stays usable)
            log.exception("live demo step failed")
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self.phase = "idle"

    def _set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _reset_work(self) -> None:
        from predictions import live
        from predictions.core import DATASET_CSV

        self._set(phase="starting")
        cut = self.stamps[self.start_index]
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True)
        base = live.read_dataset(DATASET_CSV)
        live.write_dataset(base[base.timestamp < cut], self.directory / "dataset.csv")
        day = self._build(cut, previous=None)
        self._set(cut=cut, day=day, steps=0, last=None)
        self.on_day(day)
        log.info("live demo: %s measured until %s", self.date, _local_hhmm(cut))

    def _fetch_work(self, hours: float) -> None:
        t0 = time.time()
        with self._lock:
            old_cut, previous = self.cut, self.day
        new_cut = min(old_cut + int(round(hours * 4)) * STEP_S, self.stamps[-1] + STEP_S)

        self._set(phase="scraping")
        s = self._tick(datetime.fromtimestamp(new_cut, timezone.utc), directory=self.directory, train=None)
        if s.get("error"):
            raise RuntimeError(f"scraping failed: {s['error']}")
        scraped_s = time.time() - t0

        self._set(phase="training")
        trained = self._train(self.directory / "dataset.csv", self.directory / "models")

        self._set(phase="forecasting")
        day = self._build(new_cut, previous=previous)
        last = {
            "from": _local_hhmm(old_cut), "to": _local_hhmm(new_cut) if new_cut < self.stamps[-1] + STEP_S else "24:00",
            "source": s.get("energy_source"), "added": s.get("added", 0),
            "scrape_seconds": round(scraped_s, 1), "train_seconds": trained.get("seconds"),
            "trained_until_utc": trained.get("trained_until"), "total_seconds": round(time.time() - t0, 1),
        }
        with self._lock:
            self.cut, self.day, self.last, self.steps = new_cut, day, last, self.steps + 1
        self.on_day(day)
        log.info("live demo: +%s intervals from %s, data until %s, %.0f s", last["added"], last["source"],
                 last["to"], last["total_seconds"])

    # ------------------------------------------------------------------- build

    def _models(self, data: Any) -> dict:
        from predictions import DemandForecaster, ProductionForecaster
        from predictions.core import MODELS_DIR
        out = {}
        for key, cls in (("demand", DemandForecaster), ("production", ProductionForecaster)):
            p = self.directory / "models" / f"{cls.NAME}.pkl"
            out[key] = cls.load(p if p.exists() else MODELS_DIR / f"{cls.NAME}.pkl", data=data)
        return out

    def _build(self, cut: int, previous: Optional[DayInput]) -> DayInput:
        """The day with what is known at `cut`: measured values before it, forecasts from it on.

        Intervals before `cut` keep the forecasts they were planned with; the
        rest are forecast again from `cut` with the newest models.
        """
        from predictions.core import load_data
        data = load_data(self.directory / "dataset.csv")
        models = self._models(data)
        day = previous or real_day(self.date, scale=self.scale, models=models, data=data)
        i = sum(t < cut for t in self.stamps)
        forecasts = list(day.forecasts)
        if i < len(self.stamps):
            forecasts[i:] = site_forecasts(self.stamps[i:], issued_at=cut, scale=self.scale,
                                           models=models, data=data)
        actuals = site_actuals(self.stamps, scale=self.scale, data=data)
        return replace(day, forecasts=forecasts, actuals=actuals)
