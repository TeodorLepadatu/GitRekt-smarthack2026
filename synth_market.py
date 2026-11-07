"""
Synthetic electricity market simulator for the hackathon.

Input : the scraped Romanian dataset (15-min, MW, real ENTSO-E-style columns).
Output: 3 CSV files with SIMULATED data (everything synthetic is driven by real
        load/generation, plus clearly stated assumptions in ASSUMPTIONS):

  portfolio.csv  - PPC's simulated customer load, own wind/solar, net need (MW)
  prices.csv     - day-ahead price + imbalance prices (EUR/MWh), per 15 min
  offers.csv     - daily offers from other Romanian producers (volume MW, price EUR/MWh)

Usage: python synth_market.py dataset.csv [out_dir]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Assumptions - tweak these and present them openly in the pitch.
# --------------------------------------------------------------------------
ASSUMPTIONS = dict(
    seed=42,
    ppc_load_share=0.18,     # share of national load supplied by PPC Energy
    ppc_wind_share=0.20,     # PPC share of national wind output (Dobrogea parks)
    ppc_solar_share=0.05,    # PPC share of national solar output
    gas_price=35.0,          # EUR/MWh thermal (TTF-like)
    eua_price=70.0,          # EUR/t CO2
    gas_efficiency=0.50,
    import_premium=15.0,     # EUR/MWh above the most expensive domestic unit
    price_noise=0.08,        # relative noise on prices
    free_share=0.30,         # share of each producer's output NOT already contracted
)

# Marginal cost per technology (EUR/MWh). Hydro reservoir gets an opportunity
# cost that rises at peak hours (water is "saved" for expensive hours).
def marginal_costs(a, hour):
    gas = a["gas_price"] / a["gas_efficiency"] + a["eua_price"] * 0.37
    lignite = 15 + a["eua_price"] * 1.0
    hydro_res = 40 + 50 * np.isin(hour, range(17, 22))  # opportunity cost
    return {
        "solar": 0, "wind_onshore": 0, "hydro_run_of_river_and_pondage": 2,
        "nuclear": 10, "biomass": 60, "hydro_water_reservoir": hydro_res,
        "fossil_brown_coal/Lignite": lignite, "fossil_gas": gas,
    }

# Which company sells which technology, and which product it offers.
PRODUCERS = {
    "Nuclearelectrica": ("nuclear", "baseload"),             # flat 24h block
    "Hidroelectrica": ("hydro_water_reservoir", "peak"),     # 08-20h block
    "Hidroelectrica RoR": ("hydro_run_of_river_and_pondage", "baseload"),
    "CE Oltenia": ("fossil_brown_coal/Lignite", "baseload"),
    "Gas producers": ("fossil_gas", "peak"),
}
MARKUP = {"baseload": (0.05, 0.15), "peak": (0.10, 0.30)}


def load_clean(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("time").drop(columns="timestamp")
    df.loc[df["actual_total_load"] <= 100, "actual_total_load"] = np.nan  # gaps
    df["energy_storage"] = df["energy_storage"].fillna(0)
    return df.interpolate(limit=8).dropna()


def day_ahead_price(df, a, rng):
    """Merit order: stack real generation by cost; marginal unit sets price."""
    hour = df.index.tz_convert("Europe/Bucharest").hour.values
    costs = marginal_costs(a, hour)
    order = sorted(costs, key=lambda k: np.mean(costs[k]))
    load = df["actual_total_load"].values
    price = np.full(len(df), np.nan)
    cum = np.zeros(len(df))
    for tech in order:
        cum += df[tech].values
        c = np.broadcast_to(costs[tech], len(df))
        hit = np.isnan(price) & (cum >= load)
        price[hit] = c[hit]
    import_cost = max(np.max(np.atleast_1d(v)) for v in costs.values()) + a["import_premium"]
    price[np.isnan(price)] = import_cost  # domestic supply short -> imports set price
    # smooth the step function a bit and add noise
    price = pd.Series(price, index=df.index).rolling(4, min_periods=1).mean().values
    return price * rng.normal(1, a["price_noise"], len(df))


def main(path, out):
    a, rng = ASSUMPTIONS, np.random.default_rng(ASSUMPTIONS["seed"])
    df = load_clean(path)
    out = Path(out); out.mkdir(parents=True, exist_ok=True)

    # 1) PPC portfolio
    p = pd.DataFrame(index=df.index)
    p["ppc_load_mw"] = df["actual_total_load"] * a["ppc_load_share"]
    p["own_wind_mw"] = df["wind_onshore"] * a["ppc_wind_share"]
    p["own_solar_mw"] = df["solar"] * a["ppc_solar_share"]
    p["net_need_mw"] = p.ppc_load_mw - p.own_wind_mw - p.own_solar_mw
    p.to_csv(out / "portfolio.csv")

    # 2) Prices
    pr = pd.DataFrame(index=df.index)
    pr["da_price"] = day_ahead_price(df, a, rng)
    pr["imb_short_price"] = pr.da_price * rng.uniform(1.2, 1.8, len(pr))  # you pay
    pr["imb_long_price"] = pr.da_price * rng.uniform(0.3, 0.8, len(pr))   # you receive
    pr.round(2).to_csv(out / "prices.csv")

    # 3) Daily offers from other producers
    local = df.index.tz_convert("Europe/Bucharest")
    hour, date = local.hour, local.date
    da_daily = pr["da_price"].groupby(date).mean()
    rows = []
    for name, (tech, product) in PRODUCERS.items():
        s = df[tech]
        if product == "peak":
            s = s[(hour >= 8) & (hour < 20)]
            d = pd.Series(date, index=df.index)[(hour >= 8) & (hour < 20)]
        else:
            d = pd.Series(date, index=df.index)
        vol = s.groupby(d.values).min() * a["free_share"]       # firm, deliverable volume
        lo, hi = MARKUP[product]
        markup = rng.uniform(lo, hi, len(vol))
        # producers price near the expected market price, not their cost
        price = da_daily.reindex(vol.index).values * (1 + markup) * (1.1 if product == "peak" else 1.0)
        rows.append(pd.DataFrame({"date": vol.index, "producer": name, "product": product,
                                  "volume_mw": vol.round(0).values, "price_eur_mwh": price.round(2)}))
    pd.concat(rows).sort_values(["date", "producer"]).to_csv(out / "offers.csv", index=False)

    print(f"rows: {len(df)}  |  outputs in {out.resolve()}")
    print(p.describe().round(0).T[["mean", "min", "max"]])
    print(pr.describe().round(1).T[["mean", "min", "max"]])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "synthetic_out")
