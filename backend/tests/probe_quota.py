"""Quota probe — spend ONE tiny model call per bucket to find out what today allows.

Run this BEFORE any live suite (test_phase_0_3.py, test_multilang.py, the prompt
battery). Those cost 9-97 calls; discovering mid-run that the bucket was already
spent wastes the run and the remaining quota on retry backoffs.

WHY THIS EXISTS — the failure modes look identical but mean opposite things:

  429 RESOURCE_EXHAUSTED  the daily bucket is genuinely spent. Waiting will not
                          help; the free tier resets at midnight US-Pacific,
                          which is 12:30 PM IST.
  503 UNAVAILABLE         "high demand". The bucket is NOT spent — retry, or pin
                          the other model via SUMIO_MODEL and carry on.

You cannot tell them apart from the error message. llm._generate_with_retry wraps
BOTH in the same friendly ModelUnavailableError ("...the free tier has a usage
cap..."), so string-matching that text tells you nothing. It does raise
`... from last_exc`, so the original errors.APIError survives on __cause__ —
walk the chain and read .code / .status. That is what describe() below does.

Run from backend:
  .venv\\Scripts\\python.exe tests\\probe_quota.py
  .venv\\Scripts\\python.exe tests\\probe_quota.py --models gemini-2.5-flash

Exit code 0 if at least one bucket is usable, 1 if none are — so it can gate a
longer run:
  .venv\\Scripts\\python.exe tests\\probe_quota.py && .venv\\Scripts\\python.exe tests\\test_phase_0_3.py --only "· EN"
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

DEFAULT_MODELS = ("gemini-2.5-flash-lite", "gemini-2.5-flash")

# A minimal structure: enough for a real plan, small enough to cost almost nothing.
STRUCTURE = {
    "tables": {
        "sheet1": {
            "columns": ["Product", "Price"],
            "dtypes": {"Product": "text", "Price": "number"},
            "row_count": 3,
            "sample": [{"Product": "A", "Price": 10}],
        }
    },
    "primary": "sheet1",
}
PROMPT = "sort by Price highest to lowest"


def describe(exc: BaseException) -> tuple[object, object, str]:
    """Walk the __cause__ chain for the first error carrying a real API status."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "code", None)
        status = getattr(cur, "status", None)
        if code is not None or status is not None:
            return code, status, f"{type(cur).__name__}: {str(cur)[:200]}"
        cur = cur.__cause__
    return None, None, f"{type(exc).__name__}: {str(exc)[:200]}"


def quota_detail(exc: BaseException) -> tuple[str, str]:
    """Pull (quotaId, retryDelay) out of a 429 body.

    Google returns BOTH per-minute and per-day exhaustion as 429 RESOURCE_EXHAUSTED, so
    the status code alone cannot tell them apart. The quotaId can:
      GenerateRequestsPerMinute...   short burst limit, seconds from clearing
      GenerateRequestsPerDay...      the daily allowance

    Crucially, a PerDay 429 STILL carries a retryDelay. Observed 2026-08-12: the 20/day
    flash-lite allowance was exhausted, the body said "Please retry in 45.3s", and a
    retry DID succeed. So the daily allowance trickles back instead of hard-locking
    until midnight. Never report a 429 as "nothing more today" when a retryDelay is
    present — say how long to wait.
    """
    text = ""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text += str(cur)
        cur = cur.__cause__
    qid = re.search(r"'quotaId':\s*'([^']+)'", text)
    delay = re.search(r"'retryDelay':\s*'([^']+)'", text) or re.search(r"retry in ([\d.]+)s", text)
    return (qid.group(1) if qid else ""), (delay.group(1) if delay else "")


def probe(model: str) -> bool:
    """One call on `model`. Returns True if the bucket is usable."""
    os.environ["SUMIO_MODEL"] = model
    # config.MODEL is read at import time, so drop app.* to pick up the new env var.
    for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
        del sys.modules[name]

    try:
        from app import llm
        res = llm.parse_instruction(PROMPT, STRUCTURE)
    except Exception as exc:  # noqa: BLE001 — classifying is the whole point
        code, status, detail = describe(exc)
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            qid, delay = quota_detail(exc)
            scope = "per-day" if "PerDay" in qid else ("per-minute" if "PerMinute" in qid else "unknown-scope")
            if delay:
                verdict = f"429 {scope} quota hit — retry in {delay} (NOT locked out; it replenishes)"
            else:
                verdict = f"429 {scope} quota hit — no retryDelay given"
            if qid:
                print(f"  {model:26s} {verdict}")
                print(f"      quotaId: {qid}")
                return False
        elif code == 503 or status == "UNAVAILABLE":
            verdict = "503 HIGH DEMAND — bucket NOT spent, retry is worthwhile"
        else:
            verdict = f"UNCLASSIFIED — code={code} status={status}"
        print(f"  {model:26s} {verdict}")
        print(f"      {detail}")
        return False

    actions = [op.get("action") for op in (res.get("operations") or [])]
    print(f"  {model:26s} OK — parsed a plan, actions={actions}")
    return True


if __name__ == "__main__":
    models = list(DEFAULT_MODELS)
    if "--models" in sys.argv:
        models = [m.strip() for m in sys.argv[sys.argv.index("--models") + 1].split(",") if m.strip()]

    print("QUOTA PROBE — one call per bucket\n")
    # The key normally lives in backend/.env, not the shell environment, and config
    # accepts GOOGLE_API_KEY too — so ask config (which load_dotenv()s) rather than
    # checking os.getenv here, which would false-negative on a working setup.
    from app import config as _config
    if not _config.GEMINI_API_KEY:
        print("  No GEMINI_API_KEY / GOOGLE_API_KEY (checked env and backend/.env) —")
        print("  nothing to probe; live suites would fall back to the offline path.")
        sys.exit(1)

    usable = [m for m in models if probe(m)]

    print()
    if usable:
        print(f"{len(usable)} of {len(models)} usable: {', '.join(usable)}")
        print(f"Pin one for a live slice:  $env:SUMIO_MODEL=\"{usable[0]}\"")
    else:
        print("No bucket answered right now.")
        print("A 429 with a retryDelay is NOT a hard lockout — the free-tier allowance")
        print("replenishes, and a SINGLE call may well get through on a retry.")
        print("But once the daily 20 is spent the trickle is far too slow to carry a")
        print("multi-call suite: measured 2026-08-12, two single-case runs each burned")
        print("~240s of backoff and still failed. Treat a spent day as done, and run")
        print("matrices after the hard reset at midnight US-Pacific = 12:30 PM IST.")
    sys.exit(0 if usable else 1)
