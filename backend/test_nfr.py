"""Non-functional requirements — the cross-cutting guarantees, locked in.

  PERF   Typical files (≤50k rows) process fast; larger files still work AND show a
         "large file" notice.
  REL    An operation fully succeeds or fails cleanly — the file is left in a known state
         (the prior version is intact; no half-applied result is pushed).
  SEC    Secrets come from the environment, never hardcoded; the API key never appears in
         responses; sensitive connection credentials are never echoed.
  I18N   Full UTF-8: Hindi/Urdu headers + data survive a round-trip unchanged.
  COST   The Brain uses a fast, low-cost tier, and every AI call goes through ONE wrapper.

Run from backend:  .venv\\Scripts\\python.exe test_nfr.py
"""
from __future__ import annotations

import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import config, main
from app.executor import OperationError, execute_multi

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


client = TestClient(main.app)
print("NON-FUNCTIONAL REQUIREMENTS\n")

# =========================================================================
# PERF  Typical files fast; large files work + show a notice
# =========================================================================
print("PERF  Performance + large-file notice")

# 50k rows processed within a few seconds.
big = pd.DataFrame({"Region": ["N", "S", "E", "W"] * 12500, "Revenue": list(range(50000))})
main._remember_session("nfr_big", {"tables": {"Big": big}, "primary": "Big", "exts": {"Big": "csv"}, "notes": {}})
t0 = time.time()
r = client.post("/execute", data={"session_id": "nfr_big",
                                  "plan": '{"operations":[{"action":"sort","columns":["Revenue"],"orders":["desc"]}]}'})
elapsed = time.time() - t0
body = r.json()
check("PERF 50k-row sort succeeds", body.get("status") == "ok", str(body)[:120])
check("PERF 50k rows handled in a few seconds", elapsed < 8.0, f"{elapsed:.2f}s")

# >50k rows → a friendly "large file" notice is surfaced.
huge = pd.DataFrame({"A": range(60000), "B": range(60000)})
main._remember_session("nfr_huge", {"tables": {"H": huge}, "primary": "H", "exts": {"H": "csv"}, "notes": {}})
r2 = client.post("/execute", data={"session_id": "nfr_huge",
                                   "plan": '{"operations":[{"action":"remove_duplicates"}]}'}).json()
notes_text = " ".join(r2.get("notes", []))
check("PERF large file shows a notice", "large file" in notes_text.lower(), notes_text[:120])

# =========================================================================
# REL  Fully succeed or fail cleanly — known state preserved
# =========================================================================
print("\nREL  Atomic — known state on failure")

df = pd.DataFrame({"R": ["N", "S", "N"], "P": [3, 1, 2]})
# A later step fails (bad column). The result reflects ONLY the good steps; it never
# half-applies the bad one, and the executor surfaces it cleanly.
try:
    execute_multi({"t": df}, "t", [
        {"action": "sort", "columns": ["P"], "orders": ["desc"]},
        {"action": "drop_columns", "columns": ["ghost"]},
    ])
    check("REL later-step failure raises (not silent)", False, "no error")
except Exception as e:
    from app.executor import MultiStepError
    check("REL later-step failure is a clean MultiStepError", isinstance(e, MultiStepError), type(e).__name__)
    check("REL partial result = state after the LAST GOOD step", list(e.partial_result["P"]) == [3, 2, 1], str(list(e.partial_result["P"])))

# A first-step failure leaves nothing applied (clean error, original untouched).
try:
    execute_multi({"t": df}, "t", [{"action": "drop_columns", "columns": ["ghost"]}])
    check("REL first-step failure raises cleanly", False, "no error")
except OperationError:
    check("REL first-step failure raises cleanly", True)
check("REL source df untouched by a failed plan", list(df["P"]) == [3, 1, 2], str(list(df["P"])))

# =========================================================================
# SEC  Secrets from env, never hardcoded or echoed
# =========================================================================
print("\nSEC  Secrets + privacy")

import inspect
import app.config as cfg
import app.llm as llm_mod

cfg_src = inspect.getsource(cfg)
check("SEC API key read from the environment", "os.getenv" in cfg_src and "GEMINI_API_KEY" in cfg_src, "")
check("SEC no hardcoded Google API key in config", "AIza" not in cfg_src, "")
check("SEC no hardcoded key in the llm wrapper", "AIza" not in inspect.getsource(llm_mod), "")
# A connection's secret is never returned (reuses 3.2 safe view).
from app import connectors as cn
cn._CONNECTIONS.clear()
conn = cn.register_connection("db", "postgres", {"password": "topsecret"})
check("SEC connection credentials never returned", "topsecret" not in str(cn.get_connection(conn["id"])), str(conn))
cn._CONNECTIONS.clear()

# =========================================================================
# I18N  Full UTF-8 round-trip (Hindi / Urdu)
# =========================================================================
print("\nI18N  UTF-8 / multilingual")

# Hindi header + Devanagari data + Urdu text survive load → operate → output unchanged.
# Build the CSV from the same literals the assertion uses (no script-mixing surprises).
NAAM, MULYA = "नाम", "मूल्य"          # Hindi headers
URDU = "راہُل"                        # Urdu name (row 1, duplicated in row 3)
DEV = "प्रिया"                        # Devanagari name (row 2)
csv = f"{NAAM},{MULYA}\n{URDU},१००\n{DEV},200\n{URDU},१००\n".encode("utf-8")
client.post("/inspect", data={"session_id": "nfr_i18n"}, files=[("files", ("d.csv", csv, "text/csv"))])
res = client.post("/execute", data={"session_id": "nfr_i18n",
                                    "plan": '{"operations":[{"action":"remove_duplicates"}]}'}).json()
cols = [c["name"] for c in res["preview"][0]["columns"]]
check("I18N non-Latin headers preserved", cols == [NAAM, MULYA], str(cols))
sample = res["preview"][0]["sample_rows"]
names = [row.get(NAAM) for row in sample]
check("I18N Devanagari/Urdu cell values preserved unchanged", set(names) == {URDU, DEV}, str(names))
check("I18N values are genuinely non-ASCII (UTF-8 kept)", all(not str(n).isascii() for n in names), str(names))
check("I18N dedupe worked on Unicode keys (3 → 2 rows)", res["row_count"] == 2, str(res["row_count"]))

# =========================================================================
# COST  Fast low-cost tier + single wrapper
# =========================================================================
print("\nCOST  Model tier + single wrapper")

check("COST default model is a fast/low-cost tier", "flash" in config.MODEL.lower(), config.MODEL)
check("COST model is swappable via env (one place)", "SUMIO_MODEL" in cfg_src, "")
# Every AI call goes through llm.py: only that module imports the genai client.
import pathlib
app_dir = pathlib.Path(main.__file__).parent
offenders = []
for p in app_dir.glob("*.py"):
    if p.name == "llm.py":
        continue
    txt = p.read_text(encoding="utf-8")
    if "genai" in txt or "generativeai" in txt:
        # allow a passing mention in a comment, but not an actual import/Client use
        if "import" in txt and ("genai" in txt.split("#")[0] if "#" in txt else "genai" in txt):
            offenders.append(p.name)
check("COST all AI calls go through the single llm wrapper", offenders == [], f"genai used in: {offenders}")
check("COST llm wrapper exposes one client factory", hasattr(llm_mod, "_client"), "")

main._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
