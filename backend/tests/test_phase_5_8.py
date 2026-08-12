"""ENGINE PHASE 5.8 — performance & scale layer (BUILD).

Two honest big-data levers for a single-process engine:

  RESULT CACHE   the same plan on the same data → the same output, so memoize it. A repeat
                 run is a cache HIT that skips the recompute. Keyed by a cheap, stable
                 fingerprint (shape + columns + a bounded row sample — never a full scan).
  SAMPLING       a huge sheet is previewed via an evenly-spaced SAMPLE (flagged), not by
                 scanning every row.

(True multi-node "distributed processing" is infra, out of this engine's scope — documented
in scale.py; caching + sampling are what the engine can honestly deliver.)

Wiring: /execute memoizes via the cache (response carries `cached`); /scale/preview samples;
/scale/stats reports hit-rate. No llm.py change → no schema/serving/quota risk; no battery
rows (endpoint/mechanism).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_8.py
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

_fd, _db = tempfile.mkstemp(suffix="-p58.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import scale  # noqa: E402
from app.db import init_db  # noqa: E402
from app.executor import execute_multi  # noqa: E402

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


print("ENGINE PHASE 5.8 — performance & scale\n")

DF = pd.DataFrame({"A": [3, 1, 2], "B": [9, 8, 7]})
SORT = [{"action": "sort", "columns": ["A"], "orders": ["asc"]}]

# ===================== fingerprint + signature =====================
fp = scale.fingerprint({"t": DF})
check("fingerprint is stable for identical data", fp == scale.fingerprint({"t": DF.copy()}), "")
check("fingerprint changes when the data changes", fp != scale.fingerprint({"t": pd.DataFrame({"A": [3, 1, 9], "B": [9, 8, 7]})}), "")
check("fingerprint changes when columns change", fp != scale.fingerprint({"t": DF.rename(columns={"B": "C"})}), "")
sig = scale.plan_signature({"t": DF}, SORT)
check("plan_signature is stable for same data+plan", sig == scale.plan_signature({"t": DF.copy()}, SORT), "")
check("plan_signature differs for a different plan", sig != scale.plan_signature({"t": DF}, [{"action": "remove_duplicates"}]), "")

# ===================== ResultCache (LRU + stats) =====================
cache = scale.ResultCache(max_entries=2)
cache.put("a", 1); cache.put("b", 2)
check("cache returns stored values + counts a hit", cache.get("a") == 1 and cache.stats()["hits"] == 1, str(cache.stats()))
cache.put("c", 3)  # evicts the least-recently-used ("b", since "a" was just touched)
check("LRU evicts the least-recently-used entry", cache.get("b") is None and cache.get("a") == 1 and cache.get("c") == 3, str(list(cache._d)))
check("cache reports a miss + hit-rate", cache.stats()["misses"] >= 1 and 0 <= cache.stats()["hit_rate"] <= 1, str(cache.stats()))

# ===================== run_cached: miss then hit, runner skipped on hit =====================
calls = {"n": 0}


def spy_runner(tables, primary, ops):
    calls["n"] += 1
    return execute_multi(tables, primary, ops)


cc = scale.ResultCache()
r1, hit1 = scale.run_cached({"t": DF}, "t", SORT, spy_runner, cc)
r2, hit2 = scale.run_cached({"t": DF.copy()}, "t", SORT, spy_runner, cc)
check("run_cached: first call is a miss, second is a hit", hit1 is False and hit2 is True, f"{hit1},{hit2}")
check("run_cached: the runner ran only ONCE (recompute skipped on hit)", calls["n"] == 1, str(calls["n"]))
check("run_cached: cached result equals the computed one", list(r1[0]["A"]) == list(r2[0]["A"]) == [1, 2, 3], str(list(r2[0]["A"])))

# ===================== sampling =====================
big = pd.DataFrame({"X": range(5000), "Y": range(5000)})
sampled, was = scale.sample_tables({"big": big, "small": DF}, max_rows=1000)
check("sampling caps an oversized table at max_rows", len(sampled["big"]) == 1000 and was is True, str(len(sampled["big"])))
check("sampling leaves a small table untouched", sampled["small"].equals(DF), "")
check("sampling is evenly-spaced (not just the head)", sampled["big"]["X"].iloc[-1] > 1000, str(sampled["big"]["X"].iloc[-1]))
check("is_large / total_rows", scale.total_rows({"big": big}) == 5000 and scale.is_large({"z": pd.DataFrame({'a': range(150000)})}) and not scale.is_large({"t": DF}), "")

# ===================== END TO END: /execute caching across sessions =====================
scale.RESULT_CACHE.clear()
CSV = b"A,B\n3,9\n1,8\n2,7\n"
plan = json.dumps({"operations": SORT})
c.post("/inspect", data={"session_id": "s1"}, files=[("files", ("d.csv", CSV, "text/csv"))])
r_a = c.post("/execute", data={"session_id": "s1", "plan": plan}).json()
check("/execute first run is NOT cached", r_a.get("status") == "ok" and r_a.get("cached") is False, str(r_a)[:160])
# a second, identical session (same data + plan) → the compute is memoized → cache HIT
c.post("/inspect", data={"session_id": "s2"}, files=[("files", ("d.csv", CSV, "text/csv"))])
r_b = c.post("/execute", data={"session_id": "s2", "plan": plan}).json()
check("/execute identical data+plan hits the cache (recompute skipped)", r_b.get("cached") is True, str(r_b)[:160])
check("/execute cached result is correct (same row_count)", r_b.get("row_count") == r_a.get("row_count") == 3, str((r_a.get("row_count"), r_b.get("row_count"))))
# a DIFFERENT plan is a miss
r_c = c.post("/execute", data={"session_id": "s2", "plan": json.dumps({"operations": [{"action": "remove_duplicates"}]})}).json()
check("/execute a different plan is a cache miss", r_c.get("cached") is False, str(r_c)[:120])

stats = c.get("/scale/stats").json()["cache"]
check("/scale/stats reports at least one hit", stats["hits"] >= 1 and stats["entries"] >= 1, str(stats))

# ===================== END TO END: /scale/preview sampling =====================
rows = "\n".join(f"{i},{i*2}" for i in range(3000))
BIG_CSV = ("N,M\n" + rows + "\n").encode()
c.post("/inspect", data={"session_id": "big"}, files=[("files", ("big.csv", BIG_CSV, "text/csv"))])
pv = c.post("/scale/preview", data={"session_id": "big", "sample_rows": "500"}).json()
check("/scale/preview samples a large sheet", pv.get("sampled") is True and pv.get("total_rows") == 3000 and pv.get("sample_rows") == 500, str(pv)[:200])
# a small sheet is NOT sampled
c.post("/inspect", data={"session_id": "sm"}, files=[("files", ("d.csv", CSV, "text/csv"))])
pv2 = c.post("/scale/preview", data={"session_id": "sm", "sample_rows": "500"}).json()
check("/scale/preview does not sample a small sheet", pv2.get("sampled") is False and pv2.get("total_rows") == 3, str(pv2)[:160])

m._SESSIONS.clear()
scale.RESULT_CACHE.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
