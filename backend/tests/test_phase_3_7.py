"""ENGINE PHASE 3.7 — continuous learning & personalization (verify & HARDEN).

The mechanism already existed (app/personalization.py + /memory endpoints, covered by
backend/test_personalization.py). This suite HARDENS the DoD's three promises against the
gaps that suite didn't reach:

  DoD "custom term ('our ARR') applied consistently"
    - a MULTI-WORD / possessive term ("our ARR") is injected into the glossary AND
      deterministically expanded offline — not just single tokens like "ARR".
    - CONSISTENCY: the same instruction expands identically on every call.
    - a term that is only a SUBSTRING of another word ("arrears") is never falsely
      expanded (word-boundary honesty — a wrong-but-confident expansion is the bad case).

  DoD "preferred formats reused"
    - already exercised for apply/context by test_personalization.py; here we add the
      MISSING management half:

  DoD "user can view / edit / DELETE memory"
    - preferences were the one memory type with no delete endpoint. Phase 3.7 adds
      POST /memory/preferences/delete — proven here to remove one preference while
      leaving the others (view/edit/delete symmetry with definitions & templates).

  "real memory — survives restarts"
    - a persistence ROUND-TRIP: write to disk, drop the in-memory copy, reload, and the
      learning is back.

No llm.py schema change (memory is context injection + plan post-processing), so there is
no serving/quota risk — this suite is fully offline.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_3_7.py
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

_fd, _db = tempfile.mkstemp(suffix="-p37.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import personalization as P  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
# Keep tests off the real memory file + isolated.
P._PERSIST = False
P._MEMORY.clear()
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


print("ENGINE PHASE 3.7 — personalization (verify & harden)\n")

# ============ custom term "our ARR" — multi-word, consistent, boundary-safe ============
TEAM = "harden"
P._MEMORY.clear()
P.set_definition(TEAM, "our ARR", "Annual Recurring Revenue", "{MRR} * 12")

ctx = P.context(TEAM)
check("multi-word term appears in the injected glossary", "our ARR" in ctx and "{MRR} * 12" in ctx, ctx)

exp1 = P.expand_definitions("add our ARR column", TEAM)
check("multi-word 'our ARR' expands offline to the team formula",
      exp1 == [{"action": "add_formula_column", "name": "our ARR", "formula": "{MRR} * 12"}], str(exp1))

# CONSISTENCY: identical expansion on a repeat call (same learned behavior every time).
exp2 = P.expand_definitions("add our ARR column", TEAM)
check("expansion is consistent across calls", exp1 == exp2, f"{exp1} vs {exp2}")

# BOUNDARY HONESTY: define a short term, ensure it doesn't fire inside a bigger word.
P._MEMORY.clear()
P.set_definition(TEAM, "ARR", "Annual Recurring Revenue", "{MRR} * 12")
check("term is NOT expanded inside another word ('calculate arrears')",
      P.expand_definitions("calculate arrears", TEAM) is None, str(P.expand_definitions("calculate arrears", TEAM)))
check("the real term still expands ('add ARR')",
      P.expand_definitions("add ARR", TEAM) == [{"action": "add_formula_column", "name": "ARR", "formula": "{MRR} * 12"}], "")

# TWO defined terms in one instruction both expand.
P.set_definition(TEAM, "GM", "Gross Margin", "{Revenue} - {COGS}")
both = P.expand_definitions("add ARR and GM", TEAM)
check("multiple defined terms in one instruction both expand",
      both is not None and {o["name"] for o in both} == {"ARR", "GM"}, str(both))

# ============ preferences: the NEW delete endpoint completes view/edit/delete ============
print()
P._MEMORY.clear()
c.post("/memory/preferences", data={"team_id": "web", "currency_symbol": "₹", "date_format": "dd-mm-yyyy", "decimals": "2", "bold_header": "true"})
mem = c.get("/memory", params={"team_id": "web"}).json()["memory"]["preferences"]
check("preferences stored via API (view)", mem.get("currency_symbol") == "₹" and mem.get("decimals") == 2, str(mem))

# delete ONE preference — others must remain
r = c.post("/memory/preferences/delete", data={"team_id": "web", "key": "currency_symbol"}).json()
check("preference-delete endpoint reports removed=True", r.get("removed") is True, str(r)[:160])
prefs = r["memory"]["preferences"]
check("deleted preference is gone", "currency_symbol" not in prefs, str(prefs))
check("other preferences survive a single-key delete",
      prefs.get("date_format") == "dd-mm-yyyy" and prefs.get("decimals") == 2 and prefs.get("bold_header") is True, str(prefs))

# deleting a preference that isn't set is a safe no-op (removed=False)
r = c.post("/memory/preferences/delete", data={"team_id": "web", "key": "currency_symbol"}).json()
check("re-deleting a missing preference is a safe no-op (removed=False)", r.get("removed") is False, str(r)[:120])

# symmetry: definitions & templates already have delete; now all three do.
c.post("/memory/definition", data={"team_id": "web", "term": "ARR", "definition": "x", "formula": "{MRR}*12"})
c.post("/memory/template", data={"team_id": "web", "name": "clean", "operations": json.dumps([{"action": "remove_duplicates"}])})
d1 = c.post("/memory/definition/delete", data={"team_id": "web", "term": "ARR"}).json()
d2 = c.post("/memory/template/delete", data={"team_id": "web", "name": "clean"}).json()
check("all three memory types are deletable via API (view/edit/delete complete)",
      d1.get("removed") is True and d2.get("removed") is True, f"{d1.get('removed')},{d2.get('removed')}")

# ============ real memory: persistence round-trip (survives 'restart') ============
print()
tmp_dir = tempfile.mkdtemp()
tmp_path = Path(tmp_dir) / "memory.json"
_saved_persist, _saved_path = P._PERSIST, P._MEMORY_PATH
try:
    P._PERSIST = True
    P._MEMORY_PATH = tmp_path
    P._MEMORY.clear()
    P.set_definition("acme", "ARR", "Annual Recurring Revenue", "{MRR} * 12")
    P.set_preferences("acme", currency_symbol="₹", decimals=2)
    check("memory file was written to disk", tmp_path.exists(), str(tmp_path))
    # Simulate a restart: drop the in-memory copy, reload from disk.
    P._MEMORY.clear()
    check("in-memory copy is empty after clear", P.definitions("acme") == {}, "")
    P._load()
    check("definitions survive a reload (restart)", P.definitions("acme").get("ARR", {}).get("formula") == "{MRR} * 12", str(P.definitions("acme")))
    check("preferences survive a reload (restart)", P.preferences("acme").get("currency_symbol") == "₹", str(P.preferences("acme")))
finally:
    P._PERSIST, P._MEMORY_PATH = _saved_persist, _saved_path
    P._MEMORY.clear()
    try:
        tmp_path.unlink()
        os.rmdir(tmp_dir)
    except Exception:
        pass

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
