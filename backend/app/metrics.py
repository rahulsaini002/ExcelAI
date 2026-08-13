"""Usage metrics — where users struggle (Enhancement Track 5, item 4).

An AGGREGATION over what oplog already records (Track 4 item 5). No new collection: the
raw plan/execution/outcome events, their durations, their failure text and the operations
they touched are all there. What was missing was the arithmetic that turns them into an
answer.

TWO SUCCESS RATES, NOT ONE. "Prompt success rate" sounds like a single number and isn't,
because two different things can go wrong and they need different fixes:

  understanding   of the instructions users typed, how many did the Brain turn into a
                  plan (rather than asking a clarifying question, declining, or failing)?
                  A low number here is a PROMPT problem.
  execution       of the plans that ran, how many finished cleanly (rather than erroring,
                  partially applying, or timing out)? A low number here is an ENGINE
                  problem.

Averaging them into one "success rate" would hide exactly the distinction that tells you
what to go and fix, so they are reported separately and named precisely.

⚠️ A LIMIT WORTH KNOWING. /parse and /execute record under DIFFERENT run ids — the id is
minted per request — so we cannot currently say "this typed instruction ended in this
result". These are two populations measured separately, not a funnel. Linking them would
need a parse id threaded through the UI into /execute; until then, do not read
`understanding` and `execution` as consecutive stages of the same visits.

TIME-TO-RESULT is reported as MEDIAN and P95, never as a mean. One 120k-row job drags a
mean somewhere no user actually experienced; the median says what a typical run feels
like and p95 says how bad the slow tail gets.

FAILURE REASONS ARE GROUPED, NOT LISTED. Raw error text embeds column names and counts, so
a hundred failures produce a hundred unique strings and no signal. They are classified
into a small set of causes, keeping ONE redacted example each so a reason stays traceable
without becoming a data dump.
"""
from __future__ import annotations

import re
from collections import Counter

from . import oplog

# Ordered: the first pattern that matches wins, so put the specific ones first.
_REASON_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("timed_out", re.compile(r"time budget|timed out", re.I)),
    # "don't see the column" is the engine's actual wording — an earlier version of this
    # list guessed at the phrasing and sent every missing-column failure to 'other'. The
    # growing 'other' bucket is what surfaced it, which is the bucket doing its job.
    ("missing_column", re.compile(
        r"don't see the column|couldn't find the column|no column|isn't in your file", re.I)),
    ("unknown_column_reference", re.compile(r"#REF!|don't have a table|no sheet called", re.I)),
    ("needs_more_files", re.compile(r"at least two tables", re.I)),
    ("unsupported_operation", re.compile(r"don't have an operation called", re.I)),
    ("bad_filter", re.compile(r"filter with|filter condition", re.I)),
    ("wrong_type", re.compile(r"isn't numbers or dates|can't be compared|not numeric", re.I)),
    ("malformed_plan", re.compile(r"isn't a valid operation|nothing to run|couldn't be read", re.I)),
    ("model_unavailable", re.compile(r"rate-limited|couldn't reach the AI", re.I)),
    ("internal", re.compile(r"went wrong on our side|unexpected ", re.I)),
]


def classify_failure(message: str) -> str:
    """Group a failure message into a cause. Unmatched text becomes 'other' rather than
    being silently dropped — an 'other' bucket that grows is itself the signal that this
    list needs a new entry."""
    text = (message or "").strip()
    if not text:
        return "unknown"
    for name, pattern in _REASON_PATTERNS:
        if pattern.search(text):
            return name
    return "other"


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. Returns None for an empty sample rather than 0, because
    "no data" and "instant" are very different answers."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100) * len(ordered) + 0.5)) - 1))
    return ordered[k]


def _rate(part: int, whole: int) -> float | None:
    """A rate, or None when there is nothing to divide. Reporting 0.0 for an empty sample
    would read as "everything failed", which is a different claim from "nothing ran"."""
    if whole <= 0:
        return None
    return round(part / whole, 4)


def summary(limit: int = 1000) -> dict:
    """Aggregate the recent operation log into an answer about where users struggle."""
    events = oplog.events(limit=limit)
    plans = [e for e in events if e["phase"] == "plan"]
    outcomes = [e for e in events if e["phase"] == "outcome"]

    # --- understanding: did the Brain turn the instruction into a plan? ---------------
    # Only model-produced plans count. A "user" plan is one the UI already had, so
    # including those would inflate the number with work the Brain never did.
    brain_plans = [p for p in plans if p.get("source") in ("brain", "fallback")]
    understood = [p for p in brain_plans if p.get("status") == "plan"]
    clarified = [p for p in brain_plans if p.get("status") == "clarify"]
    declined = [p for p in brain_plans if p.get("status") == "message"]

    # --- execution: did the run finish cleanly? --------------------------------------
    by_status = Counter(o.get("status") for o in outcomes)
    ok_runs = by_status.get("ok", 0)

    # --- retries: the only signal that someone went round again ----------------------
    exec_plans = [p for p in plans if p.get("source") == "user"]
    retried = [p for p in exec_plans if p.get("retry")]

    # --- operation usage -------------------------------------------------------------
    usage: Counter = Counter()
    for p in plans:
        for step in p.get("plan") or []:
            action = step.get("action")
            if action:
                usage[action] += 1

    # --- failure reasons, grouped ----------------------------------------------------
    reasons: Counter = Counter()
    examples: dict[str, str] = {}
    for o in outcomes:
        if o.get("status") in ("ok", None):
            continue
        reason = classify_failure(o.get("error") or "")
        reasons[reason] += 1
        examples.setdefault(reason, (o.get("error") or "")[:160])

    # --- time to result: successful runs only ----------------------------------------
    # A failed run's duration measures how fast we gave up, not how long work takes;
    # mixing them in would flatter the numbers.
    durations = [
        float(o["duration_ms"]) for o in outcomes
        if o.get("status") == "ok" and isinstance(o.get("duration_ms"), (int, float))
    ]

    return {
        "sampled_events": len(events),
        "understanding": {
            "instructions": len(brain_plans),
            "planned": len(understood),
            "clarified": len(clarified),
            "declined": len(declined),
            "plan_rate": _rate(len(understood), len(brain_plans)),
            "clarify_rate": _rate(len(clarified), len(brain_plans)),
        },
        "execution": {
            "runs": len(outcomes),
            "ok": ok_runs,
            "by_status": dict(by_status),
            "success_rate": _rate(ok_runs, len(outcomes)),
        },
        "retries": {
            "runs": len(exec_plans),
            "retried": len(retried),
            "retry_rate": _rate(len(retried), len(exec_plans)),
        },
        "operations": {
            "total_steps": sum(usage.values()),
            "by_action": dict(usage.most_common()),
            "most_used": usage.most_common(5),
        },
        "failures": {
            "total": sum(reasons.values()),
            "by_reason": dict(reasons.most_common()),
            "examples": examples,
        },
        "time_to_result_ms": {
            "runs_measured": len(durations),
            "median": _percentile(durations, 50),
            "p95": _percentile(durations, 95),
            "slowest": max(durations) if durations else None,
        },
        "note": (
            "understanding and execution are measured over DIFFERENT populations — "
            "/parse and /execute record under different run ids, so this is not a funnel. "
            "Durations cover successful runs only."
        ),
    }
