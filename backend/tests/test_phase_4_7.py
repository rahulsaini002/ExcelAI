"""ENGINE PHASE 4.7 — data quality monitoring (verify & HARDEN).

The observability engine pre-existed (app/quality.py + /quality/snapshot + /quality/check,
covered by backend/test_quality.py 29/29): schema drift, missing-data spikes, staleness,
with false-alarm gating (added cols = info; reordering = no diff; spikes double-gated).
This suite HARDENS the DoD's "LOW false alarms" and completes the monitoring loop:

  HARDEN 1 — false alarm removed: integer → number (a column merely gaining a decimal,
    e.g. 10 → 10.5 on refresh) no longer fires a schema-drift alarm. A REAL break
    (number → text) still does. Fix is local to quality diffing (_norm_type); the Brain's
    structure summary keeps its granular integer-vs-number dtype.

  HARDEN 2 — re-baseline: /quality/snapshot?set_baseline=true makes the current data the
    new baseline. Without it, once the user deliberately changes the schema, /quality/check
    would alarm forever against the original upload — training people to ignore alarms, the
    opposite of "low false alarms". Re-baselining resets the schema/blank reference only,
    NOT the freshness clock.

  VERIFY — in-session drift: /quality/check with NO file compares the CURRENT working data
    (after the user's own ops) to the baseline, so drift the user themselves caused is
    caught. (The pre-existing suite only tested drift via an uploaded refresh.)

No llm.py change (observability is endpoint-driven, not model routing) → no schema/serving/
quota risk, and — like other endpoint phases (3.2/3.3/3.7) — no battery rows.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_4_7.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p47.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import quality  # noqa: E402
from app.db import init_db  # noqa: E402

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


def prof(df_map):
    return quality.profile(df_map)


print("ENGINE PHASE 4.7 — data quality monitoring (verify & harden)\n")

# ===================== HARDEN 1: integer↔number is NOT schema drift =====================
old = prof({"S": pd.DataFrame({"Amt": [10, 20, 30]})})
new_float = prof({"S": pd.DataFrame({"Amt": [10.0, 20.5, 30.0]})})
d = quality.compare_schema(old, new_float)
check("integer → number (gained a decimal) is NOT a schema change (false alarm removed)",
      d["changed"] is False and not d["columns"], str(d))
# the reverse is equally benign
d = quality.compare_schema(prof({"S": pd.DataFrame({"Amt": [1.0, 2.0]})}),
                           prof({"S": pd.DataFrame({"Amt": [1, 2]})}))
check("number → integer is also NOT a schema change", d["changed"] is False, str(d))
# a REAL break must still fire
d = quality.compare_schema(old, prof({"S": pd.DataFrame({"Amt": ["a", "b", "c"]})}))
tc = d["columns"].get("S", {}).get("type_changed", [])
check("number → text IS still flagged (real break preserved)",
      d["changed"] is True and any(x["column"] == "Amt" for x in tc), str(d))
d = quality.compare_schema(prof({"S": pd.DataFrame({"Amt": ["a", "b"]})}),
                           prof({"S": pd.DataFrame({"Amt": [1, 2]})}))
check("text → number IS still flagged", d["changed"] is True, str(d))
# and it flows through assess(): int→float refresh raises no alarms
report = quality.assess(old, new_float, time.time(), time.time())
check("assess(): integer→number refresh raises NO alarms (ok=True)",
      report["ok"] is True and report["alarms"] == [], str(report["alarms"]))

# ===================== LOW-FALSE-ALARM sanity (reorder / added / wiggle) =====================
reordered = prof({"S": pd.DataFrame({"Amt": [10, 20, 30], "ID": [1, 2, 3]})})
base_two = prof({"S": pd.DataFrame({"ID": [1, 2, 3], "Amt": [10, 20, 30]})})
check("reordered columns raise no schema change", quality.compare_schema(base_two, reordered)["changed"] is False, "")
added = prof({"S": pd.DataFrame({"ID": [1, 2, 3], "Amt": [10, 20, 30], "New": [1, 2, 3]})})
rep_added = quality.assess(base_two, added, time.time(), time.time())
check("an ADDED column is info, not an alarm", rep_added["ok"] is True and any("New" in i for i in rep_added["info"]), str(rep_added))

# ===================== VERIFY + HARDEN 2 over HTTP: in-session drift & re-baseline =====================
CSV = b"ID,Name,Amt\n1,a,10\n2,b,20\n3,c,30\n4,d,40\n5,e,50\n"
c.post("/inspect", data={"session_id": "q"}, files=[("files", ("d.csv", CSV, "text/csv"))])

# The user's OWN op drops a column (no model — /execute runs a given plan).
plan = {"operations": [{"action": "drop_columns", "columns": ["Amt"]}]}
r = c.post("/execute", data={"session_id": "q", "plan": json.dumps(plan)}).json()
check("in-session op (drop Amt) ran", r.get("status") == "ok", str(r)[:120])

# /quality/check with NO file compares current working data to the upload baseline → drift.
chk = c.post("/quality/check", data={"session_id": "q"}).json()
check("in-session drift caught with NO file: 'Amt' reported removed",
      "Amt" in chk.get("schema_changes", {}).get("columns", {}).get("d", {}).get("removed", []), str(chk.get("schema_changes")))
check("in-session drift raises an alarm (ok=False)", chk.get("ok") is False, str(chk.get("alarms")))

# Freshness must NOT be reset by a re-baseline. Capture last_updated first.
snap_a = c.post("/quality/snapshot", data={"session_id": "q"}).json()
last_updated_a = snap_a["last_updated"]

# HARDEN 2: re-baseline to the current (Amt-less) data.
snap_b = c.post("/quality/snapshot", data={"session_id": "q", "set_baseline": "true"}).json()
check("re-baseline reports baseline_set=true", snap_b.get("baseline_set") is True, str(snap_b)[:120])
check("re-baseline does NOT touch 'last updated' (freshness stays honest)",
      snap_b["last_updated"] == last_updated_a, f"{snap_b['last_updated']} vs {last_updated_a}")

# After re-baselining, the SAME drift no longer alarms (current == new baseline).
chk2 = c.post("/quality/check", data={"session_id": "q"}).json()
check("after re-baseline, the prior drift no longer alarms (ok=True)",
      chk2.get("ok") is True and chk2.get("alarms") == [], str(chk2.get("alarms")))

# But a NEW change after re-baselining IS caught (re-baseline didn't blind the monitor).
plan2 = {"operations": [{"action": "drop_columns", "columns": ["Name"]}]}
c.post("/execute", data={"session_id": "q", "plan": json.dumps(plan2)}).json()
chk3 = c.post("/quality/check", data={"session_id": "q"}).json()
check("a NEW drift after re-baseline is still caught ('Name' removed)",
      "Name" in chk3.get("schema_changes", {}).get("columns", {}).get("d", {}).get("removed", []), str(chk3.get("schema_changes")))

# default set_baseline is false (a plain snapshot must not silently move the baseline)
snap_default = c.post("/quality/snapshot", data={"session_id": "q"}).json()
check("plain snapshot does NOT set the baseline (baseline_set=false)", snap_default.get("baseline_set") is False, str(snap_default)[:120])

# ===================== int→float over HTTP raises no schema alarm =====================
c.post("/inspect", data={"session_id": "q2"}, files=[("files", ("d.csv", CSV, "text/csv"))])
# refresh: Amt becomes float (one value gained a decimal), everything else identical.
CSV_FLOAT = b"ID,Name,Amt\n1,a,10.0\n2,b,20.5\n3,c,30.0\n4,d,40.0\n5,e,50.0\n"
chk_f = c.post("/quality/check", data={"session_id": "q2"}, files=[("files", ("d.csv", CSV_FLOAT, "text/csv"))]).json()
sc = chk_f.get("schema_changes", {}).get("columns", {})
check("HTTP refresh int→float raises no schema-type alarm (low false alarms)",
      not any(tc for t in sc.values() for tc in t.get("type_changed", [])), str(sc))

m._SESSIONS.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
