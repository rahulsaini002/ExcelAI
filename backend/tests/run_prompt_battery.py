"""Prompt Battery runner — the permanent regression harness (Build & Test Program,
"Global test assets", asset B).

Reads tests/prompt_battery.csv, sends each prompt through the REAL engine (in-process
FastAPI TestClient — no server needed), and reports pass/fail per row and per area.

CSV columns
-----------
  area              1-17 (the Excel capability area)
  capability        short slug, e.g. "sort", "remove-duplicates"
  prompt            the plain-language instruction, exactly as a user would type it
  language          EN | HI | UR | Hinglish
  type              happy | edge | failure
  expected_plan     checks against the /parse Operation Plan (blank = skip /parse)
  expected_outcome  checks against the /process result   (blank = skip /process)
  file              OPTIONAL last column: a path relative to tests/ that replaces the
                    Standard Test Workbook for this row (e.g. files/corrupt.xlsx) —
                    how the Phase-0.1 file-validation rows ride in the battery

Expectation grammar (semicolon-separated; ALL must hold)
--------------------------------------------------------
  expected_plan     each fragment is a case-insensitive substring of the plan's
                    operations JSON, e.g.   sort   or   "action": "sort";Price
  expected_outcome  any of:
                    status=ok            response status equals ok
                    status=message|clarify   any of the |-alternatives
                    contains:some text   substring of the whole response JSON
                    rows=96  rows<200  rows>0   compared against row_count

A failure-type row PASSES when the engine handles it SAFELY (declines/clarifies per
its expected_outcome) — a confident wrong answer is the thing this battery catches.

Usage (from backend/, so .env with GEMINI_API_KEY is picked up)
---------------------------------------------------------------
  .venv\\Scripts\\python.exe tests\\run_prompt_battery.py                # whole battery
  .venv\\Scripts\\python.exe tests\\run_prompt_battery.py --area 4       # one area
  .venv\\Scripts\\python.exe tests\\run_prompt_battery.py --lang HI --type failure
  .venv\\Scripts\\python.exe tests\\run_prompt_battery.py --row 2        # one row (1-based)
  .venv\\Scripts\\python.exe tests\\run_prompt_battery.py --workbook other.xlsx

Exit code: 0 all pass, 1 any fail — so it can gate CI.
Note: rows with a non-blank expected_plan cost one Brain call, and rows with a
non-blank expected_outcome cost another (real Gemini calls when the key is set).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# Isolated throwaway DB (same pattern as every other backend test) — set BEFORE app import.
_fd, _db = tempfile.mkstemp(suffix="-battery.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


PACE_SECONDS = 5.0  # base gap before each engine call, set by --pace (free-tier RPM cap)


def _post_retry(path: str, *, data: dict, files: list | None = None, tries: int = 3):
    """POST with retry-on-rate-limit: the engine answers 'rate-limited' honestly when the
    free Gemini tier's per-minute cap trips, but that means the row wasn't judged — wait
    out the window and retry instead of reporting a false failure."""
    if PACE_SECONDS > 0:
        time.sleep(PACE_SECONDS)
    for attempt in range(tries):
        r = client.post(path, data=data, files=files) if files else client.post(path, data=data)
        try:
            j = r.json()
        except Exception:
            return r
        if not (j.get("status") == "error" and "rate-limit" in str(j.get("error", "")).lower()):
            return r
        if attempt < tries - 1:
            # Wait out a full per-minute quota window (short waits land inside the same
            # throttled window and llm.py's internal retries amplify the burn).
            wait = 70 * (attempt + 1)
            print(f"        (rate-limited — waiting {wait}s, attempt {attempt + 2}/{tries})")
            time.sleep(wait)
    return r


# ------------------------------------------------------------------ expectation checks --
def check_plan(expected: str, parse_json: dict) -> list[str]:
    """Return a list of failure messages (empty = pass) for the /parse leg."""
    fails: list[str] = []
    status = parse_json.get("status")
    if status != "plan":
        return [f"/parse returned status={status!r} (wanted a plan): "
                f"{json.dumps(parse_json, ensure_ascii=False)[:160]}"]
    ops_json = json.dumps(parse_json.get("plan", {}).get("operations", []), ensure_ascii=False).lower()
    for frag in filter(None, (f.strip() for f in expected.split(";"))):
        if frag.lower() not in ops_json:
            fails.append(f"plan missing {frag!r} (operations: {ops_json[:160]})")
    return fails


def check_outcome(expected: str, proc_json: dict) -> list[str]:
    """Return a list of failure messages (empty = pass) for the /process leg."""
    fails: list[str] = []
    whole = json.dumps(proc_json, ensure_ascii=False).lower()
    for chk in filter(None, (c.strip() for c in expected.split(";"))):
        low = chk.lower()
        if low.startswith("status="):
            allowed = [s.strip() for s in low[len("status="):].split("|")]
            got = str(proc_json.get("status", "")).lower()
            if got not in allowed:
                detail = proc_json.get("error") or proc_json.get("message") or proc_json.get("explanation") or ""
                fails.append(f"status={got!r}, wanted {'|'.join(allowed)} ({str(detail)[:120]})")
        elif low.startswith("contains:"):
            needle = low[len("contains:"):].strip()
            if needle not in whole:
                fails.append(f"response does not contain {needle!r}")
        elif low.startswith("rows"):
            rows = proc_json.get("row_count")
            if not isinstance(rows, int):
                fails.append(f"no row_count in response (wanted {chk})")
                continue
            op, num = (low[4], int(low[5:])) if low[4] in "<>" else ("=", int(low[5:]))
            ok = rows < num if op == "<" else rows > num if op == ">" else rows == num
            if not ok:
                fails.append(f"row_count={rows}, wanted rows{op}{num}")
        else:
            fails.append(f"unknown expected_outcome check {chk!r}")
    return fails


# ------------------------------------------------------------------------- one row ------
def run_row(row: dict, workbook: bytes, wb_name: str) -> list[str]:
    """Run one battery row through the engine. Returns failure messages (empty = pass)."""
    fails: list[str] = []
    prompt = row["prompt"].strip()

    # Leg 1 — plan check via the two-phase flow (/inspect seeds the session, /parse plans).
    if row["expected_plan"].strip():
        sid = f"battery-{uuid.uuid4().hex[:12]}"
        ins = client.post("/inspect", data={"session_id": sid},
                          files=[("files", (wb_name, workbook, XLSX_MIME))])
        if ins.status_code != 200:
            fails.append(f"/inspect failed HTTP {ins.status_code}: {ins.text[:120]}")
        else:
            par = _post_retry("/parse", data={"instruction": prompt, "session_id": sid})
            fails += check_plan(row["expected_plan"], par.json())

    # Leg 2 — outcome check via the one-shot flow (fresh session: file + instruction).
    if row["expected_outcome"].strip():
        proc = _post_retry(
            "/process",
            data={"instruction": prompt, "session_id": f"battery-{uuid.uuid4().hex[:12]}"},
            files=[("files", (wb_name, workbook, XLSX_MIME))],
        )
        try:
            payload = proc.json()
        except Exception:
            payload = {"status": f"http-{proc.status_code}", "body": proc.text[:200]}
        # _error responses come back with 4xx + {"status":"error"} — that's still a
        # legitimate engine answer for failure-type rows, so judge on the JSON.
        fails += check_outcome(row["expected_outcome"], payload)

    return fails


# ----------------------------------------------------------------------------- main -----
def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Sumio prompt battery.")
    ap.add_argument("--csv", default=str(TESTS_DIR / "prompt_battery.csv"))
    ap.add_argument("--workbook", default=str(TESTS_DIR / "standard_test_workbook.xlsx"))
    ap.add_argument("--area", help="only rows with this area number")
    ap.add_argument("--cap", help="only rows whose capability contains this substring")
    ap.add_argument("--lang", help="only rows with this language (EN/HI/UR/Hinglish)")
    ap.add_argument("--type", dest="typ", help="only rows of this type (happy/edge/failure)")
    ap.add_argument("--row", type=int, help="run a single row (1-based, as in the CSV)")
    ap.add_argument("--pace", type=float, default=5.0,
                    help="seconds to wait before each engine call (default 5; 0 = full speed)")
    args = ap.parse_args()
    global PACE_SECONDS
    PACE_SECONDS = max(0.0, args.pace)

    wb_path = Path(args.workbook)
    if not wb_path.exists():
        print(f"Workbook not found: {wb_path}\nGenerate it first: python tests/make_standard_workbook.py")
        return 1
    workbook = wb_path.read_bytes()

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    selected = [
        (i + 1, r) for i, r in enumerate(rows)
        if (not args.area or r["area"].strip() == args.area)
        and (not args.cap or args.cap.lower() in r["capability"].strip().lower())
        and (not args.lang or r["language"].strip().lower() == args.lang.lower())
        and (not args.typ or r["type"].strip().lower() == args.typ.lower())
        and (not args.row or i + 1 == args.row)
    ]
    if not selected:
        print("No battery rows match the given filters.")
        return 1

    print(f"Prompt battery: {len(selected)} of {len(rows)} rows "
          f"(workbook: {wb_path.name}, {'Gemini' if os.getenv('GEMINI_API_KEY') else 'NO API KEY — offline fallback only'})\n")

    per_area: dict[str, list[bool]] = defaultdict(list)
    failed_rows: list[tuple[int, dict, list[str]]] = []
    file_cache: dict[str, bytes] = {}
    for n, row in selected:
        # Optional per-row file override (relative to tests/); default = standard workbook.
        row_file, row_name = workbook, wb_path.name
        override = (row.get("file") or "").strip()
        if override:
            p = TESTS_DIR / override
            if not p.exists():
                per_area[row["area"].strip()].append(False)
                failed_rows.append((n, row, [f"file not found: {p}"]))
                print(f"  FAIL  row {n:>3}  [area {row['area']:>2} · {row['capability']}]  missing file {override}")
                continue
            if override not in file_cache:
                file_cache[override] = p.read_bytes()
            row_file, row_name = file_cache[override], p.name
        t0 = time.time()
        fails = run_row(row, row_file, row_name)
        ms = int((time.time() - t0) * 1000)
        ok = not fails
        per_area[row["area"].strip()].append(ok)
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  row {n:>3}  [area {row['area']:>2} · {row['capability']} · "
              f"{row['language']} · {row['type']}]  {ms}ms  {row['prompt'][:60]!r}")
        for msg in fails:
            print(f"        - {msg}")
        if not ok:
            failed_rows.append((n, row, fails))

    # Result tables: per area, per operation (capability), per language, and the Tier-1
    # exit bar (happy >= 95%, failure-cases 100% handled safely).
    def table(title: str, groups: dict[str, list[bool]]) -> None:
        print(f"\n{title}")
        for key in sorted(groups):
            r = groups[key]
            pct = 100.0 * sum(r) / len(r)
            print(f"  {key:<26} {sum(r):>3}/{len(r):<3}  {pct:5.1f}%")

    per_cap: dict[str, list[bool]] = defaultdict(list)
    per_lang: dict[str, list[bool]] = defaultdict(list)
    per_type: dict[str, list[bool]] = defaultdict(list)
    ok_by_row = {n: True for n, _ in selected}
    for n, _, _ in failed_rows:
        ok_by_row[n] = False
    for n, row in selected:
        ok = ok_by_row[n]
        per_cap[row["capability"].strip()].append(ok)
        per_lang[row["language"].strip()].append(ok)
        per_type[row["type"].strip().lower()].append(ok)

    print("\nPer-area summary")
    for area in sorted(per_area, key=lambda a: int(a) if a.isdigit() else 99):
        results = per_area[area]
        print(f"  area {area:>2}: {sum(results)}/{len(results)} passed")
    table("Per-operation (capability)", per_cap)
    table("Per-language", per_lang)
    table("Per-type", per_type)

    if per_type.get("happy") or per_type.get("failure"):
        print("\nTier-1 exit bar")
        if per_type.get("happy"):
            h = per_type["happy"]
            rate = 100.0 * sum(h) / len(h)
            print(f"  happy >= 95%:            {rate:5.1f}%  -> {'MET' if rate >= 95.0 else 'NOT MET'}")
        if per_type.get("failure"):
            f_ = per_type["failure"]
            rate = 100.0 * sum(f_) / len(f_)
            print(f"  failure-safety = 100%:   {rate:5.1f}%  -> {'MET' if rate == 100.0 else 'NOT MET'}")

    total = sum(len(v) for v in per_area.values())
    passed = sum(sum(v) for v in per_area.values())
    print(f"\n{passed}/{total} rows passed.")
    return 1 if failed_rows else 0


if __name__ == "__main__":
    code = main()
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(code)
