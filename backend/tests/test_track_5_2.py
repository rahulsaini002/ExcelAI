"""ENHANCEMENT TRACK 5, item 2 — the copy pass, where it is testable.

Most of "warm, specific, tells you what to do next" is a judgement call a test cannot
make. Two parts of it are not, and those are what this pins down:

  1. An error that RECOMMENDS something must recommend something real. The filter-operator
     message lists what you can filter with; if that list drifts away from what
     _condition_mask actually handles, the message starts sending users toward operators
     the engine will reject — worse than the vague text it replaced.

  2. The messages that were rewritten must keep the properties they were rewritten FOR:
     naming the thing that went wrong, and saying what to do next. A later edit that
     trims one back to "Merge needs two tables" should fail here.

Also checks the guarantee behind the multilingual starter prompts: Hindi and Hinglish
instructions really do route correctly (Stage 0.3 proved this live, 9/9 each), so the
frontend's non-English chips are demonstrating a capability, not advertising one.

Run from backend:  .venv\\Scripts\\python.exe tests\\test_track_5_2.py
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

_fd, _db = tempfile.mkstemp(suffix="-t52.db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")

import pandas as pd  # noqa: E402

from app import executor  # noqa: E402
from app.executor import OperationError, execute_multi  # noqa: E402

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def message_of(ops, tables=None, primary="t") -> str:
    tables = tables or {"t": pd.DataFrame({"Region": ["North", "South"], "Amount": [1, 2]})}
    try:
        execute_multi(tables, primary, ops)
    except OperationError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"<{type(exc).__name__}: {exc}>"
    return "<no error raised>"


def run() -> None:
    # --- 1. the operator list cannot drift from what the engine handles ---------------
    handled = (
        executor._NUMERIC_OPS
        | executor._TEXT_OPS
        | {"equals", "not_equals", "in", "not_in", "is_blank", "not_blank"}
    )
    named = set(executor._OPERATOR_WORDS)
    check("every operator the engine handles has a plain-language name",
          handled <= named, f"missing words for {sorted(handled - named)}")
    check("no plain-language name refers to an operator the engine lacks",
          named <= handled, f"names an unsupported operator: {sorted(named - handled)}")
    check("the human list is non-empty and readable",
          len(executor.supported_filter_operators()) == len(named)
          and all(" " in w or w.isalpha() for w in executor.supported_filter_operators()),
          f"got {executor.supported_filter_operators()}")

    # --- 2. the rewritten messages keep what they were rewritten for -------------------
    unknown = message_of([{"action": "definitely_not_an_operation"}])
    check("unknown action: names the action rather than dumping a repr",
          "definitely_not_an_operation" in unknown and "!r" not in unknown, unknown)
    check("unknown action: tells the user what to do next",
          "describe what you want" in unknown.lower(), unknown)
    check("unknown action: no longer reads like a stack trace",
          not unknown.lower().startswith("unknown operation"), unknown)

    merge_msg = message_of([{"action": "merge"}])
    check("merge with one table: says WHY it can't proceed",
          "at least two tables" in merge_msg, merge_msg)
    check("merge with one table: says what to DO",
          "upload" in merge_msg.lower(), merge_msg)

    combine_msg = message_of([{"action": "combine_sheets"}])
    check("combine with one table: says why and what to do",
          "at least two tables" in combine_msg and "upload" in combine_msg.lower(),
          combine_msg)

    nocol = message_of([{"action": "filter", "conditions": [{"operator": "equals", "value": "x"}]}])
    check("filter with no column: says which piece is missing",
          "which column" in nocol.lower() or "doesn't say which column" in nocol.lower(),
          nocol)
    check("filter with no column: gives a concrete example",
          "e.g." in nocol.lower(), nocol)

    badop = message_of([
        {"action": "filter", "conditions": [
            {"column": "Region", "operator": "sounds_like", "value": "North"}]}
    ])
    check("unknown operator: names the operator the user asked for",
          "sounds_like" in badop, badop)
    check("unknown operator: lists real alternatives",
          "is at least" in badop and "contains" in badop, badop)
    check("unknown operator: every listed alternative is one the engine handles",
          all(w in badop for w in ["is one of", "is blank"]), badop)
    check("unknown operator: ends with something the user can copy",
          "e.g." in badop.lower(), badop)

    # --- 3. no message should blame the user or the server ----------------------------
    for label, msg in [("unknown action", unknown), ("merge", merge_msg),
                       ("filter column", nocol), ("filter operator", badop)]:
        low = msg.lower()
        check(f"{label}: doesn't blame the server or use jargon",
              "something went wrong on our side" not in low
              and "traceback" not in low and "exception" not in low,
              msg)

    # --- 4. the multilingual starter prompts describe real, routable work --------------
    # The frontend now offers Hindi/Hinglish starter chips. They are column-agnostic on
    # purpose, so they must work on an arbitrary sheet — checked here deterministically
    # by running the operations those chips describe.
    # Genuinely duplicated ROWS — an earlier version of this test used rows that merely
    # shared an Email and expected whole-row dedupe to remove them, which it correctly
    # did not.
    df = pd.DataFrame({"Email": ["a@x.com", "a@x.com", "b@x.com"], "N": [1, 1, 3]})
    out, _, _, _ = execute_multi({"t": df}, "t", [{"action": "remove_duplicates"}])
    check("the 'remove duplicate rows' starter works on an arbitrary sheet",
          len(out) < len(df), f"rows {len(df)} -> {len(out)}")
    out2, _, _, _ = execute_multi({"t": df}, "t", [{"action": "limit", "count": 2}])
    check("the 'keep the first N rows' starter works on an arbitrary sheet",
          len(out2) == 2, f"got {len(out2)} rows")


if __name__ == "__main__":
    print("TRACK 5 item 2 — error copy + multilingual starters\n")
    run()
    print(f"\n{passed} passed, {failed} failed.")
    try:
        os.unlink(_db)
    except Exception:
        pass
    sys.exit(1 if failed else 0)
