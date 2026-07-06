"""Phase 3.12 — Continuous learning & personalization tests.

PRD criteria:
  PL-consistent  Learned definitions are applied consistently — injected into the parse
                 context every time, deterministically expanded by the offline fallback,
                 and formatting preferences filled into plans the same way each run.
  PL-crud        The user can VIEW / EDIT / DELETE what's remembered (definitions,
                 preferences, templates), and it survives across requests.

Run from backend:  .venv\\Scripts\\python.exe test_personalization.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from fastapi.testclient import TestClient

from app import fallback, main, personalization

# Keep tests off disk + isolated from any persisted memory file.
personalization._PERSIST = False
personalization._MEMORY.clear()

passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("PHASE 3.12 — CONTINUOUS LEARNING & PERSONALIZATION\n")

TEAM = "acme"

# =========================================================================
# PL-crud  Definitions: view / edit / delete
# =========================================================================
print("PL-crud  Definitions CRUD")

personalization.set_definition(TEAM, "ARR", "Annual Recurring Revenue", "{MRR} * 12")
defs = personalization.definitions(TEAM)
check("definition stored", "ARR" in defs and defs["ARR"]["formula"] == "{MRR} * 12", str(defs))

# edit (re-set) keeps created_at, updates formula
created = defs["ARR"]["created_at"]
personalization.set_definition(TEAM, "ARR", "Annual Recurring Revenue", "{MRR} * 12 * 1.0")
edited = personalization.definitions(TEAM)["ARR"]
check("definition edited (formula updated)", edited["formula"] == "{MRR} * 12 * 1.0", str(edited))
check("definition edit preserves created_at", edited["created_at"] == created, "")

# delete
check("definition deleted", personalization.delete_definition(TEAM, "ARR") is True)
check("definition gone after delete", "ARR" not in personalization.definitions(TEAM), "")
check("deleting a missing definition is False", personalization.delete_definition(TEAM, "ghost") is False)

# =========================================================================
# PL-crud  Preferences + templates
# =========================================================================
print("\nPL-crud  Preferences & templates")

personalization.set_preferences(TEAM, currency_symbol="₹", date_format="dd-mm-yyyy", decimals=2, bold_header=True)
prefs = personalization.preferences(TEAM)
check("preferences stored", prefs["currency_symbol"] == "₹" and prefs["decimals"] == 2, str(prefs))
# partial update doesn't wipe other prefs
personalization.set_preferences(TEAM, decimals=0)
prefs = personalization.preferences(TEAM)
check("partial preference update keeps others", prefs["currency_symbol"] == "₹" and prefs["decimals"] == 0, str(prefs))

personalization.save_template(TEAM, "Monthly clean", [{"action": "remove_duplicates"}, {"action": "trim"}])
tmpls = personalization.templates(TEAM)
check("template saved", "Monthly clean" in tmpls and len(tmpls["Monthly clean"]["operations"]) == 2, str(tmpls))
check("template delete", personalization.delete_template(TEAM, "Monthly clean") is True)
check("template gone", "Monthly clean" not in personalization.templates(TEAM), "")

# =========================================================================
# PL-consistent  Glossary context + deterministic application
# =========================================================================
print("\nPL-consistent  Definitions applied consistently")

personalization.set_definition(TEAM, "ARR", "Annual Recurring Revenue", "{MRR} * 12")
ctx = personalization.context(TEAM)
check("context includes the definition", "ARR" in ctx and "{MRR} * 12" in ctx, ctx)
check("context includes preferences", "₹" in ctx and "dd-mm-yyyy" in ctx, ctx)
# Consistency: the same context every call
check("context is stable across calls", personalization.context(TEAM) == ctx, "")

# Deterministic offline expansion via the fallback parser
plan = fallback.parse("add ARR", {"primary_table": "t", "tables": {"t": {"columns": [{"name": "MRR"}]}}},
                      personalization.definitions(TEAM))
ops = (plan or {}).get("operations") or []
check("fallback expands a defined term to its team formula",
      ops and ops[0] == {"action": "add_formula_column", "name": "ARR", "formula": "{MRR} * 12"}, str(ops))

# An undefined term is NOT expanded
plan2 = fallback.parse("add Profit", {"primary_table": "t", "tables": {"t": {"columns": [{"name": "MRR"}]}}},
                       personalization.definitions(TEAM))
check("undefined term not falsely expanded",
      not any(o.get("name") == "ARR" for o in (plan2 or {}).get("operations") or []), str(plan2))

# =========================================================================
# PL-consistent  apply_preferences fills formatting defaults the same way
# =========================================================================
print("\nPL-consistent  Preference application")

p = {"currency_symbol": "₹", "date_format": "dd-mm-yyyy", "decimals": 2, "bold_header": True}
in_ops = [
    {"action": "format_cells", "format_columns": ["Revenue"], "number_format": "currency"},
    {"action": "format_cells", "format_columns": ["Date"], "number_format": "date"},
    {"action": "sort", "columns": ["Revenue"], "orders": ["desc"]},
]
out_ops = personalization.apply_preferences(in_ops, p)
check("currency symbol filled in", out_ops[0]["currency_symbol"] == "₹", str(out_ops[0]))
check("decimals filled in", out_ops[0]["decimals"] == 2, str(out_ops[0]))
check("date format filled in", out_ops[1]["date_format"] == "dd-mm-yyyy", str(out_ops[1]))
check("bold header applied", out_ops[0]["bold_header"] is True, str(out_ops[0]))
check("non-format ops untouched", out_ops[2] == in_ops[2], str(out_ops[2]))
# Explicit user value is NOT overridden by the preference
explicit = personalization.apply_preferences(
    [{"action": "format_cells", "format_columns": ["P"], "number_format": "currency", "currency_symbol": "$"}], p)
check("explicit value wins over preference", explicit[0]["currency_symbol"] == "$", str(explicit[0]))
check("apply_preferences doesn't mutate input", in_ops[0].get("currency_symbol") is None, str(in_ops[0]))

# =========================================================================
# API  view / edit / delete + injection into /parse
# =========================================================================
print("\nAPI  Memory endpoints + parse integration")
client = TestClient(main.app)
_orig = main.llm.parse_instruction

# CRUD over HTTP
client.post("/memory/definition", data={"team_id": "web", "term": "ARR", "definition": "Annual Recurring Revenue", "formula": "{MRR} * 12"})
client.post("/memory/preferences", data={"team_id": "web", "currency_symbol": "₹", "decimals": "2"})
mem = client.get("/memory", params={"team_id": "web"}).json()
check("API view memory", mem["memory"]["definitions"].get("ARR", {}).get("formula") == "{MRR} * 12", str(mem)[:160])
check("API view preferences", mem["memory"]["preferences"]["currency_symbol"] == "₹", str(mem["memory"]["preferences"]))

# edit then delete
client.post("/memory/definition", data={"team_id": "web", "term": "ARR", "definition": "ARR", "formula": "{MRR} * 12 + {Setup}"})
mem = client.get("/memory", params={"team_id": "web"}).json()
check("API edit definition", mem["memory"]["definitions"]["ARR"]["formula"] == "{MRR} * 12 + {Setup}", "")
client.post("/memory/definition/delete", data={"team_id": "web", "term": "ARR"})
mem = client.get("/memory", params={"team_id": "web"}).json()
check("API delete definition", "ARR" not in mem["memory"]["definitions"], str(mem["memory"]["definitions"]))

# The glossary is injected into the parse call (consistency of mechanism), and prefs
# are applied to the returned plan.
captured = {}


def _spy(instruction, structure, history=""):
    captured["history"] = history  # glossary is prepended into the context/history
    return {
        "operations": [{"action": "format_cells", "format_columns": ["Revenue"], "number_format": "currency"}],
        "title": "Format revenue", "translation": "Format Revenue as currency", "confidence": 95,
    }


try:
    main.llm.parse_instruction = _spy
    client.post("/memory/definition", data={"team_id": "web", "term": "ARR", "definition": "ARR", "formula": "{MRR} * 12"})
    client.post("/inspect", data={"session_id": "plweb"}, files=[("files", ("d.csv", b"Revenue,MRR\n100,10\n200,20\n", "text/csv"))])
    r = client.post("/parse", data={"instruction": "format revenue", "session_id": "plweb", "team_id": "web"}).json()
    check("glossary injected into parse context", "ARR" in (captured.get("history") or ""), captured.get("history"))
    op0 = r["plan"]["operations"][0]
    check("team preference applied to parsed plan", op0.get("currency_symbol") == "₹" and op0.get("decimals") == 2, str(op0))
finally:
    main.llm.parse_instruction = _orig
    main._SESSIONS.clear()

personalization._MEMORY.clear()
print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
