"""The dashboard payloads must match `frontend/src/api/types.ts` exactly.

The TypeScript file is the contract, so these tests read it instead of
repeating it: every interface is parsed out of it and the payloads are checked
against the real thing. Add a field on either side without the other and a
test here fails, which is the whole point - the two used to drift silently.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from battery import BatteryConfig
from dashboard import (
    day_payload,
    forecast_payload,
    goals_payload,
    interval_payload,
    metrics_payload,
    state_payload,
)
from goal_parser import parse_goals
from loop import DayRunner, demo_day

TYPES_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "types.ts"

# --------------------------------------------------------------------------- #
# a small reader for the handful of TypeScript shapes types.ts uses


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


@pytest.fixture(scope="module")
def contract() -> dict:
    """{'interfaces': {name: [(field, optional, type)]}, 'aliases': {name: [literals]}}"""
    assert TYPES_TS.exists(), f"{TYPES_TS} is the API contract and must exist"
    src = _strip_comments(TYPES_TS.read_text(encoding="utf-8"))

    aliases: dict[str, list[str]] = {}
    for name, body in re.findall(r"export\s+type\s+(\w+)\s*=\s*([^;]+);", src):
        literals = re.findall(r'"([^"]+)"', body)
        if literals:
            aliases[name] = literals

    interfaces: dict[str, list[tuple[str, bool, str]]] = {}
    for name, body in re.findall(r"export\s+interface\s+(\w+)\s*\{(.*?)\n\}", src, flags=re.S):
        fields = []
        for line in body.split("\n"):
            line = line.strip().rstrip(";").strip()
            m = re.match(r"^(\w+)(\?)?\s*:\s*(.+)$", line)
            if m:
                fields.append((m.group(1), m.group(2) == "?", m.group(3).strip()))
        interfaces[name] = fields
    return {"interfaces": interfaces, "aliases": aliases}


def _check_type(value, ts_type: str, contract: dict, where: str) -> None:
    """Assert a Python value satisfies a TypeScript type from types.ts."""
    parts = [p.strip() for p in ts_type.split("|")]
    if value is None:
        assert "null" in parts, f"{where}: null is not allowed by `{ts_type}`"
        return
    parts = [p for p in parts if p != "null"]
    assert parts, f"{where}: `{ts_type}` allows nothing but null"

    for part in parts:
        if part.endswith("[]"):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    _check_type(item, part[:-2], contract, f"{where}[{i}]")
                return
        elif part == "number":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return
        elif part == "string":
            if isinstance(value, str):
                return
        elif part == "boolean":
            if isinstance(value, bool):
                return
        elif part.startswith('"'):
            if value == part.strip('"'):
                return
        elif part in contract["aliases"]:
            if value in contract["aliases"][part]:
                return
        elif part in contract["interfaces"]:
            if isinstance(value, dict):
                _check_interface(value, part, contract, where)
                return
        else:  # a type this reader does not model: do not invent a rule for it
            return
    raise AssertionError(f"{where}: {value!r} ({type(value).__name__}) does not match `{ts_type}`")


def _check_interface(payload: dict, name: str, contract: dict, where: str = "") -> None:
    where = where or name
    fields = contract["interfaces"][name]
    assert fields, f"{name} was not found in types.ts"
    declared = {f for f, _, _ in fields}
    for field, optional, ts_type in fields:
        if field not in payload:
            assert optional, f"{where}.{field} is required by types.ts but missing"
            continue
        _check_type(payload[field], ts_type, contract, f"{where}.{field}")
    extra = set(payload) - declared
    assert not extra, f"{where} has fields types.ts does not declare: {sorted(extra)}"


# --------------------------------------------------------------------------- #
# a day to build payloads from


def _no_llm(prompt: str) -> str:
    """``run_cycle`` defaults ``llm_call`` to a real ``DecisionAgent``; these tests
    check shapes, so they take the rule-based path and make no Ollama calls."""
    raise RuntimeError("no LLM in these tests")


@pytest.fixture(scope="module")
def runner() -> DayRunner:
    """Half a demo day, with both a reserve and a min-sell goal, so goals are live."""
    goals = parse_goals("Store 6 MWh by 18:00. Never sell below 50 EUR/MWh",
                        battery_capacity_mwh=10).config
    r = DayRunner(demo_day(seed=7), goals, start_soc_mwh=4.0, battery=BatteryConfig(),
                  llm_call=_no_llm)
    r.run(until="13:00")
    return r


def test_types_ts_was_understood(contract):
    """The reader must actually find the interfaces, or every other test passes blindly."""
    for name in ("Interval", "ForecastInterval", "Decision", "GuardResult", "Settlement",
                 "StateSnapshot", "SimStatus", "Metrics", "Goal", "DayResponse"):
        assert name in contract["interfaces"], f"{name} not parsed out of types.ts"
    assert contract["aliases"]["DecisionSource"] == ["llm", "fallback", "operator"]
    assert len(contract["interfaces"]["Interval"]) == 11


def test_interval_matches_contract(runner, contract):
    for record in runner.records:
        _check_interface(interval_payload(record), "Interval", contract,
                         f"Interval@{record.get('time')}")


def test_sources_and_split(runner):
    """solar + wind must equal production: the energy-flow diagram balances on it."""
    for record in runner.records:
        iv = interval_payload(record)
        assert iv["solar_mw"] + iv["wind_mw"] == pytest.approx(iv["production_mw"], abs=1e-6)
        assert iv["solar_mw"] >= 0 and iv["wind_mw"] >= 0


def test_soc_is_the_end_of_the_interval(runner):
    """types.ts: 'Stored energy at the END of the interval.'"""
    for record in runner.records:
        assert interval_payload(record)["soc_mwh"] == record["soc_after_mwh"]


def test_settlement_is_nested_and_complete(runner, contract):
    declared = {f for f, _, _ in contract["interfaces"]["Settlement"]}
    for record in runner.records:
        s = interval_payload(record)["settlement"]
        assert set(s) == declared
        # the same numbers the ledger keeps flat
        for field in declared:
            assert s[field] == pytest.approx(record[field], abs=1e-9)
        assert s["profit_eur"] == pytest.approx(
            s["energy_cost_eur"] + s["wear_eur"] + s.get("imbalance_eur", 0.0), abs=0.02)


def test_day_payload_matches_contract(runner, contract):
    day = day_payload(runner)
    _check_interface(day, "DayResponse", contract)
    assert len(day["intervals"]) == runner.day.n
    assert day["now"] == runner.records[-1]["time"]
    decided = [s for s in day["intervals"] if "decision" in s]
    assert len(decided) == len(runner.records)
    for slot in day["intervals"][len(runner.records):]:
        _check_interface(slot, "ForecastInterval", contract, f"ForecastInterval@{slot['time']}")


def test_day_payload_times_are_the_whole_day(runner):
    times = [s["time"] for s in day_payload(runner)["intervals"]]
    assert times[0] == "00:00" and times[-1] == "23:45"
    assert len(set(times)) == len(times)


def test_isdecided_discriminator(runner):
    """`isDecided()` in types.ts checks for a non-null `decision` key."""
    for slot in day_payload(runner)["intervals"]:
        assert ("decision" in slot) == ("settlement" in slot)
        if "decision" in slot:
            assert slot["decision"] is not None


def test_state_payload_matches_contract(runner, contract):
    state = state_payload(runner, running=True, speed=60.0)
    _check_interface(state, "StateSnapshot", contract)
    assert state["capacity_mwh"] == runner.cfg.capacity_mwh
    assert state["max_power_mw"] == runner.cfg.p_max_mw
    assert state["sim"] == {"running": True, "speed": 60.0}
    assert state["current"]["time"] == runner.records[-1]["time"]


def test_metrics_payload_matches_contract(runner, contract):
    """Every key the ledger sends must be declared, and every required one sent.

    The ledger's own evaluation numbers (forecast MAE, band coverage, rates) ride
    along on this response, so types.ts declares them as optional. A new metric
    has to be declared there too, or this fails.
    """
    metrics = metrics_payload(runner)
    _check_interface(metrics, "Metrics", contract)
    assert metrics["decisions_total"] == len(runner.records)
    assert metrics["decisions_llm"] + metrics["decisions_fallback"] == metrics["decisions_total"]


def test_metrics_carries_the_evaluation_numbers(runner):
    """The plan's evaluation metrics must reach the dashboard, not stop at the ledger."""
    metrics = metrics_payload(runner)
    for field in ("demand_mae_mw", "production_mae_mw", "production_range_coverage",
                  "battery_throughput_mwh", "fallback_rate", "clip_rate"):
        assert field in metrics, f"{field} was dropped on the way out"


def test_goals_payload_matches_contract(runner, contract):
    goals = goals_payload(runner)
    assert [g["id"] for g in goals] == ["g1", "g2"]
    for goal in goals:
        _check_interface(goal, "Goal", contract, f"Goal[{goal['id']}]")
    reserve = next(g for g in goals if g["type"] == "reserve")
    assert reserve["mwh"] == 6 and reserve["by"] == "18:00" and reserve["hard"] is True
    assert reserve["stored_mwh"] == pytest.approx(runner.sim.stored_mwh)
    assert next(g for g in goals if g["type"] == "min_sell_price")["eur_per_mwh"] == 50


# --------------------------------------------------------------------------- #
# a dashboard must never be able to stop the site


@pytest.mark.parametrize("record", [
    {}, {"time": "07:00"}, {"time": "07:00", "demand_mw": float("nan")},
    {"decision": None, "guard": None, "features": None},
    {"decision": {"source": "nonsense", "confidence": 7}, "guard": {"applied_rules": None}},
    {"production_mw": -5.0, "soc_after_mwh": None},
])
def test_interval_payload_survives_broken_records(record, contract):
    _check_interface(interval_payload(record), "Interval", contract)


def test_payloads_survive_an_empty_runner(contract):
    empty = DayRunner(demo_day(seed=3), None, llm_call=_no_llm)
    _check_interface(state_payload(empty), "StateSnapshot", contract)
    assert state_payload(empty)["current"] is None
    day = day_payload(empty)
    assert day["now"] is None and len(day["intervals"]) == 96
    _check_interface(day, "DayResponse", contract)
    assert goals_payload(empty) == []


def test_payloads_survive_a_junk_runner(contract):
    class Junk:
        records = [{"time": "00:00"}]
    _check_interface(state_payload(Junk()), "StateSnapshot", contract)
    assert metrics_payload(Junk())["decisions_total"] == 0
    assert goals_payload(Junk()) == []


def test_forecast_payload_sums_without_a_split(contract):
    slot = forecast_payload({"demand_mw": 2.0}, time="05:00", price_eur_mwh=70.0, break_even=90.0)
    _check_interface(slot, "ForecastInterval", contract)
    assert slot["production_mw"] == 0.0
