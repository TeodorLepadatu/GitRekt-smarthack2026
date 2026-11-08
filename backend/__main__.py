"""Start the dashboard backend.

    python -m backend                          # demo day, decision LLM from Settings (ENERGY_AGENT_MODEL)
    python -m backend --model qwen3:4b
    python -m backend --no-llm                 # rule-based fallback only, no Ollama needed
    python -m backend --start 11:15            # pre-run the day up to 11:15 (like the frontend's mock)
    python -m backend --date 2026-06-15        # a real day: ML forecasts + ENTSO-E prices (loop/real_day.py)

Then run the dashboard against it:  cd frontend && npm run dev:api   (http://localhost:5173)
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import threading

import uvicorn

from config import Settings
from goal_parser import GoalParser, GoalStore
from llm import DecisionAgent, OllamaClient
from loop import RealDayError, SiteScale, demo_day, real_day
from loop.real_day import live_signature

from .app import create_app
from .service import Backend

DEFAULT_GOALS = ["Maximise profit", "Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh"]


def _llm_off(prompt: str) -> str:
    raise RuntimeError("LLM disabled (--no-llm)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m backend", description="Energy agent dashboard backend.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", help="Ollama model for decisions and goal parsing (default: Settings)")
    ap.add_argument("--no-llm", action="store_true", help="rule-based decisions and goal parsing, no Ollama")
    ap.add_argument("--start-soc", type=float, default=4.0, help="stored energy at 00:00, MWh")
    ap.add_argument("--start", default="00:00", help="pre-run the day up to this time, HH:MM")
    ap.add_argument("--goals-path", help="where the goals are saved (default: Settings().goals_path)")
    ap.add_argument("--log", help="write every interval record to this JSONL file")
    ap.add_argument("--date", help="run a real day (YYYY-MM-DD) instead of the demo day")
    ap.add_argument("--plan", action="store_true", help="with --date: settle on the forecasts, not the actuals")
    ap.add_argument("--imbalance", action="store_true",
                    help="with --date: charge for deviating from the promised grid position")
    ap.add_argument("--live-demo", action="store_true",
                    help="with --date: the 'Fetch the next hour' demo of the live data (backend/live_demo.py)")
    ap.add_argument("--live-demo-start", default="08:00", metavar="HH:MM",
                    help="with --live-demo: the day's data starts cut off here (default 08:00)")
    ap.add_argument("--max-import", type=float, default=None, metavar="MW",
                    help="grid import cap for peak shaving, MW (default: ENERGY_AGENT_MAX_IMPORT_MW, unset = none)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings()
    if args.model:
        settings = dataclasses.replace(settings, decision_model=args.model, goal_model=args.model)

    if args.no_llm:
        llm_call, goal_llm = _llm_off, None
    else:
        llm_call, goal_llm = DecisionAgent(settings=settings), OllamaClient(settings.goal_model, settings)
    store = GoalStore(GoalParser(goal_llm, battery_capacity_mwh=settings.battery.capacity_mwh),
                      args.goals_path or settings.goals_path)
    if not store.sentences:
        store.set(DEFAULT_GOALS)

    if args.live_demo and not args.date:
        sys.exit("--live-demo needs --date: the demo replays a past day")
    demo = None
    if args.date:
        def build():
            return real_day(args.date, scale=SiteScale(), with_actuals=not args.plan, imbalance=args.imbalance)

        try:
            built = {"day": build(), "sig": live_signature()}
        except RealDayError as exc:
            sys.exit(f"cannot build {args.date}: {exc}")

        def day_factory():
            """A reset replays the same day, rebuilt if the live loop has new data or models since."""
            if demo is not None and demo.day is not None:
                return demo.day                     # the live demo's day, with the hours fetched so far
            sig = live_signature()
            if sig != built["sig"]:
                try:
                    built.update(day=build(), sig=sig)
                    logging.getLogger("backend").info("rebuilt %s on the live loop's new data/models", args.date)
                except RealDayError as exc:
                    logging.getLogger("backend").warning("kept the previous %s: %s", args.date, exc)
            return built["day"]
    else:
        day_factory = demo_day

    backend = Backend(store, llm_call=llm_call, battery=settings.battery, start_soc_mwh=args.start_soc,
                      day_factory=day_factory, log_path=args.log,
                      max_import_mw=settings.max_import_mw if args.max_import is None else args.max_import)
    if args.date:
        backend.date = args.date
    if args.start != "00:00":
        n = backend._runner._index_of(args.start)  # validates the time
        threading.Thread(target=backend.advance, args=(n,), name="warm-up", daemon=True).start()

    if args.live_demo:
        from .live_demo import LiveDemo
        demo = LiveDemo(args.date, backend.replace_day, start=args.live_demo_start, scale=SiteScale())
        demo.start()          # cuts the data at the start hour and swaps that day in, in the background

    uvicorn.run(create_app(backend, live_demo=demo), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
