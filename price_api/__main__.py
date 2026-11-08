"""Start the mock price API.

    python -m price_api                          # http://127.0.0.1:8100
    python -m price_api --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m price_api", description="Mock energy price API.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args(argv)
    uvicorn.run("price_api.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
