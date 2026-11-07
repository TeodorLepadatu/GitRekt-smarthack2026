"""Run a day through the whole cycle from the command line.

    python -m loop                                   # demo day, rule-based fallback only (instant)
    python -m loop --date 2026-06-15                 # a real day: ML forecasts + ENTSO-E prices
    python -m loop --llm                             # demo day with the decision LLM (Ollama)
    python -m loop --llm --model qwen3:4b --goals "Store 6 MWh by 18:00"
    python -m loop --speed 600                       # live: one interval every 1.5 s
    python -m loop --until 12:00 --out logs/day.jsonl
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Optional

from config import Settings
from goal_parser import parse_goals

from .day import DayRunner
from .demo import demo_day
from .real_day import RealDayError, SiteScale, real_day


def _llm_off(prompt: str) -> str:
    raise RuntimeError("LLM disabled (run with --llm to use it)")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m loop", description="Run one day through the energy agent.")
    ap.add_argument("--llm", action="store_true", help="use the decision LLM (default: rule-based fallback only)")
    ap.add_argument("--model", help="Ollama model for decisions; implies --llm (default: Settings().decision_model)")
    ap.add_argument("--goals", default="", help='goal sentences, e.g. "Store 6 MWh by 18:00"')
    ap.add_argument("--start-soc", type=float, default=4.0, help="stored energy at the start, MWh")
    ap.add_argument("--start", default="00:00", help="first interval, HH:MM")
    ap.add_argument("--until", default=None, help="stop before this interval, HH:MM")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="real-time factor: 1 = real time, 600 = 1.5 s per interval; 0 = as fast as possible")
    ap.add_argument("--seed", type=int, default=7, help="demo day seed")
    ap.add_argument("--date", help="run a real day (YYYY-MM-DD): ML forecasts + ENTSO-E day-ahead prices")
    ap.add_argument("--plan", action="store_true",
                    help="with --date: settle on the forecasts instead of the measured actuals")
    ap.add_argument("--imbalance", action="store_true",
                    help="with --date: charge for deviating from the promised grid position, and let "
                         "the battery defend it (synthetic_out/prices.csv)")
    ap.add_argument("--max-import", type=float, default=None, metavar="MW",
                    help="grid import cap for peak shaving, MW (default: ENERGY_AGENT_MAX_IMPORT_MW, unset = none)")
    ap.add_argument("--out", help="write every interval record to this JSONL file")
    ap.add_argument("--quiet", action="store_true", help="print only the summary")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.model:
        settings = dataclasses.replace(settings, decision_model=args.model)
        args.llm = True  # naming a model means using it
    llm_call = _llm_off
    if args.llm:
        from llm import DecisionAgent
        llm_call = DecisionAgent(settings=settings)

    goals = None
    if args.goals:
        result = parse_goals(args.goals, battery_capacity_mwh=settings.battery.capacity_mwh)
        for c in result.clarifications:
            print(f"goal not understood: {c.question}", file=sys.stderr)
        goals = result.config

    if args.date:
        try:
            day = real_day(args.date, scale=SiteScale(), with_actuals=not args.plan,
                           imbalance=args.imbalance)
        except RealDayError as exc:
            print(f"cannot build {args.date}: {exc}", file=sys.stderr)
            return 2
    else:
        day = demo_day(seed=args.seed)

    on_interval = None if args.quiet else (lambda rec: print(rec["dashboard_line"], flush=True))
    runner = DayRunner(day, goals, start_soc_mwh=args.start_soc, start_time=args.start,
                       battery=settings.battery, llm_call=llm_call, on_interval=on_interval, log_path=args.out,
                       max_import_mw=settings.max_import_mw if args.max_import is None else args.max_import)
    if args.speed > 0:
        runner.run_realtime(args.speed, until=args.until)
    else:
        runner.run(until=args.until)

    s = runner.summary()
    m = s["metrics"]
    print("\n=== summary ===")
    print(f"intervals {s['intervals_run']}/{s['intervals_total']}, stored at the end {s['stored_mwh']:.2f} MWh")
    print(f"profit EUR {m['profit_today_eur']:+.2f} (without a battery EUR {m['no_battery_profit_eur']:+.2f}, "
          f"battery added EUR {m['value_added_eur']:+.2f})")
    print(f"bought {m['energy_bought_mwh']:.2f} MWh, sold {m['energy_sold_mwh']:.2f} MWh, "
          f"curtailed {m['curtailed_mwh']:.2f} MWh, battery throughput {m['battery_throughput_mwh']:.2f} MWh")
    if m["imbalance_eur"]:
        print(f"imbalance: EUR {m['imbalance_eur']:+.2f} over {m['deviation_mwh']:.2f} MWh that the "
              f"battery could not absorb")
    if m["import_limit_mw"] is not None:
        print(f"grid import: peak {m['peak_import_mw']:.2f} MW (without a battery "
              f"{m['peak_import_mw_no_battery']:.2f} MW), limit {m['import_limit_mw']:g} MW, "
              f"{m['import_limit_exceeded_intervals']} intervals over it")
    print(f"decisions: {m['decisions_llm']} LLM, {m['decisions_fallback']} fallback, {m['decisions_clipped']} clipped by the guard")
    if m["demand_mae_mw"] is not None:
        print(f"forecast error: demand {m['demand_mae_mw']:.3f} MW MAE, production {m['production_mae_mw']:.3f} MW MAE, "
              f"production inside its 10-90 % band {m['production_range_coverage']:.0%} of the time")
    if s["final_goal_status"] is not None:
        print("goals:", json.dumps(s["final_goal_status"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
