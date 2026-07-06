"""Fire any DUE scheduled deliveries and data syncs, then exit.

Armed schedules/syncs only send when something POSTs their run-due endpoints. Point a
cron at this script (e.g. a Render Cron Job every 5-15 min, or Windows Task Scheduler):

    python run_due.py

Environment:
  SUMIO_BASE_URL    base URL of the running backend (default http://127.0.0.1:8000)
  SUMIO_API_TOKEN   sent as X-API-Key if the API requires a token (optional)

It only TRIGGERS work the server already gated (schedules must be armed + due, syncs
enabled + due); it never sends anything on its own. Exit code is non-zero if any endpoint
call failed, so cron can alert on failures.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("SUMIO_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("SUMIO_API_TOKEN", "").strip()
ENDPOINTS = ("/distribution/run-due", "/sync/run-due", "/digest/run-due")


def _hit(path: str) -> None:
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    if TOKEN:
        req.add_header("X-API-Key", TOKEN)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (operator-configured URL)
        body = resp.read().decode("utf-8", "replace")
        try:
            reports = json.loads(body)
            count = len(reports.get("reports", reports)) if isinstance(reports, dict) else len(reports)
        except Exception:
            count = "?"
        print(f"{path}: HTTP {resp.status}, {count} item(s) processed")


def main() -> int:
    failures = 0
    for path in ENDPOINTS:
        try:
            _hit(path)
        except Exception as exc:  # keep going so one failing endpoint doesn't block the other
            failures += 1
            print(f"{path}: ERROR {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
