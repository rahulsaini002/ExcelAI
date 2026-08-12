"""ENGINE PHASE 5.2 — shared AI memory (BUILD on 3.7).

Phase 3.7 gave each TEAM its own glossary. Phase 5.2 adds an ORG-shared glossary that many
teams inherit, so terminology ("our ARR", "Runway") is consistent across the whole
organization — with a team's OWN definition overriding the shared one (local wins).

  shared CRUD        set/get/delete org-scoped definitions (own store + file, separate
                     from per-team memory.json).
  inheritance        effective_definitions(team, org) = org base + team override.
  context injection   context(team, scope=org) emits an "Organization glossary" block then
                     the team block; an overridden term appears ONLY in the team block.
  offline expansion   the fallback expands org-shared terms too (works when the model is down).
  backward compatible  with no scope, context()/definitions() are byte-identical to 3.7.
  API + wiring        /memory/shared/* CRUD; /parse & /process take org_id and inject the
                     shared glossary into the parse context.

No llm.py change (context injection, like 3.7) → no schema/serving/quota risk; no battery
rows (glossary injection needs seeded org memory the battery runner doesn't set up).

Run from backend:  .venv\\Scripts\\python.exe tests\\test_phase_5_2.py
"""
from __future__ import annotations

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

_fd, _db = tempfile.mkstemp(suffix="-p52.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import fallback, personalization as P  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
# Keep off disk + isolated.
P._PERSIST = False
P._MEMORY.clear()
P._SHARED.clear()
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


print("ENGINE PHASE 5.2 — shared AI memory (org-wide glossary)\n")

ORG, TEAM = "acme", "acme-sales"

# ===================== shared CRUD =====================
P._MEMORY.clear(); P._SHARED.clear()
P.set_shared_definition(ORG, "ARR", "Annual Recurring Revenue", "{MRR} * 12")
sd = P.shared_definitions(ORG)
check("shared definition stored", sd.get("ARR", {}).get("formula") == "{MRR} * 12", str(sd))
check("get_shared_memory returns the scope + definitions", P.get_shared_memory(ORG)["scope"] == ORG and "ARR" in P.get_shared_memory(ORG)["definitions"], str(P.get_shared_memory(ORG)))
try:
    P.set_shared_definition("", "X", "y")
    check("shared definition needs a scope", False)
except ValueError:
    check("shared definition needs a scope", True)
check("delete_shared_definition works", P.delete_shared_definition(ORG, "ARR") is True and "ARR" not in P.shared_definitions(ORG))
check("shared_definitions('') is empty (no scope, no leakage)", P.shared_definitions("") == {}, "")

# ===================== inheritance + override =====================
P._MEMORY.clear(); P._SHARED.clear()
P.set_shared_definition(ORG, "ARR", "org ARR", "{MRR} * 12")
P.set_shared_definition(ORG, "Runway", "months of cash", "{Cash} / {Burn}")
eff = P.effective_definitions(TEAM, ORG)
check("team inherits org-shared terms it doesn't define", "ARR" in eff and "Runway" in eff, str(list(eff)))
# team overrides one term
P.set_definition(TEAM, "ARR", "team ARR", "{MRR} * 12 * 1.05")
eff = P.effective_definitions(TEAM, ORG)
check("a team's OWN definition overrides the shared one (local wins)",
      eff["ARR"]["formula"] == "{MRR} * 12 * 1.05", str(eff["ARR"]))
check("non-overridden org term still inherited", eff["Runway"]["formula"] == "{Cash} / {Burn}", str(eff.get("Runway")))
check("no scope → only the team's own definitions", set(P.effective_definitions(TEAM)) == set(P.definitions(TEAM)), "")

# ===================== context injection =====================
ctx = P.context(TEAM, scope=ORG)
check("context has an Organization glossary block", "Organization glossary" in ctx, ctx)
check("context has the Team glossary block", "Team glossary" in ctx, ctx)
check("inherited org term appears (Runway)", "Runway" in ctx and "{Cash} / {Burn}" in ctx, ctx)
# the overridden term shows the TEAM version, and only once
check("overridden term shows the team formula, not the org one",
      "{MRR} * 12 * 1.05" in ctx and "org ARR" not in ctx, ctx)
check("overridden term not duplicated under the org block",
      ctx.count("ARR:") == 1, f"count={ctx.count('ARR:')}\n{ctx}")
# backward compatibility: no scope → no org block, identical to team-only
check("no scope → NO organization block (backward compatible)", "Organization glossary" not in P.context(TEAM), P.context(TEAM))

# ===================== offline expansion includes org terms =====================
P._MEMORY.clear(); P._SHARED.clear()
P.set_shared_definition(ORG, "GM", "Gross Margin", "{Revenue} - {COGS}")
struct = {"primary_table": "t", "tables": {"t": {"columns": [{"name": "Revenue"}, {"name": "COGS"}]}}}
plan = fallback.parse("add GM", struct, P.effective_definitions("team-x", ORG))
ops = (plan or {}).get("operations") or []
check("offline fallback expands an ORG-shared term (model down)",
      ops and ops[0] == {"action": "add_formula_column", "name": "GM", "formula": "{Revenue} - {COGS}"}, str(ops))
check("expand_definitions(scope=org) also sees org terms",
      P.expand_definitions("add GM", "team-x", ORG) is not None, "")
check("without the org scope, the org term does NOT expand",
      P.expand_definitions("add GM", "team-x") is None, str(P.expand_definitions("add GM", "team-x")))

# ===================== stores are separate =====================
check("shared store is separate from per-team memory", P._MEMORY.get("team-x", {}).get("definitions", {}) == {} and "GM" in P.shared_definitions(ORG), "")

# ===================== API + parse wiring =====================
P._MEMORY.clear(); P._SHARED.clear()
c.post("/memory/shared/definition", data={"org_id": ORG, "term": "ARR", "definition": "Annual Recurring Revenue", "formula": "{MRR} * 12"})
got = c.get("/memory/shared", params={"org_id": ORG}).json()
check("API stores + views a shared definition", got["shared"]["definitions"].get("ARR", {}).get("formula") == "{MRR} * 12", str(got)[:160])
check("API delete shared definition", c.post("/memory/shared/definition/delete", data={"org_id": ORG, "term": "ARR"}).json().get("removed") is True, "")

# the shared glossary reaches the /parse context when org_id is passed
P._SHARED.clear()
P.set_shared_definition(ORG, "Runway", "months of cash", "{Cash} / {Burn}")
captured = {}


def _spy(instruction, structure, context=""):
    captured["ctx"] = context
    return {"operations": [{"action": "sort", "columns": ["Cash"], "orders": ["desc"]}], "title": "x"}


_real = m.llm.parse_instruction
try:
    m.llm.parse_instruction = _spy
    c.post("/inspect", data={"session_id": "s52"}, files=[("files", ("d.csv", b"Cash,Burn\n100,10\n200,20\n", "text/csv"))])
    c.post("/parse", data={"instruction": "sort by cash", "session_id": "s52", "team_id": TEAM, "org_id": ORG})
    check("org shared glossary is injected into the /parse context", "Runway" in (captured.get("ctx") or ""), captured.get("ctx"))
    # without org_id, it is NOT injected
    captured.clear()
    c.post("/parse", data={"instruction": "sort by cash", "session_id": "s52", "team_id": TEAM})
    check("without org_id, the shared glossary is NOT injected", "Runway" not in (captured.get("ctx") or ""), captured.get("ctx"))
finally:
    m.llm.parse_instruction = _real
    m._SESSIONS.clear()

P._MEMORY.clear(); P._SHARED.clear()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
