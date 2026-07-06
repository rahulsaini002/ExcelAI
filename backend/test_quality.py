"""Phase 3.11 — Data quality & observability tests.

PRD criteria:
  QL-schema    Schema changes (added/removed/retyped columns) are detected and surfaced;
               column REORDERING is NOT a change.
  QL-updated   "last updated" is accurate; staleness flagged past the threshold.
  QL-noalarm   False alarms minimised — unchanged/reordered data and tiny blank wiggles
               raise nothing; only real spikes/changes do.

Run from backend:  .venv\\Scripts\\python.exe test_quality.py
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

from app import main, quality

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("PHASE 3.11 — DATA QUALITY & OBSERVABILITY\n")

# =========================================================================
# QL-schema  Schema-change detection (+ reorder is not a change)
# =========================================================================
print("QL-schema  Schema-change detection")

OLD = {"S": pd.DataFrame({"ID": [1, 2, 3], "Name": ["a", "b", "c"], "Amt": [10, 20, 30]})}
# Amt removed, City added, ID retyped (int → text), columns reordered.
NEW = {"S": pd.DataFrame({"Name": ["a", "b", "c"], "ID": ["1", "2", "x"], "City": ["X", "Y", "Z"]})}

op, npf = quality.profile(OLD), quality.profile(NEW)
diff = quality.compare_schema(op, npf)
check("QL-schema detects removed column", "Amt" in diff["columns"]["S"]["removed"], str(diff["columns"]))
check("QL-schema detects added column", "City" in diff["columns"]["S"]["added"], str(diff["columns"]))
tc = diff["columns"]["S"]["type_changed"]
check("QL-schema detects type change on ID", any(t["column"] == "ID" for t in tc), str(tc))
check("QL-schema marks changed=True", diff["changed"] is True, str(diff["changed"]))

# Reorder only → NOT a change
REORDER = {"S": pd.DataFrame({"Amt": [10, 20, 30], "ID": [1, 2, 3], "Name": ["a", "b", "c"]})}
diff2 = quality.compare_schema(op, quality.profile(REORDER))
check("QL-schema reorder is NOT a change", diff2["changed"] is False, str(diff2))

# Table add/remove
diff3 = quality.compare_schema({"A": op["S"]}, {"A": op["S"], "B": op["S"]})
# (profiles keyed by table; build directly)
prof_one = {"A": op["S"]}
prof_two = {"A": op["S"], "B": op["S"]}
d = quality.compare_schema(prof_one, prof_two)
check("QL-schema detects a new table", d["tables_added"] == ["B"], str(d["tables_added"]))

# =========================================================================
# QL-missing  Missing-data spikes (with false-alarm gating)
# =========================================================================
print("\nQL-missing  Missing-data spikes")

base = {"T": pd.DataFrame({"Phone": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
                            "Note": ["x"] * 10})}
# Phone: 6/10 now blank (spike). Note: 1/10 blank (noise — must NOT alarm).
spiked = {"T": pd.DataFrame({"Phone": ["1", "2", "3", "4", "", "", "", "", "", ""],
                             "Note": ["x", "x", "x", "x", "x", "x", "x", "x", "x", ""]})}
spikes = quality.missing_spikes(quality.profile(base), quality.profile(spiked))
cols = {s["column"] for s in spikes}
check("QL-missing flags the real spike (Phone)", "Phone" in cols, str(spikes))
check("QL-missing ignores a small wiggle (Note)", "Note" not in cols, str(spikes))
check("QL-missing reports the from→to rates", any(s["from"] == 0.0 and s["to"] >= 0.5 for s in spikes), str(spikes))

# =========================================================================
# QL-updated  Accurate last-updated + staleness
# =========================================================================
print("\nQL-updated  Last-updated + staleness")

now = 1_000_000.0
fresh = quality.staleness(now - 10, now, max_age_seconds=86400)
check("QL-updated fresh data is not stale", fresh["stale"] is False, str(fresh))
check("QL-updated last_updated is accurate", fresh["last_updated"] == now - 10, str(fresh["last_updated"]))

old = quality.staleness(now - 100000, now, max_age_seconds=86400)
check("QL-updated old data is stale", old["stale"] is True, str(old))
check("QL-updated humanizes age", "day" in old["human"], old["human"])

# =========================================================================
# QL-assess  Combined report — alarms vs info
# =========================================================================
print("\nQL-assess  Combined report")

report = quality.assess(op, npf, updated_at=now - 100000, now=now, max_age_seconds=86400)
msgs = " ".join(a["message"] for a in report["alarms"])
check("QL-assess alarms on removed column", "Amt" in msgs and "removed" in msgs.lower(), msgs)
check("QL-assess alarms on type change", "ID" in msgs and "type" in msgs.lower(), msgs)
check("QL-assess alarms on staleness", any(a["kind"] == "staleness" for a in report["alarms"]), msgs)
check("QL-assess added column is INFO, not an alarm",
      any("City" in i for i in report["info"]) and "City" not in msgs, f"info={report['info']} alarms={msgs}")
check("QL-assess ok=False when there are alarms", report["ok"] is False, str(report["ok"]))

# Identical, fresh data → no alarms (false-alarm minimisation)
clean = quality.assess(op, quality.profile(REORDER), updated_at=now - 10, now=now, max_age_seconds=86400)
check("QL-assess unchanged+fresh data is OK (no false alarms)", clean["ok"] is True and clean["alarms"] == [], str(clean["alarms"]))

# =========================================================================
# API  Over HTTP
# =========================================================================
print("\nAPI  HTTP endpoints")
client = TestClient(main.app)
CSV_OLD = b"ID,Name,Amt\n1,a,10\n2,b,20\n3,c,30\n"
client.post("/inspect", data={"session_id": "ql"}, files=[("files", ("d.csv", CSV_OLD, "text/csv"))])

snap = client.post("/quality/snapshot", data={"session_id": "ql"}).json()
check("API snapshot ok", snap.get("status") == "ok" and "profile" in snap, str(snap)[:120])
check("API last_updated is accurate (≈ now)", abs(time.time() - snap["last_updated"]) < 30, str(snap["last_updated"]))
check("API freshly-uploaded data is not stale", snap["staleness"]["stale"] is False, str(snap["staleness"]))

# Refresh with a changed schema + a blank spike
CSV_NEW = b"Name,ID,City\na,1,X\n,2,Y\n,x,Z\n"  # Name 2/3 blank, ID->text, Amt removed, City added
chk = client.post("/quality/check", data={"session_id": "ql"},
                  files=[("files", ("d.csv", CSV_NEW, "text/csv"))]).json()
check("API check ok", chk.get("status") == "ok", str(chk)[:120])
check("API check surfaces removed column", "Amt" in chk["schema_changes"]["columns"]["d"]["removed"], str(chk["schema_changes"]))
check("API check surfaces a missing-data spike", any(s["column"] == "Name" for s in chk["missing_spikes"]), str(chk["missing_spikes"]))
check("API check raises alarms (ok=False)", chk["ok"] is False, str(chk["alarms"]))

# last_updated moved forward after the refresh
snap2 = client.post("/quality/snapshot", data={"session_id": "ql"}).json()
check("API last_updated bumped by the refresh", snap2["last_updated"] >= snap["last_updated"], "")

# Re-check identical-to-baseline data → no false alarms
chk2 = client.post("/quality/check", data={"session_id": "ql"},
                   files=[("files", ("d.csv", CSV_NEW, "text/csv"))]).json()
check("API unchanged refresh raises NO alarms", chk2["ok"] is True and chk2["alarms"] == [], str(chk2["alarms"]))

# Staleness over HTTP: backdate the session and re-snapshot
main._SESSIONS["ql"]["updated_at"] = time.time() - 100000
stale_snap = client.post("/quality/snapshot", data={"session_id": "ql", "max_age_hours": "24"}).json()
check("API staleness flagged for old data", stale_snap["staleness"]["stale"] is True, str(stale_snap["staleness"]))

main._SESSIONS.clear()

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
