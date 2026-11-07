"""Scrape ENTSO-E "Total Load - Day Ahead / Actual" data for Romania.

The ENTSO-E transparency platform only exports up to 7 days per request, so the
date range is split into 7-day windows, walking backwards from the end date.

For each window:
  1. POST the export payload to /downloadRequest/register -> {"requestId": "..."}
  2. GET /downloadRequest/get?requestId=<id> -> CSV file (polled until ready)

Each window is saved as its own CSV in the output directory, and at the end all
windows are merged (chronologically, single header) into one CSV.

Exports require a logged-in ENTSO-E account. Log in on the website, open
DevTools > Network, trigger one export, and copy the `Authorization` header of
the `downloadRequest/register` request (or its `Cookie` header):

    set ENTSOE_TOKEN=eyJhbGciOi...        (PowerShell: $env:ENTSOE_TOKEN="...")

On Windows, `pip install truststore` if you get CERTIFICATE_VERIFY_FAILED.

Usage:
    python total_load_day_ahead_actual.py
    python total_load_day_ahead_actual.py --start 2022-11-07 --end 2026-11-07 --out data/load
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Python only trusts root certificates already present in the local store, while
# Windows downloads some roots (e.g. ENTSO-E's Sectigo R46) on demand. If the
# optional `truststore` package is installed, verify TLS via the OS instead.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

BASE_URL = "https://transparency.entsoe.eu"
REGISTER_URL = f"{BASE_URL}/downloadRequest/register"
GET_URL = f"{BASE_URL}/downloadRequest/get?requestId={{request_id}}"

USE_CASE = "load/total/dayAhead/export"
AREA = "BZN|10YRO-TEL------P"  # Romania bidding zone
TIME_ZONE = "CET"
MAX_DAYS_PER_REQUEST = 7

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/csv, */*",
    "User-Agent": "Mozilla/5.0 (compatible; entsoe-load-scraper/1.0)",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/load/total/dayAhead",
}


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - 6) % 7)


def cet_midnight_utc(day: date) -> datetime:
    """UTC instant of local midnight (CET/CEST) for `day`.

    EU DST runs from the last Sunday of March to the last Sunday of October,
    so midnight is 22:00Z the previous day in summer and 23:00Z in winter.
    """
    dst_start = _last_sunday(day.year, 3)
    dst_end = _last_sunday(day.year, 10)
    offset_hours = 2 if dst_start < day <= dst_end else 1
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return local_midnight - timedelta(hours=offset_hours)


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def windows_backwards(start: date, end: date, size: int = MAX_DAYS_PER_REQUEST):
    """Yield (from_day, to_day_exclusive) windows from `end` back to `start`.

    `end` is inclusive (its whole day is fetched), `start` is inclusive.
    """
    cursor = end + timedelta(days=1)  # exclusive upper bound
    while cursor > start:
        lower = max(start, cursor - timedelta(days=size))
        yield lower, cursor
        cursor = lower


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_payload(from_day: date, to_day_exclusive: date) -> dict:
    return {
        "useCase": USE_CASE,
        "parameters": {
            "dateTimeRange": {
                "from": iso_z(cet_midnight_utc(from_day)),
                "to": iso_z(cet_midnight_utc(to_day_exclusive)),
            },
            "areaList": [AREA],
            "timeZone": TIME_ZONE,
            "sorterList": [],
            "filterMap": {},
            "format": "CSV",
        },
        "tryDownload": False,
    }


def set_auth(token: str | None, cookie: str | None) -> None:
    """Exports require a logged-in user; anonymous calls get 403 accessDenied."""
    if token:
        token = token.strip()
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        HEADERS["Authorization"] = token
    if cookie:
        HEADERS["Cookie"] = cookie.strip()


def http(method: str, url: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


class AuthError(RuntimeError):
    pass


def register(payload: dict) -> str:
    try:
        _, _, raw = http("POST", REGISTER_URL, payload)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(
                f"HTTP {e.code}: not authorized. Log in on transparency.entsoe.eu, open DevTools > "
                "Network, export once, and copy the 'Authorization' header (or 'Cookie') of the "
                "downloadRequest/register request into ENTSOE_TOKEN / ENTSOE_COOKIE."
            ) from e
        raise
    request_id = json.loads(raw).get("requestId")
    if not request_id:
        raise RuntimeError(f"No requestId in response: {raw[:300]!r}")
    return request_id


def _extract_csv(headers, raw: bytes) -> str | None:
    """Return CSV text if `raw` is the finished file, otherwise None."""
    if raw[:2] == b"PK":  # zipped export
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if not n.endswith("/"))
            return zf.read(name).decode("utf-8-sig")

    ctype = (headers.get("Content-Type") or "").lower()
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if "json" in ctype or stripped.startswith(("{", "[")):
        return None  # still a status / pending message
    return text if stripped else None


def download(request_id: str, max_wait: float = 180, poll: float = 2) -> str:
    url = GET_URL.format(request_id=request_id)
    deadline = time.monotonic() + max_wait
    last = b""
    while True:
        try:
            _, headers, raw = http("GET", url)
            csv_text = _extract_csv(headers, raw)
            if csv_text is not None:
                return csv_text
            last = raw
        except urllib.error.HTTPError as e:
            # The file may not be ready yet; retry on "not ready"-ish codes.
            if e.code not in (202, 204, 404, 409, 425, 429, 500, 502, 503):
                raise
            last = e.read() or str(e).encode()
        if time.monotonic() > deadline:
            raise TimeoutError(f"Request {request_id} not ready: {last[:300]!r}")
        time.sleep(poll)


def fetch_window(from_day: date, to_day_excl: date, retries: int = 3) -> str:
    payload = build_payload(from_day, to_day_excl)
    for attempt in range(1, retries + 1):
        try:
            request_id = register(payload)
            return download(request_id)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            if "CERTIFICATE_VERIFY_FAILED" in str(e) and truststore is None:
                raise RuntimeError(
                    f"{e}\n    -> run `pip install truststore` so Python uses the OS certificate store"
                ) from e
            if attempt == retries:
                raise
            wait = 5 * attempt
            print(f"    attempt {attempt} failed ({e}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def merge_csvs(files: list[Path], target: Path) -> int:
    rows_written = 0
    header = None
    with target.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        for f in files:  # already chronological
            with f.open(newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                file_header = next(reader, None)
                if file_header is None:
                    continue
                if header is None:
                    header = file_header
                    writer.writerow(header)
                for row in reader:
                    if row and row != header:
                        writer.writerow(row)
                        rows_written += 1
    return rows_written


def parse_args() -> argparse.Namespace:
    today = date.today()
    four_years_ago = today.replace(year=today.year - 4)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=four_years_ago,
                   help=f"first day to fetch, inclusive (default: {four_years_ago})")
    p.add_argument("--end", type=date.fromisoformat, default=today,
                   help=f"last day to fetch, inclusive (default: {today})")
    p.add_argument("--out", type=Path, default=Path("data/total_load_day_ahead_actual"),
                   help="output directory")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between windows")
    p.add_argument("--force", action="store_true", help="re-download windows already on disk")
    p.add_argument("--token", default=os.environ.get("ENTSOE_TOKEN"),
                   help="Authorization bearer token from a logged-in browser session "
                        "(default: $ENTSOE_TOKEN)")
    p.add_argument("--cookie", default=os.environ.get("ENTSOE_COOKIE"),
                   help="raw Cookie header from a logged-in browser session (default: $ENTSOE_COOKIE)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.start > args.end:
        print("--start must be before --end", file=sys.stderr)
        return 2
    if not args.token and not args.cookie:
        print("Warning: no --token/--cookie given; ENTSO-E rejects anonymous exports (403).",
              file=sys.stderr)
    set_auth(args.token, args.cookie)

    chunks_dir = args.out / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    windows = list(windows_backwards(args.start, args.end))
    print(f"Fetching {args.start} -> {args.end} for {AREA} in {len(windows)} windows")

    failed = []
    for i, (lo, hi_excl) in enumerate(windows, 1):
        hi = hi_excl - timedelta(days=1)
        target = chunks_dir / f"{lo.isoformat()}_{hi.isoformat()}.csv"
        label = f"[{i}/{len(windows)}] {lo} .. {hi}"
        if target.exists() and target.stat().st_size > 0 and not args.force:
            print(f"{label}  skip (exists)")
            continue
        try:
            csv_text = fetch_window(lo, hi_excl)
        except AuthError as e:
            print(f"{label}  {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"{label}  FAILED: {e}", file=sys.stderr)
            failed.append((lo, hi))
            continue
        target.write_text(csv_text, encoding="utf-8", newline="")
        print(f"{label}  ok ({len(csv_text.splitlines()) - 1} rows)")
        time.sleep(args.delay)

    chunk_files = sorted(chunks_dir.glob("*.csv"))
    merged = args.out / f"total_load_day_ahead_actual_{args.start}_{args.end}.csv"
    n = merge_csvs(chunk_files, merged)
    print(f"\nMerged {len(chunk_files)} chunk(s), {n} rows -> {merged}")

    if failed:
        print(f"{len(failed)} window(s) failed; re-run the script to retry them:", file=sys.stderr)
        for lo, hi in failed:
            print(f"  {lo} .. {hi}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
