"""ENHANCEMENT TRACK 3, item 1 — richer Brain context.

reader.summarize_tables already sent column names, inferred types, sample rows, row
counts and the primary table. The missing piece was SHEET RELATIONSHIPS: the
foreign keys a workbook has but never declares. Without them the Brain sees several
tables and no idea how they connect, so a cross-sheet request has to be guessed at.

main._brain_structure adds them, reusing kg.relationships (Phase 5.4) so a detected
link is matched the same normalized way the executor's lookup actually joins.

What these checks pin down:
  - the pre-existing context (types/samples/counts) is still there, unchanged
  - a real FK link IS detected and shaped correctly
  - a single sheet gets no relationships key (nothing to relate to)
  - coincidental overlap does NOT become a fabricated relationship
  - a huge workbook SKIPS detection rather than shipping approximate coverage
  - detection failure degrades to plain structure instead of breaking the request

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_3_1.py
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

_fd, _db = tempfile.mkstemp(suffix="-t31.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402

from app import kg, main  # noqa: E402

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


# Sales.Customer_ID -> Customers.Customer_ID is a genuine foreign key.
SALES = pd.DataFrame({
    "Order_ID": [1, 2, 3, 4],
    "Customer_ID": ["C1", "C2", "C1", "C3"],
    "Amount": [100.0, 250.5, 90.0, 310.25],
})
CUSTOMERS = pd.DataFrame({
    "Customer_ID": ["C1", "C2", "C3"],
    "Email": ["a@x.com", "b@x.com", "c@x.com"],
    "City": ["Pune", "Delhi", "Surat"],
})


def run() -> None:
    # --- the pre-existing context must survive the change ----------------------------
    s = main._brain_structure({"Sales": SALES, "Customers": CUSTOMERS}, "Sales")
    check("primary_table still marked", s.get("primary_table") == "Sales", f"got {s.get('primary_table')}")
    sales = s["tables"]["Sales"]
    check("row_count still present", sales.get("row_count") == 4, f"got {sales.get('row_count')}")
    check("sample rows still present", bool(sales.get("sample_rows")), "no sample_rows")
    types = {c["name"]: c["type"] for c in sales["columns"]}
    check("inferred types still present and correct",
          types.get("Amount") == "number" and types.get("Customer_ID") == "text",
          f"got {types}")

    # --- the new bit: a real relationship is detected --------------------------------
    rels = s.get("relationships") or []
    hit = [r for r in rels
           if r["from_table"] == "Sales" and r["from_column"] == "Customer_ID"
           and r["to_table"] == "Customers" and r["to_column"] == "Customer_ID"]
    check("real FK Sales.Customer_ID -> Customers.Customer_ID detected", len(hit) == 1,
          f"rels={rels}")
    if hit:
        r = hit[0]
        check("relationship carries measured coverage", isinstance(r.get("coverage"), (int, float)),
              f"coverage={r.get('coverage')!r}")
        check("full coverage reported as 1.0 (every sale's customer exists)",
              r.get("coverage") == 1.0, f"coverage={r.get('coverage')}")
        check("internal name_match flag not leaked to the Brain", "name_match" not in r,
              f"keys={sorted(r)}")

    # --- a single sheet has nothing to relate to --------------------------------------
    one = main._brain_structure({"Sales": SALES}, "Sales")
    check("single sheet gets no relationships key", "relationships" not in one,
          f"got {one.get('relationships')}")

    # --- honesty: unrelated tables must NOT produce a relationship ---------------------
    other = pd.DataFrame({
        "Ticket_No": ["T9", "T8"],
        "Note": ["late delivery", "wrong item"],
    })
    s2 = main._brain_structure({"Sales": SALES, "Tickets": other}, "Sales")
    bogus = [r for r in (s2.get("relationships") or [])
             if {r["from_table"], r["to_table"]} == {"Sales", "Tickets"}]
    check("unrelated sheets produce no fabricated link", not bogus, f"invented {bogus}")

    # --- size guard: skip rather than ship approximate coverage ------------------------
    big_a = pd.DataFrame({"Customer_ID": [f"C{i}" for i in range(60_000)]})
    big_b = pd.DataFrame({"Customer_ID": [f"C{i}" for i in range(60_000)],
                          "Email": [f"u{i}@x.com" for i in range(60_000)]})
    s3 = main._brain_structure({"A": big_a, "B": big_b}, "A")
    check("oversized workbook skips relationship detection", "relationships" not in s3,
          "detection ran on a workbook past the row cap")
    check("oversized workbook still gets the normal structure",
          s3.get("primary_table") == "A" and "tables" in s3, "base structure missing")

    # --- degradation: a detector failure must not break the request --------------------
    real = kg.relationships
    try:
        kg.relationships = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        s4 = main._brain_structure({"Sales": SALES, "Customers": CUSTOMERS}, "Sales")
        check("detector failure degrades to plain structure, no crash",
              "relationships" not in s4 and s4.get("primary_table") == "Sales",
              f"got {s4.get('relationships')}")
    finally:
        kg.relationships = real

    # --- the enriched structure is JSON-serializable (it is sent as JSON) --------------
    import json
    try:
        json.dumps(s, ensure_ascii=False)
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"        {exc}")
    check("enriched structure is JSON-serializable", ok)


if __name__ == "__main__":
    print("TRACK 3 item 1 — richer Brain context (sheet relationships)\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
