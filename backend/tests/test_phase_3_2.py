"""ENGINE PHASE 3.2 — version history + undo/redo (labeled history + compare versions).

Undo/redo mechanics already work (see test_undo_redo). This phase adds the LABELED
history (every step tagged with what it did) and COMPARE VERSIONS (what changed between
two versions, via the 2.9 compare engine). No LLM — hand-written plans through the API.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_3_2.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

_fd, _db = tempfile.mkstemp(suffix="-p32.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
c = TestClient(m.app)
passed = failed = 0
CSV = b"Name,Revenue\na,50\nb,150\nc,200\nd,80\ne,300\n"  # 5 rows


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def ex(sid, plan):
    return c.post("/execute", data={"session_id": sid, "plan": json.dumps(plan)}).json()


print("ENGINE PHASE 3.2 — labeled history + compare versions (no AI)\n")

c.post("/inspect", data={"session_id": "h"}, files={"files": ("s.csv", CSV, "text/csv")})
ex("h", {"operations": [{"action": "filter", "conditions": [
    {"column": "Revenue", "operator": "greater_than", "value": "100"}]}]})  # → 3 rows
ex("h", {"operations": [{"action": "sort", "columns": ["Revenue"], "orders": ["desc"]}]})

# ---- (a) labeled history ----
h = c.post("/history", data={"session_id": "h"}).json()
vs = h["versions"]
check("history: one version per step + the upload (3 versions)", len(vs) == 3, str(len(vs)))
check("history: step 0 is labeled 'Uploaded'", vs[0]["label"] == "Uploaded", str(vs[0]))
check("history: the filter step is labeled from its note (mentions Revenue/rows)",
      "Revenue" in vs[1]["label"] or "row" in vs[1]["label"].lower(), str(vs[1]))
check("history: the sort step is labeled 'Sorted…'", "Sorted" in vs[2]["label"], str(vs[2]))
check("history: row counts tracked (5 → 3 → 3)",
      [v["row_count"] for v in vs] == [5, 3, 3], str([v["row_count"] for v in vs]))
check("history: the last version is marked current",
      vs[-1]["current"] is True and h["current_index"] == 2 and not vs[0]["current"], str(h))
check("history: can_undo true, can_redo false at the tip", h["can_undo"] and not h["can_redo"], str(h))

# ---- (b) undo returns the label of where we land ----
u = c.post("/undo", data={"session_id": "h"}).json()
check("undo returns the landed version's label", "Revenue" in u["label"] or "row" in u["label"].lower(), str(u))
r = c.post("/redo", data={"session_id": "h"}).json()
check("redo returns the landed version's label", "Sorted" in r["label"], str(r))

# ---- (c) compare versions: what changed between the upload and now ----
# a step that actually changes rows so the diff is non-trivial
c.post("/inspect", data={"session_id": "cmp"}, files={"files": ("s.csv", CSV, "text/csv")})
ex("cmp", {"operations": [{"action": "filter", "conditions": [
    {"column": "Revenue", "operator": "greater_than", "value": "100"}]}]})  # 5 → 3 rows
d = c.post("/compare-versions", data={"session_id": "cmp", "from_index": 0, "to_index": -1}).json()
check("compare-versions: ok", d.get("status") == "ok", str(d)[:150])
check("compare-versions: labels the two sides (Uploaded → filtered)",
      d["from"]["label"] == "Uploaded" and ("Revenue" in d["to"]["label"] or "row" in d["to"]["label"].lower()),
      str(d.get("from")) + str(d.get("to")))
check("compare-versions: reports removed rows (5→3 means 2 removed)",
      "removed row" in d["note"], d["note"])
check("compare-versions: differences are a real diff table",
      isinstance(d["differences"], list) and any(r.get("Change") == "Row removed" for r in d["differences"]),
      str(d["differences"])[:200])

# defaults (from 0, to current) work with no indices given
d2 = c.post("/compare-versions", data={"session_id": "cmp"}).json()
check("compare-versions: defaults to upload vs current", d2.get("status") == "ok" and d2["to"]["index"] == 1, str(d2)[:120])

# ---- (d) failures ----
check("history on a fresh/unknown session errors",
      c.post("/history", data={"session_id": "nope"}).status_code == 400)
check("compare-versions with only one version (no steps) errors",
      (lambda: (c.post("/inspect", data={"session_id": "one"}, files={"files": ("s.csv", CSV, "text/csv")}),
                c.post("/compare-versions", data={"session_id": "one", "from_index": 0, "to_index": 1}).status_code)[1])() == 400)
check("compare-versions same index errors",
      c.post("/compare-versions", data={"session_id": "cmp", "from_index": 0, "to_index": 0}).status_code == 400)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
