"""ENTSO-E official REST API: Romania's actual total load and generation per type.

The same data as the website exports (total_load_day_ahead_actual.py,
actual_generation_per_production_type.py), but authorised by a permanent
security token instead of a browser token that expires after ~15 minutes, so
it can run unattended (predictions/live.py calls it every 15 minutes).

Getting a token (free): register on https://transparency.entsoe.eu, email
transparency@entsoe.eu with the subject "Restful API access" and your account's
e-mail address in the body, then generate the token under My Account Settings
once access is granted. Put it in $ENTSOE_API_TOKEN.

    $env:ENTSOE_API_TOKEN = "..."
    python entsoe_api.py                 # last 2 days: prints the rows it got
    python entsoe_api.py --days 7
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:  # Windows fetches some root certificates on demand (see entsoe_export.py)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

API_URL = "https://web-api.tp.entsoe.eu/api"
ROMANIA = "10YRO-TEL------P"

# ENTSO-E psrType code -> dataset column (same columns as build_dataset.GENERATION_COLUMNS)
PSR_COLUMNS = {
    "B01": "biomass",
    "B02": "fossil_brown_coal/Lignite",
    "B04": "fossil_gas",
    "B11": "hydro_run_of_river_and_pondage",
    "B12": "hydro_water_reservoir",
    "B14": "nuclear",
    "B16": "solar",
    "B19": "wind_onshore",
    "B25": "energy_storage",
}
RESOLUTIONS = {"PT15M": 15, "PT30M": 30, "PT60M": 60}
STEP = timedelta(minutes=15)


class EntsoeApiError(RuntimeError):
    pass


class EntsoeAuthError(EntsoeApiError):
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def query(params: dict, token: str, retries: int = 3, timeout: int = 60) -> bytes:
    """GET the API; the XML body. A 'no matching data' reply comes back as b''."""
    url = f"{API_URL}?{urllib.parse.urlencode({'securityToken': token, **params})}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 401:
                raise EntsoeAuthError("HTTP 401: ENTSOE_API_TOKEN is missing or not valid") from e
            if e.code == 400 and b"No matching data" in body:
                return b""
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise EntsoeApiError(f"HTTP {e.code}: {_reason(body) or body[:300]!r}") from e
        except urllib.error.URLError as e:
            if attempt == retries:
                raise EntsoeApiError(f"ENTSO-E API unreachable: {e.reason}") from e
        time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def _reason(body: bytes) -> str:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    return " ".join(el.text or "" for el in root.iter() if _local(el.tag) == "text").strip()


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: ET.Element, path: str) -> str | None:
    for name in path.split("/"):
        el = _child(el, name) if el is not None else None
    return el.text.strip() if el is not None and el.text else None


def _parse_time(text: str) -> datetime:
    return datetime.strptime(text.replace("Z", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


def parse_series(xml: bytes) -> list[tuple[dict[str, str | None], dict[datetime, float]]]:
    """Every TimeSeries in a GL_MarketDocument: (attributes, {15-min UTC start: MW}).

    Coarser resolutions are spread over their 15-min intervals (an hourly MW
    value holds for each of its quarters). Curve type A03 omits a point when the
    value repeats, so a missing position carries the previous value forward.
    """
    if not xml:
        return []
    root = ET.fromstring(xml)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = _reason(xml)
        if "No matching data" in reason:
            return []
        raise EntsoeApiError(f"ENTSO-E rejected the request: {reason}")
    out = []
    for ts in (el for el in root if _local(el.tag) == "TimeSeries"):
        attrs = {
            "psr_type": _text(ts, "MktPSRType/psrType"),
            "in_domain": _text(ts, "inBiddingZone_Domain.mRID"),
            "out_domain": _text(ts, "outBiddingZone_Domain.mRID"),
            "curve_type": _text(ts, "curveType"),
        }
        values: dict[datetime, float] = {}
        for period in (el for el in ts if _local(el.tag) == "Period"):
            start = _parse_time(_text(period, "timeInterval/start"))
            end = _parse_time(_text(period, "timeInterval/end"))
            minutes = RESOLUTIONS.get(_text(period, "resolution") or "")
            if minutes is None:
                raise EntsoeApiError(f"unsupported resolution {_text(period, 'resolution')!r}")
            step = timedelta(minutes=minutes)
            points = {int(_text(p, "position")): float(_text(p, "quantity"))
                      for p in period if _local(p.tag) == "Point"}
            n = int((end - start) / step)
            last = None
            for pos in range(1, n + 1):
                last = points.get(pos, last if attrs["curve_type"] == "A03" else None)
                if last is None:
                    continue
                t = start + (pos - 1) * step
                for k in range(minutes // 15):
                    values[t + k * STEP] = last
        out.append((attrs, values))
    return out


# --------------------------------------------------------------------------- #
# The two series the dataset needs
# --------------------------------------------------------------------------- #
def fetch_load(start: datetime, end: datetime, token: str, area: str = ROMANIA) -> dict[datetime, float]:
    """Actual total load (MW) per 15-min UTC start in [start, end)."""
    xml = query({"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": area,
                 "periodStart": _stamp(start), "periodEnd": _stamp(end)}, token)
    out: dict[datetime, float] = {}
    for _, values in parse_series(xml):
        out.update(values)
    return {t: v for t, v in out.items() if start <= t < end}


def fetch_generation(start: datetime, end: datetime, token: str,
                     area: str = ROMANIA) -> dict[datetime, dict[str, float]]:
    """Actual generation (MW) per production type, keyed by 15-min UTC start in [start, end).

    Series with an outBiddingZone are consumption (e.g. storage charging), not
    generation, and are skipped, as the website export's "Generation" column does.
    """
    xml = query({"documentType": "A75", "processType": "A16", "in_Domain": area,
                 "periodStart": _stamp(start), "periodEnd": _stamp(end)}, token)
    out: dict[datetime, dict[str, float]] = {}
    for attrs, values in parse_series(xml):
        col = PSR_COLUMNS.get(attrs["psr_type"] or "")
        if col is None or attrs["out_domain"]:
            continue
        for t, v in values.items():
            if start <= t < end:
                out.setdefault(t, {})[col] = v
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Check ENTSOE_API_TOKEN: fetch recent load and generation.")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--token", default=os.environ.get("ENTSOE_API_TOKEN"))
    args = p.parse_args()
    if not args.token:
        print("Set ENTSOE_API_TOKEN (or pass --token); see this file's docstring.", file=sys.stderr)
        return 2
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=args.days)
    load = fetch_load(start, end, args.token)
    gen = fetch_generation(start, end, args.token)
    print(f"load: {len(load)} intervals, last {max(load) if load else '-'}")
    print(f"generation: {len(gen)} intervals, last {max(gen) if gen else '-'}")
    for t in sorted(load)[-4:]:
        print(t.isoformat(), load[t], gen.get(t, {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
