"""Create a small set of useful plots from the energy dataset.

Usage:
    python visualize_data.py
    python visualize_data.py --input dataset.csv --output-dir plots
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


LOAD_COLUMN = "actual_total_load"
RENEWABLE_COLUMNS = [
    "biomass",
    "hydro_run_of_river_and_pondage",
    "hydro_water_reservoir",
    "solar",
    "wind_onshore",
]
ENERGY_STORAGE_COLUMN = "energy_storage"
REQUIRED_COLUMNS = {"timestamp", LOAD_COLUMN, *RENEWABLE_COLUMNS, ENERGY_STORAGE_COLUMN}


def load_dataset(path: Path) -> pd.DataFrame:
    """Load the CSV, parse timestamps and coerce measurements to numbers."""
    data = pd.read_csv(path)
    missing = REQUIRED_COLUMNS.difference(data.columns)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise ValueError(f"{path} is missing required columns: {missing_columns}")

    data["timestamp"] = pd.to_datetime(data["timestamp"], unit="s", utc=True)
    data = data.set_index("timestamp").sort_index()
    numeric_columns = [column for column in data.columns if column != "timestamp"]
    data[numeric_columns] = data[numeric_columns].apply(pd.to_numeric, errors="coerce")
    return data


def save_parsed_dataset(data: pd.DataFrame, path: Path) -> None:
    """Save parsed data with sensible storage handling and an observation flag."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parsed = data.reset_index()
    parsed["timestamp"] = parsed["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed["energy_storage_observed"] = parsed[ENERGY_STORAGE_COLUMN].notna()
    parsed[ENERGY_STORAGE_COLUMN] = (
        parsed[ENERGY_STORAGE_COLUMN]
        .interpolate(limit_area="inside")
        .fillna(0)
    )
    parsed.to_csv(path, index=False)


def create_plots(data: pd.DataFrame, output_dir: Path) -> None:
    """Create and save overview, hourly profile, and correlation plots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    renewable = data[RENEWABLE_COLUMNS].sum(axis=1, min_count=1)

    daily = pd.DataFrame(
        {
            "Total load": data[LOAD_COLUMN],
            "Renewable generation": renewable,
            "Energy storage": data[ENERGY_STORAGE_COLUMN],
        },
        index=data.index,
    ).resample("D").mean()
    figure, axis = plt.subplots(figsize=(13, 6))
    daily.plot(ax=axis, linewidth=1.2)
    axis.set_title("Daily average load and renewable generation")
    axis.set_xlabel("Date (UTC)")
    axis.set_ylabel("Power")
    axis.grid(alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_dir / "energy_overview.png", dpi=150)
    plt.close(figure)

    hourly = data[[LOAD_COLUMN] + RENEWABLE_COLUMNS].groupby(data.index.hour).mean()
    hourly = hourly.rename(columns={LOAD_COLUMN: "Total load"})
    hourly["Renewable generation"] = hourly[RENEWABLE_COLUMNS].sum(axis=1)
    figure, axis = plt.subplots(figsize=(11, 6))
    hourly[["Total load", "Renewable generation"]].plot(
        ax=axis, marker="o", linewidth=1.5
    )
    axis.set_title("Average power by hour of day")
    axis.set_xlabel("Hour (UTC)")
    axis.set_ylabel("Power")
    axis.set_xticks(range(24))
    axis.grid(alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_dir / "hourly_profile.png", dpi=150)
    plt.close(figure)

    correlation_columns = [LOAD_COLUMN] + RENEWABLE_COLUMNS + [ENERGY_STORAGE_COLUMN]
    correlation = data[correlation_columns].corr()
    figure, axis = plt.subplots(figsize=(9, 7))
    image = axis.imshow(correlation, cmap="coolwarm", vmin=-1, vmax=1)
    axis.set_title("Generation and load correlations")
    axis.set_xticks(range(len(correlation_columns)), correlation_columns, rotation=45, ha="right")
    axis.set_yticks(range(len(correlation_columns)), correlation_columns)
    for row in range(len(correlation_columns)):
        for column in range(len(correlation_columns)):
            axis.text(column, row, f"{correlation.iloc[row, column]:.2f}", ha="center", va="center")
    figure.colorbar(image, ax=axis, label="Correlation")
    figure.tight_layout()
    figure.savefig(output_dir / "correlation_heatmap.png", dpi=150)
    plt.close(figure)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("dataset.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("plots"))
    parser.add_argument(
        "--cleaned-output",
        type=Path,
        default=Path("parsed_dataset.csv"),
        help="Where to save parsed timestamps and numeric values.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_dataset(args.input)
    save_parsed_dataset(data, args.cleaned_output)
    create_plots(data, args.output_dir)
    print(f"Saved parsed data to {args.cleaned_output.resolve()}")
    print(f"Saved three plots to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
