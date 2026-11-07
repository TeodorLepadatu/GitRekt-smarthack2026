"""Shared helpers for ENTSO-E transparency platform CSV exports.

The platform exports at most 7 days per request, so a date range is split into
7-day windows (walking backwards from the end date). For each window:
  1. POST the payload to /downloadRequest/register -> {"requestId": "..."}
  2. GET /downloadRequest/get?requestId=<id>       -> CSV (polled until ready)

Windows are cached in a temporary `.<name>_parts` folder so an interrupted run
(e.g. expired token) resumes where it stopped. When every window succeeds they
are combined into ONE CSV and the temporary folder is removed.

Exports require a logged-in account: pass the browser's `Authorization: Bearer`
token via --token or $ENTSOE_TOKEN (it expires after ~15 minutes; just re-run).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Python only trusts roots already in the local store; Windows fetches some
# (e.g. ENTSO-E's Sectigo R46) on demand. `truststore` verifies via the OS.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

BASE_URL = "https://transparency.entsoe.eu"
REGISTER_URL = f"{BASE_URL}/downloadRequest/register"
GET_URL = f"{BASE_URL}/downloadRequest/get?requestId={{request_id}}"
ROMANIA = "BZN|10YRO-TEL------P"
MAX_DAYS_PER_REQUEST = 7

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/csv, */*",
    "User-Agent": "Mozilla/5.0 (compatible; entsoe-scraper/1.0)",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)


def midnight_utc(day: date, tz: str) -> datetime:
    """UTC instant of local midnight of `day` in `tz` ("UTC" or "CET")."""
    utc_midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if tz.upper() == "UTC":
        return utc_midnight
    if tz.upper() == "CET":  # CET/CEST, EU DST: last Sun of Mar .. last Sun of Oct
        summer = _last_sunday(day.year, 3) < day <= _last_sunday(day.year, 10)
        return utc_midnight - timedelta(hours=2 if summer else 1)
    raise ValueError(f"Unsupported time zone: {tz}")


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def windows_backwards(start: date, end: date, size: int = MAX_DAYS_PER_REQUEST):
    """Yield (from_day, to_day_exclusive) from `end` back to `start` (both inclusive)."""
    cursor = end + timedelta(days=1)
    while cursor > start:
        lower = max(start, cursor - timedelta(days=size))
        yield lower, cursor
        cursor = lower


def build_payload(use_case: str, area: str, tz: str, from_day: date, to_day_excl: date) -> dict:
    return {
        "useCase": use_case,
        "parameters": {
            "dateTimeRange": {
                "from": iso_z(midnight_utc(from_day, tz)),
                "to": iso_z(midnight_utc(to_day_excl, tz)),
            },
            "areaList": [area],
            "timeZone": tz,
            "sorterList": [],
            "filterMap": {},
            "format": "CSV",
        },
        "tryDownload": False,
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def set_auth(token: str | None, cookie: str | None) -> None:
    if token:
        token = token.strip()
        HEADERS["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    if cookie:
        HEADERS["Cookie"] = cookie.strip()


def http(method: str, url: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers, resp.read()


def register(payload: dict) -> str:
    try:
        _, raw = http("POST", REGISTER_URL, payload)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(
                f"HTTP {e.code}: not authorized (token missing or expired). Log in on "
                "transparency.entsoe.eu, copy the 'Authorization' header of a "
                "downloadRequest/register request from DevTools > Network into ENTSOE_TOKEN, "
                "and re-run (finished weeks are kept)."
            ) from e
        raise
    request_id = json.loads(raw).get("requestId")
    if not request_id:
        raise RuntimeError(f"No requestId in response: {raw[:300]!r}")
    return request_id


def _extract_csv(headers, raw: bytes) -> str | None:
    """CSV text if `raw` is the finished file, else None (still pending)."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if not n.endswith("/"))
            return zf.read(name).decode("utf-8-sig")
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if "json" in (headers.get("Content-Type") or "").lower() or stripped.startswith(("{", "[")):
        return None
    return text if stripped else None


def download(request_id: str, max_wait: float = 180, poll: float = 2) -> str:
    url = GET_URL.format(request_id=request_id)
    deadline = time.monotonic() + max_wait
    last = b""
    while True:
        try:
            headers, raw = http("GET", url)
            csv_text = _extract_csv(headers, raw)
            if csv_text is not None:
                return csv_text
            last = raw
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code} while downloading: token expired? Re-run with a new one.") from e
            if e.code not in (202, 204, 404, 409, 425, 429, 500, 502, 503):
                raise
            last = e.read() or str(e).encode()
        if time.monotonic() > deadline:
            raise TimeoutError(f"Request {request_id} not ready: {last[:300]!r}")
        time.sleep(poll)


def fetch_window(payload: dict, retries: int = 3) -> str:
    for attempt in range(1, retries + 1):
        try:
            return download(register(payload))
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            if "CERTIFICATE_VERIFY_FAILED" in str(e) and truststore is None:
                raise RuntimeError(f"{e}\n    -> run `pip install truststore`") from e
            if attempt == retries:
                raise
            print(f"    attempt {attempt} failed ({e}); retrying in {5 * attempt}s", file=sys.stderr)
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def combine(parts_dir: Path, output: Path) -> tuple[int, int]:
    """Merge the window CSVs (chronological by filename) into one file, header once."""
    files = sorted(parts_dir.glob("*.csv"))
    header = None
    seen: set[tuple[str, ...]] = set()
    rows = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        for f in files:
            with f.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                file_header = next(reader, None)
                if not file_header:
                    continue
                if header is None:
                    header = file_header
                    writer.writerow(header)
                for row in reader:
                    key = tuple(row)
                    if row and row != header and key not in seen:
                        seen.add(key)
                        writer.writerow(row)
                        rows += 1
    return len(files), rows


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(name: str, use_case: str, tz: str, area: str = ROMANIA, years_back: int = 4) -> int:
    """CLI entry point: fetch `use_case` for the date range and write one CSV."""
    today = date.today()
    default_start = today.replace(year=today.year - years_back)
    p = argparse.ArgumentParser(description=f"Download ENTSO-E '{use_case}' for {area} into one CSV.")
    p.add_argument("--start", type=date.fromisoformat, default=default_start,
                   help=f"first day, inclusive (default: {default_start})")
    p.add_argument("--end", type=date.fromisoformat, default=today,
                   help=f"last day, inclusive (default: {today})")
    p.add_argument("--out", type=Path, default=Path("data") / f"{name}.csv", help="output CSV file")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    p.add_argument("--token", default=os.environ.get("ENTSOE_TOKEN"), help="bearer token (default: $ENTSOE_TOKEN)")
    p.add_argument("--cookie", default=os.environ.get("ENTSOE_COOKIE"), help="Cookie header (default: $ENTSOE_COOKIE)")
    args = p.parse_args()

    if args.start > args.end:
        print("--start must be on or before --end", file=sys.stderr)
        return 2
    if not args.token and not args.cookie:
        print("Warning: no --token/--cookie; ENTSO-E rejects anonymous exports.", file=sys.stderr)
    set_auth(args.token, args.cookie)

    out: Path = args.out
    parts = out.parent / f".{out.stem}_parts"
    parts.mkdir(parents=True, exist_ok=True)

    windows = list(windows_backwards(args.start, args.end))
    print(f"{use_case}: {args.start} -> {args.end}, {area}, tz={tz}, {len(windows)} windows")
    failed = 0
    for i, (lo, hi_excl) in enumerate(windows, 1):
        hi = hi_excl - timedelta(days=1)
        part = parts / f"{lo}_{hi}.csv"
        label = f"[{i}/{len(windows)}] {lo} .. {hi}"
        if part.exists() and part.stat().st_size > 0:
            print(f"{label}  cached")
            continue
        try:
            text = fetch_window(build_payload(use_case, area, tz, lo, hi_excl))
        except AuthError as e:
            print(f"{label}  {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"{label}  FAILED: {e}", file=sys.stderr)
            failed += 1
            continue
        part.write_text(text, encoding="utf-8", newline="")
        print(f"{label}  ok ({len(text.splitlines()) - 1} rows)")
        time.sleep(args.delay)

    if failed:
        print(f"{failed} window(s) failed; re-run to retry (finished weeks are kept in {parts}).",
              file=sys.stderr)
        return 1

    n_files, n_rows = combine(parts, out)
    shutil.rmtree(parts)
    print(f"\nDone: {n_rows} rows from {n_files} windows -> {out}")
    return 0
