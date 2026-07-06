"""Phase 3.5 — Predictive analytics tests.

PRD requirements tested here:
  AN-a  Forecasts include a confidence range (Lower95 < Forecast < Upper95).
  AN-b  What-if math is correct.
  AN-c  Anomalies flagged are genuinely unusual (extreme values caught, normal values not).
  AN-d  Insufficient-data cases declined honestly (< 5 rows → clear message, no crash).
  AN-e  End-to-end through the API (mocked LLM) for all three operations.

Run from backend:  .venv\\Scripts\\python.exe test_analytics.py
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from app import main
from app.executor import OperationError, execute_multi

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("PHASE 3.5 — PREDICTIVE ANALYTICS\n")

client = TestClient(main.app)
_orig_parse = main.llm.parse_instruction


# Shared test data ---------------------------------------------------------

REVENUE = [100, 120, 115, 135, 150, 140, 170, 160, 180, 200]  # 10 points, upward trend
REV_DF = pd.DataFrame({"Month": list(range(1, 11)), "Revenue": REVENUE})

PRICES = [10.0, 9.5, 10.5, 11.0, 10.0]
UNITS  = [100,  110,  90,  80,  100]
SALES_DF = pd.DataFrame({"Price": PRICES, "Units": UNITS,
                          "Revenue": [p * u for p, u in zip(PRICES, UNITS)]})

# Outlier dataset: 8 normal rows + 1 extreme high + 1 extreme low
VALUES = [10, 12, 11, 13, 10, 12, 11, 13, 1000, -500]
ANOMALY_DF = pd.DataFrame({"Score": VALUES, "Label": list("ABCDEFGHIJ")})

SMALL_DF = pd.DataFrame({"Revenue": [100, 200, 150]})  # only 3 rows

# =========================================================================
# AN-a  Forecasts include a confidence range
# =========================================================================
print("AN-a  Forecast includes confidence range")

out, _, notes, _ = execute_multi(
    {"t": REV_DF}, "t",
    [{"action": "forecast", "columns": ["Revenue"], "count": 3}],
)

# Output table has original rows + 3 forecast rows
check("AN-a forecast adds 3 rows", len(out) == len(REV_DF) + 3, f"rows={len(out)}")

# Required columns present
for col_suffix in ("Revenue_Forecast", "Revenue_Lower95", "Revenue_Upper95"):
    check(f"AN-a column '{col_suffix}' exists", col_suffix in out.columns, str(out.columns.tolist()))

# Confidence interval is ordered: Lower ≤ Forecast ≤ Upper
forecast_rows = out.tail(3)
lower_lt_hat = (forecast_rows["Revenue_Lower95"] <= forecast_rows["Revenue_Forecast"]).all()
hat_lt_upper = (forecast_rows["Revenue_Forecast"] <= forecast_rows["Revenue_Upper95"]).all()
check("AN-a Lower95 ≤ Forecast", bool(lower_lt_hat), str(forecast_rows[["Revenue_Lower95","Revenue_Forecast","Revenue_Upper95"]]))
check("AN-a Forecast ≤ Upper95", bool(hat_lt_upper), str(forecast_rows[["Revenue_Lower95","Revenue_Forecast","Revenue_Upper95"]]))

# Forecast is plausible (linear trend from 100–200: next values should be ~200–230)
first_hat = float(forecast_rows["Revenue_Forecast"].iloc[0])
check("AN-a forecast is plausible (continues upward trend)", first_hat > 190, f"first_forecast={first_hat:.2f}")

# Note mentions CI
check("AN-a note mentions 95 % CI", "95" in notes[0] or "CI" in notes[0], notes[0])

# AN-a.2  Confidence interval widens for further-out predictions
ci_widths = (forecast_rows["Revenue_Upper95"] - forecast_rows["Revenue_Lower95"]).values
check("AN-a confidence interval widens for further periods",
      float(ci_widths[2]) >= float(ci_widths[0]),
      f"widths={[round(float(w),2) for w in ci_widths]}")

# AN-a.3  With a date column
DATE_DF = pd.DataFrame({
    "Date": pd.date_range("2024-01", periods=10, freq="MS").astype(str),
    "Revenue": REVENUE,
})
out_date, _, notes_date, _ = execute_multi(
    {"t": DATE_DF}, "t",
    [{"action": "forecast", "columns": ["Revenue"], "date_column": "Date", "count": 2}],
)
check("AN-a.date forecast with date column adds 2 rows", len(out_date) == 12, f"rows={len(out_date)}")
check("AN-a.date forecast rows have future date labels",
      pd.notna(out_date["Date"].iloc[-1]), str(out_date["Date"].tail(3).tolist()))

# AN-a.4  Goodness-of-fit: a clean linear series reports a high R² and no caveat;
#         pure noise reports a low R² and an honest "weak trend" caveat.
LINEAR_DF = pd.DataFrame({"Y": [10, 20, 30, 40, 50, 60, 70]})  # perfect line
out_lin, _, notes_lin, _ = execute_multi(
    {"t": LINEAR_DF}, "t", [{"action": "forecast", "columns": ["Y"], "count": 2}])
check("AN-a.4 clean trend reports high R²",
      any(s in notes_lin[0] for s in ("R²=1.0", "R²=1.00", "R²=0.99")), notes_lin[0])
check("AN-a.4 clean trend has NO weak-trend caveat", "Caution" not in notes_lin[0], notes_lin[0])
first_fc = float(out_lin["Y_Forecast"].dropna().iloc[0])
check("AN-a.4 clean line forecast ≈ 80", abs(first_fc - 80) < 1e-6, f"first_forecast={first_fc}")

NOISE_DF = pd.DataFrame({"Y": [50, 10, 80, 20, 65, 15, 70, 25]})  # no linear trend
out_noise, _, notes_noise, _ = execute_multi(
    {"t": NOISE_DF}, "t", [{"action": "forecast", "columns": ["Y"], "count": 3}])
check("AN-a.4 noisy data still includes a confidence range",
      "Y_Lower95" in out_noise.columns and "Y_Upper95" in out_noise.columns,
      str(out_noise.columns.tolist()))
check("AN-a.4 noisy data flagged with weak-trend caution",
      "Caution" in notes_noise[0] and "R²" in notes_noise[0], notes_noise[0])


# =========================================================================
# AN-b  What-if math is correct
# =========================================================================
print("\nAN-b  What-if math is correct")

# Scenario: price increases by 10%
out2, _, notes2, _ = execute_multi(
    {"t": SALES_DF}, "t",
    [{"action": "what_if",
      "column": "Price",
      "formula": "{Price} * 1.1",
      "name": "Price (Scenario)"}],
)

check("AN-b scenario column added", "Price (Scenario)" in out2.columns, str(out2.columns.tolist()))

# Each scenario value is exactly original * 1.1
expected = [round(p * 1.1, 10) for p in PRICES]
got      = [round(float(v), 10) for v in out2["Price (Scenario)"]]
check("AN-b scenario values are exactly original × 1.1",
      all(math.isclose(e, g, rel_tol=1e-9) for e, g in zip(expected, got)),
      f"expected={expected} got={got}")

# Note includes direction and magnitude
check("AN-b note summarises impact", any(w in notes2[0].lower() for w in ("scenario", "what-if", "total", "%")),
      notes2[0])

# AN-b.2  Additive change
out3, _, _, _ = execute_multi(
    {"t": SALES_DF}, "t",
    [{"action": "what_if",
      "column": "Price",
      "formula": "{Price} + 2",
      "name": "Price +2"}],
)
expected3 = [p + 2 for p in PRICES]
got3 = [float(v) for v in out3["Price +2"]]
check("AN-b.2 additive scenario correct",
      all(math.isclose(e, g, rel_tol=1e-9) for e, g in zip(expected3, got3)),
      f"expected={expected3} got={got3}")

# AN-b.3  Missing column raises error
try:
    execute_multi({"t": SALES_DF}, "t", [{"action": "what_if", "column": "Ghost", "formula": "{Ghost}*2", "name": "X"}])
    check("AN-b.3 missing column raises OperationError", False, "no error")
except OperationError as e:
    check("AN-b.3 missing column raises OperationError", "Ghost" in str(e) or "column" in str(e).lower(), str(e))


# =========================================================================
# AN-c  Anomalies flagged are genuinely unusual
# =========================================================================
print("\nAN-c  Anomaly detection")

out4, _, notes4, _ = execute_multi(
    {"t": ANOMALY_DF}, "t",
    [{"action": "detect_anomalies", "columns": ["Score"], "anomaly_method": "zscore", "anomaly_threshold": 2.5}],
)

check("AN-c Is_Anomaly column added", "Is_Anomaly" in out4.columns, str(out4.columns.tolist()))
check("AN-c Anomaly_Note column added", "Anomaly_Note" in out4.columns, str(out4.columns.tolist()))

# The extreme high (1000) and low (-500) should be flagged
extreme_high_idx = ANOMALY_DF["Score"].idxmax()
extreme_low_idx  = ANOMALY_DF["Score"].idxmin()
check("AN-c extreme HIGH value (1000) is flagged",
      bool(out4.at[extreme_high_idx, "Is_Anomaly"]),
      str(out4[["Score","Is_Anomaly","Anomaly_Note"]].iloc[extreme_high_idx]))
check("AN-c extreme LOW value (-500) is flagged",
      bool(out4.at[extreme_low_idx, "Is_Anomaly"]),
      str(out4[["Score","Is_Anomaly","Anomaly_Note"]].iloc[extreme_low_idx]))

# Normal values (10–13) should NOT be flagged
normal_mask = out4["Score"].between(10, 13)
check("AN-c normal values (10–13) are NOT flagged",
      not out4.loc[normal_mask, "Is_Anomaly"].any(),
      str(out4.loc[normal_mask, ["Score","Is_Anomaly"]]))

# Anomaly_Note explains WHY
check("AN-c Anomaly_Note mentions HIGH for outlier",
      "HIGH" in (out4.at[extreme_high_idx, "Anomaly_Note"] or ""),
      str(out4.at[extreme_high_idx, "Anomaly_Note"]))

# Note includes count of flagged rows
n_flagged = int(out4["Is_Anomaly"].sum())
check("AN-c at least 2 rows flagged (high + low)", n_flagged >= 2, f"flagged={n_flagged}")
check("AN-c note mentions flagged count", str(n_flagged) in notes4[0], notes4[0])

# AN-c.2  IQR method
out5, _, notes5, _ = execute_multi(
    {"t": ANOMALY_DF}, "t",
    [{"action": "detect_anomalies", "columns": ["Score"], "anomaly_method": "iqr", "anomaly_threshold": 1.5}],
)
check("AN-c.2 IQR method also flags the extreme values",
      bool(out5.at[extreme_high_idx, "Is_Anomaly"]) and bool(out5.at[extreme_low_idx, "Is_Anomaly"]),
      f"high={out5.at[extreme_high_idx,'Is_Anomaly']} low={out5.at[extreme_low_idx,'Is_Anomaly']}")

# AN-c.3  All-constant column: no anomalies (no crash)
CONST_DF = pd.DataFrame({"Value": [5, 5, 5, 5, 5], "Label": list("ABCDE")})
out6, _, notes6, _ = execute_multi(
    {"t": CONST_DF}, "t",
    [{"action": "detect_anomalies", "columns": ["Value"]}],
)
check("AN-c.3 constant column: no anomalies (no crash)",
      not out6["Is_Anomaly"].any(), str(out6[["Value","Is_Anomaly"]].to_dict()))


# =========================================================================
# AN-d  Insufficient data declined honestly
# =========================================================================
print("\nAN-d  Insufficient data declined honestly")

# Forecast with < 5 rows
try:
    execute_multi({"t": SMALL_DF}, "t",
                  [{"action": "forecast", "columns": ["Revenue"], "count": 3}])
    check("AN-d forecast < 5 rows raises OperationError", False, "no error raised")
except OperationError as e:
    msg = str(e)
    check("AN-d forecast raises OperationError", True)
    check("AN-d forecast message mentions minimum rows", "5" in msg or "data point" in msg.lower(), msg)
    check("AN-d forecast message is friendly (no traceback language)", "traceback" not in msg.lower(), msg)

# Anomaly detection with < 5 rows
try:
    execute_multi({"t": SMALL_DF}, "t",
                  [{"action": "detect_anomalies", "columns": ["Revenue"]}])
    check("AN-d anomaly < 5 rows raises OperationError", False, "no error raised")
except OperationError as e:
    msg = str(e)
    check("AN-d anomaly raises OperationError", True)
    check("AN-d anomaly message mentions minimum rows", "5" in msg or "row" in msg.lower(), msg)

# Forecast with a non-numeric column
TEXT_DF = pd.DataFrame({"Month": list(range(1, 8)), "Notes": ["a","b","c","d","e","f","g"]})
try:
    execute_multi({"t": TEXT_DF}, "t",
                  [{"action": "forecast", "columns": ["Notes"], "count": 3}])
    check("AN-d forecast text column raises OperationError", False, "no error raised")
except OperationError as e:
    check("AN-d forecast text column raises OperationError", True)
    check("AN-d message tells user the column has no numeric data",
          any(w in str(e).lower() for w in ("numeric", "non-blank", "blank")), str(e))


# =========================================================================
# AN-e  End-to-end through the API
# =========================================================================
print("\nAN-e  API integration")

CSV_10 = b"Month,Revenue\n1,100\n2,120\n3,115\n4,135\n5,150\n6,140\n7,170\n8,160\n9,180\n10,200\n"

# --- forecast ---
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [{"action": "forecast", "columns": ["Revenue"], "count": 3}],
        "title": "Forecast revenue",
        "translation": "Forecast Revenue 3 periods ahead",
        "confidence": 90,
        "steps": [{"label": "Forecast Revenue for 3 periods"}],
    }
    r = client.post("/process",
                    data={"instruction": "forecast next 3 months", "session_id": "an_fc", "rewind": "-1", "history": ""},
                    files=[("files", ("r.csv", CSV_10, "text/csv"))])
    body = r.json()
    check("AN-e forecast API ok", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
    check("AN-e forecast API not partial", not body.get("partial"), str(body.get("partial")))
    check("AN-e forecast API row_count = 13", body.get("row_count") == 13, f"row_count={body.get('row_count')}")
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# --- what_if ---
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [{"action": "what_if", "column": "Revenue", "formula": "{Revenue} * 1.2", "name": "Revenue (Scenario)"}],
        "title": "What-if 20% uplift",
        "translation": "Apply 20% uplift scenario to Revenue",
        "confidence": 95,
        "steps": [{"label": "Add Revenue (Scenario) = Revenue × 1.2"}],
    }
    r = client.post("/process",
                    data={"instruction": "what if revenue increases 20%", "session_id": "an_wi", "rewind": "-1", "history": ""},
                    files=[("files", ("r.csv", CSV_10, "text/csv"))])
    body = r.json()
    check("AN-e what_if API ok", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
    check("AN-e what_if same row count", body.get("row_count") == 10, f"row_count={body.get('row_count')}")
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

# --- detect_anomalies ---
ANOMALY_CSV = b"Score\n10\n12\n11\n13\n10\n12\n11\n13\n1000\n-500\n"
try:
    main.llm.parse_instruction = lambda i, s, h: {
        "operations": [{"action": "detect_anomalies", "columns": ["Score"]}],
        "title": "Flag anomalies",
        "translation": "Detect anomalies in Score column",
        "confidence": 88,
        "steps": [{"label": "Flag anomalous Score values", "rationale": "Audit for outliers using z-score method."}],
    }
    r = client.post("/process",
                    data={"instruction": "flag anomalies in Score", "session_id": "an_ad", "rewind": "-1", "history": ""},
                    files=[("files", ("a.csv", ANOMALY_CSV, "text/csv"))])
    body = r.json()
    check("AN-e anomaly API ok", r.status_code == 200 and body.get("status") == "ok", str(body)[:200])
    check("AN-e anomaly API note mentions flagged rows", any(w in (body.get("explanation","")).lower() for w in ("flag","anomal","unusual")), body.get("explanation",""))
finally:
    main.llm.parse_instruction = _orig_parse
    main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
