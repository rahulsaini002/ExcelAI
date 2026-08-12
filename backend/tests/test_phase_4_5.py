"""ENGINE PHASE 4.5 — forecasting / what-if / anomaly detection (verify & HARDEN).

The three ops (forecast / what_if / detect_anomalies) already existed with a strong suite
(backend/test_analytics.py, 45 checks). This suite re-checks the DoD at the /process
surface and locks in the Phase-4.5 HARDEN:

  DoD "forecast + confidence"     forecast emits an `analysis` block: kind=forecast,
                                  a confidence % + band + 95% CI columns.
  DoD "insufficient-data honesty" < 5 rows / non-numeric → a friendly OperationError,
                                  never a fabricated projection.
  DoD "what-if BEFORE/AFTER"      HARDENED: what_if now emits a structured `analysis`
                                  block (kind=what_if) with before/after/delta/pct — the
                                  same shape forecast & anomaly use — instead of prose
                                  only. It carries NO confidence (exact deterministic
                                  math must not masquerade as a prediction).
  DoD "anomaly genuinely unusual  detect_anomalies emits kind=anomaly with a confidence
       + confidence"              scaled by sample size; extremes flagged, normals not.

`/process` collects every `analysis` directive into its `analysis` array — so this suite
asserts on that, proving the blocks reach the response. Offline (mocked llm), no schema
change to llm.py.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_4_5.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p45.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import OperationError, execute_multi  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def analyses(render_ops):
    return [d for d in render_ops if d.get("type") == "analysis"]


print("ENGINE PHASE 4.5 — forecast / what-if / anomaly (verify & harden)\n")

REV = pd.DataFrame({"Month": list(range(1, 11)), "Revenue": [100, 120, 115, 135, 150, 140, 170, 160, 180, 200]})
SALES = pd.DataFrame({"Price": [10.0, 9.5, 10.5, 11.0, 10.0], "Units": [100, 110, 90, 80, 100]})
ANOM = pd.DataFrame({"Score": [10, 12, 11, 13, 10, 12, 11, 13, 1000, -500], "Label": list("ABCDEFGHIJ")})

# ===================== FORECAST: confidence block + CI (verify) =====================
res, _, notes, render = execute_multi({"t": REV}, "t", [{"action": "forecast", "columns": ["Revenue"], "count": 3}])
fa = [d for d in analyses(render) if d.get("kind") == "forecast"]
check("forecast emits an analysis block", len(fa) == 1, str(analyses(render)))
check("forecast block carries a confidence % + band",
      isinstance(fa[0].get("confidence"), int) and fa[0].get("band"), str(fa[0]))
tail = res.tail(3)
check("forecast has an ordered 95% CI (Lower ≤ Forecast ≤ Upper)",
      bool((tail["Revenue_Lower95"] <= tail["Revenue_Forecast"]).all() and
           (tail["Revenue_Forecast"] <= tail["Revenue_Upper95"]).all()), str(tail))

# ===================== WHAT-IF: HARDENED before/after directive =====================
res, _, notes, render = execute_multi({"t": SALES}, "t",
    [{"action": "what_if", "column": "Price", "formula": "{Price} * 1.1", "name": "Price (Scenario)"}])
wa = [d for d in analyses(render) if d.get("kind") == "what_if"]
check("what_if now emits an analysis block (HARDEN)", len(wa) == 1, str(analyses(render)))
w = wa[0] if wa else {}
before_expected = float(sum(SALES["Price"]))
after_expected = round(before_expected * 1.1, 4)
check("what_if block reports the correct BEFORE total", w.get("before") == round(before_expected, 4), str(w))
check("what_if block reports the correct AFTER total", w.get("after") == after_expected, str(w))
check("what_if block reports delta = after - before",
      w.get("delta") is not None and abs(w["delta"] - (after_expected - round(before_expected, 4))) < 1e-6, str(w))
check("what_if block reports pct ≈ +10%", w.get("pct") is not None and abs(w["pct"] - 10.0) < 0.01, str(w))
check("what_if carries NO confidence (exact math, not a prediction)", "confidence" not in w, str(w))
check("what_if block names the scenario column + formula",
      w.get("scenario_column") == "Price (Scenario)" and w.get("formula") == "{Price} * 1.1", str(w))
# the actual scenario values must still be exactly original × 1.1
check("what_if scenario values are exactly Price × 1.1",
      [round(float(v), 6) for v in res["Price (Scenario)"]] == [round(p * 1.1, 6) for p in SALES["Price"]], str(res["Price (Scenario)"].tolist()))
# non-numeric base: block still present, before/after are null (honest, no fabricated totals)
res2, _, _, render2 = execute_multi({"t": pd.DataFrame({"Name": ["a", "b", "c"]})}, "t",
    [{"action": "what_if", "column": "Name", "formula": "{Name}", "name": "Name2"}])
w2 = [d for d in analyses(render2) if d.get("kind") == "what_if"]
check("what_if on non-numeric base: block present with null before/after (no fabrication)",
      len(w2) == 1 and w2[0].get("before") is None and w2[0].get("after") is None, str(w2))

# ===================== ANOMALY: confidence + genuinely unusual (verify) =====================
res, _, notes, render = execute_multi({"t": ANOM}, "t",
    [{"action": "detect_anomalies", "columns": ["Score"], "anomaly_method": "zscore", "anomaly_threshold": 2.5}])
aa = [d for d in analyses(render) if d.get("kind") == "anomaly"]
check("anomaly emits an analysis block with confidence", len(aa) == 1 and isinstance(aa[0].get("confidence"), int), str(analyses(render)))
check("anomaly flags the extreme HIGH and LOW, not the normals",
      bool(res.at[ANOM['Score'].idxmax(), "Is_Anomaly"]) and bool(res.at[ANOM['Score'].idxmin(), "Is_Anomaly"])
      and not res.loc[res["Score"].between(10, 13), "Is_Anomaly"].any(), str(res[["Score", "Is_Anomaly"]].to_dict("records")))

# ===================== INSUFFICIENT-DATA HONESTY (verify) =====================
SMALL = pd.DataFrame({"Revenue": [100, 200, 150]})  # 3 rows


def declines(ops):
    try:
        execute_multi({"t": SMALL}, "t", ops)
        return None
    except OperationError as e:
        return str(e)


msg = declines([{"action": "forecast", "columns": ["Revenue"], "count": 3}])
check("forecast on <5 rows declines honestly (no fabricated projection)",
      msg is not None and ("5" in msg or "data point" in msg.lower()), str(msg))
msg = declines([{"action": "detect_anomalies", "columns": ["Revenue"]}])
check("anomaly on <5 rows declines honestly", msg is not None and ("5" in msg or "row" in msg.lower()), str(msg))
# what_if on a missing column declines (doesn't invent a scenario)
try:
    execute_multi({"t": SALES}, "t", [{"action": "what_if", "column": "Ghost", "formula": "{Ghost}*2", "name": "X"}])
    check("what_if on a missing column declines", False, "no error")
except OperationError as e:
    check("what_if on a missing column declines", "Ghost" in str(e) or "column" in str(e).lower(), str(e))

# ===================== /process surfaces the analysis blocks (end-to-end) =====================
_real = m.llm.parse_instruction
CSV = b"Month,Revenue\n1,100\n2,120\n3,115\n4,135\n5,150\n6,140\n7,170\n8,160\n9,180\n10,200\n"


def _run(op, sid):
    m.llm.parse_instruction = lambda i, s, h="": {"operations": [op], "title": "t"}
    try:
        return c.post("/process", data={"instruction": "x", "session_id": sid},
                      files=[("files", ("r.csv", CSV, "text/csv"))]).json()
    finally:
        m.llm.parse_instruction = _real


r = _run({"action": "forecast", "columns": ["Revenue"], "count": 3}, "fc")
check("/process forecast: analysis array has a forecast block with confidence",
      any(a.get("kind") == "forecast" and isinstance(a.get("confidence"), int) for a in (r.get("analysis") or [])), str(r.get("analysis")))
r = _run({"action": "what_if", "column": "Revenue", "formula": "{Revenue} * 1.2", "name": "Revenue (Scenario)"}, "wi")
wblk = [a for a in (r.get("analysis") or []) if a.get("kind") == "what_if"]
check("/process what_if: analysis array carries the before/after block",
      len(wblk) == 1 and wblk[0].get("before") is not None and wblk[0].get("after") is not None and wblk[0].get("pct") is not None, str(r.get("analysis")))
r = _run({"action": "detect_anomalies", "columns": ["Revenue"]}, "ad")
check("/process anomaly: analysis array has an anomaly block",
      any(a.get("kind") == "anomaly" for a in (r.get("analysis") or [])), str(r.get("analysis")))

m.llm.parse_instruction = _real

# ===================== battery coverage =====================
recs = list(csv.DictReader(open(TESTS / "prompt_battery.csv", encoding="utf-8-sig", newline="")))
an = [r for r in recs if r["capability"] == "analytics"]
by_op = defaultdict(set)
for r in an:
    by_op[(r.get("expected_plan") or "").strip()].add(r["language"])
LANGS = {"EN", "HI", "UR", "Hinglish"}
check("battery covers forecast/what_if/detect_anomalies each in all 4 languages",
      all(LANGS <= by_op.get(op, set()) for op in ("forecast", "what_if", "detect_anomalies")),
      {k: sorted(v) for k, v in by_op.items()})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
