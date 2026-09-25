"""FastAPI app: the engine that ties everything together.

Flow for POST /process:
  1. read the uploaded file (pandas)
  2. summarize its structure
  3. call Gemini -> operation plan (or a clarifying question)
  4. execute the plan (pandas)
  5. return the processed file (base64) + a plain-language "here's what I did"
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import re
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from openpyxl import Workbook
from openpyxl.chart import (
    AreaChart, BarChart, BubbleChart, DoughnutChart, LineChart, PieChart,
    RadarChart, Reference, ScatterChart, Series, StockChart,
)
from openpyxl.chart.marker import Marker
from openpyxl.formatting.rule import (
    CellIsRule,
    ColorScaleRule,
    DataBarRule,
    FormulaRule,
    IconSetRule,
    Rule,
)
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter

# Matches {ColumnName} placeholders inside a formula template.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

from . import (
    apikeys, audit, auth, autoreport, cloudsessions, collab, compliance, compute_mode, config,
    connectors, digest, distribution, execsummary, exports, fallback, guardrails, jobs, kg,
    lineage, llm, marketplace, metrics, oidc, oplog, org, permissions, personalization, pii,
    quality, resultstore, scale, selfheal, slack, solver, store, sync, voice, workflow,
)
from .db import init_db, session_scope
from .executor import (
    MultiStepError, OperationCancelled, OperationError, execute_multi, _resolve_sheet_name,
)
from .operations.base import to_datetime as _to_datetime
from .reader import load_files, summarize_structure, summarize_tables

# Startup/shutdown via lifespan (on_event is deprecated in this FastAPI version).
#   - create any missing database tables (safe every boot; existing tables untouched).
#   - warn loudly if production-ish settings are on but the JWT secret is still the
#     insecure dev default (anyone who reads the source could forge logins).
@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    if config.JWT_SECRET == "dev-only-insecure-change-me" and (
        config.REQUIRE_AUTH or config.PERSIST
    ):
        print(
            "WARNING: SUMIO_JWT_SECRET is still the insecure dev default while "
            "auth/persistence is enabled. Set a long random secret in production, e.g. "
            "python -c \"import secrets; print(secrets.token_hex(32))\"",
            flush=True,
        )
    yield


app = FastAPI(title="Sumio API", version="0.1.0", lifespan=_lifespan)


# Login / signup endpoints (POST /auth/signup, POST /auth/login, GET /auth/me).
app.include_router(auth.router)

# --- Pre-deploy hardening: optional API token + per-IP rate limit ---------------------
# Both are OFF by default (env-gated), so local dev and the test suite are unaffected.
# Registered BEFORE the CORS middleware below so CORS remains the OUTERMOST layer and its
# headers apply to the gate's 401/429 responses too (browsers can then read the error).
_RATE: dict[str, list] = {}


def _client_ip(request: Request) -> str:
    """The identity the rate limiter counts against.

    X-Forwarded-For is a plain request header: ANY client can send one. Trusting it
    unconditionally — and taking the LEFTMOST entry, which is the part a client controls —
    meant a caller could put a different value on every request and get a fresh bucket
    each time, i.e. no rate limit at all. That is worse than having none, because it looks
    like protection.

    So the header is only consulted when the deployment says it is behind a proxy
    (SUMIO_TRUST_PROXY), and then we take the LAST entry: each hop appends, so the
    rightmost value is the one OUR proxy observed and the client cannot forge past it.
    Anything a client prepends sits harmlessly to the left.

    Default off is deliberately fail-CLOSED: behind an unconfigured proxy every user
    shares the proxy's IP and one bucket, which over-limits. Over-limiting is a visible
    annoyance; a silently bypassable limiter is a security hole.
    """
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            hops = [h.strip() for h in fwd.split(",") if h.strip()]
            if hops:
                return hops[-1]
    return request.client.host if request.client else "?"


def _rate_exceeded(ip: str, limit: int, window: float) -> bool:
    """Fixed-window per-IP counter. Bounded memory (prunes stale IPs when it grows)."""
    now = time.time()
    entry = _RATE.get(ip)
    if entry is None or now - entry[0] >= window:
        if len(_RATE) > 5000:
            for k in [k for k, v in _RATE.items() if now - v[0] >= window]:
                _RATE.pop(k, None)
        _RATE[ip] = [now, 1]
        return False
    entry[1] += 1
    return entry[1] > limit


# After any write request, snapshot the runtime stores (connections/schedules/etc.) to
# the database so they survive a restart. No-op unless persistence is on (config.PERSIST,
# auto-on when DATABASE_URL is set). GETs don't change state, so we skip them.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _persist_stores(request: Request, call_next):
    response = await call_next(request)
    if config.PERSIST and request.method in _MUTATING_METHODS:
        try:
            store.save_all()
        except Exception:
            traceback.print_exc()  # never let a storage hiccup break the response
    return response


@app.middleware("http")
async def _gate(request: Request, call_next):
    # Never gate CORS preflight or the health check (deploy platforms poll /health).
    if request.method == "OPTIONS" or request.url.path == "/health":
        return await call_next(request)

    token = config.API_TOKEN
    api_key = request.headers.get("x-api-key") or ""
    authz_header = request.headers.get("authorization") or ""
    bearer = authz_header[7:].strip() if authz_header.lower().startswith("bearer ") else ""
    if token:
        # The key may arrive as X-API-Key or as the Bearer value (server callers).
        if (api_key or bearer) != token:
            return JSONResponse(
                {"status": "error", "error": "Missing or invalid API key."},
                status_code=401,
            )

    # Login wall: when enabled, require a valid login token everywhere except the login
    # endpoints themselves (so users can actually sign in) and /health (exempted above).
    if config.REQUIRE_AUTH and not request.url.path.startswith("/auth/"):
        # Downloads are fetched via plain <a href> links, which CAN'T carry an auth
        # header — the unguessable download id is itself the capability. Reads only.
        is_download = request.method in ("GET", "HEAD") and request.url.path.startswith("/download/")
        # A caller presenting the (genuinely secret) server API key is a trusted
        # server-to-server client — the Sheets add-on and the run_due cron have no
        # per-user login to send.
        is_server_caller = bool(token) and (api_key == token or bearer == token)
        if not (is_download or is_server_caller):
            valid = False
            if bearer:
                try:
                    valid = bool(auth.decode_token(bearer).get("sub"))
                except Exception:
                    valid = False
            if not valid:
                return JSONResponse(
                    {"status": "error", "error": "Please sign in to continue."},
                    status_code=401,
                )

    limit = config.RATE_LIMIT
    if limit and limit > 0 and _rate_exceeded(_client_ip(request), limit, config.RATE_WINDOW):
        return JSONResponse(
            {"status": "error", "error": "Too many requests — please slow down and try again shortly."},
            status_code=429,
        )

    return await call_next(request)


# Open for local dev (any localhost/127.0.0.1/LAN origin) so the browser is never
# blocked by CORS. In production set SUMIO_CORS_ALLOW_ALL=0 to restrict to the origins
# listed in SUMIO_CORS_ORIGINS (your deployed frontend).
if config.CORS_ALLOW_ALL:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# Global safety net (error-handling rule 2): NO technical error ever reaches the user.
# Any exception that escapes an endpoint — a library error, a bad request body, an
# unexpected bug — is caught here, logged server-side, and returned as a friendly message.
# Endpoints still handle their own expected errors with specific wording; these are the
# last line of defense so a stack trace, status code, or library message never leaks.
@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {
            "status": "error",
            "error": "Some required details were missing or malformed in that request. "
                     "Please try again.",
        },
        status_code=400,
    )


@app.exception_handler(Exception)
async def _on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    traceback.print_exc()  # the real cause is logged for us; never sent to the user
    return JSONResponse(
        {"status": "error", "error": _INTERNAL_ERROR},
        status_code=500,
    )


# In-memory per-session working data, keyed by a session id from the frontend.
# Each entry: {"tables": {name: DataFrame}, "primary": str, "exts": {name: ext}}.
# This lets follow-up instructions build on the previous result (chaining) instead
# of re-reading the original uploads every time. Cleared when the server restarts.
_SESSIONS: dict[str, dict] = {}

# Generated result files, served on demand via GET /download/{id}. We hand the
# frontend a small download id instead of inlining the whole file as base64 in the
# JSON (base64-in-JSON makes the browser hold several copies of a big file and run out
# of memory). Files are written to DISK + indexed in memory, so downloads — and the
# frontend's "continue on the result" — SURVIVE A BACKEND RESTART (even for big files).
# Bounded by total size and a TTL.
_RESULTS_DIR = Path(config.RESULTS_DIR)
_RESULTS_INDEX = _RESULTS_DIR / "index.json"
_RESULTS_MAX_BYTES = config.MAX_RESULTS_MB * 1024 * 1024
_RESULTS_TTL = config.RESULTS_TTL_HOURS * 3600
_RESULTS: "OrderedDict[str, dict]" = OrderedDict()  # id -> {filename, media_type, size, created}
# Results small enough to ALSO inline as base64 (instant download for normal files).
_INLINE_MAX_BYTES = 6 * 1024 * 1024


def _save_results_index() -> None:
    try:
        _RESULTS_INDEX.write_text(json.dumps(_RESULTS))
    except Exception:
        pass


def _delete_result(rid: str) -> None:
    _RESULTS.pop(rid, None)
    try:
        (_RESULTS_DIR / rid).unlink(missing_ok=True)
    except Exception:
        pass
    # Drop the durable copy too, or an expired/evicted result would come back to life
    # from the database — the cache and the durable store must agree on what exists.
    resultstore.delete(rid)


def _prune_results() -> None:
    """Drop expired results, then the oldest until under the size cap."""
    now = time.time()
    for rid in list(_RESULTS):
        if now - _RESULTS[rid].get("created", 0) > _RESULTS_TTL:
            _delete_result(rid)
    total = sum(m["size"] for m in _RESULTS.values())
    while total > _RESULTS_MAX_BYTES and len(_RESULTS) > 1:
        rid, meta = next(iter(_RESULTS.items()))
        total -= meta["size"]
        _delete_result(rid)


def _load_results_index() -> None:
    """Re-attach to result files written before a restart (so downloads still work)."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for rid, meta in json.loads(_RESULTS_INDEX.read_text()).items():
            if (_RESULTS_DIR / rid).exists():
                _RESULTS[rid] = meta
    except Exception:
        pass
    _prune_results()
    _save_results_index()


def _store_result(out_bytes: bytes, filename: str, media_type: str) -> str:
    """Write a result to disk for download and return its id (evicts old ones)."""
    rid = uuid.uuid4().hex
    try:
        (_RESULTS_DIR / rid).write_bytes(out_bytes)
    except Exception:
        traceback.print_exc()
    _RESULTS[rid] = {
        "filename": filename, "media_type": media_type,
        "size": len(out_bytes), "created": time.time(),
    }
    # Durable copy so the link still works after a restart — this host has no persistent
    # disk, and both the file above and the index below are lost with it. Best-effort and
    # size-capped: a result the user can already download must never fail over this.
    resultstore.save(rid, out_bytes, filename, media_type)
    _prune_results()
    _save_results_index()
    return rid


_load_results_index()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": config.MODEL}


# --- Cross-device sessions --------------------------------------------------
# Saving the SOURCE FILE against the account is what lets a session opened on a phone be
# the one that was uploaded on a laptop. See app/cloudsessions.py for why it's the file
# and not the session state, and for the two size bounds.


@app.post("/sessions/sync")
async def sessions_sync(
    files: list[UploadFile] = File(...),
    session_id: str = Form(""),
    name: str = Form("Untitled session"),
    user: "auth.User | None" = Depends(auth.current_user_optional),
) -> JSONResponse:
    """Store this session's source file against the signed-in account.

    Deliberately NOT an error for anonymous callers: the workspace works fine without an
    account, it simply can't follow you to another device. Saying so in the response beats
    a 401 the UI would have to special-case — and beats implying it synced when it didn't.
    """
    if user is None:
        return JSONResponse({"status": "ok", "synced": False, "reason": "not_signed_in"})
    if not files:
        return _error("Please upload a spreadsheet.", status=400)
    first = files[0]
    blob = await first.read()
    try:
        with session_scope() as db:
            cloudsessions.save(
                db, user.id, session_id, name,
                first.filename or "upload.xlsx",
                first.content_type or "application/octet-stream",
                blob,
            )
    except cloudsessions.CloudSessionError as exc:
        # An over-cap file is NOT a failed upload: the session still works on this device.
        # Report it as a non-sync with the reason so the UI can say so honestly.
        if exc.status == 413:
            return JSONResponse(
                {"status": "ok", "synced": False, "reason": "too_large", "detail": exc.message}
            )
        return _error(exc.message, status=exc.status)
    return JSONResponse({"status": "ok", "synced": True})


@app.get("/sessions")
async def sessions_list(
    user: "auth.User | None" = Depends(auth.current_user_optional),
) -> JSONResponse:
    """The signed-in user's saved sessions (metadata only — never the file bytes)."""
    if user is None:
        return JSONResponse({"status": "ok", "sessions": [], "signed_in": False})
    with session_scope() as db:
        return JSONResponse(
            {"status": "ok", "signed_in": True, "sessions": cloudsessions.listing(db, user.id)}
        )


@app.post("/sessions/{session_id}/restore")
async def sessions_restore(
    session_id: str,
    user: "auth.User" = Depends(auth.current_user),
) -> JSONResponse:
    """Rebuild a live session on THIS server from the stored file.

    Reuses the same load path as /inspect, so a restored session is byte-for-byte the one
    a fresh upload would produce — there is no second notion of "a session".
    """
    try:
        with session_scope() as db:
            row = cloudsessions.get(db, user.id, session_id)
            blob, filename = row.blob, row.filename
    except cloudsessions.CloudSessionError as exc:
        return _error(exc.message, status=exc.status)

    try:
        data = load_files([(filename, blob)])
    except ValueError as exc:
        return _error(str(exc), status=400)
    except llm.ModelUnavailableError as exc:
        return _error(str(exc), status=503)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    _remember_session(session_id, {
        "tables": data.tables,
        "primary": data.primary,
        "exts": data.exts,
        "notes": data.notes,
    })
    return JSONResponse({
        "status": "ok",
        "restored": True,
        "filename": filename,
        # Same shape as /inspect, from the same helper — a restored session must look
        # identical to a freshly uploaded one.
        "tables": _preview_payload(data.tables, data.notes),
    })


@app.post("/sessions/{session_id}/delete")
async def sessions_delete(
    session_id: str,
    user: "auth.User" = Depends(auth.current_user),
) -> JSONResponse:
    """Forget a saved session. Deleting locally must delete the cloud copy too, or
    "deleted" would be a lie the next device exposes."""
    with session_scope() as db:
        return JSONResponse({"status": "ok", "deleted": cloudsessions.delete(db, user.id, session_id)})


@app.get("/limits")
def limits() -> dict:
    """The limits actually enforced on this deployment, so a client can state them
    instead of guessing.

    Added because the API Platform page displayed "60 requests/min" and a monthly quota
    of 100,000 — neither of which exists here. The real gate is a per-IP request budget
    (`_gate`/`_RATE`) plus an upload size cap; there is NO monthly quota, and saying so
    is more useful than inventing one.

    `requests_per_window` of 0 means unlimited (rate limiting disabled), which callers
    must render as "no limit" rather than as zero requests allowed.
    """
    return {
        "status": "ok",
        "rate_limit": {
            "requests_per_window": config.RATE_LIMIT,
            "window_seconds": config.RATE_WINDOW,
            "scope": "ip",
            "enabled": config.RATE_LIMIT > 0,
        },
        "upload": {"max_mb": config.MAX_UPLOAD_MB},
        # Stated explicitly so a UI never has to infer it from a missing field.
        "monthly_quota": None,
    }


@app.get("/operations/compute-mode")
def operations_compute_mode() -> dict:
    """The formula-vs-computed-value rule, per operation (Track 4 item 3).

    A live formula recalculates when the user edits the sheet; a computed value is frozen
    at the moment it ran. Both are legitimate — being unclear about which is not, because
    someone who assumes a total recalculates when it doesn't will ship a wrong number.
    """
    return {
        "status": "ok",
        "formula_operations": {
            "add_formula_column": compute_mode.declared_mode("add_formula_column"),
            "lookup": compute_mode.declared_mode("lookup"),
            "pivot_summary": compute_mode.declared_mode("pivot_summary"),
        },
        "default": compute_mode.VALUES,
        "settings": {
            "lookup_style": config.LOOKUP_STYLE,
            "pivot_style": config.PIVOT_STYLE,
        },
        "note": (
            "Everything not listed writes computed values. pivot_summary depends on "
            "SUMIO_PIVOT_STYLE; lookup's formula flavour depends on SUMIO_LOOKUP_STYLE. "
            "Every run also reports what it actually did in its `computation` field."
        ),
    }


@app.get("/metrics/usage")
def metrics_usage(limit: int = 1000) -> dict:
    """Where users struggle (Track 5 item 4) — aggregated from the operation log.

    Reports understanding and execution success SEPARATELY (a low first number is a
    prompt problem, a low second one is an engine problem), retry rate, operation usage,
    grouped failure reasons, and time-to-result as median/p95 rather than a mean.
    """
    return {"status": "ok", "metrics": metrics.summary(limit=limit)}


@app.get("/debug/oplog")
def debug_oplog(limit: int = 100, run_id: str = "", phase: str = "") -> dict:
    """Recent Operation Plans, executions and outcomes (Track 4 item 5).

    Pass `run_id` (returned on every successful /execute) to see one run's plan and
    outcome together — the view that actually answers a bug report. Records what the
    system DID: actions, column names, row deltas, durations. Never cell values, and the
    instruction is stored PII-redacted.
    """
    if run_id:
        return {"run_id": run_id, "events": oplog.run(run_id)}
    return {"events": oplog.events(limit=limit, phase=phase or None)}


@app.get("/brain/version")
def brain_version() -> dict:
    """Which prompt + model the Brain is currently running (Track 3 item 7).

    Record this alongside any battery run: a pass-rate is only comparable to another
    pass-rate produced by the same prompt. `prompt_fingerprint` is computed from the
    prompt text, so it stays truthful even if PROMPT_VERSION wasn't bumped.
    """
    return llm.prompt_identity()


def _abbrev(x: float) -> str:
    """Compact human number: 4,820,000 -> 4.82M, 18204 -> 18.2K."""
    ax = abs(x)
    if ax >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if ax >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{int(x)}" if x == int(x) else f"{x:.2f}"


def _format_number(x: float, fmt: str | None) -> str:
    if fmt == "percent":
        return f"{x:.1f}%"
    if fmt == "currency":
        return "₹" + _abbrev(x)
    if abs(x) >= 10_000:
        return _abbrev(x)
    return f"{int(x):,}" if x == int(x) else f"{x:,.2f}"


def _compute_kpi_explained(df: pd.DataFrame, metric: dict) -> tuple[str | None, str]:
    """A KPI value PLUS a plain description of what it was computed from.

    The basis exists because a number with the wrong label is more misleading than no
    number. `agg="count"` ignores `column` entirely and returns the row count, so a block
    titled "Orders" pointed at a student shortlist rendered "548" — a true row count
    presented as a sales figure. Returning "count of rows" alongside it lets the caller
    show the user what they are actually looking at.
    """
    value = _compute_kpi(df, metric)
    if value is None:
        return None, ""
    agg = (metric.get("agg") or "").lower()
    col = metric.get("column")
    if agg == "count":
        return value, "count of rows"
    if agg == "count_distinct" and col:
        return value, f"distinct values in {col}"
    names = {"sum": "sum of", "mean": "average of", "average": "average of",
             "min": "lowest", "max": "highest"}
    if agg in names and col:
        return value, f"{names[agg]} {col}"
    return value, (f"from {col}" if col else "")


def _kpi_reason(df: pd.DataFrame, metric: dict) -> str:
    """Why a KPI could not be computed — specific enough to act on."""
    col = metric.get("column")
    agg = (metric.get("agg") or "").lower()
    if not agg:
        return "no way to measure this from the columns in this file"
    if col and col not in df.columns:
        return f"there is no '{col}' column in this file"
    if col:
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            return f"'{col}' has no numeric values to {agg}"
    return "this file has no column that fits this metric"


def _compute_kpi(df: pd.DataFrame, metric: dict) -> str | None:
    """Compute a single KPI value from the data, formatted for display."""
    agg = metric.get("agg")
    col = metric.get("column")
    fmt = metric.get("format")
    try:
        if agg == "count":
            return _format_number(float(len(df)), fmt or "number")
        if not col or col not in df.columns:
            return None
        if agg == "count_distinct":
            return _format_number(float(df[col].nunique()), fmt or "number")
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            return None
        if agg == "sum":
            val = float(series.sum())
        elif agg in ("mean", "average"):
            val = float(series.mean())
        elif agg == "min":
            val = float(series.min())
        elif agg == "max":
            val = float(series.max())
        else:
            return None
        return _format_number(val, fmt)
    except Exception:
        return None


def _compute_series(df: pd.DataFrame, metric: dict, top: int = 8) -> list[float]:
    """Compute a numeric series (an aggregate per group) for a chart."""
    agg = metric.get("agg")
    col = metric.get("column")
    gb = metric.get("group_by")
    try:
        if not gb or gb not in df.columns:
            return []
        if agg == "count" or not col or col not in df.columns:
            grouped = df.groupby(gb).size()
        else:
            vals = pd.to_numeric(df[col], errors="coerce")
            tmp = pd.DataFrame({"_g": df[gb].values, "_v": vals.values}).dropna(subset=["_v"])
            g = tmp.groupby("_g")["_v"]
            grouped = {
                "sum": g.sum, "mean": g.mean, "average": g.mean, "min": g.min, "max": g.max,
            }.get(agg, g.sum)()
        grouped = grouped.sort_values(ascending=False).head(top)
        return [round(float(v), 2) for v in grouped.tolist()]
    except Exception:
        return []


def _compute_table(df: pd.DataFrame, metric: dict, top: int = 10) -> dict | None:
    """Compute a small aggregated table (group_by + aggregate) for a report block."""
    agg = metric.get("agg")
    col = metric.get("column")
    gb = metric.get("group_by")
    fmt = metric.get("format")
    try:
        if not gb or gb not in df.columns:
            return None
        if agg == "count" or not col or col not in df.columns:
            grouped = df.groupby(gb).size()
            value_label = "Count"
        else:
            vals = pd.to_numeric(df[col], errors="coerce")
            tmp = pd.DataFrame({"_g": df[gb].values, "_v": vals.values}).dropna(subset=["_v"])
            g = tmp.groupby("_g")["_v"]
            grouped = {
                "sum": g.sum, "mean": g.mean, "average": g.mean, "min": g.min, "max": g.max,
            }.get(agg, g.sum)()
            value_label = f"{str(agg).title()} {col}"
        grouped = grouped.sort_values(ascending=False).head(top)
        rows = [[str(idx), _format_number(float(v), fmt)] for idx, v in grouped.items()]
        return {"columns": [str(gb), value_label], "rows": rows}
    except Exception:
        return None


@app.post("/dashboard")
async def dashboard(
    prompt: str = Form(...),
    columns: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    """Design a dashboard (a set of widgets) from a plain-language prompt. The Brain
    chooses the widgets + a metric (agg + column) for each; if a DATA FILE is provided,
    trusted code then computes the REAL numbers from it. On model failure the frontend
    falls back to a local template, so we return a clear error here."""
    prompt = (prompt or "").strip()
    if not prompt:
        return _error("Please describe the dashboard you'd like.", status=400)

    # With a data file we compute real numbers; otherwise use the columns hint only.
    df: pd.DataFrame | None = None
    if files:
        too_big = _too_big(files)
        if too_big:
            return _error(too_big, status=413)
        uploads = [(f.filename or "upload", await f.read()) for f in files]
        too_big = _too_big_read(uploads)  # real bytes; the declared size can be absent
        if too_big:
            return _error(too_big, status=413)
        try:
            data = load_files(uploads)
        except ValueError as exc:
            return _error(str(exc), status=400)
        except Exception:
            return _error(_INTERNAL_ERROR, status=500)
        df = data.tables[data.primary]
        structure = summarize_tables(data.tables, data.primary)
        # PII shield (3.9): never send sensitive sample values to the dashboard model.
        structure, _ = pii.redact_structure(structure, pii.scan_tables(data.tables))
    else:
        # `columns` is an optional JSON array like [{"name": "...", "type": "..."}].
        try:
            structure = json.loads(columns) if columns.strip() else {}
        except Exception:
            structure = {}

    try:
        spec = llm.generate_dashboard(prompt, structure)
    except Exception as exc:
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()
        if unavailable:
            return _error(str(exc), status=503)
        if key_missing:
            return _error(str(exc), status=500)
        return _error(
            "I couldn't reach the AI service to build the dashboard right now — "
            "please try again in a moment.",
            status=502,
        )

    # Fill in REAL numbers from the data wherever the model gave a metric.
    if df is not None:
        for w in spec.get("widgets", []):
            metric = w.get("metric")
            if not metric:
                continue
            if w.get("type") == "kpi":
                val = _compute_kpi(df, metric)
                if val is not None:
                    w["value"] = val
                    w["delta"] = None  # real value — no fabricated change
            elif w.get("type") == "chart" and metric.get("group_by"):
                series = _compute_series(df, metric)
                if series:
                    w["data"] = series
        spec["computed"] = True

    return JSONResponse({"status": "ok", **spec})


def _safe_filename(name: str) -> str:
    """A safe download filename stem from a report title."""
    return re.sub(r"[^\w\-]+", "_", (name or "").strip()).strip("_")[:40] or "report"


def _build_report_xlsx(title: str, source: str, blocks: list[dict]) -> bytes:
    """Render a report definition (title + ordered blocks) into a formatted .xlsx."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=16)
    meta = [f"Source: {source}"] if source else []
    meta.append(time.strftime("%d %b %Y"))
    ws["A2"] = " · ".join(meta)
    ws["A2"].font = Font(italic=True, color="888888")

    row = 4
    for b in blocks:
        bt = b.get("type")
        btitle = (b.get("title") or "").strip()
        if bt == "kpi":
            ws.cell(row=row, column=1, value=btitle or "Metric").font = Font(bold=True)
            ws.cell(row=row, column=2, value=b.get("value") or "")
            if b.get("delta"):
                ws.cell(row=row, column=3, value=b.get("delta"))
            row += 1
        elif bt == "narrative":
            ws.cell(row=row, column=1, value=btitle or "Narrative").font = Font(bold=True)
            row += 1
            ws.cell(row=row, column=1, value=b.get("text") or "")
            row += 2
        elif bt == "chart":
            ws.cell(row=row, column=1, value=btitle or "Chart").font = Font(bold=True)
            ws.cell(row=row, column=2, value=f"[{b.get('chartType') or 'chart'} chart]")
            row += 2
        elif bt == "table":
            ws.cell(row=row, column=1, value=btitle or "Table").font = Font(bold=True)
            row += 1
            cols = b.get("columns") or []
            for ci, col in enumerate(cols, start=1):
                ws.cell(row=row, column=ci, value=col).font = Font(bold=True)
            if cols:
                row += 1
            for r in b.get("rows") or []:
                for ci, val in enumerate(r, start=1):
                    ws.cell(row=row, column=ci, value=val)
                row += 1
            row += 1
        else:
            row += 1

    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 14
    _disarm_injection(ws)  # the report text is user-influenced — neutralize "=" cells
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@app.post("/report/export")
async def report_export(report: str = Form(...)) -> JSONResponse:
    """Build a formatted .xlsx from a report definition (JSON: title, source, blocks)
    and return it via the same download mechanism as processed files."""
    try:
        data = json.loads(report)
    except Exception:
        return _error("That report couldn't be read — please try again.", status=400)

    title = (data.get("title") or "Report").strip() or "Report"
    source = (data.get("source") or "").strip()
    blocks = data.get("blocks") or []
    try:
        out_bytes = _build_report_xlsx(title, source, blocks)
    except Exception:
        traceback.print_exc()
        return _error(_INTERNAL_ERROR, status=500)

    filename = f"{_safe_filename(title)}.xlsx"
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    download_id = _store_result(out_bytes, filename, media)
    inline = (
        base64.b64encode(out_bytes).decode("ascii")
        if len(out_bytes) <= _INLINE_MAX_BYTES else None
    )
    return JSONResponse({
        "status": "ok",
        "filename": filename,
        "media_type": media,
        "download_id": download_id,
        "file_base64": inline,
    })


@app.post("/report/auto")
async def report_auto(files: list[UploadFile] = File(...)) -> JSONResponse:
    """Generate a COMPLETE report from an uploaded file, with no template and no model call.

    /report/compute starts from blocks someone chose in advance and asks the Brain to map
    them onto the data, which fails whenever the file is not shaped the way the template
    assumed — a recruitment shortlist run through a sales template produced blank revenue
    blocks and an empty PDF. This starts from the DATA instead: it reads what is actually
    there and decides what the report should be.

    Deliberately model-free. Deciding that a file has 548 rows across 12 branches is a
    data question, not a language one, and the free tier's daily cap should not be able to
    stop someone getting a report. See app/autoreport.py.
    """
    if not files:
        return _error("Please choose a data file to report on.", status=400)
    too_big = _too_big(files)
    if too_big:
        return _error(too_big, status=413)
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    too_big = _too_big_read(uploads)
    if too_big:
        return _error(too_big, status=413)
    try:
        data = load_files(uploads)
    except ValueError as exc:
        return _error(str(exc), status=400)
    except llm.ModelUnavailableError as exc:
        # Only reachable for a scanned image/PDF, where OCR genuinely needs the model.
        return _error(str(exc), status=503)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    primary = data.primary
    df = data.tables.get(primary)
    if df is None or not len(df):
        return _error("That file has no data rows to report on.", status=400)

    source = uploads[0][0]
    blocks = autoreport.build(df, source=source, sheet=str(primary))
    for i, b in enumerate(blocks):
        b["id"] = f"auto-{i}"

    # Other sheets are named rather than silently ignored, so nobody assumes the report
    # covered a sheet it didn't.
    others = [str(t) for t in data.tables if t != primary]
    return JSONResponse({
        "status": "ok",
        "title": str(primary),
        "source": source,
        "blocks": blocks,
        "other_sheets": others,
    })


@app.post("/report/compute")
async def report_compute(
    blocks: str = Form(...),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    """Bind a report's blocks to a data file: the Brain assigns a metric per block, then
    trusted code computes real KPI values, chart series, and table rows from the file."""
    try:
        block_list = json.loads(blocks)
    except Exception:
        return _error("That report couldn't be read — please try again.", status=400)
    if not isinstance(block_list, list) or not block_list:
        return _error("This report has no blocks to compute.", status=400)
    if not files:
        return _error("Please choose a data file to compute from.", status=400)

    too_big = _too_big(files)
    if too_big:
        return _error(too_big, status=413)
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    too_big = _too_big_read(uploads)  # real bytes; the declared size can be absent
    if too_big:
        return _error(too_big, status=413)
    try:
        data = load_files(uploads)
    except ValueError as exc:
        return _error(str(exc), status=400)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)
    df = data.tables[data.primary]
    structure = summarize_tables(data.tables, data.primary)
    # PII shield (3.9): never send sensitive sample values to the report model.
    structure, _ = pii.redact_structure(structure, pii.scan_tables(data.tables))

    try:
        plan = llm.assign_report_metrics(block_list, structure)
    except Exception as exc:
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()
        if unavailable:
            return _error(str(exc), status=503)
        if key_missing:
            return _error(str(exc), status=500)
        return _error(
            "I couldn't reach the AI service to compute the report — please try again.",
            status=502,
        )

    metrics = {
        it["index"]: it.get("metric")
        for it in plan.get("items", [])
        if isinstance(it.get("index"), int) and it.get("metric")
    }
    # Blocks the data genuinely cannot fill. Reported back rather than left blank: a
    # report that silently comes out empty looks broken, and the user has no way to learn
    # that the template simply didn't suit their file. Real case that prompted this: a
    # campus-recruitment shortlist run through "Executive Summary" produced a blank Total
    # Revenue, a blank Avg Order Value, and empty charts, with nothing saying why.
    unfilled: list[dict] = []

    def _cant(index: int, block: dict, reason: str) -> None:
        unfilled.append({
            "index": index,
            "title": block.get("title") or block.get("type") or "Block",
            "reason": reason,
        })

    for i, b in enumerate(block_list):
        metric = metrics.get(i)
        t = b.get("type")
        if t == "narrative":
            continue  # prose the user writes; nothing to compute
        if not metric:
            _cant(i, b, "no column in this file matches what this block measures")
            continue
        if t == "kpi":
            v, basis = _compute_kpi_explained(df, metric)
            if v is not None:
                b["value"] = v
                b["delta"] = None
                # WHAT THE NUMBER ACTUALLY IS. Without this, a bare count rendered under a
                # business label reads as that business metric: this endpoint returned
                # "Orders 548" for a file of 548 STUDENTS, because the model mapped the
                # block to a row count and nothing said so. The figure was real; the label
                # was not. Naming the basis makes a mismatch visible instead of plausible.
                b["basis"] = basis
            else:
                _cant(i, b, _kpi_reason(df, metric))
        elif t == "chart":
            s = _compute_series(df, metric) if metric.get("group_by") else []
            if s:
                b["data"] = s
            else:
                _cant(i, b, "needs a column to group by and a number to plot")
        elif t == "table":
            tbl = _compute_table(df, metric) if metric.get("group_by") else None
            if tbl:
                b["columns"] = tbl["columns"]
                b["rows"] = tbl["rows"]
            else:
                _cant(i, b, "needs a column to group rows by")

    return JSONResponse({
        "status": "ok",
        "blocks": block_list,
        # The caller can now say "4 of 5 blocks couldn't be filled from this file, because…"
        "unfilled": unfilled,
        "filled": sum(1 for i, b in enumerate(block_list)
                      if b.get("type") != "narrative"
                      and not any(u["index"] == i for u in unfilled)),
    })


@app.api_route("/download/{result_id}", methods=["GET", "HEAD"])
def download(result_id: str):
    """Stream a generated result file, from disk when it's there and from the database
    when it isn't.

    The disk path is the fast one: FileResponse streams it, so a large file never sits in
    this process's memory. But the disk here does NOT survive a restart (nor does the
    in-memory index, nor index.json) — which is why a link that worked yesterday used to
    404 today. The database fallback below is what makes the link keep working; the disk
    copy is rebuilt on the way out so the next request is fast again.

    HEAD is supported so the frontend can check a file still exists before downloading."""
    meta = _RESULTS.get(result_id)
    path = _RESULTS_DIR / result_id
    if meta and path.exists():
        return FileResponse(path, media_type=meta["media_type"], filename=meta["filename"])

    stored = resultstore.load(result_id)
    if stored is None:
        return _error("That download has expired — please re-run the step.", status=404)
    blob, filename, media_type = stored
    try:
        # Rehydrate the cache, and the index the metadata came from, so this costs the
        # database read only once.
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        _RESULTS[result_id] = {
            "filename": filename, "media_type": media_type,
            "size": len(blob), "created": time.time(),
        }
        _save_results_index()
        return FileResponse(path, media_type=media_type, filename=filename)
    except Exception:
        # Couldn't write the cache (read-only disk, full disk) — still serve the bytes.
        return Response(content=blob, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })


@app.post("/undo")
async def undo(session_id: str = Form(...)) -> JSONResponse:
    """Undo the last successful step: move the most recent state onto the session's
    redo stack (so /redo can restore it) and build on the step before it."""
    entry = _SESSIONS.get(session_id)
    states = entry["states"] if entry else []
    if len(states) <= 1:  # states[0] is the original upload — nothing to undo
        return _error("There's nothing to undo yet.", status=400)
    entry.setdefault("redo", []).append(states.pop())  # keep it for redo
    cur = states[-1]
    biggest = max((len(t) for t in cur["tables"].values()), default=0)
    return JSONResponse({
        "status": "ok",
        "steps_remaining": len(states) - 1,  # not counting the original upload
        "row_count": biggest,
        "primary": cur["primary"],
        "label": cur.get("label", "Uploaded"),  # where we landed (Phase 3.2)
        "can_undo": len(states) > 1,
        "can_redo": True,  # we just put one step on the redo stack
    })


@app.post("/redo")
async def redo(session_id: str = Form(...)) -> JSONResponse:
    """Redo a step that was undone with /undo. The redo branch is discarded the moment
    a NEW operation runs (standard undo/redo), so this only works right after undo(s)."""
    entry = _SESSIONS.get(session_id)
    redo_stack = entry.get("redo") if entry else None
    if not redo_stack:
        return _error("There's nothing to redo.", status=400)
    entry["states"].append(redo_stack.pop())
    cur = entry["states"][-1]
    biggest = max((len(t) for t in cur["tables"].values()), default=0)
    return JSONResponse({
        "status": "ok",
        "steps_remaining": len(entry["states"]) - 1,
        "row_count": biggest,
        "primary": cur["primary"],
        "label": cur.get("label", "Uploaded"),  # where we landed (Phase 3.2)
        "can_undo": True,  # we just appended a step
        "can_redo": len(redo_stack) > 0,
    })


@app.post("/history")
async def history(session_id: str = Form(...)) -> JSONResponse:
    """Phase 3.2 — the LABELED version history: every step so far, what it did, and how
    many rows it left, with the current version marked. Powers a restore/redo timeline."""
    entry = _SESSIONS.get(session_id)
    if not entry:
        return _error("No history yet — upload a file to start.", status=400)
    states = entry["states"]
    versions = [
        {
            "index": i,
            "label": st.get("label", "Uploaded" if i == 0 else "Changed the data"),
            "row_count": max((len(t) for t in st["tables"].values()), default=0),
            "current": i == len(states) - 1,
        }
        for i, st in enumerate(states)
    ]
    return JSONResponse({
        "status": "ok",
        "versions": versions,
        "current_index": len(states) - 1,
        "can_undo": len(states) > 1,
        "can_redo": len(entry.get("redo") or []) > 0,
    })


@app.post("/compare-versions")
async def compare_versions(
    session_id: str = Form(...),
    from_index: int = Form(0),
    to_index: int = Form(-1),
) -> JSONResponse:
    """Phase 3.2 (ties to 2.9) — what changed BETWEEN two versions of this session's data.
    Defaults to the original upload (0) vs the current version (-1). Diffs the working
    table via the trusted compare engine, so every reported change is real."""
    from .operations.compare import compare_tables

    entry = _SESSIONS.get(session_id)
    states = entry["states"] if entry else []
    if not states:
        return _error("No history to compare — upload a file first.", status=400)
    n = len(states)
    fi = from_index if from_index >= 0 else n + from_index
    ti = to_index if to_index >= 0 else n + to_index
    if not (0 <= fi < n) or not (0 <= ti < n):
        return _error(f"This session has {n} version(s) (0…{n - 1}) — pick two of them.", status=400)
    if fi == ti:
        return _error("Those are the same version — pick two different ones.", status=400)
    a, b = states[fi], states[ti]
    try:
        diff, note = compare_tables(
            a["tables"][a["primary"]], b["tables"][b["primary"]],
            a.get("label", f"version {fi}"), b.get("label", f"version {ti}"),
        )
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)
    return JSONResponse({
        "status": "ok",
        "from": {"index": fi, "label": a.get("label", f"version {fi}")},
        "to": {"index": ti, "label": b.get("label", f"version {ti}")},
        "note": note,
        "differences": diff.to_dict("records"),
    })


@app.post("/inspect")
async def inspect(
    files: list[UploadFile] = File(...),
    session_id: str = Form(""),
    mode: str = Form("replace"),
) -> JSONResponse:
    """Read uploaded file(s) and return their structure (sheets, columns + types,
    row count, sample rows) so the UI can show a preview BEFORE any operation.

    If a `session_id` is given, the loaded data is ALSO remembered for that session
    so the two-phase flow (/parse then /execute) can reuse it without re-uploading.

    `mode` decides what happens to work already done in that session:
      "replace" (default)  start over — the new file becomes the session, history reset.
      "add"                bring the new file ALONGSIDE the current data, keeping every
                           step already applied. Uploading a price list to look values up
                           from should not throw away an hour of work, which is what
                           replace-only behaviour did.

    "add" pushes a NEW STATE rather than rewriting the current one, so Undo removes the
    added file and puts the session back exactly as it was — the same model every other
    operation follows.
    """
    if not files:
        return _error("Please upload a spreadsheet.", status=400)
    too_big = _too_big(files)
    if too_big:
        return _error(too_big, status=413)
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    too_big = _too_big_read(uploads)  # real bytes; the declared size can be absent
    if too_big:
        return _error(too_big, status=413)
    try:
        data = load_files(uploads)
    except ValueError as exc:
        return _error(str(exc), status=400)
    except llm.ModelUnavailableError as exc:
        # An image or scanned PDF needs OCR, which needs the model. When that is
        # rate-limited or down, this is NOT our server failing — and reader.py passes the
        # error up untouched precisely so the caller can say so. Without this branch it
        # fell into the generic handler below and returned 500 "something went wrong on
        # our side", blaming us for a queue the user only has to wait out. Every other
        # model-calling endpoint already translated this to a 503; /inspect was the gap.
        return _error(str(exc), status=503)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    # Remember the upload for the session so /parse + /execute can use it.
    added_to_existing = False
    if session_id:
        entry = _SESSIONS.get(session_id)
        wants_add = str(mode).strip().lower() == "add"
        if wants_add and entry and entry.get("states"):
            # Keep everything already done: start from the CURRENT state and add the new
            # tables beside it. A name clash gets a numeric suffix rather than silently
            # overwriting a table the user is working on.
            base = entry["states"][-1]
            tables_now = dict(base["tables"])
            exts_now = dict(base.get("exts") or {})
            notes_now = dict(base.get("notes") or {})
            for name, df in data.tables.items():
                unique = name
                n = 2
                while unique in tables_now:
                    unique = f"{name} ({n})"
                    n += 1
                tables_now[unique] = df
                exts_now[unique] = data.exts.get(name, "xlsx")
                if data.notes.get(name):
                    notes_now[unique] = data.notes[name]
            _push_state(session_id, {
                "tables": tables_now,
                # The working table stays what it was: adding a reference sheet must not
                # silently redirect the next instruction onto the new file.
                "primary": base["primary"],
                "exts": exts_now,
                "notes": notes_now,
                "label": f"Added {files[0].filename or 'file'}",
            })
            added_to_existing = True
        else:
            _remember_session(
                session_id,
                {
                    "tables": dict(data.tables),
                    "primary": data.primary,
                    "exts": dict(data.exts),
                    "notes": dict(data.notes),
                },
            )

    # After an "add", report the WHOLE session (existing tables + the new ones) so the UI
    # shows the user everything they can now work with, not just the file they dropped.
    if added_to_existing:
        latest = _SESSIONS[session_id]["states"][-1]
        preview_tables = latest["tables"]
        preview_notes = latest.get("notes") or {}
    else:
        preview_tables = data.tables
        preview_notes = data.notes

    tables = _preview_payload(preview_tables, preview_notes)
    return JSONResponse({
        "status": "ok",
        "tables": tables,
        # True when this upload joined an existing session instead of replacing it, so
        # the UI can say "added" rather than implying a fresh start.
        "added": added_to_existing,
        "added_tables": list(data.tables) if added_to_existing else [],
    })


def _preview_payload(tables: dict, notes: dict | None = None) -> list[dict]:
    """The table preview the UI renders: columns + types, row count, 5 sample rows.

    Extracted from /inspect so a session RESTORED from cloud storage is described exactly
    the same way as one that was just uploaded — two hand-written copies of this would
    drift, and the UI would show subtly different things depending on how the data arrived.
    """
    notes = notes or {}
    out = []
    for name, df in tables.items():
        s = summarize_structure(df, sample_rows=5)
        rc = s["row_count"]
        note_parts = []
        ocr_note = notes.get(name, "")
        if ocr_note:
            note_parts.append(ocr_note)
        if rc == 0:
            note_parts.append("This sheet has no data rows.")
        elif rc > 50_000:
            note_parts.append(f"Large file ({rc:,} rows) — preview shows the first 5 rows.")
        out.append(
            {
                "name": name,
                "row_count": rc,
                "columns": s["columns"],
                "sample_rows": s["sample_rows"],
                "note": " ".join(note_parts) if note_parts else None,
            }
        )
    return out


def _shield_columns(shielded: list[str]) -> list[str]:
    """Distinct column names from "table::column" markers, for the UI's shield notice."""
    return list(dict.fromkeys(c.split("::", 1)[-1] for c in shielded))


def _describe_op(op: dict) -> str:
    """A deterministic plain-language phrase for ONE operation."""
    a = op.get("action")
    cols = ", ".join(op.get("columns") or [])
    if a == "sort":
        order = (op.get("orders") or ["asc"])[0]
        return (
            f"sort by {cols or 'the chosen column'} "
            f"({'high to low' if order == 'desc' else 'low to high'})"
        )
    if a == "filter":
        return "keep only the rows matching your condition"
    if a == "limit":
        return f"keep the {'last' if op.get('from_end') else 'top'} {op.get('count') or 'N'} rows"
    if a == "remove_duplicates":
        return "remove duplicate rows" + (f" on {cols}" if cols else "")
    if a == "add_formula_column":
        return f"add a '{op.get('name') or 'new'}' column"
    if a == "aggregate":
        return f"{op.get('agg_func') or 'aggregate'} {op.get('agg_column') or ''}".strip()
    if a == "lookup":
        return f"look up {op.get('return_column') or 'a value'} from another table"
    if a == "merge":
        return "merge the tables"
    if a == "forecast":
        n = op.get("count") or 3
        unit = op.get("period_unit") or "period"
        return f"forecast {cols or 'the values'} for the next {n} {unit}{'s' if n != 1 else ''}"
    if a == "what_if":
        return f"simulate a what-if scenario on {op.get('column') or 'the chosen column'}"
    if a == "detect_anomalies":
        return f"flag anomalies in {cols or 'the numeric columns'}"
    if a in ("fill_missing", "drop_missing", "drop_invalid", "flag_missing"):
        return a.replace("_", " ") + (f" in {cols}" if cols else "")
    return (a or "operation").replace("_", " ")


# Op keys that REFERENCE existing columns (validated by _missing_columns). Keys that
# CREATE columns ("name", "new_column") are deliberately absent — those may be anything.
_COLUMN_REF_KEYS = (
    "column", "columns", "group_by", "agg_column",
    "key_column", "source_key_column", "return_column", "value_column",
    "pivot_column", "index_columns",
)


def _missing_columns(operations: list[dict], tables: dict) -> list[str]:
    """PRD 1.3 / Definition-of-Done plan validation: every column an operation READS
    must exist somewhere in the workbook. Checked case-insensitively (space-trimmed)
    against the UNION of all sheets' columns, so cross-sheet ops (lookup) never
    false-positive. Returns referenced names found in NO sheet — a Brain planning on a
    phantom column is exactly the confident-but-wrong failure the PRD forbids, and
    weaker models do it even where stronger ones correctly ask."""
    known = {str(c).strip().lower() for df in tables.values() for c in df.columns}
    # Names the PLAN ITSELF creates (a formula column added in step 1 may be sorted in
    # step 2; a named range declared early is used later) count as known — otherwise a
    # perfectly good chained plan gets a spurious clarify.
    for op in operations:
        for key in ("name", "new_column", "new_name", "range_name"):
            v = op.get(key)
            if isinstance(v, str) and v.strip():
                known.add(v.strip().lower())
        for key in ("new_columns", "rename_to"):
            for v in op.get(key) or []:
                if isinstance(v, str) and v.strip():
                    known.add(v.strip().lower())
    known_list = sorted({str(c).strip() for df in tables.values() for c in df.columns})
    missing: list[str] = []

    def flag(name: str) -> None:
        if name not in missing:
            missing.append(name)

    for op in operations:
        for key in _COLUMN_REF_KEYS:
            val = op.get(key)
            names = val if isinstance(val, list) else [val] if val else []
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    continue
                if name.strip().lower() not in known:
                    flag(name)
        # Phase-1.1 formula templates: {Col} / {Col:} / {Sheet.Col:}. Only flag refs the
        # executor's self-correction could NOT repair (no close match anywhere) — typos
        # with an obvious fix are auto-repaired downstream, and that's a feature.
        for inner in re.findall(r"\{([^{}]+)\}", op.get("formula") or ""):
            text = inner.strip()
            if text.lower() == "var":  # Goal Seek's reserved unknown, not a column
                continue
            is_range = text.endswith(":")
            if is_range:
                text = text[:-1].strip()
            if is_range and "." in text:
                sheet, col = (p.strip() for p in text.split(".", 1))
                sheet_df = next((d for n, d in tables.items() if n == sheet), None)
                if sheet_df is None or col not in {str(c).strip() for c in sheet_df.columns}:
                    flag(f"{sheet}.{col}")
                continue
            if text.strip().lower() in known:
                continue
            if not difflib.get_close_matches(text, known_list, n=1, cutoff=0.6):
                flag(text)
    return missing


def _missing_columns_clarification(missing: list[str], tables: dict) -> str:
    """A helpful clarify message: name what wasn't found, suggest the nearest match."""
    all_cols = sorted({str(c).strip() for df in tables.values() for c in df.columns})
    parts = []
    for name in missing[:3]:
        close = difflib.get_close_matches(name, all_cols, n=1, cutoff=0.6)
        parts.append(f"'{name}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
    cols_note = ", ".join(all_cols[:10]) + ("…" if len(all_cols) > 10 else "")
    return (
        f"I couldn't find a column called {' or '.join(parts)} in your file. "
        f"Available columns: {cols_note}. Which one should I use?"
    )


# Above this many total rows we skip relationship detection when building the Brain's
# context. kg._valset normalizes EVERY value of a column (no sampling), so on a big
# multi-sheet workbook that is a full scan per column per table on every /parse call.
# Detecting on a sample instead would make `coverage` approximate, and an approximate
# foreign key is a hint that can be WRONG — so we omit the section entirely rather than
# hand the Brain something it might act on. Silence is honest; a bad hint is not.
_REL_MAX_ROWS = 50_000


def _brain_structure(tables: dict, primary: str) -> dict:
    """The structure sent to the Brain: reader.summarize_tables (column names, inferred
    types, sample rows, row counts, primary table) PLUS detected sheet relationships.

    Relationships are the foreign keys a workbook has but never declares. Without them
    the Brain sees several tables and no idea how they connect, so a cross-sheet request
    ("bring in each sale's customer email") has to be guessed at. kg.relationships only
    asserts a link when the values genuinely line up, so anything listed here is real.

    Note this enriches the Brain's INPUT context, not the response schema — no schema
    cliff risk.
    """
    structure = summarize_tables(tables, primary)
    if len(tables) < 2:
        return structure  # a single sheet has nothing to relate to
    if scale.total_rows(tables) > _REL_MAX_ROWS:
        return structure
    try:
        rels = kg.relationships(tables)
    except Exception:
        # Context enrichment must never break a request that would otherwise work.
        return structure
    if rels:
        structure["relationships"] = [
            {k: v for k, v in r.items() if k != "name_match"} for r in rels
        ]
    return structure


def _sane_plan(plan: object) -> dict:
    """Guard against MALFORMED Brain output (PRD 1.3): the parser must hand back a dict
    of {operations?, clarification?, reply?, ...}. Raw text, a list, None, or a plan
    whose operations aren't a list of dicts would crash downstream `.get` calls with a
    500 — normalize all of those to an empty plan, which flows into the standard
    "I didn't understand that" answer instead of a fake result or a traceback."""
    if not isinstance(plan, dict):
        return {}
    ops = plan.get("operations")
    if ops is not None and (
        not isinstance(ops, list) or any(not isinstance(op, dict) for op in ops)
    ):
        plan = dict(plan)
        plan["operations"] = []
    return plan


def _describe_plan(operations: list[dict]) -> str:
    """A deterministic one-line plain-language restatement of a plan, used when the
    model didn't provide its own 'translation' (e.g. the offline fallback parser)."""
    parts = [_describe_op(op) for op in operations]
    return ", then ".join(parts) if parts else "run the operation"


def _synthesize_steps(operations: list[dict]) -> list[dict]:
    """Build a reviewable step list from raw operations when the model didn't supply
    one (offline fallback, or an LLM that returned a mismatched/empty 'steps'). Each
    step is {label, rationale} so the UI can show — and later verify — the plan."""
    steps: list[dict] = []
    for op in operations:
        label = _describe_op(op)
        steps.append({"label": label[:1].upper() + label[1:], "rationale": None})
    return steps


@app.post("/parse")
async def parse(
    instruction: str = Form(...),
    session_id: str = Form(""),
    history: str = Form(""),
    team_id: str = Form("default"),
    org_id: str = Form(""),  # Phase 5.2: inherit this org's shared glossary
) -> JSONResponse:
    """Phase 1 of the two-phase flow: the Brain ONLY. Translate the instruction into an
    operation plan WITHOUT executing it, so the UI can preview the interpretation +
    confidence before running. Ambiguous -> clarify; unsupported -> message; the file is
    never touched. /execute then runs the returned plan."""
    instruction = (instruction or "").strip()
    if not instruction:
        return _error("Please describe what you'd like done to the data.", status=400)

    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    tables, primary = base["tables"], base["primary"]
    structure = _brain_structure(tables, primary)
    # PII shield (3.9): mask sensitive sample values + history BEFORE the model sees them.
    structure, shielded = pii.redact_structure(structure, pii.scan_tables(tables))
    history = pii.redact_text(history)
    # Personalization (3.12): prepend the team's learned glossary + preferences to the
    # context so definitions are applied consistently (kept inside `history` so the call
    # signature is unchanged for callers/mocks).
    glossary = personalization.context(team_id, scope=org_id or None)
    context = (glossary + "\n\n" + history).strip() if glossary else history

    # Translate via the Brain (falling back to the deterministic parser if it's down).
    used_fallback = False
    try:
        plan = llm.parse_instruction(instruction, structure, context)
    except Exception as exc:
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()
        used_fallback = True
        plan = fallback.parse(instruction, structure, personalization.effective_definitions(team_id, org_id or None))
        if plan is None:
            if unavailable:
                return _error(str(exc), status=503)
            if key_missing:
                return _error(str(exc), status=500)
            return _error(
                "I couldn't reach the AI service to understand your request right now — "
                "this isn't a problem with your instruction. Please try again in a moment.",
                status=502,
            )
    plan = _sane_plan(plan)

    clarification = plan.get("clarification")
    reply = plan.get("reply")
    operations = plan.get("operations") or []
    # Track 4 item 5: record what the Brain proposed BEFORE anything acts on it — a plan
    # that was never run is exactly what you need when the complaint is "it misunderstood
    # me". `source` distinguishes the real Brain from the offline fallback parser.
    oplog.record_plan(
        oplog.new_run_id(),
        session_id=session_id,
        instruction=instruction,
        operations=operations,
        source="fallback" if used_fallback else "brain",
        status=("plan" if operations else ("clarify" if clarification else "message")),
        confidence=plan.get("confidence") if isinstance(plan.get("confidence"), int) else None,
    )
    if not operations:
        if reply:
            return JSONResponse({"status": "message", "message": reply})
        if clarification:
            return JSONResponse({"status": "clarify", "clarification": clarification})
        return JSONResponse(
            {
                "status": "message",
                "message": (
                    "I didn't understand that — try describing the task, e.g. "
                    '"sort by Revenue descending" or "remove duplicate rows".'
                ),
            }
        )

    # Personalization (3.12): fill the team's formatting defaults into the plan.
    operations = personalization.apply_preferences(operations, personalization.preferences(team_id))

    # Plan validation (PRD 1.3): a plan that reads a column no sheet has is confidently
    # wrong — turn it into a clarifying question instead of previewing a doomed plan.
    phantom = _missing_columns(operations, tables)
    if phantom:
        return JSONResponse(
            {"status": "clarify", "clarification": _missing_columns_clarification(phantom, tables)}
        )

    translation = (plan.get("translation") or "").strip() or _describe_plan(operations)
    confidence = plan.get("confidence")
    if not isinstance(confidence, int) or not (0 <= confidence <= 100):
        confidence = 80

    # Agentic plan (Phase 3.4): every multi-step plan gets a reviewable step list, even
    # when the Brain didn't supply one (offline fallback) or supplied a list whose length
    # doesn't line up with the operations (so each step maps 1:1 to an operation, and the
    # result card can verify each one independently).
    steps = plan.get("steps")
    if (not steps or len(steps) != len(operations)) and len(operations) >= 2:
        steps = _synthesize_steps(operations)

    # Agentic plan review (Phase 4.4): assess the ORDERED plan for destructive steps and
    # surface each step's concrete impact ("removes 1,240 of 5,000 rows") HERE, at review
    # time — so the user judges the whole plan-of-plans before approving it, not only after
    # hitting run. Same guardrails engine /execute uses to gate destructive runs. Best-
    # effort and read-only: it never mutates data and never blocks previewing a plan.
    try:
        review = guardrails.assess(operations, tables, primary, known_formulas=(entry or {}).get("formulas"))
    except Exception:
        review = {"destructive": False, "warnings": [], "summary": ""}
    # Annotate each reviewable step with its impact when steps line up 1:1 with operations
    # (guardrails warnings carry a 1-based step index), so the review card can badge the
    # risky step inline instead of only listing warnings separately.
    if steps and len(steps) == len(operations):
        by_step = {w["step"]: w for w in review.get("warnings", [])}
        annotated = []
        for i, s in enumerate(steps, 1):
            s = dict(s)
            w = by_step.get(i)
            s["impact"] = w["impact"] if w else None
            s["severity"] = w["severity"] if w else None
            annotated.append(s)
        steps = annotated

    return JSONResponse(
        {
            "status": "plan",
            "translation": translation,
            "confidence": confidence,
            # Sensitive columns masked before the model saw them (Phase 3.9).
            "shielded_columns": _shield_columns(shielded),
            # Plan review (Phase 4.4): destructive-step flags + per-step impact, computed
            # against the real data, for the user to weigh BEFORE running. /execute still
            # enforces confirmation independently — this is the informative half.
            "review": review,
            # The full plan the UI hands back to /execute (no second Brain call).
            "plan": {
                "operations": operations,
                "title": (plan.get("title") or "").strip() or None,
                "steps": steps or None,
                "plan_rationale": (plan.get("plan_rationale") or "").strip() or None,
            },
        }
    )


def _run_operations(
    session_id: str, base: dict, operations: list[dict], ai_title, started_at: float,
    progress=None, run_id: str | None = None, retry: bool = False,
) -> JSONResponse:
    """Synchronous wrapper: run the plan and render the HTTP response.

    The body is produced by _run_operations_body so the async job path (Track 4 item 1)
    can reuse the IDENTICAL execution — see that function's docstring."""
    status, body = _run_operations_body(
        session_id, base, operations, ai_title, started_at, progress, run_id, retry
    )
    return JSONResponse(body, status_code=status)


def _run_operations_body(
    session_id: str, base: dict, operations: list[dict], ai_title, started_at: float,
    progress=None, run_id: str | None = None, retry: bool = False,
) -> tuple[int, dict]:
    """Run an operation plan on a base state, push the new state, serialize, and build
    the OK response BODY. Shared shape with /process (deltas, formulas, preview, partial
    warnings, streamed download). Returns a friendly 422 on an expected step failure or
    500 on an unexpected bug. Trusted code runs the plan — the model never executes.

    Returns (http_status, body) rather than a Response so that /execute and /execute/async
    run the same code. The async path needs the plain dict: it stores the body and replays
    it later, and building a Response on a worker thread just to unpack it again would
    mean serializing a multi-MB base64 workbook twice.

    `progress` is an optional jobs.Progress. Every call on it reflects something that
    actually happened — a step the executor reached, or the run entering the serialize
    phase — so nothing here can report movement that didn't occur.
    """
    tables, primary, exts = base["tables"], base["primary"], base["exts"]

    # Track 4 item 5: one run_id ties this execution to its plan and its outcome, so a
    # later "it dropped my rows" can be answered from the recorded row delta + plan shape
    # instead of guesswork. Overlapping requests stay distinguishable. The async path
    # passes its job id in, because a job and its run are the same event under one name.
    run_id = run_id or oplog.new_run_id()
    rows_before = sum(int(len(d)) for d in tables.values())
    oplog.record_plan(
        run_id, session_id=session_id, operations=operations, source="user",
        status="executing", retry=retry,
    )

    partial_warning = None
    completed_steps = len(operations)  # all steps ran unless a later one fails
    failed_step = None
    shield_cols: list[str] = []  # /execute runs a pre-approved plan — no AI call, nothing to shield
    # Result cache (Phase 5.8): the same plan on the same data yields the same output, so a
    # repeat run is a hit that skips the recompute entirely. Only CLEAN successes are cached
    # (a partial failure isn't a stable result). State is still pushed on a hit, so chaining
    # stays correct — the cache only saves the compute, never the bookkeeping.
    cache_hit = False
    _sig = scale.plan_signature(tables, operations)
    _cached = scale.RESULT_CACHE.get(_sig)
    if _cached is not None:
        result, result_name, notes, render_ops = _cached
        cache_hit = True
        if progress is not None:
            # No steps will run, so report the plan complete AND latch the fact that this
            # was memoized — a client should be able to say "reused an identical earlier
            # result" rather than implying the work was redone.
            progress.phase(jobs.PHASE_CACHED)
            progress.steps_done()
    else:
        try:
            result, result_name, notes, render_ops = execute_multi(
                tables, primary, operations,
                on_step=(progress.step if progress is not None else None),
            )
            if progress is not None:
                progress.steps_done()
        except MultiStepError as exc:
            # A later step failed: keep the file reflecting the completed steps (PRD MS-b).
            result, result_name = exc.partial_result, exc.partial_name
            notes, render_ops = exc.notes, exc.format_ops
            done = exc.failed_step - 1
            completed_steps = done
            failed_step = exc.failed_step
            partial_warning = (
                f"Step {exc.failed_step} couldn't be done: {exc.reason} "
                f"Your file reflects the {done} step{'s' if done != 1 else ''} that "
                "completed before it — fix that step and try again."
            )
        except OperationCancelled as exc:
            # Track 4 item 6. Stopped between steps, so NOTHING is pushed to the session:
            # completed steps existed only in memory and are discarded, leaving the user's
            # file exactly as it was. Discarding beats half-applying — a partially applied
            # plan the user didn't ask for and can't see is worse than no change at all.
            oplog.record_outcome(
                run_id, status="timeout", rows_before=rows_before,
                duration_ms=int((time.time() - started_at) * 1000),
                completed_steps=exc.completed_steps, error=str(exc),
            )
            done, total = exc.completed_steps, exc.total_steps or len(operations)
            return 504, {
                "status": "timeout",
                "error": (
                    f"This took longer than the time budget and was stopped after "
                    f"{done} of {total} step{'s' if total != 1 else ''}. Your file is "
                    "unchanged — try a smaller file, or split the request into steps."
                ),
                "completed_steps": done,
                "total_steps": total,
                "run_id": run_id,
            }
        except OperationError as exc:
            oplog.record_outcome(
                run_id, status="error", rows_before=rows_before,
                duration_ms=int((time.time() - started_at) * 1000), error=str(exc),
            )
            return 422, _error_body(str(exc))
        except Exception as exc:
            oplog.record_outcome(
                run_id, status="error", rows_before=rows_before,
                duration_ms=int((time.time() - started_at) * 1000),
                error=f"unexpected {type(exc).__name__}: {exc}",
            )
            return 500, _error_body(_INTERNAL_ERROR)
        else:
            scale.RESULT_CACHE.put(_sig, (result, result_name, notes, render_ops))

    # Push the new state so the next instruction chains on it (and Retry/Edit can branch).
    if session_id:
        if isinstance(result, dict):
            new_state = {
                "tables": {**tables, **result},
                "primary": next(iter(result)),
                "exts": {**exts, **{k: "xlsx" for k in result}},
            }
        else:
            new_state = {
                "tables": {**tables, result_name: result},
                "primary": result_name,
                "exts": {**exts, result_name: exts.get(result_name, "xlsx")},
            }
        new_state["label"] = _step_label(notes)  # labeled version history (Phase 3.2)
        _push_state(session_id, new_state)
        _record_formulas(session_id, operations, result)  # dependency registry (Phase 4.8)

    biggest = max((len(t) for t in tables.values()), default=0)
    if biggest > 50_000:
        notes = [
            f"Heads up: this is a large file (~{biggest:,} rows) — it still processed, "
            "but big files can take a little longer."
        ] + notes

    # Writing the workbook is a real phase, not padding — serializing 120k rows to .xlsx
    # is a large share of the wall clock, and a tracker that froze on "last step done"
    # while this ran would look hung.
    if progress is not None:
        progress.phase(jobs.PHASE_SAVING)
    try:
        if isinstance(result, dict):
            out_bytes, out_name, media_type = _serialize_workbook(result, result_name, primary=result_name, render_ops=render_ops)
            row_count = sum(int(len(d)) for d in result.values())
        else:
            out_ext, upgrade_note = _output_ext(exts.get(result_name, "xlsx"), render_ops)
            if upgrade_note:
                notes = notes + [upgrade_note]
            out_bytes, out_name, media_type = _serialize(result, result_name, out_ext, render_ops)
            row_count = int(len(result))
    except Exception as exc:
        oplog.record_outcome(
            run_id, status="error", rows_before=rows_before,
            duration_ms=int((time.time() - started_at) * 1000),
            error=f"serialize failed: {type(exc).__name__}: {exc}",
        )
        return 500, _error_body(_INTERNAL_ERROR)

    oplog.record_outcome(
        run_id,
        status="partial" if failed_step else "ok",
        rows_before=rows_before,
        rows_after=row_count,
        duration_ms=int((time.time() - started_at) * 1000),
        cached=cache_hit,
        completed_steps=completed_steps,
        failed_step=failed_step,
    )

    download_id = _store_result(out_bytes, out_name, media_type)
    inline_b64 = (
        base64.b64encode(out_bytes).decode("ascii")
        if len(out_bytes) <= _INLINE_MAX_BYTES else None
    )
    return 200, {
            "status": "ok",
            "session_id": session_id,
            "run_id": run_id,  # Track 4 item 5: quote this in a bug report to find the log
            "cached": cache_hit,  # Phase 5.8: this result came from the cache (recompute skipped)
            "explanation": " ".join(notes) if notes else "No changes were needed.",
            "notes": notes,
            "formulas": _describe_formulas(render_ops),
            # Track 4 item 3: for EVERY step, whether the file got a live Excel formula or
            # a computed value — and so whether it recalculates when the user edits the
            # data. Derived from the directives the run really emitted, not from intent.
            "computation": compute_mode.describe(operations, render_ops),
            "code": _explain_code(operations),  # Phase 3.3 "Show Code" — mirrors the executed plan
            # Confidence on forecasts/anomalies (Phase 3.10).
            "analysis": [d for d in render_ops if d.get("type") == "analysis"],
            "row_count": row_count,
            "rows_before": int(len(tables[primary])) if primary in tables else None,
            "preview": _result_preview(result, result_name),
            "insight": _summarize_insight(result, result_name),
            "actions": list(dict.fromkeys(op.get("action") for op in operations)),
            "ai_title": ai_title,
            "partial": partial_warning is not None,
            "warning": partial_warning,
            # Per-step outcome (Phase 3.4): lets the UI render a ✓/✗ checklist mapped
            # to the plan's steps, so each sub-step is independently verifiable.
            "completed_steps": completed_steps,
            "failed_step": failed_step,
            # Sensitive columns hidden from the AI (Phase 3.9).
            "shielded_columns": shield_cols,
            "filename": out_name,
            "media_type": media_type,
            "file_size": len(out_bytes),
            "elapsed_ms": int((time.time() - started_at) * 1000),
            "download_id": download_id,
            "file_base64": inline_b64,
    }


@app.post("/execute")
async def execute(
    session_id: str = Form(...),
    plan: str = Form(...),
    rewind: int = Form(-1),
    guard: str = Form("false"),
    confirm: str = Form("false"),
    user: "auth.User | None" = Depends(auth.current_user_optional),
) -> JSONResponse:
    """Phase 2 of the two-phase flow: the Hands ONLY. Run an already-approved plan
    (from /parse) on the session's data with NO model call. Same result shape as
    /process. The plan's columns/types are validated by the executor before it runs.

    With guard=true (the UI sets this), a destructive plan returns status
    'confirm_required' with a concrete impact instead of running, until confirm=true."""
    started_at = time.time()
    err, prep = _prepare_execution(session_id, plan, rewind, guard, confirm)
    if err is not None:
        return err
    base, operations, ai_title = prep["base"], prep["operations"], prep["ai_title"]

    resp = _run_operations(
        session_id, base, operations, ai_title, started_at, retry=prep["retry"]
    )

    # Record the run for the weekly digest — ONLY for a signed-in user and ONLY on success
    # (a 200; _run_operations returns 4xx/5xx on failure). Best-effort: digest.record_run
    # swallows its own errors, and _extract_row_count guards the body parse, so nothing here
    # can turn a successful task into an error for the user.
    if user is not None and resp.status_code == 200:
        summary = ai_title or ", ".join(dict.fromkeys(
            op.get("action") for op in operations if op.get("action"))) or None
        digest.record_run(user.id, summary, _extract_row_count(resp))

    return resp


def _prepare_execution(
    session_id: str, plan: str, rewind: int, guard: str, confirm: str
) -> tuple[JSONResponse | None, dict | None]:
    """Everything /execute does BEFORE running: resolve the session, rewind history,
    parse and shape-check the plan, and apply the destructive-action guard.

    Shared verbatim by /execute and /execute/async (Track 4 item 1) so the two cannot
    drift — an async path that validated differently would be a second, subtly different
    API. Returns (response_to_return_now, None) or (None, prepared).
    """
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400), None

    # One live job per session. Two plans executing against one session would race on its
    # undo/redo history and the second one's "previous state" would be undefined, so this
    # is a correctness guard, not a courtesy — and it applies to the SYNCHRONOUS path too.
    busy = jobs.active_for_session(session_id)
    if busy:
        return _error(
            "That spreadsheet already has a change running. Wait for it to finish "
            "before starting another.",
            status=409,
        ), None

    states = entry["states"]
    if 0 <= rewind < len(states):
        del states[rewind + 1:]  # Retry/Edit: branch from an earlier step
    base = states[-1]

    try:
        parsed = json.loads(plan)
    except Exception:
        return _error("That plan couldn't be read — please try running again.", status=400), None
    # NORMALIZE the container before reading anything out of it. A plan may arrive either
    # wrapped ({"operations": [...]}, what the UI sends) or as a bare list of steps, which
    # is the natural way to hand-write or script one — and which /marketplace, /workflow
    # and /sheets already accept. /execute was the odd one out: a list hit `parsed.get`
    # and raised AttributeError -> 500 "something went wrong on our side", blaming the
    # server for input the caller can fix. Same class as the malformed-STEP guard below,
    # one level up — that shape-checks the steps but assumed the container was a dict.
    # Normalizing here (rather than at each call site) also covers the later
    # parsed.get("title").
    if isinstance(parsed, list):
        parsed = {"operations": parsed}
    elif not isinstance(parsed, dict):
        return _error(
            "That plan isn't in a format I recognise — it should be a list of steps, or "
            'an object with an "operations" list.',
            status=400,
        ), None
    operations = parsed.get("operations") or []
    if not isinstance(operations, list):
        return _error('That plan\'s "operations" should be a list of steps.', status=400), None
    if not operations:
        return _error("There's nothing to run.", status=400), None
    # Shape-check every step before executing. /process routes Brain output through
    # _sane_plan; /execute takes a plan from the UI (which the user can hand-edit), so
    # it needs the same guard. Without it a null/!dict step reached the executor and
    # surfaced as a 500 blaming the server — when the real cause is a malformed step
    # the user can fix.
    bad = next(
        (i for i, op in enumerate(operations, 1) if not isinstance(op, dict) or not op.get("action")),
        None,
    )
    if bad is not None:
        return _error(
            f"Step {bad} of that plan isn't a valid operation — each step needs an "
            "\"action\". Edit the plan and try again.",
            status=422,
        ), None
    ai_title = (parsed.get("title") or "").strip() or None

    # Guardrails (3.10): warn before destructive actions, with concrete impact. Opt-in via
    # `guard` so non-UI callers keep the immediate behaviour; bypassed once `confirm`ed.
    # This runs BEFORE any job is created, so a plan awaiting confirmation never becomes a
    # job the user then has to wait on.
    want_guard = str(guard).strip().lower() in ("1", "true", "yes")
    confirmed = str(confirm).strip().lower() in ("1", "true", "yes")
    if want_guard and not confirmed:
        assessment = guardrails.assess(operations, base["tables"], base["primary"], known_formulas=(entry or {}).get("formulas"))
        if assessment["destructive"]:
            return JSONResponse({
                "status": "confirm_required",
                "warnings": assessment["warnings"],
                "summary": assessment["summary"],
            }), None

    # rewind >= 0 means the caller branched from an earlier step — the UI's Retry/Edit.
    # It is the only signal that someone was dissatisfied enough to go round again.
    return None, {
        "base": base, "operations": operations, "ai_title": ai_title, "entry": entry,
        "retry": rewind is not None and rewind >= 0,
    }


@app.post("/execute/async")
async def execute_async(
    session_id: str = Form(...),
    plan: str = Form(...),
    rewind: int = Form(-1),
    guard: str = Form("false"),
    confirm: str = Form("false"),
    timeout_seconds: str = Form(""),
    user: "auth.User | None" = Depends(auth.current_user_optional),
) -> JSONResponse:
    """Same as /execute, but returns immediately with a job to watch (Track 4 item 1).

    WHY THIS EXISTS: /execute does tens of seconds of pandas + openpyxl work on a 100k+
    row workbook, inside an `async def` — i.e. ON the event loop — so one big file stalls
    every other request in the process. Here the work moves to a worker thread.

    POLLING, NOT SSE. This is a single-process app with in-memory state, and a poll is
    one cheap dict lookup (`snapshot` never touches the result body). SSE would hold a
    connection open per run and still need the same store behind it, buying nothing but a
    second failure mode — a dropped stream leaves the client with no way to re-read state,
    whereas a missed poll is simply retried. Revisit if this ever becomes multi-process.

    Validation, the destructive-action guard and the execution itself are the SAME code
    /execute uses — see _prepare_execution and _run_operations_body.
    """
    err, prep = _prepare_execution(session_id, plan, rewind, guard, confirm)
    if err is not None:
        return err
    base, operations, ai_title = prep["base"], prep["operations"], prep["ai_title"]

    # The job id IS the oplog run id: one identifier for one event (see jobs.py).
    job_id = oplog.new_run_id()
    started_at = time.time()

    def work(progress):
        status, body = _run_operations_body(
            session_id, base, operations, ai_title, started_at, progress,
            run_id=job_id, retry=prep["retry"],
        )
        # Digest parity with the synchronous path: signed-in users, successes only.
        if user is not None and status == 200:
            summary = ai_title or ", ".join(dict.fromkeys(
                op.get("action") for op in operations if op.get("action"))) or None
            digest.record_run(user.id, summary, body.get("row_count"))
        return status, body

    # Track 4 item 6: a caller may ask for a SHORTER budget than the server default (e.g.
    # an interactive UI that would rather fail fast), but never a longer one — otherwise a
    # client could pin a worker indefinitely.
    budget = config.JOB_TIMEOUT_SECONDS
    try:
        want = float(timeout_seconds)
        if want > 0:
            budget = min(budget, want) if budget > 0 else want
    except (TypeError, ValueError):
        pass

    try:
        job = jobs.submit(job_id, session_id, operations, work, timeout_seconds=budget)
    except jobs.JobError as exc:
        return _error(str(exc), status=exc.status)

    return JSONResponse(
        {
            "status": "accepted",
            "job_id": job["id"],
            "run_id": job["id"],
            "session_id": session_id,
            "total_steps": job["total_steps"],
            "timeout_seconds": job["timeout_seconds"],
            "poll": f"/jobs/{job['id']}",
            "result": f"/jobs/{job['id']}/result",
        },
        status_code=202,
    )


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    """Where a job actually is. Cheap enough to poll once a second: the snapshot never
    includes the result body."""
    snap = jobs.snapshot(job_id)
    if snap is None:
        return _error("I don't know that job — it may have finished long ago.", status=404)
    return JSONResponse({"status": "ok", "job": snap})


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str) -> JSONResponse:
    """Collect a finished job's result — byte-for-byte what /execute would have returned.

    409 while it is still running: saying "not yet" is honest, whereas returning an empty
    success would be a lie the client cannot detect.
    """
    snap = jobs.snapshot(job_id)
    if snap is None:
        return _error("I don't know that job — it may have finished long ago.", status=404)
    if not snap["done"]:
        return JSONResponse(
            {
                "status": "pending",
                "error": "That change is still running.",
                "job": snap,
            },
            status_code=409,
        )
    got = jobs.result(job_id)
    if got is None:
        # Finished, but the body was dropped by the memory budget. The receipt still says
        # where the file is, so this is a redirect to the download rather than a loss.
        return JSONResponse(
            {
                "status": "expired",
                "error": "That result is no longer held in memory, but the file is still "
                         "available to download.",
                "job": snap,
                "receipt": snap["receipt"],
            },
            status_code=410,
        )
    http_status, body = got
    return JSONResponse(body, status_code=http_status)


@app.get("/jobs")
def job_list(limit: int = 20) -> JSONResponse:
    """Recent jobs, most-recent-first — for debugging a live server."""
    return JSONResponse({"status": "ok", "jobs": jobs.recent(limit)})


def _extract_row_count(resp: JSONResponse) -> int | None:
    """Pull row_count out of a rendered /execute response body, tolerantly. The success
    body is JSON we just built, but we never let a parse hiccup break the response."""
    try:
        return json.loads(resp.body).get("row_count")
    except Exception:
        return None


@app.post("/process")
async def process(
    instruction: str = Form(...),
    session_id: str = Form(""),
    rewind: int = Form(-1),
    history: str = Form(""),
    team_id: str = Form("default"),
    org_id: str = Form(""),  # Phase 5.2: inherit this org's shared glossary
    compliance_mode: str = Form(""),  # Phase 5.5: e.g. "GDPR,HIPAA" — widen the PII shield
    # Voice input & spoken feedback (Phase 3.6). `input_source` is "text" (default) or
    # "voice"; a voice transcript is normalized + confidence-checked before we act.
    # `feedback_mode` (silent/step/summary) shapes the `speech` field in the response.
    input_source: str = Form("text"),
    feedback_mode: str = Form(voice.DEFAULT_MODE),
    transcript_confidence: float = Form(1.0),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    started_at = time.time()
    instruction = (instruction or "").strip()
    speak_mode = (feedback_mode or voice.DEFAULT_MODE).strip().lower()

    def _spk(text: str | None) -> str | None:
        """Spoken form of a one-off message (clarify/reply), honoring silent mode."""
        return None if speak_mode == "silent" else text

    # Voice input (Phase 3.6): transcripts are noisy — strip spoken fillers, then assess
    # before doing anything. On empty/low-confidence/garbled audio we decline gracefully
    # and ask the user to repeat rather than risk acting on a misheard command.
    if (input_source or "").strip().lower() == "voice":
        instruction = voice.normalize_transcript(instruction)
        ok, msg = voice.assess_transcript(instruction, transcript_confidence)
        if not ok:
            return JSONResponse({"status": "clarify", "clarification": msg, "speech": _spk(msg)})

    if not instruction:
        return _error("Please describe what you'd like done to the data.", status=400)

    # 1. Resolve the base working state. A session keeps a STACK of states:
    #    states[0] = the uploaded data, plus one more per successful step. `rewind`
    #    lets Retry/Edit re-run an earlier step by branching from the state just
    #    before it (dropping the now-stale later steps).
    if files:
        too_big = _too_big(files)
        if too_big:
            return _error(too_big, status=413)
        uploads = [(f.filename or "upload", await f.read()) for f in files]
        too_big = _too_big_read(uploads)  # real bytes; the declared size can be absent
        if too_big:
            return _error(too_big, status=413)
        try:
            data = load_files(uploads)
        except ValueError as exc:
            return _error(str(exc), status=400)
        if session_id and session_id in _SESSIONS:
            cur = _SESSIONS[session_id]["states"][-1]
            base = {
                "tables": {**cur["tables"], **data.tables},
                "primary": data.primary,
                "exts": {**cur["exts"], **data.exts},
                "notes": {**cur.get("notes", {}), **data.notes},
            }
        else:
            base = {
                "tables": dict(data.tables),
                "primary": data.primary,
                "exts": dict(data.exts),
                "notes": dict(data.notes),
            }
        if session_id:
            _remember_session(session_id, base)  # fresh upload resets step history (+ evicts old)
    elif session_id and session_id in _SESSIONS:
        states = _SESSIONS[session_id]["states"]
        if 0 <= rewind < len(states):
            del states[rewind + 1:]  # branch: drop steps at/after the rewind point
        base = states[-1]
    else:
        return _error(
            "Please upload a spreadsheet to start (or start a new session).", status=400
        )

    tables, primary, exts = base["tables"], base["primary"], base["exts"]

    # 2. Summarize all tables for the model so it can plan across files, including the
    #    foreign-key links between them (Track 3 item 1) so cross-sheet requests don't
    #    have to be guessed at.
    structure = _brain_structure(tables, primary)
    # PII shield (3.9): mask sensitive sample values + history BEFORE the model sees them.
    # Compliance profiles (5.5) WIDEN the scan with regime-specific fields (HIPAA MRN, IRDAI
    # policy no, …) so they're masked too; with none set, this is the base shield unchanged.
    profiles = compliance.parse_profiles(compliance_mode)
    scan = compliance.sensitive_columns(tables, profiles) if profiles else pii.scan_tables(tables)
    structure, shielded = pii.redact_structure(structure, scan)
    history = pii.redact_text(history)
    # Personalization (3.12): prepend the team's learned glossary + preferences to the
    # context so definitions are applied consistently (kept inside `history`).
    glossary = personalization.context(team_id, scope=org_id or None)
    context = (glossary + "\n\n" + history).strip() if glossary else history

    # 3. Translate the instruction into an operation plan.
    try:
        plan = llm.parse_instruction(instruction, structure, context)
    except Exception as exc:
        # The Brain is unavailable (rate limit / quota / outage / parse error). Try a
        # deterministic fallback for simple commands so basic work still happens. The
        # fallback result is shown like any normal result (no "AI unavailable" notice —
        # that reads as a bad/uncertain experience to the user).
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()  # log the real cause; never blame the instruction
        plan = fallback.parse(instruction, structure, personalization.effective_definitions(team_id, org_id or None))
        if plan is None:
            if unavailable:
                return _error(str(exc), status=503)
            if key_missing:
                return _error(str(exc), status=500)
            return _error(
                "I couldn't reach the AI service to understand your request right now — "
                "this isn't a problem with your instruction. Please try again in a moment.",
                status=502,
            )
    plan = _sane_plan(plan)

    clarification = plan.get("clarification")
    reply = plan.get("reply")
    ai_title = (plan.get("title") or "").strip() or None
    operations = plan.get("operations") or []
    if not operations:
        # No action to take: answer a data question, ask for clarity, or nudge.
        if reply:
            return JSONResponse({"status": "message", "message": reply, "speech": _spk(reply)})
        if clarification:
            return JSONResponse({"status": "clarify", "clarification": clarification, "speech": _spk(clarification)})
        nudge = (
            "I didn't understand that — try describing the task, e.g. "
            '"sort by Revenue descending" or "remove duplicate rows".'
        )
        return JSONResponse({"status": "message", "message": nudge, "speech": _spk(nudge)})

    # Personalization (3.12): fill the team's formatting defaults into the plan.
    operations = personalization.apply_preferences(operations, personalization.preferences(team_id))

    # Plan validation (PRD 1.3): never execute a plan that reads a column no sheet has —
    # ask instead. (The executor would also catch it, but a clarifying question with a
    # did-you-mean beats an error after the fact.)
    phantom = _missing_columns(operations, tables)
    if phantom:
        _clar = _missing_columns_clarification(phantom, tables)
        return JSONResponse({"status": "clarify", "clarification": _clar, "speech": _spk(_clar)})

    # 4. Execute the plan across all tables.
    partial_warning = None
    completed_steps = len(operations)
    failed_step = None
    try:
        result, result_name, notes, render_ops = execute_multi(tables, primary, operations)
    except MultiStepError as exc:
        # A later step failed: keep the file reflecting the steps that completed and
        # tell the user exactly which step failed and why (PRD MS-b).
        result, result_name = exc.partial_result, exc.partial_name
        notes, render_ops = exc.notes, exc.format_ops
        done = exc.failed_step - 1
        completed_steps = done
        failed_step = exc.failed_step
        partial_warning = (
            f"Step {exc.failed_step} couldn't be done: {exc.reason} "
            f"Your file reflects the {done} step{'s' if done != 1 else ''} that "
            "completed before it — fix that step and try again."
        )
    except OperationError as exc:
        # Expected, user-facing problem (bad column, wrong type, …) — explain it.
        return _error(str(exc), status=422)
    except Exception:
        # Unexpected bug: never leak the exception/traceback to the user (1.14-d).
        return _error(_INTERNAL_ERROR, status=500)

    # 5. Push the new state so the next instruction chains on it (and so Retry/Edit
    #    can branch from any earlier step). Originals stay reachable for lookups/merges.
    if session_id:
        if isinstance(result, dict):
            new_state = {
                "tables": {**tables, **result},
                "primary": next(iter(result)),
                "exts": {**exts, **{k: "xlsx" for k in result}},
            }
        else:
            new_state = {
                "tables": {**tables, result_name: result},
                "primary": result_name,
                "exts": {**exts, result_name: exts.get(result_name, "xlsx")},
            }
        new_state["label"] = _step_label(notes)  # labeled version history (Phase 3.2)
        _push_state(session_id, new_state)
        _record_formulas(session_id, operations, result)  # dependency registry (Phase 4.8)

    # Large-file notice (PRD: big files still work, but tell the user).
    biggest = max((len(t) for t in tables.values()), default=0)
    if biggest > 50_000:
        notes = [
            f"Heads up: this is a large file (~{biggest:,} rows) — it still processed, "
            "but big files can take a little longer."
        ] + notes

    # PII-shield notice (3.9): tell the user what was hidden from the AI.
    shield_cols = _shield_columns(shielded)
    if shield_cols:
        notes = [
            f"Shielded {len(shield_cols)} sensitive field"
            f"{'s' if len(shield_cols) != 1 else ''} from the AI: {', '.join(shield_cols)}."
        ] + notes
        # Audit trail (5.5): record WHAT was shielded (field names/counts only, never values)
        # and under which compliance regime — so "what happened to sensitive data?" is answerable.
        audit.record("pii_shielded", actor=team_id,
                     detail=f"Masked {len(shield_cols)} field(s) before the AI.",
                     meta={"columns": shield_cols, "compliance": profiles})

    # 6. Serialize. The result is either one table or a multi-sheet workbook.
    try:
        if isinstance(result, dict):
            out_bytes, out_name, media_type = _serialize_workbook(result, result_name, primary=result_name, render_ops=render_ops)
            row_count = sum(int(len(d)) for d in result.values())
        else:
            out_ext, upgrade_note = _output_ext(exts.get(result_name, "xlsx"), render_ops)
            if upgrade_note:
                notes = notes + [upgrade_note]
            out_bytes, out_name, media_type = _serialize(result, result_name, out_ext, render_ops)
            row_count = int(len(result))
    except Exception:  # saving the workbook failed unexpectedly — stay friendly (1.14-d)
        return _error(_INTERNAL_ERROR, status=500)

    # Always offer the file via a streamed download URL (small JSON, no browser OOM).
    # Only ALSO inline it as base64 when it's small enough to be cheap.
    download_id = _store_result(out_bytes, out_name, media_type)
    inline_b64 = (
        base64.b64encode(out_bytes).decode("ascii")
        if len(out_bytes) <= _INLINE_MAX_BYTES else None
    )
    return JSONResponse(
        {
            "status": "ok",
            "session_id": session_id,
            "explanation": " ".join(notes) if notes else "No changes were needed.",
            "notes": notes,
            "formulas": _describe_formulas(render_ops),
            # Track 4 item 3: for EVERY step, whether the file got a live Excel formula or
            # a computed value — and so whether it recalculates when the user edits the
            # data. Derived from the directives the run really emitted, not from intent.
            "computation": compute_mode.describe(operations, render_ops),
            "code": _explain_code(operations),  # Phase 3.3 "Show Code" — mirrors the executed plan
            # Spoken feedback (Phase 3.6): a TTS-ready line shaped to the user's chosen
            # verbosity (silent/step/summary). null in silent mode. Purely a view over
            # the real per-step notes — never a fresh, driftable description.
            "speech": voice.spoken_feedback(notes, speak_mode),
            # Confidence on forecasts/anomalies (Phase 3.10).
            "analysis": [d for d in render_ops if d.get("type") == "analysis"],
            "row_count": row_count,
            "rows_before": int(len(tables[primary])) if primary in tables else None,
            "preview": _result_preview(result, result_name),
            "insight": _summarize_insight(result, result_name),
            "actions": list(dict.fromkeys(op.get("action") for op in operations)),
            "ai_title": ai_title,
            "partial": partial_warning is not None,
            "warning": partial_warning,
            # Per-step outcome (Phase 3.4): lets the UI render a ✓/✗ checklist mapped
            # to the plan's steps, so each sub-step is independently verifiable.
            "completed_steps": completed_steps,
            "failed_step": failed_step,
            # Sensitive columns hidden from the AI (Phase 3.9).
            "shielded_columns": shield_cols,
            "filename": out_name,
            "media_type": media_type,
            "file_size": len(out_bytes),
            "elapsed_ms": int((time.time() - started_at) * 1000),
            "download_id": download_id,
            "file_base64": inline_b64,
        }
    )


def _result_preview(result, result_name: str, sample_rows: int = 8) -> list[dict]:
    """A compact, JSON-safe preview of the RESULT so the UI can SHOW the transformed
    data (not just offer a download). Same shape as /inspect's tables. For a
    multi-sheet workbook, previews each sheet."""
    frames = result if isinstance(result, dict) else {result_name: result}
    preview = []
    for name, df in frames.items():
        s = summarize_structure(df, sample_rows=sample_rows)
        preview.append({
            "name": name,
            "row_count": s["row_count"],
            "columns": s["columns"],
            "sample_rows": s["sample_rows"],
            "truncated": s["row_count"] > sample_rows,
        })
    return preview


def _looks_numeric(series) -> bool:
    """True if every non-blank value in the column is a number."""
    nonblank = series[series.notna() & (series.astype(str).str.strip() != "")]
    if len(nonblank) == 0:
        return False
    return bool(pd.to_numeric(nonblank, errors="coerce").notna().all())


def _date_column(df):
    """Find a clear DATE column (returns (name, parsed_series) or (None, None)).

    Numeric columns are never treated as dates (pd.to_datetime would happily read an
    integer as epoch-nanoseconds and fabricate a date), and a candidate must have ≥80%
    of its non-blank values parse as real dates."""
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            return c, s
        if _looks_numeric(s):  # guard: don't mistake a number column for dates
            continue
        nonblank = s[s.notna() & (s.astype(str).str.strip() != "")]
        if len(nonblank) < 3:
            continue
        parsed = _to_datetime(nonblank)
        if float(parsed.notna().mean()) >= 0.8:
            return c, _to_datetime(s)
    return None, None


def _change_driver(df, date_col, periods, value, vseries, last_p, prev_p, net_change):
    """If ONE category accounts for most of the period-over-period change, name it."""
    cats = [
        c for c in df.columns
        if c not in (date_col, value) and not _looks_numeric(df[c])
        and 2 <= int(df[c].nunique(dropna=True))
    ]
    if not cats or net_change == 0:
        return None
    cat = cats[0]
    work = pd.DataFrame({"_p": periods, "_c": df[cat].astype(str), "_v": vseries}).dropna(
        subset=["_p", "_v"]
    )
    change = (
        work[work["_p"] == last_p].groupby("_c")["_v"].sum()
        .subtract(work[work["_p"] == prev_p].groupby("_c")["_v"].sum(), fill_value=0)
    )
    if change.empty:
        return None
    # the category moving most in the SAME direction as the net change
    top = change.idxmax() if net_change > 0 else change.idxmin()
    if abs(float(change.loc[top])) >= 0.4 * abs(net_change):  # meaningful share only
        return f"— driven mostly by {cat} “{top}”."
    return None


def _period_over_period(df) -> str | None:
    """Compare the last two months of a clear date column on the first numeric column.
    Returns None (so the caller falls back) when there's no date column, fewer than two
    periods, or a zero base — never guesses."""
    date_col, dt = _date_column(df)
    if date_col is None:
        return None
    numeric = [c for c in df.columns if c != date_col and _looks_numeric(df[c])]
    if not numeric:
        return None
    value = numeric[0]
    vseries = pd.to_numeric(df[value], errors="coerce")
    periods = dt.dt.to_period("M")
    g = pd.DataFrame({"_p": periods, "_v": vseries}).dropna(subset=["_p", "_v"])
    if g.empty:
        return None
    totals = g.groupby("_p")["_v"].sum().sort_index()
    if len(totals) < 2:  # only one period — can't compare (cautious)
        return None
    last_p, prev_p = totals.index[-1], totals.index[-2]
    last_v, prev_v = float(totals.iloc[-1]), float(totals.iloc[-2])
    if prev_v == 0:  # can't compute a % change from zero (cautious)
        return None
    pct = round((last_v - prev_v) / abs(prev_v) * 100)
    direction = "up" if last_v >= prev_v else "down"
    msg = (
        f"{value} is {direction} {abs(pct)}% in {last_p.strftime('%b %Y')} vs "
        f"{prev_p.strftime('%b %Y')} ({_abbrev(last_v)} vs {_abbrev(prev_v)})."
    )
    driver = _change_driver(df, date_col, periods, value, vseries, last_p, prev_p, last_v - prev_v)
    return f"{msg} {driver}" if driver else msg


def _summarize_insight(result, result_name: str) -> str | None:
    """A ONE-LINE observation about the RESULT, COMPUTED from the data (never an LLM
    free-generation, so the numbers can't be fabricated). Returns None when the data is
    too small or unsuitable to draw a conclusion — i.e. it stays cautious by default."""
    df = result[result_name] if isinstance(result, dict) else result
    try:
        if df is None or len(df) < 3:  # too few rows to claim a trend
            return None
        # Prefer a period-over-period trend when there's a clean date column; otherwise
        # fall back to the always-correct top-contributor / range observation.
        pop = _period_over_period(df)
        if pop:
            return pop
        cols = list(df.columns)
        numeric = [c for c in cols if _looks_numeric(df[c])]
        if not numeric:
            return None
        value = numeric[0]
        vseries = pd.to_numeric(df[value], errors="coerce")
        total = float(vseries.sum())
        # A grouping column: non-numeric, with 2..(rows-1) distinct values.
        cats = [
            c for c in cols
            if c not in numeric and 2 <= int(df[c].nunique(dropna=True)) < len(df)
        ]
        if cats and total > 0:
            cat = cats[0]
            grouped = (
                pd.DataFrame({"_c": df[cat].astype(str), "_v": vseries})
                .dropna(subset=["_v"])
                .groupby("_c")["_v"].sum()
            )
            # A "% of the total" share is only meaningful as a part-of-whole when every
            # group's contribution is non-negative and the top is a sane 0–100% slice.
            # Mixed-sign data (e.g. profit with losses) can make top_val exceed the total
            # → a >100% "share" would be a fabricated-looking claim; fall through to the
            # always-correct average+range instead.
            if len(grouped) >= 2 and float(grouped.min()) >= 0:
                top = str(grouped.idxmax())
                top_val = float(grouped.max())
                share = round(top_val / total * 100)
                if 0 <= share <= 100:
                    return (
                        f"{cat} “{top}” is the largest, at {share}% of {value} "
                        f"({_abbrev(top_val)} of {_abbrev(total)})."
                    )
        # numeric-only fallback: average + range (all real figures)
        return (
            f"{value} averages {_abbrev(float(vseries.mean()))}, "
            f"ranging {_abbrev(float(vseries.min()))} to {_abbrev(float(vseries.max()))}."
        )
    except Exception:
        return None


def _describe_formulas(render_ops: list[dict]) -> list[str]:
    """Plain descriptions of any formulas/lookups added, for the UI's summary panel."""
    out = []
    for d in render_ops:
        if d.get("type") == "formula":
            out.append(f"{d['column']} = {d['formula']}")
        elif d.get("type") == "lookup":
            out.append(
                f"{d['new_column']} = look up '{d['return_column']}' from "
                f"'{d['source_name']}' matched on '{d['key_column']}'"
            )
    return out


def _explain_code(operations: list[dict]) -> list[str]:
    """Phase 3.3 'Show Code': a faithful, pandas-flavored rendering of the EXACT plan
    that ran. It's built from the executed `operations`, so it always matches what
    actually happened (never a separate AI re-description that could drift)."""
    def L(x):  # compact repr for a list of column names
        return ", ".join(map(str, x or []))

    lines: list[str] = []
    for op in operations:
        a = op.get("action")
        try:
            if a == "sort":
                cols = op.get("columns") or []
                orders = op.get("orders") or []
                asc = [str(orders[i] if i < len(orders) else "asc").lower() != "desc" for i in range(len(cols))]
                lines.append(f"df = df.sort_values([{L(cols)}], ascending={asc})")
            elif a == "filter":
                conds = op.get("conditions") or []
                parts = [f"{c.get('column')} {c.get('operator')} {c.get('value')!r}" for c in conds]
                j = " & " if (op.get("combine") or "and").lower() == "and" else " | "
                lines.append(f"df = df[{j.join(parts)}]")
            elif a == "limit":
                lines.append(f"df = df.{'tail' if op.get('from_end') else 'head'}({op.get('count')})")
            elif a == "remove_duplicates":
                lines.append(f"df = df.drop_duplicates(subset=[{L(op.get('columns'))}] or all)")
            elif a == "add_formula_column":
                lines.append(f"df[{op.get('name')!r}] = {op.get('formula')}")
            elif a == "aggregate":
                lines.append(f"df = df.groupby([{L(op.get('group_by'))}])[{op.get('agg_column')!r}]"
                             f".{op.get('agg_func') or 'agg'}()")
            elif a in ("pivot", "pivot_summary"):
                lines.append(f"df = pivot(index=[{L(op.get('group_by') or op.get('index_columns'))}], "
                             f"columns={op.get('pivot_column')!r}, values={op.get('value_column')!r}, "
                             f"agg={op.get('agg_func') or 'sum'!r})")
            elif a == "lookup":
                lines.append(f"df[{(op.get('new_column') or op.get('return_column'))!r}] = "
                             f"lookup(df[{op.get('key_column')!r}], "
                             f"{op.get('source_sheet')!r}[{op.get('source_key_column')!r}] -> {op.get('return_column')!r})")
            elif a == "rename_columns":
                m = dict(zip(op.get("rename_from") or [], op.get("rename_to") or []))
                lines.append(f"df = df.rename(columns={m})")
            elif a == "drop_columns":
                lines.append(f"df = df.drop(columns=[{L(op.get('columns'))}])")
            elif a == "select_columns":
                lines.append(f"df = df[[{L(op.get('columns'))}]]")
            elif a == "find_replace":
                where = f" in {op.get('column')!r}" if op.get("column") else ""
                lines.append(f"df = df.replace({op.get('find')!r}, {op.get('replace')!r}){where}")
            elif a in ("merge", "combine_sheets"):
                lines.append(f"df = {a}([{L(op.get('merge_tables') or op.get('sheet_tables'))}])")
            elif a in ("fill_missing", "drop_missing", "flag_missing", "trim", "drop_invalid"):
                lines.append(f"df = {a}(columns=[{L(op.get('columns'))}]"
                             + (f", value={op.get('fill_value')!r}" if op.get("fill_value") is not None else "") + ")")
            else:
                params = {k: v for k, v in op.items() if k not in ("action", "table") and v is not None}
                inside = ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:6])
                lines.append(f"df = {a}({inside})")
            if op.get("table"):
                lines[-1] += f"   # on table '{op['table']}'"
        except Exception:
            lines.append(f"df = {a}(...)")  # never let the code view break the response
    return lines


def _output_ext(in_ext: str, render_ops: list[dict]) -> tuple[str, str | None]:
    """Choose the download extension. A .csv can't carry live formulas, formatting, or
    charts, so when an operation produced any render directive (formula/lookup/format/
    highlight/chart) we upgrade the download to .xlsx — that's what makes the 1.13
    promise actually hold. PDF/image-extracted tables also always output as .xlsx since
    there is no original spreadsheet file to mirror. Returns (ext, optional note)."""
    if in_ext == "pdf":
        # Tables extracted from a PDF or image are always saved as Excel.
        return "xlsx", None
    if in_ext == "csv" and render_ops:
        has_chart = any(d.get("type") in ("chart", "dashboard") for d in render_ops)
        what = "chart, formulas, and formatting" if has_chart else "live formulas and formatting"
        return "xlsx", (
            f"Saved as .xlsx so the {what} are preserved (a .csv file can't hold them)."
        )
    return in_ext, None


def _serialize_workbook(
    sheets: dict, base_name: str, primary: str | None = None, render_ops: list | None = None
) -> tuple[bytes, str, str]:
    """Write several tables into ONE .xlsx, each on its own sheet/tab. Render directives
    (live formulas / formatting) apply to the PRIMARY sheet, after every sheet exists —
    cross-sheet formula ranges (Prices!$B$2:...) need the other tabs in place."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        taken: set[str] = set()
        placed: dict[str, str] = {}
        for name, d in sheets.items():
            sheet_name = _safe_sheet_name(name, taken)
            taken.add(sheet_name)
            placed[name] = sheet_name
            d.to_excel(writer, index=False, sheet_name=sheet_name)
            _disarm_injection(writer.sheets[sheet_name])
        if render_ops and primary in placed:
            _apply_render(writer, placed[primary], sheets[primary], render_ops)
    stem = (base_name or "combined").rsplit(".", 1)[0]
    return (
        buf.getvalue(),
        f"{stem}_sumio.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _disarm_injection(ws) -> None:
    """Neutralize spreadsheet formula-injection from UPLOADED data.

    openpyxl writes any string that starts with '=' as a live formula, so a cell
    like '=HYPERLINK("http://evil")' in the user's file would execute when they open
    Sumio's output in Excel/Sheets. We force every such DATA cell to plain text (the
    visible value is unchanged). Call this right after writing data and BEFORE writing
    our own intentional formulas, so those are left live.
    """
    for row in ws.iter_rows():
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"


def _serialize(df, original_name: str, ext: str, render_ops=None) -> tuple[bytes, str, str]:
    render_ops = render_ops or []
    stem = (original_name or "result").rsplit(".", 1)[0]
    buf = io.BytesIO()
    if ext == "csv":
        # CSV has no styling/formulas; render directives are simply ignored.
        df.to_csv(buf, index=False)
        return buf.getvalue(), f"{stem}_sumio.csv", "text/csv"
    # xlsx via openpyxl
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
        _disarm_injection(writer.sheets["Sheet1"])  # before our own formulas go in
        _apply_render(writer, "Sheet1", df, render_ops)
    return (
        buf.getvalue(),
        f"{stem}_sumio.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _apply_render(writer, main_name: str, df, render_ops: list[dict]) -> None:
    """Apply each render directive (formatting / live formula / highlight / lookup)
    to the workbook. Only meaningful for .xlsx output."""
    ws = writer.sheets[main_name]
    # Ordering: layout runs LAST of the row-anchored kinds (its title row shifts rows,
    # translating already-written formulas/CF/DV/tables) — and comments run after even
    # that, computing their own row offset, since openpyxl's insert_rows doesn't move
    # comment anchors.
    render_ops = sorted(
        render_ops,
        key=lambda d: (d.get("type") == "layout") + 2 * (d.get("type") == "comments"),
    )
    title_offset = 1 if any(
        d.get("type") == "layout" and d.get("title") for d in render_ops
    ) else 0
    for directive in render_ops:
        kind = directive.get("type")
        if kind == "format":
            _apply_format(ws, df, directive)
        elif kind == "formula":
            _apply_formula(writer, ws, df, directive)
        elif kind == "highlight":
            _apply_highlight(ws, df, directive)
        elif kind == "cf":
            _apply_cf(ws, df, directive)
        elif kind == "dv":
            _apply_dv(writer, ws, df, directive)
        elif kind == "table":
            _apply_table(ws, df, directive)
        elif kind == "pivot_formula":
            _apply_pivot_formula(writer, ws, df, directive)
        elif kind == "comments":
            _apply_comments(ws, df, directive, title_offset)
        elif kind == "defined_name":
            from openpyxl.workbook.defined_name import DefinedName

            col = directive.get("column")
            cols = list(df.columns)
            if col in cols:
                letter = get_column_letter(cols.index(col) + 1)
                sheet_ref = _excel_sheet_ref(ws.title)
                attr = f"{sheet_ref}!${letter}$2:${letter}${len(df) + 1}"
                dn_name = directive.get("name")
                if dn_name in ws.parent.defined_names:
                    del ws.parent.defined_names[dn_name]
                ws.parent.defined_names[dn_name] = DefinedName(dn_name, attr_text=attr)
        elif kind == "goal_seek_sheet":
            name = _safe_sheet_name("Goal Seek", set(writer.book.sheetnames))
            gs = writer.book.create_sheet(name)
            writer.sheets[name] = gs
            for r, row in enumerate(directive.get("rows") or [], start=1):
                for c, val in enumerate(row, start=1):
                    gs.cell(row=r, column=c, value=val)
            gs["A1"].font = Font(bold=True, size=12)
            gs.column_dimensions["A"].width = 14
            gs.column_dimensions["B"].width = 44
        elif kind == "stats_sheet":
            # Phase 2.3: a small "label / value" summary sheet for regression / t-test
            # (data itself is unchanged). Same shape as goal_seek_sheet, named per directive.
            sname = _safe_sheet_name(directive.get("sheet_name") or "Analysis", set(writer.book.sheetnames))
            st = writer.book.create_sheet(sname)
            writer.sheets[sname] = st
            for r, row in enumerate(directive.get("rows") or [], start=1):
                for c, val in enumerate(row, start=1):
                    st.cell(row=r, column=c, value=val)
            st["A1"].font = Font(bold=True, size=12)
            st.column_dimensions["A"].width = 22
            st.column_dimensions["B"].width = 48
        elif kind == "sheet_style":
            real = _resolve_sheet_name(writer.sheets.keys(), directive.get("sheet_name") or "")
            if real is not None:
                target_ws = writer.sheets[real]
                if directive.get("tab_color"):
                    target_ws.sheet_properties.tabColor = directive["tab_color"]
                if "hidden" in directive:
                    target_ws.sheet_state = "hidden" if directive["hidden"] else "visible"
        elif kind == "sheet_protect":
            _apply_sheet_protect(writer, directive)
        elif kind == "workbook_protect":
            from openpyxl.workbook.protection import WorkbookProtection

            writer.book.security = WorkbookProtection(lockStructure=bool(directive.get("lock_structure")))
        elif kind == "layout":
            _apply_layout(ws, df, directive)
        elif kind == "lookup":
            _apply_lookup(writer, ws, df, directive)
        elif kind == "chart":
            _apply_chart(ws, df, directive)
        elif kind == "dashboard":
            _apply_dashboard(writer, main_name, df, directive)


# Category charts share one build path (labels on the x-axis + value series).
_CATEGORY_CLASSES = {
    "bar": BarChart, "line": LineChart, "area": AreaChart, "pie": PieChart,
    "doughnut": DoughnutChart, "radar": RadarChart, "stock": StockChart,
}
# XY charts plot numbers against numbers (a distinct openpyxl API).
_XY_CLASSES = {"scatter": ScatterChart, "bubble": BubbleChart}


def _build_chart(data_ws, df, chart_type, x_col, y_cols, title, height=8, width=16,
                 size_col=None):
    """Build an openpyxl chart that references `data_ws`'s cells, so it always reflects
    the current data. Returns the chart, or None if the request can't be charted.
    `data_ws` may differ from the chart's host sheet (the dashboard charts the data
    sheet from the Dashboard sheet)."""
    columns = list(df.columns)
    y_cols = [c for c in (y_cols or []) if c in columns]
    n = len(df)
    if x_col not in columns or not y_cols or n == 0:
        return None
    col_idx = lambda c: columns.index(c) + 1  # noqa: E731 (1-based worksheet column)

    if chart_type in _XY_CLASSES:
        chart = _XY_CLASSES[chart_type]()
        xref = Reference(data_ws, min_col=col_idx(x_col), min_row=2, max_row=n + 1)
        if chart_type == "bubble":
            size_col = size_col if size_col in columns else y_cols[0]
            yref = Reference(data_ws, min_col=col_idx(y_cols[0]), min_row=1, max_row=n + 1)
            zref = Reference(data_ws, min_col=col_idx(size_col), min_row=2, max_row=n + 1)
            chart.series.append(Series(yref, xref, zvalues=zref, title_from_data=True))
        else:  # scatter — one XY series per value column, drawn as points
            for c in y_cols:
                yref = Reference(data_ws, min_col=col_idx(c), min_row=1, max_row=n + 1)
                s = Series(yref, xref, title_from_data=True)
                s.marker = Marker(symbol="circle", size=6)
                s.graphicalProperties.line.noFill = True  # points, not a connecting line
                chart.series.append(s)
        chart.x_axis.title = x_col
        chart.y_axis.title = ", ".join(y_cols)
    else:
        chart = _CATEGORY_CLASSES.get(chart_type, BarChart)()
        if chart_type == "bar":
            chart.type = "col"  # vertical columns
        if chart_type == "radar":
            chart.type = "marker"
        for c in y_cols:
            chart.add_data(Reference(data_ws, min_col=col_idx(c), min_row=1, max_row=n + 1),
                           titles_from_data=True)
        chart.set_categories(Reference(data_ws, min_col=col_idx(x_col), min_row=2, max_row=n + 1))
        if chart_type == "stock" and len(y_cols) >= 2:
            from openpyxl.chart.axis import ChartLines

            chart.hiLowLines = ChartLines()  # the high-low connectors that make it a stock chart
            for s in chart.series:  # markers, no connecting fill line
                s.graphicalProperties.line.noFill = True
                s.marker = Marker(symbol="dot", size=5)

    if title:
        chart.title = title
    chart.height = height
    chart.width = width
    return chart


def _apply_chart(ws, df, directive: dict) -> None:
    """Add a chart to the data sheet itself (the chart operation, 2.2)."""
    chart = _build_chart(
        ws, df, directive.get("chart_type"), directive.get("x_column"),
        directive.get("y_columns"), directive.get("title"),
        size_col=directive.get("size_column"),
    )
    if chart is None:
        return
    anchor = f"{get_column_letter(len(list(df.columns)) + 2)}2"  # two columns past the data
    ws.add_chart(chart, anchor)


def _apply_dashboard(writer, main_name: str, df, directive: dict) -> None:
    """Build a one-page 'Dashboard' sheet: title, KPI cells, a written summary, and
    charts that reference the data sheet (so the whole thing reflects current data).
    Text lives in columns A–C; charts are stacked in column E so nothing overlaps."""
    data_ws = writer.sheets.get(main_name)
    if data_ws is None:
        return
    book = writer.book
    if "Dashboard" in book.sheetnames:  # regenerate cleanly — never stack duplicates
        book.remove(book["Dashboard"])
    dash = book.create_sheet("Dashboard", 0)  # make it the first tab

    dash["A1"] = directive.get("title") or "Dashboard"
    dash["A1"].font = Font(bold=True, size=16)

    row = 3
    if directive.get("kpis"):
        dash.cell(row=row, column=1, value="Key metrics").font = Font(bold=True)
        row += 1
        for kpi in directive["kpis"]:
            dash.cell(row=row, column=1, value=kpi.get("label") or "")
            dash.cell(row=row, column=2, value=kpi.get("value")).font = Font(bold=True)
            row += 1
        row += 1

    summary = (directive.get("summary") or "").strip()
    if summary:
        dash.cell(row=row, column=1, value="Summary").font = Font(bold=True)
        row += 1
        cell = dash.cell(row=row, column=1, value=summary)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        dash.merge_cells(start_row=row, start_column=1, end_row=row + 4, end_column=3)
        row += 6

    dash.column_dimensions["A"].width = 26
    dash.column_dimensions["B"].width = 18

    # Charts go in column E onward (right of the A–C text block), stacked vertically.
    anchor_row = 2
    for spec in directive.get("charts") or []:
        chart = _build_chart(
            data_ws, df, spec.get("chart_type"), spec.get("x_column"),
            spec.get("y_columns"), spec.get("title"), height=7, width=13,
            size_col=spec.get("size_column"),
        )
        if chart is None:
            continue
        dash.add_chart(chart, f"E{anchor_row}")
        anchor_row += 16  # vertical spacing so charts never overlap


def _safe_sheet_name(base: str, taken: set[str]) -> str:
    """A valid, unique Excel sheet name (<=31 chars, no : \\ / ? * [ ]).

    Multi-file table names look like 'long_file_name - Sales'; when truncating, keep
    the SHEET part (the ' - ' tail) and trim the file stem instead — that's what users
    recognize on the tab, and cross-sheet formulas resolve sheets by that tail."""
    name = re.sub(r"[:\\/?*\[\]]", " ", str(base)).strip() or "Lookup"
    if len(name) > 28:
        stem, sep, tail = name.rpartition(" - ")
        if sep and tail:
            room = 28 - len(sep + tail)
            name = (stem[:room].rstrip() + sep + tail) if room > 0 else tail[:28]
        else:
            name = name[:28]
    candidate = name
    i = 2
    while candidate in taken:
        candidate = f"{name} {i}"[:31]
        i += 1
    return candidate


def _apply_pivot_formula(writer, main_ws, df, directive: dict) -> None:
    """Phase 2.1 live mode: put the pivot's SOURCE data on its own sheet and replace
    the statically-written grid with a single live =GROUPBY()/=PIVOTBY() spill
    formula anchored at A1. The preview keeps the computed values (a written formula
    has no cached value); the note warns these functions need Microsoft 365."""
    src = directive.get("source_df")
    if src is None or len(src) == 0:
        return
    sheet_name = _safe_sheet_name("Pivot Data", set(writer.book.sheetnames))
    src.to_excel(writer, index=False, sheet_name=sheet_name)
    src_ws = writer.sheets[sheet_name]
    _disarm_injection(src_ws)  # source cells are uploaded data too

    q = sheet_name.replace("'", "''")
    n = len(src)

    def rng(c1: int, c2: int) -> str:
        # Header row INCLUDED — paired with field_headers=3 so the spill shows labels.
        return f"'{q}'!${get_column_letter(c1)}$1:${get_column_letter(c2)}${n + 1}"

    row_fields = directive.get("rows") or []
    func = directive.get("func") or "SUM"
    totals = 1 if directive.get("totals") else 0
    row_range = rng(1, len(row_fields))
    val_idx = len(src.columns)  # the value column is always last in the source frame
    val_range = rng(val_idx, val_idx)
    if directive.get("column"):
        col_idx = len(row_fields) + 1
        formula = (f"=PIVOTBY({row_range},{rng(col_idx, col_idx)},{val_range},"
                   f"{func},3,{totals},,{totals})")
    else:
        formula = f"=GROUPBY({row_range},{val_range},{func},3,{totals})"

    # Clear the static grid so the spill has room (a value in its way = #SPILL!).
    for r in range(1, len(df) + 2):
        for c in range(1, len(df.columns) + 1):
            main_ws.cell(row=r, column=c).value = None
    main_ws["A1"].value = formula


def _apply_lookup(writer, main_ws, df, directive: dict) -> None:
    """Write the lookup source as its own sheet and a LIVE lookup formula into the
    column, so the result stays editable in Excel/Google Sheets.

    To keep the live file CONSISTENT with the computed preview, we add a hidden
    normalized-key helper column to the source sheet (trimmed, lowercased, numbers
    coerced to text) and match the lookup key against THAT — so case/space and
    number-vs-text differences match exactly as the preview did, not Excel's
    stricter exact match.
    """
    columns = list(df.columns)
    new_col = directive.get("new_column")
    key_col = directive.get("key_column")
    source_df = directive.get("source_df")
    skey = directive.get("source_key_column")
    sret = directive.get("return_column")
    if new_col not in columns or key_col not in columns or source_df is None:
        return
    src_cols = list(source_df.columns)
    if skey not in src_cols or sret not in src_cols or len(source_df) == 0:
        return

    sheet_name = _safe_sheet_name(directive.get("source_name") or "Lookup", set(writer.book.sheetnames))
    source_df.to_excel(writer, index=False, sheet_name=sheet_name)
    src_ws = writer.sheets[sheet_name]
    _disarm_injection(src_ws)  # the lookup source is uploaded data too

    n = len(source_df)
    q = sheet_name.replace("'", "''")
    sret_l = get_column_letter(src_cols.index(sret) + 1)
    key_l = get_column_letter(columns.index(key_col) + 1)
    target = columns.index(new_col) + 1

    # Write the hidden normalized-key helper column just past the source's columns.
    norm_keys = directive.get("source_norm_keys") or []
    helper_idx = len(src_cols) + 1
    helper_l = get_column_letter(helper_idx)
    src_ws.cell(row=1, column=helper_idx, value="_match_key")
    for i, k in enumerate(norm_keys[:n]):
        src_ws.cell(row=i + 2, column=helper_idx, value=k)
    src_ws.column_dimensions[helper_l].hidden = True

    match_range = f"'{q}'!${helper_l}$2:${helper_l}${n + 1}"
    return_range = f"'{q}'!${sret_l}$2:${sret_l}${n + 1}"
    # Rows matched only by fuzzy similarity (Phase 3.1) get a STATIC value — a typo
    # match ("Jon"→"John") can't be reproduced by an exact-match Excel formula.
    static_overrides = directive.get("static_overrides") or {}
    for i in range(len(df)):
        r = i + 2
        if i in static_overrides:
            main_ws.cell(row=r, column=target, value=static_overrides[i])
        else:
            formula = _lookup_formula(f"{key_l}{r}", match_range, return_range)
            main_ws.cell(row=r, column=target, value=formula)


def _norm_key_formula(key_cell: str) -> str:
    """Excel expression that normalizes a key cell the same way the backend does:
    coerce to text (so 123 == "123"), trim spaces, lowercase."""
    return f'LOWER(TRIM({key_cell}&""))'


def _lookup_formula(key_cell: str, match_range: str, return_range: str) -> str:
    """Build the lookup formula in the configured style, matching against the
    normalized helper column so live results equal the preview.

    INDEX/MATCH (default) works in every Excel version and Google Sheets; XLOOKUP
    is cleaner but needs Excel 2021/365 or Google Sheets.
    """
    key = _norm_key_formula(key_cell)
    if config.LOOKUP_STYLE == "xlookup":
        return f'=XLOOKUP({key},{match_range},{return_range},"Not found")'
    return f'=IFERROR(INDEX({return_range},MATCH({key},{match_range},0)),"Not found")'


def _apply_format(ws, df, directive: dict) -> None:
    columns = list(df.columns)
    if directive.get("bold_header"):
        for cell in ws[1]:
            cell.font = Font(bold=True)
    fmt = directive.get("format")
    if not fmt:
        return
    code = _number_format_code(
        fmt, directive.get("decimals"), directive.get("currency_symbol"),
        directive.get("date_format"),
    )
    for col in directive.get("columns") or []:
        if col not in columns:
            continue
        col_idx = columns.index(col) + 1  # openpyxl columns are 1-based
        for row in range(2, ws.max_row + 1):  # skip the header row
            ws.cell(row=row, column=col_idx).number_format = code


def _excel_sheet_ref(sheet: str) -> str:
    """Quote a sheet name for use in a formula when it isn't a plain identifier."""
    return sheet if re.fullmatch(r"[A-Za-z0-9_]+", sheet) else "'" + sheet.replace("'", "''") + "'"


def _apply_formula(writer, ws, df, directive: dict) -> None:
    """Write LIVE Excel formulas down the formula column, translating the Phase-1.1
    template grammar into real references from the final layout:
        {Col}        -> B2           (this row's cell)
        {Col:}       -> $B$2:$B$201  (the column's data range)
        {Sheet.Col:} -> Prices!$B$2:$B$6 (another sheet's range, located by header)
    A directive marked spill (UNIQUE/SORT/FILTER/SEQUENCE) is written ONCE in the first
    data cell — Excel spills the rest. If any reference can't be resolved in the final
    workbook (column dropped, sheet renamed), the computed values are kept instead of
    writing a broken formula."""
    columns = list(df.columns)
    name = directive.get("column")
    template = directive.get("formula") or ""
    if name not in columns:
        return
    last_row = len(df) + 1  # data ends here (row 1 is the header)

    def resolve(inner: str, excel_row: int) -> str | None:
        text = inner.strip()
        is_range = text.endswith(":")
        if is_range:
            text = text[:-1].strip()
        if is_range and "." in text:
            sheet, col = (p.strip() for p in text.split(".", 1))
            real = _resolve_sheet_name(writer.sheets.keys(), sheet)
            if real is None and sheet in (directive.get("source_sheets") or {}):
                # The referenced sheet isn't in the output — write it from the data the
                # executor attached, so the live formula has a real range to point at.
                src_df = directive["source_sheets"][sheet]
                real = _safe_sheet_name(sheet, set(writer.book.sheetnames))
                src_df.to_excel(writer, index=False, sheet_name=real)
                _disarm_injection(writer.sheets[real])
            ws2 = writer.sheets.get(real) if real else None
            if ws2 is None:
                return None
            sheet = real
            headers = [c.value for c in ws2[1]]
            if col not in headers:
                return None
            letter = get_column_letter(headers.index(col) + 1)
            return f"{_excel_sheet_ref(sheet)}!${letter}$2:${letter}${max(ws2.max_row, 2)}"
        if text not in columns:
            return None
        letter = get_column_letter(columns.index(text) + 1)
        if is_range:
            return f"${letter}$2:${letter}${last_row}"
        return f"{letter}{excel_row}"

    # Dry-run row 2 first: if anything is unresolvable, keep the computed values.
    if any(resolve(m, 2) is None for m in _PLACEHOLDER.findall(template)):
        return

    target_idx = columns.index(name) + 1
    if directive.get("spill"):
        # A spill needs EMPTY cells below its anchor or Excel shows #SPILL! — clear the
        # padded preview values, then write the one spilling formula. (Assign .value
        # directly: openpyxl's cell(value=None) leaves the cell untouched.)
        for excel_row in range(3, last_row + 1):
            ws.cell(row=excel_row, column=target_idx).value = None
        rows = [2]
    else:
        rows = range(2, last_row + 1)
    for excel_row in rows:
        cell_formula = _PLACEHOLDER.sub(lambda m: resolve(m.group(1), excel_row), template)
        ws.cell(row=excel_row, column=target_idx, value="=" + cell_formula)


def _apply_cf(ws, df, directive: dict) -> None:
    """Turn a 'cf' directive into REAL Excel conditional-formatting rules (Phase 1.2) —
    the rules stay live, so highlights update as the user edits the file."""
    from .operations.conditional_format import COLORS as _CF_COLORS
    from .operations.conditional_format import ICON_SETS as _CF_ICONS
    from .operations.conditional_format import STRONG as _CF_STRONG

    columns = list(df.columns)
    rule = directive.get("rule_type")
    fill_name = directive.get("color") or {"duplicates": "red", "blanks": "yellow"}.get(rule, "green")
    fill_hex, font_hex = _CF_COLORS.get(fill_name, _CF_COLORS["green"])
    fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
    font = Font(color=font_hex)
    dxf = DifferentialStyle(fill=fill, font=font)
    last = len(df) + 1
    value, value2 = directive.get("value"), directive.get("value2")

    for col in directive.get("columns") or []:
        if col not in columns:
            continue
        letter = get_column_letter(columns.index(col) + 1)
        rng = f"{letter}2:{letter}{last}"
        obj = None
        if rule in ("greater_than", "less_than", "equal_to", "not_equal", "between"):
            op_name = {"greater_than": "greaterThan", "less_than": "lessThan",
                       "equal_to": "equal", "not_equal": "notEqual", "between": "between"}[rule]
            formulas = [str(value)] + ([str(value2)] if rule == "between" else [])
            obj = CellIsRule(operator=op_name, formula=formulas, fill=fill, font=font)
        elif rule == "text_contains":
            text = str(value).replace('"', '""')
            obj = FormulaRule(
                formula=[f'ISNUMBER(SEARCH("{text}",{letter}2))'], fill=fill, font=font)
        elif rule in ("date_before", "date_after"):
            d = pd.to_datetime(str(value))
            cmp_ = "<" if rule == "date_before" else ">"
            obj = FormulaRule(
                formula=[f"AND({letter}2<>\"\",{letter}2{cmp_}DATE({d.year},{d.month},{d.day}))"],
                fill=fill, font=font)
        elif rule == "blanks":
            obj = Rule(type="containsBlanks", dxf=dxf,
                       formula=[f"LEN(TRIM({letter}2))=0"])
        elif rule in ("duplicates", "unique"):
            obj = Rule(type="duplicateValues" if rule == "duplicates" else "uniqueValues", dxf=dxf)
        elif rule in ("top_n", "bottom_n"):
            obj = Rule(type="top10", rank=int(directive.get("count") or 10),
                       percent=bool(directive.get("percent")), bottom=rule == "bottom_n", dxf=dxf)
        elif rule == "color_scale":
            obj = ColorScaleRule(
                start_type="min", start_color="F8696B",
                mid_type="percentile", mid_value=50, mid_color="FFEB84",
                end_type="max", end_color="63BE7B")
        elif rule == "data_bars":
            obj = DataBarRule(start_type="min", end_type="max",
                              color=_CF_STRONG.get(fill_name, "638EC6"))
        elif rule == "icon_set":
            obj = IconSetRule(icon_style=_CF_ICONS.get(int(directive.get("icons") or 3), "3TrafficLights1"),
                              type="percent", values=[0, 33, 67][: 3] if int(directive.get("icons") or 3) == 3
                              else ([0, 25, 50, 75] if int(directive.get("icons") or 3) == 4
                                    else [0, 20, 40, 60, 80]))
        elif rule == "formula":
            template = directive.get("formula") or ""
            # {Col} -> $<L>2 (absolute column, relative row — the CF idiom: Excel walks
            # the rule down the range); {Col:} -> the absolute data range.
            def cf_ref(m):
                text = m.group(1).strip()
                is_range = text.endswith(":")
                if is_range:
                    text = text[:-1].strip()
                if text not in columns:
                    return m.group(0)
                L = get_column_letter(columns.index(text) + 1)
                return f"${L}$2:${L}${last}" if is_range else f"${L}2"
            excel_formula = _PLACEHOLDER.sub(cf_ref, template)
            if "{" in excel_formula:  # an unresolved reference — skip rather than break the file
                continue
            obj = FormulaRule(formula=[excel_formula], fill=fill, font=font)
        if obj is not None:
            ws.conditional_formatting.add(rng, obj)


def _apply_comments(ws, df, directive: dict, offset: int = 0) -> None:
    """Attach explain-changes NOTES to cells (Phase 1.9). Values are untouched —
    openpyxl Comment objects only. `offset` accounts for a Phase-1.4 title row (this
    runs after layout because insert_rows doesn't move comment anchors)."""
    from openpyxl.comments import Comment

    columns = [str(c) for c in df.columns]
    for cell_spec in directive.get("cells") or []:
        col = cell_spec.get("column")
        if col not in columns:
            continue
        row = int(cell_spec.get("row") or 0) + offset
        if row < 1:
            continue
        c = ws.cell(row=row, column=columns.index(col) + 1)
        c.comment = Comment(f"Sumio: {cell_spec.get('text') or ''}", "Sumio", height=80, width=260)
    if directive.get("summary"):
        a1 = ws.cell(row=1, column=1)
        a1.comment = Comment(f"Sumio — what changed:\n{directive['summary']}", "Sumio",
                             height=140, width=320)


def _apply_table(ws, df, directive: dict) -> None:
    """Turn a 'table' directive into a native Excel Table (Phase 1.7): banded rows,
    header filters, chosen style, and a live =SUBTOTAL() totals row. Re-running
    REPLACES any table that overlaps or shares the name instead of corrupting the file."""
    from openpyxl.worksheet.table import Table, TableColumn, TableStyleInfo

    columns = [str(c) for c in df.columns]
    if not columns or len(df) == 0:
        return
    totals: dict = directive.get("totals") or {}
    name = directive.get("name") or "SumioTable"
    last_col = get_column_letter(len(columns))
    last_data_row = len(df) + 1
    ref = f"A1:{last_col}{last_data_row + (1 if totals else 0)}"

    # Replace, never duplicate: drop tables that share the name or overlap our range.
    for existing in list(ws.tables.values()):
        if existing.displayName == name or str(existing.ref).split(":")[0] == "A1":
            del ws.tables[existing.displayName]
    while name in ws.parent.defined_names or any(name in s.tables for s in ws.parent.worksheets):
        name = name + "_2"

    tcols = []
    for i, col in enumerate(columns, start=1):
        kwargs = {"id": i, "name": col}
        spec = totals.get(col)
        if spec:
            kwargs["totalsRowFunction"] = spec["agg"] if spec["agg"] != "average" else "average"
        elif totals and i == 1:
            kwargs["totalsRowLabel"] = "Total"
        tcols.append(TableColumn(**kwargs))

    table = Table(displayName=name, ref=ref, tableColumns=tcols)
    if totals:
        table.totalsRowCount = 1
        table.totalsRowShown = True
        trow = last_data_row + 1
        for i, col in enumerate(columns, start=1):
            spec = totals.get(col)
            cell = ws.cell(row=trow, column=i)
            if spec:
                letter = get_column_letter(i)
                cell.value = f"=SUBTOTAL({spec['code']},{letter}2:{letter}{last_data_row})"
            elif i == 1:
                cell.value = "Total"
    table.tableStyleInfo = TableStyleInfo(
        name=directive.get("style") or "TableStyleMedium2",
        showRowStripes=True, showColumnStripes=False,
        showFirstColumn=False, showLastColumn=False,
    )
    ws.add_table(table)


def _apply_dv(writer, ws, df, directive: dict) -> None:
    """Turn a 'dv' directive into a real openpyxl DataValidation (Phase 1.5). Long or
    comma-containing dropdown lists go on a hidden helper sheet (Excel's inline list
    syntax caps at 255 chars and splits on commas)."""
    from openpyxl.worksheet.datavalidation import DataValidation

    columns = list(df.columns)
    vtype = directive.get("validation_type")
    min_v, max_v = directive.get("min_value"), directive.get("max_value")
    last = len(df) + 1

    kwargs: dict = {"allow_blank": bool(directive.get("allow_blank", True))}
    if vtype == "list":
        allowed = directive.get("allowed_values") or []
        inline = ",".join(allowed)
        if len(inline) <= 250 and not any("," in v for v in allowed):
            kwargs.update(type="list", formula1=f'"{inline}"')
        else:
            # Helper sheet with one option per row, hidden, referenced by range.
            name = _safe_sheet_name("Options", set(writer.book.sheetnames))
            opts = writer.book.create_sheet(name)
            for i, v in enumerate(allowed, start=1):
                opts.cell(row=i, column=1, value=v)
            opts.sheet_state = "hidden"
            writer.sheets[name] = opts
            kwargs.update(type="list", formula1=f"={_excel_sheet_ref(name)}!$A$1:$A${len(allowed)}")
    elif vtype in ("whole", "decimal"):
        kwargs["type"] = vtype
        if min_v is not None and max_v is not None:
            kwargs.update(operator="between", formula1=str(min_v), formula2=str(max_v))
        elif min_v is not None:
            kwargs.update(operator="greaterThanOrEqual", formula1=str(min_v))
        else:
            kwargs.update(operator="lessThanOrEqual", formula1=str(max_v))
    elif vtype == "date":
        def dfx(v):
            d = pd.to_datetime(str(v))
            return f"DATE({d.year},{d.month},{d.day})"
        kwargs["type"] = "date"
        if min_v is not None and max_v is not None:
            kwargs.update(operator="between", formula1=dfx(min_v), formula2=dfx(max_v))
        elif min_v is not None:
            kwargs.update(operator="greaterThanOrEqual", formula1=dfx(min_v))
        else:
            kwargs.update(operator="lessThanOrEqual", formula1=dfx(max_v))
    elif vtype == "text_length":
        kwargs["type"] = "textLength"
        if min_v is not None and max_v is not None:
            kwargs.update(operator="between", formula1=str(int(min_v)), formula2=str(int(max_v)))
        elif min_v is not None:
            kwargs.update(operator="greaterThanOrEqual", formula1=str(int(min_v)))
        else:
            kwargs.update(operator="lessThanOrEqual", formula1=str(int(max_v)))
    elif vtype == "custom":
        template = directive.get("formula") or ""

        def dv_ref(m):
            text = m.group(1).strip()
            is_range = text.endswith(":")
            if is_range:
                text = text[:-1].strip()
            if text not in columns:
                return m.group(0)
            letter = get_column_letter(columns.index(text) + 1)
            return f"${letter}$2:${letter}${last}" if is_range else f"${letter}2"
        excel_formula = _PLACEHOLDER.sub(dv_ref, template)
        if "{" in excel_formula:
            return  # unresolved reference — keep the file clean rather than broken
        kwargs.update(type="custom", formula1=excel_formula)
    else:
        return

    dv = DataValidation(**kwargs)
    dv.showInputMessage = True
    dv.showErrorMessage = True
    if directive.get("input_message"):
        dv.promptTitle = "Sumio"
        dv.prompt = directive["input_message"][:255]
    if directive.get("error_message"):
        dv.errorTitle = "Not allowed"
        dv.error = directive["error_message"][:255]
    for col in directive.get("columns") or []:
        if col not in columns:
            continue
        letter = get_column_letter(columns.index(col) + 1)
        dv.add(f"{letter}2:{letter}{last}")
    ws.add_data_validation(dv)


def _shift_sqref_down(sqref: str, by: int) -> str:
    """'E2:E201 A5' -> 'E3:E202 A6' (used when a title row is inserted)."""
    return re.sub(r"(\d+)", lambda m: str(int(m.group(1)) + by), sqref)


def _apply_layout(ws, df, directive: dict) -> None:
    """Apply a 'layout' directive (Phase 1.4). Runs AFTER all other directives (see
    _apply_render): inserting the title row must shift already-written live formulas
    (openpyxl Translator) and conditional-formatting rules down with the data."""
    from openpyxl.formatting.formatting import ConditionalFormattingList
    from openpyxl.formula.translate import Translator
    from openpyxl.styles import Border, Side

    from .operations.conditional_format import COLORS as _CF_COLORS

    columns = list(df.columns)
    ncols = max(1, len(columns))
    offset = 0

    if directive.get("title"):
        ws.insert_rows(1)
        offset = 1
        # Live formulas were written for data-at-row-2 — walk them one row down.
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    here = f"{cell.column_letter}{cell.row}"
                    was = f"{cell.column_letter}{cell.row - 1}"
                    try:
                        cell.value = Translator(cell.value, origin=was).translate_formula(here)
                    except Exception:
                        pass  # leave the formula as-is rather than corrupt it
        # Conditional-formatting ranges (and their row-anchored formulas) shift too.
        old = [(str(cf.sqref), list(cf.rules)) for cf in ws.conditional_formatting]
        ws.conditional_formatting = ConditionalFormattingList()
        for sqref, rules in old:
            for rule in rules:
                if getattr(rule, "formula", None):
                    try:
                        rule.formula = [
                            Translator("=" + f, origin="A1").translate_formula("A2")[1:]
                            for f in rule.formula
                        ]
                    except Exception:
                        pass
                ws.conditional_formatting.add(_shift_sqref_down(sqref, 1), rule)
        # Native Excel Tables shift their whole ref down with the data.
        for tname in list(ws.tables):
            ws.tables[tname].ref = _shift_sqref_down(str(ws.tables[tname].ref), 1)
        # Defined names anchored to THIS sheet shift their absolute rows too.
        for dn_name in list(ws.parent.defined_names):
            dn = ws.parent.defined_names[dn_name]
            if dn.attr_text and ws.title in str(dn.attr_text):
                dn.attr_text = _shift_sqref_down(str(dn.attr_text), 1)
        # Data-validation ranges (and any row-relative custom formulas) shift too.
        for dv in list(ws.data_validations.dataValidation):
            dv.sqref = _shift_sqref_down(str(dv.sqref), 1)
            for attr in ("formula1", "formula2"):
                f = getattr(dv, attr, None)
                if isinstance(f, str) and f and not f.startswith('"') and "!" not in f:
                    try:
                        setattr(dv, attr, Translator("=" + f, origin="A1").translate_formula("A2")[1:])
                    except Exception:
                        pass
        last_col = get_column_letter(ncols)
        ws.merge_cells(f"A1:{last_col}1")
        tcell = ws["A1"]
        tcell.value = directive["title"]
        tcell.font = Font(bold=True, size=14)
        tcell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 24

    header_row = 1 + offset
    first_data_row = 2 + offset
    last_row = len(df) + 1 + offset

    if directive.get("merge_range"):
        ws.merge_cells(_shift_sqref_down(directive["merge_range"], offset))

    if directive.get("header_fill"):
        fill_hex, font_hex = _CF_COLORS.get(directive["header_fill"], _CF_COLORS["blue"])
        fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
        for c in range(1, ncols + 1):
            cell = ws.cell(row=header_row, column=c)
            cell.fill = fill
            cell.font = Font(bold=True, color=font_hex)

    if directive.get("borders"):
        thin = Side(style="thin", color="B0B0B0")
        if directive["borders"] == "all":
            box = Border(left=thin, right=thin, top=thin, bottom=thin)
            for row in ws.iter_rows(min_row=header_row, max_row=last_row, min_col=1, max_col=ncols):
                for cell in row:
                    cell.border = box
        else:  # outline: box around the used range only
            for r in range(header_row, last_row + 1):
                for c in range(1, ncols + 1):
                    edges = {}
                    if r == header_row:
                        edges["top"] = thin
                    if r == last_row:
                        edges["bottom"] = thin
                    if c == 1:
                        edges["left"] = thin
                    if c == ncols:
                        edges["right"] = thin
                    if edges:
                        cell = ws.cell(row=r, column=c)
                        old_border = cell.border
                        cell.border = Border(
                            left=edges.get("left", old_border.left),
                            right=edges.get("right", old_border.right),
                            top=edges.get("top", old_border.top),
                            bottom=edges.get("bottom", old_border.bottom),
                        )

    if directive.get("autofit"):
        # openpyxl has no real autofit — compute a good width from the content: the
        # header, and the widest of a sample of values (capped so one novel doesn't
        # blow the layout).
        for i, col in enumerate(columns):
            s = df[col].astype("string").fillna("")
            sample = s.iloc[:500]
            widest = int(sample.map(len).max() or 0) if len(sample) else 0
            width = min(60, max(len(str(col)), widest) + 2)
            ws.column_dimensions[get_column_letter(i + 1)].width = width

    if directive.get("freeze"):
        freeze = directive["freeze"]
        anchor = {
            "header": f"A{first_data_row}",
            "first_column": "B1",
            "both": f"B{first_data_row}",
        }.get(freeze)
        if anchor is None:  # explicit cell — shift with the title like everything else
            anchor = _shift_sqref_down(freeze.upper(), offset)
        ws.freeze_panes = anchor

    if directive.get("print"):
        _apply_print_setup(ws, directive["print"], offset, header_row)


def _apply_print_setup(ws, ps: dict, offset: int, header_row: int) -> None:
    """Phase 2.7 — page/print setup on the saved sheet. Display-only: never touches the
    data. `offset` (1 if a title row was inserted) keeps the print area / repeat rows
    aligned with the shifted data."""
    from openpyxl.worksheet.page import PageMargins
    from openpyxl.worksheet.properties import PageSetupProperties

    if ps.get("orientation") in ("landscape", "portrait"):
        ws.page_setup.orientation = ps["orientation"]
    if ps.get("fit_wide"):
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.fitToWidth = int(ps["fit_wide"])
        ws.page_setup.fitToHeight = 0  # 0 = however many pages tall it needs
    if ps.get("print_area"):
        ws.print_area = _shift_sqref_down(ps["print_area"], offset)
    if ps.get("repeat_header"):
        # Repeat everything from row 1 through the header row (includes a title if present).
        ws.print_title_rows = f"1:{header_row}"
    if ps.get("margins"):
        preset = {
            "narrow": dict(left=0.25, right=0.25, top=0.75, bottom=0.75, header=0.3, footer=0.3),
            "wide": dict(left=1.0, right=1.0, top=1.0, bottom=1.0, header=0.5, footer=0.5),
            "normal": dict(left=0.7, right=0.7, top=0.75, bottom=0.75, header=0.3, footer=0.3),
        }.get(ps["margins"])
        if preset:
            ws.page_margins = PageMargins(**preset)
    if ps.get("header_text"):
        ws.oddHeader.center.text = ps["header_text"]
    if ps.get("footer_text"):
        ws.oddFooter.center.text = ps["footer_text"]


def _apply_sheet_protect(writer, directive: dict) -> None:
    """Phase 2.8 — lock/unlock a sheet's cells via openpyxl sheet protection. PASSWORD-LESS
    by design: Sumio never sets or stores an open/file password (that's user-driven). Any
    'allow editing' columns are unlocked before protection is turned on (all cells are
    locked by default, so protecting the sheet freezes everything except those)."""
    from openpyxl.styles import Protection

    real = _resolve_sheet_name(writer.sheets.keys(), directive.get("sheet_name") or "")
    if real is None:
        return
    ws = writer.sheets[real]
    if not directive.get("protect"):
        ws.protection.sheet = False
        return
    allow = {str(c).strip().lower() for c in (directive.get("allow_columns") or [])}
    if allow and ws.max_row >= 1:
        header = {str(ws.cell(row=1, column=c).value).strip().lower(): c
                  for c in range(1, ws.max_column + 1)}
        idxs = [header[a] for a in allow if a in header]
        for r in range(2, ws.max_row + 1):
            for ci in idxs:
                ws.cell(row=r, column=ci).protection = Protection(locked=False)
    ws.protection.sheet = True


def _apply_highlight(ws, df, directive: dict) -> None:
    """Shade blank cells yellow without changing their (empty) value."""
    fill = PatternFill(start_color="FFF59D", end_color="FFF59D", fill_type="solid")
    columns = list(df.columns)
    for col in directive.get("columns") or []:
        if col not in columns:
            continue
        col_idx = columns.index(col) + 1
        series = df[col]
        for i in range(len(df)):
            v = series.iloc[i]
            if pd.isna(v) or (isinstance(v, str) and v.strip() == ""):
                ws.cell(row=i + 2, column=col_idx).fill = fill


# Friendly date-format names -> Excel number-format codes. Default is DD-MM-YYYY
# (Sameer's client-report style). The Brain may also pass a raw Excel code.
_DATE_FORMATS = {
    "dd-mm-yyyy": "dd-mm-yyyy",
    "mm-dd-yyyy": "mm-dd-yyyy",
    "yyyy-mm-dd": "yyyy-mm-dd",
    "dd/mm/yyyy": "dd/mm/yyyy",
    "mm/dd/yyyy": "mm/dd/yyyy",
    "dd-mmm-yyyy": "dd-mmm-yyyy",        # 09-Jun-2026
    "d mmmm yyyy": "d mmmm yyyy",        # 9 June 2026
    "mmmm d, yyyy": 'mmmm d", "yyyy',    # June 9, 2026
}


def _date_format_code(date_format) -> str:
    if not date_format:
        return "dd-mm-yyyy"
    key = str(date_format).strip().lower()
    if key in _DATE_FORMATS:
        return _DATE_FORMATS[key]
    # Looks like a raw Excel date code (only d/m/y, separators, spaces) -> pass through.
    if re.fullmatch(r"[dmyDMY/\-\. ,]+", str(date_format).strip()):
        return str(date_format).strip()
    return "dd-mm-yyyy"


def _number_format_code(fmt: str, decimals, symbol, date_format=None) -> str:
    """Translate a friendly format name into an Excel number-format code."""
    d = decimals if isinstance(decimals, int) and decimals >= 0 else 2
    dec = "." + "0" * d if d > 0 else ""
    if fmt == "currency":
        return f'"{symbol or "₹"}"#,##0{dec}'
    if fmt in ("indian_currency", "indian", "lakh", "crore"):
        # Indian digit grouping (12,34,56,789) via conditional sections — the lakh/crore
        # comma pattern Excel can't produce with plain #,##0. (True unit SCALING to
        # lakhs/crores needs a divided column — a format code can only scale by 1000s.)
        sym = f'"{symbol or "₹"}"'
        return (
            f"[>=10000000]{sym}#\\,##\\,##\\,##\\,##0{dec};"
            f"[>=100000]{sym}#\\,##\\,##\\,##0{dec};"
            f"{sym}#,##0{dec}"
        )
    if fmt == "percent":
        return f"0{dec}%"
    if fmt == "number":
        return f"#,##0{dec}"
    if fmt == "date":
        return _date_format_code(date_format)
    return "General"


# Shown for any UNEXPECTED failure, so a bug never reaches the user as a stack
# trace or raw error code (PRD 1.14-d / "no raw error codes ever reach the user").
_INTERNAL_ERROR = (
    "Something went wrong on our side while processing your file — "
    "please try again, or rephrase your instruction."
)


def _size_message(total: int) -> str:
    return (
        f"That upload is too large (~{total / 1024 / 1024:.0f} MB). "
        f"Please keep files under {config.MAX_UPLOAD_MB} MB."
    )


def _too_big(files) -> str | None:
    """Friendly message if the DECLARED combined upload size exceeds the limit.

    This is the cheap pre-check: it rejects an oversized upload before we read it. It is
    not sufficient on its own — UploadFile.size can be None when the parser hasn't
    populated it, and `or 0` then scores the file as empty and lets it through. Pair it
    with _too_big_read() after the bytes are in hand (Track 5 item 5).
    """
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    total = sum((getattr(f, "size", None) or 0) for f in files)
    return _size_message(total) if total > limit else None


def _too_big_read(uploads) -> str | None:
    """The same limit, measured against the bytes actually read.

    Closes the fail-open path above: whatever the declared size said, this is the real
    number. Cheap — the bytes are already in memory by this point — and it means an
    upload with no declared size can no longer slip past the guard.
    """
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    total = sum(len(b or b"") for _, b in uploads)
    return _size_message(total) if total > limit else None


# --------------------------------------------------------------------------- #
# Collaborative workspace (Phase 3.8) — thin HTTP wrappers over app/collab.py.
# --------------------------------------------------------------------------- #
def _collab_state(ws_id: str) -> dict:
    """state_summary with /inspect's table summarizer injected (avoids a circular import)."""
    return collab.state_summary(ws_id, table_summarizer=lambda df: summarize_structure(df, sample_rows=5))


def _collab_error(exc: collab.CollabError) -> JSONResponse:
    return _error(str(exc), status=getattr(exc, "status", 400))


def _sync_linked_session(ws_id: str) -> None:
    """After a workspace change is APPLIED, push the new data into the session it was
    created from, so the chat's /parse view (and grid) stay consistent with the shared
    state. No-op when the session is gone or was never linked."""
    try:
        sid = collab.linked_session(ws_id)
        if not sid or sid not in _SESSIONS:
            return
        state = collab.current_state(ws_id)
        _push_state(sid, {
            "tables": dict(state["tables"]),
            "primary": state["primary"],
            "exts": dict(state.get("exts", {})),
        })
    except Exception:
        pass  # syncing is best-effort; never fail the request over it


@app.post("/workspace/create")
async def workspace_create(
    session_id: str = Form(...),
    name: str = Form(""),
    user_id: str = Form(...),
    user_name: str = Form(""),
    require_approval: str = Form("true"),
) -> JSONResponse:
    """Turn the caller's current session data into a SHARED workspace others can join.
    Reuses the data already loaded for `session_id` (via /inspect), so no re-upload."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first, then share it as a workspace.", status=400)
    base = entry["states"][-1]
    # Copy the state so later single-user edits to the session don't mutate the workspace.
    seed = {"tables": dict(base["tables"]), "primary": base["primary"], "exts": dict(base.get("exts", {}))}
    needs_approval = str(require_approval).strip().lower() not in ("0", "false", "no", "")
    try:
        ws = collab.create_workspace(
            name, user_id, user_name, seed, require_approval=needs_approval, session_id=session_id,
        )
    except collab.CollabError as exc:
        return _collab_error(exc)
    entry["workspace"] = ws["id"]  # link the session to its workspace
    return JSONResponse({"status": "ok", **_collab_state(ws["id"])})


@app.get("/workspace/{ws_id}")
async def workspace_get(ws_id: str) -> JSONResponse:
    """Current workspace snapshot: members, revision, comments, approval queue, log."""
    try:
        return JSONResponse({"status": "ok", **_collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/join")
async def workspace_join(
    ws_id: str,
    user_id: str = Form(...),
    user_name: str = Form(""),
    role: str = Form("editor"),
) -> JSONResponse:
    try:
        collab.join_workspace(ws_id, user_id, user_name, role)
        return JSONResponse({"status": "ok", **_collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/propose")
async def workspace_propose(
    ws_id: str,
    user_id: str = Form(...),
    plan: str = Form(...),
    base_revision: int = Form(...),
    summary: str = Form(""),
) -> JSONResponse:
    """Propose a data change (a plan of operations). Applies immediately when approval is
    off; otherwise it joins the approval queue. Concurrency-checked against base_revision."""
    try:
        parsed = json.loads(plan)
    except Exception:
        return _error("The change plan wasn't valid JSON.", status=400)
    operations = parsed.get("operations") if isinstance(parsed, dict) else parsed
    try:
        result = collab.propose_change(ws_id, user_id, operations, base_revision, summary or None)
    except collab.CollabError as exc:
        return _collab_error(exc)
    outcome = result.pop("status")  # "applied" | "pending" — keep top-level status="ok"
    if outcome == "applied":
        _sync_linked_session(ws_id)  # approval off: change is live → sync the session
    return JSONResponse({"status": "ok", "outcome": outcome, **result, "workspace": _collab_state(ws_id)})


@app.post("/workspace/{ws_id}/approve")
async def workspace_approve(
    ws_id: str,
    user_id: str = Form(...),
    change_id: str = Form(...),
) -> JSONResponse:
    try:
        result = collab.approve_change(ws_id, user_id, change_id)
        outcome = result.pop("status")
        _sync_linked_session(ws_id)  # approved change is now live → sync the session
        return JSONResponse({"status": "ok", "outcome": outcome, **result, "workspace": _collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/reject")
async def workspace_reject(
    ws_id: str,
    user_id: str = Form(...),
    change_id: str = Form(...),
    reason: str = Form(""),
) -> JSONResponse:
    try:
        result = collab.reject_change(ws_id, user_id, change_id, reason)
        outcome = result.pop("status")
        return JSONResponse({"status": "ok", "outcome": outcome, **result, "workspace": _collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/withdraw")
async def workspace_withdraw(
    ws_id: str,
    user_id: str = Form(...),
    change_id: str = Form(...),
) -> JSONResponse:
    """The proposer (or an owner) cancels their own pending change — no approver needed."""
    try:
        result = collab.withdraw_change(ws_id, user_id, change_id)
        outcome = result.pop("status")
        return JSONResponse({"status": "ok", "outcome": outcome, **result, "workspace": _collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/comment")
async def workspace_comment(
    ws_id: str,
    user_id: str = Form(...),
    text: str = Form(...),
    target: str = Form(""),
) -> JSONResponse:
    parsed_target = None
    if target.strip():
        try:
            parsed_target = json.loads(target)
        except Exception:
            parsed_target = target  # plain-text target is fine too
    try:
        comment = collab.add_comment(ws_id, user_id, text, parsed_target)
        return JSONResponse({"status": "ok", "comment": comment, "workspace": _collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


@app.post("/workspace/{ws_id}/comment/{comment_id}/resolve")
async def workspace_resolve_comment(
    ws_id: str,
    comment_id: str,
    user_id: str = Form(...),
) -> JSONResponse:
    try:
        comment = collab.resolve_comment(ws_id, user_id, comment_id)
        return JSONResponse({"status": "ok", "comment": comment, "workspace": _collab_state(ws_id)})
    except collab.CollabError as exc:
        return _collab_error(exc)


# --------------------------------------------------------------------------- #
# Export & distribution (Phase 3.7).
# --------------------------------------------------------------------------- #
_EXPORT_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _download_response(out_bytes: bytes, filename: str, mime: str) -> dict:
    """Package generated bytes as a download (streamed id + inline base64 when small)."""
    download_id = _store_result(out_bytes, filename, mime)
    inline = (
        base64.b64encode(out_bytes).decode("ascii")
        if len(out_bytes) <= _INLINE_MAX_BYTES else None
    )
    return {
        "status": "ok", "filename": filename, "media_type": mime,
        "file_size": len(out_bytes), "download_id": download_id, "file_base64": inline,
    }


def _render_session(tables: dict, fmt: str, title: str) -> tuple[bytes, str, str]:
    """Render a session's tables to pdf/pptx/xlsx → (bytes, filename, mime)."""
    safe = _safe_filename(title) or "export"
    if fmt == "xlsx":
        out_bytes, out_name, mime = _serialize_workbook(tables, safe)
        return out_bytes, out_name, mime
    data, ext = exports.render(tables, fmt, title=title)
    return data, f"{safe}.{ext}", _EXPORT_MIME[ext]


def _render_schedule(schedule: dict) -> tuple[bytes, str, str]:
    """Render the data behind a schedule (its source session) into its chosen format.
    Raises if the underlying session is gone — surfaced as a reported delivery failure."""
    sid = (schedule.get("source") or {}).get("session_id")
    entry = _SESSIONS.get(sid) if sid else None
    if not entry or not entry.get("states"):
        raise RuntimeError("The data for this schedule is no longer available on the server.")
    tables = entry["states"][-1]["tables"]
    return _render_session(tables, schedule["format"], schedule.get("name") or "Sumio report")


def _parse_recipients(raw: str) -> list[str]:
    """Accept a JSON array or a comma/semicolon/newline-separated string of recipients."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]


def _sched_error(exc: distribution.ScheduleError) -> JSONResponse:
    return _error(str(exc), status=getattr(exc, "status", 400))


@app.post("/export")
async def export_data(session_id: str = Form(...), format: str = Form("pdf")) -> JSONResponse:
    """Export the session's current data to a downloadable PDF / PPTX / XLSX. Pure export
    — it never sends anything to anyone."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first, then export it.", status=400)
    fmt = (format or "pdf").lower()
    if fmt not in _EXPORT_MIME:
        return _error("Format must be pdf, pptx, or xlsx.", status=400)
    try:
        out_bytes, name, mime = _render_session(entry["states"][-1]["tables"], fmt, "Sumio export")
    except ValueError as exc:
        return _error(str(exc), status=400)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)
    return JSONResponse(_download_response(out_bytes, name, mime))


@app.post("/distribution/create")
async def dist_create(
    session_id: str = Form(...),
    channel: str = Form(...),
    recipients: str = Form(...),
    name: str = Form(""),
    format: str = Form("pdf"),
    cadence: str = Form("manual"),
    user_id: str = Form(""),
) -> JSONResponse:
    """Create a DRAFT delivery schedule. Never sends — the schedule must be armed first."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first, then set up delivery.", status=400)
    try:
        s = distribution.create_schedule(
            name, user_id or "owner", channel, _parse_recipients(recipients),
            format, cadence, source={"session_id": session_id},
        )
    except distribution.ScheduleError as exc:
        return _sched_error(exc)
    return JSONResponse({"status": "ok", "schedule": s})


@app.get("/distribution/list")
async def dist_list(user_id: str = "") -> JSONResponse:
    return JSONResponse({"status": "ok", "schedules": distribution.list_schedules(user_id or None)})


@app.get("/distribution/{schedule_id}")
async def dist_get(schedule_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "schedule": distribution.get_schedule(schedule_id)})
    except distribution.ScheduleError as exc:
        return _sched_error(exc)


@app.post("/distribution/{schedule_id}/arm")
async def dist_arm(schedule_id: str, confirm: str = Form("false")) -> JSONResponse:
    """Arm a schedule so it can send on cadence — REQUIRES confirm=true (explicit setup)."""
    ok = str(confirm).strip().lower() in ("1", "true", "yes")
    try:
        s = distribution.arm_schedule(schedule_id, ok)
    except distribution.ScheduleError as exc:
        return _sched_error(exc)
    return JSONResponse({"status": "ok", "schedule": s})


@app.post("/distribution/{schedule_id}/pause")
async def dist_pause(schedule_id: str) -> JSONResponse:
    try:
        s = distribution.pause_schedule(schedule_id)
    except distribution.ScheduleError as exc:
        return _sched_error(exc)
    return JSONResponse({"status": "ok", "schedule": s})


@app.post("/distribution/{schedule_id}/delete")
async def dist_delete(schedule_id: str) -> JSONResponse:
    try:
        distribution.delete_schedule(schedule_id)
    except distribution.ScheduleError as exc:
        return _sched_error(exc)
    return JSONResponse({"status": "ok"})


@app.post("/distribution/{schedule_id}/send-now")
async def dist_send_now(schedule_id: str, confirm: str = Form("false")) -> JSONResponse:
    """Send a schedule immediately — REQUIRES confirm=true. Per-recipient failures are
    reported in the response."""
    ok = str(confirm).strip().lower() in ("1", "true", "yes")
    try:
        report = distribution.send_now(
            schedule_id, ok, _render_schedule, distribution.default_transport
        )
    except distribution.ScheduleError as exc:
        return _sched_error(exc)
    return JSONResponse(
        {"status": "ok", "report": report, "schedule": distribution.get_schedule(schedule_id)}
    )


@app.post("/distribution/run-due")
async def dist_run_due() -> JSONResponse:
    """Deliver all due, armed, active schedules (the tick a scheduler/cron would call).
    Returns a per-schedule report including any failed recipients."""
    reports = distribution.run_due(_render_schedule, distribution.default_transport)
    return JSONResponse({"status": "ok", "delivered": reports})


def _digest_email_transport(recipient: str, subject: str, body: str) -> None:
    """Adapt the shared SMTP sender to the digest's (to, subject, body) transport shape —
    a digest is a plain-text email with no attachment."""
    distribution.email_transport(recipient, subject, body, None, None)


@app.post("/digest/run-due")
async def digest_run_due() -> JSONResponse:
    """Send the weekly digest to every signed-in user who ran a task in the last 7 days and
    is due (the tick the run_due cron calls). Gated on SMTP being configured — with no mail
    server there's nothing to send, so we report 'skipped' rather than failing every user.
    Returns a per-user report (sent/failed/skipped); safe to call as often as the cron likes
    because the 7-day cadence guard prevents double-sends."""
    if not distribution.email_configured():
        return JSONResponse({
            "status": "skipped",
            "reason": "email not configured (set SUMIO_SMTP_HOST / PORT / USER / PASSWORD / FROM)",
            "sent": [],
        })
    reports = digest.run_due_digests(_digest_email_transport)
    return JSONResponse({"status": "ok", "sent": reports})


@app.post("/slack/command")
async def slack_command(request: Request) -> JSONResponse:
    """Slack slash-command webhook (`/sumio …`). Verifies the request is genuinely from
    Slack (signing-secret HMAC + replay window) before doing anything, then replies. Gated
    on SUMIO_SLACK_SIGNING_SECRET — unconfigured servers say so instead of trusting input."""
    if not slack.slack_configured():
        return JSONResponse(
            {"response_type": "ephemeral",
             "text": "Sumio's Slack app isn't set up on this server yet."}
        )
    raw = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not slack.verify_signature(slack.signing_secret(), timestamp, raw, signature):
        return _error("Could not verify this request came from Slack.", status=401)

    from urllib.parse import parse_qs

    form = parse_qs(raw.decode("utf-8", "replace"))
    text = form.get("text", [""])[0]
    user_name = form.get("user_name", [None])[0]
    return JSONResponse(slack.handle_command(text, user_name))


# --------------------------------------------------------------------------- #
# OIDC single sign-on (enterprise SSO). Config-gated; see app/oidc.py.
# --------------------------------------------------------------------------- #
@app.get("/auth/oidc/status")
async def oidc_status() -> JSONResponse:
    """Whether SSO is configured on this server (the login page shows the button if so)."""
    return JSONResponse({"enabled": oidc.oidc_configured()})


@app.get("/auth/oidc/start")
async def oidc_start() -> RedirectResponse:
    """Kick off SSO: redirect the browser to the IdP with a signed state + nonce."""
    if not oidc.oidc_configured():
        return _error("Single sign-on isn't configured on this server.", status=503)
    s = oidc.settings()
    try:
        disco = oidc.discover(s["issuer"])
    except Exception:
        return _error("Couldn't reach the SSO provider. Please try again later.", status=502)
    nonce = uuid.uuid4().hex
    state = oidc.create_state(nonce)
    url = oidc.authorization_url(
        disco["authorization_endpoint"], s["client_id"], s["redirect_uri"], state, nonce
    )
    return RedirectResponse(url, status_code=302)


def _oidc_redirect_to_frontend(token: str | None, error: str | None = None) -> RedirectResponse:
    """Send the browser back to the app. On success the token rides in the URL FRAGMENT
    (never sent to a server or written to access logs); the frontend reads it and stores it."""
    base = f"{config.FRONTEND_URL}/auth/callback"
    if token:
        return RedirectResponse(f"{base}#token={token}", status_code=302)
    from urllib.parse import quote

    return RedirectResponse(f"{base}#error={quote(error or 'sso_failed')}", status_code=302)


@app.get("/auth/oidc/callback")
async def oidc_callback(code: str = "", state: str = "") -> RedirectResponse:
    """The IdP redirects here with ?code&state. Validate state, swap the code for tokens,
    verify the ID token, then sign the user in and bounce back to the app with a token.

    SSO defers MFA to the identity provider (the standard enterprise model), so a successful,
    verified SSO assertion issues a login token directly."""
    if not oidc.oidc_configured():
        return _oidc_redirect_to_frontend(None, "sso_not_configured")
    nonce = oidc.read_state(state)
    if nonce is None:
        return _oidc_redirect_to_frontend(None, "invalid_state")
    if not code:
        return _oidc_redirect_to_frontend(None, "no_code")

    s = oidc.settings()
    try:
        disco = oidc.discover(s["issuer"])
        tokens = oidc.exchange_code(
            disco["token_endpoint"], code, s["client_id"], s["client_secret"], s["redirect_uri"]
        )
        id_token = tokens.get("id_token")
        if not id_token:
            return _oidc_redirect_to_frontend(None, "no_id_token")
        key = oidc.signing_key_for_token(disco["jwks_uri"], id_token)
        claims = oidc.validate_id_token(id_token, key, s["issuer"], s["client_id"], nonce)
    except Exception:
        return _oidc_redirect_to_frontend(None, "verification_failed")

    email = oidc.email_from_claims(claims)
    if not email:
        return _oidc_redirect_to_frontend(None, "email_unverified")

    with session_scope() as db:
        user = auth.get_or_create_sso_user(db, email, claims.get("name"))
        token = auth.create_access_token(user.id, user.token_version)
    return _oidc_redirect_to_frontend(token)


# --------------------------------------------------------------------------- #
# Organizations / teams — persistent org-level RBAC (see app/org.py, app/rbac.py).
# --------------------------------------------------------------------------- #
def _org_snapshot(db, membership) -> dict:
    """The team as the signed-in member sees it: the org, their role, the roster, and any
    still-pending email invitations."""
    organization = db.get(org.Organization, membership.org_id)
    return {
        "org": {"id": organization.id, "name": organization.name} if organization else None,
        "my_role": membership.role,
        "members": org.list_members(db, membership.org_id),
        "invites": org.list_invites(db, membership.org_id),
    }


def _send_org_invite(email: str, org_name: str) -> bool:
    """Email a pending invitee a signup link (they auto-join on signup). Best-effort +
    SMTP-gated: returns False (no error) when email isn't configured or the send fails —
    the pending invite still applies whenever they sign up."""
    if not distribution.email_configured():
        return False
    from urllib.parse import quote

    link = f"{config.FRONTEND_URL}/signup?email={quote(email)}"
    body = (
        f'You\'ve been invited to join the team "{org_name}" on Sumio.\n\n'
        f"Create your account with this email address to join automatically:\n{link}\n\n"
        "If you weren't expecting this, you can ignore this email."
    )
    try:
        distribution.email_transport(email, f"You're invited to {org_name} on Sumio", body, b"", "")
        return True
    except Exception:
        return False


@app.get("/org")
async def org_get(user: "auth.User" = Depends(auth.current_user)) -> JSONResponse:
    """The caller's team (or {org: null} if they're not on one yet)."""
    with session_scope() as db:
        membership = org.get_user_membership(db, user.id)
        if membership is None:
            return JSONResponse({"org": None})
        return JSONResponse(_org_snapshot(db, membership))


@app.post("/org")
async def org_create(
    request: Request, user: "auth.User" = Depends(auth.current_user)
) -> JSONResponse:
    """Create a team; the caller becomes its owner."""
    body = await _json(request)
    try:
        with session_scope() as db:
            organization = org.create_org(db, body.get("name", ""), user.id)
            membership = org.get_membership(db, organization.id, user.id)
            return JSONResponse(_org_snapshot(db, membership))
    except org.OrgError as exc:
        return _error(exc.message, status=exc.status)


@app.post("/org/members")
async def org_add_member(
    request: Request, user: "auth.User" = Depends(auth.current_user)
) -> JSONResponse:
    """Add someone to the caller's team by email: an existing account joins immediately, a
    new email gets a pending invite + a signup link (they auto-join on signup). All
    permission-checked in org.py."""
    body = await _json(request)
    try:
        with session_scope() as db:
            actor = _require_org_actor(db, user.id)
            organization = db.get(org.Organization, actor.org_id)
            result = org.invite_or_add(db, actor, body.get("email", ""), body.get("role", "member"))
            emailed = (
                _send_org_invite(result["invite"]["email"], organization.name if organization else "your team")
                if result["kind"] == "invite" else False
            )
            return JSONResponse({"status": "ok", "emailed": emailed, **result})
    except org.OrgError as exc:
        return _error(exc.message, status=exc.status)


@app.post("/org/invites/revoke")
async def org_revoke_invite(
    request: Request, user: "auth.User" = Depends(auth.current_user)
) -> JSONResponse:
    """Cancel a pending invitation (managers only)."""
    body = await _json(request)
    try:
        with session_scope() as db:
            actor = _require_org_actor(db, user.id)
            org.revoke_invite(db, actor, body.get("invite_id", ""))
            return JSONResponse(_org_snapshot(db, actor))
    except org.OrgError as exc:
        return _error(exc.message, status=exc.status)


@app.post("/org/members/role")
async def org_set_role(
    request: Request, user: "auth.User" = Depends(auth.current_user)
) -> JSONResponse:
    body = await _json(request)
    try:
        with session_scope() as db:
            actor = _require_org_actor(db, user.id)
            org.set_member_role(db, actor, body.get("user_id", ""), body.get("role", ""))
            return JSONResponse(_org_snapshot(db, actor))
    except org.OrgError as exc:
        return _error(exc.message, status=exc.status)


@app.post("/org/members/remove")
async def org_remove_member(
    request: Request, user: "auth.User" = Depends(auth.current_user)
) -> JSONResponse:
    body = await _json(request)
    try:
        with session_scope() as db:
            actor = _require_org_actor(db, user.id)
            org.remove_member(db, actor, body.get("user_id", ""))
            return JSONResponse(_org_snapshot(db, actor))
    except org.OrgError as exc:
        return _error(exc.message, status=exc.status)


def _require_org_actor(db, user_id: str):
    """The caller's membership, or a 403-carrying OrgError if they're not on a team."""
    membership = org.get_user_membership(db, user_id)
    if membership is None:
        raise org.OrgError("You're not on a team yet.", status=403)
    return membership


async def _json(request: Request) -> dict:
    """Parse a JSON body, tolerating an empty/invalid one (→ {})."""
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Data quality & observability (Phase 3.11).
# --------------------------------------------------------------------------- #
@app.post("/quality/snapshot")
async def quality_snapshot(
    session_id: str = Form(...),
    max_age_hours: float = Form(24.0),
    set_baseline: str = Form("false"),
) -> JSONResponse:
    """Observability snapshot of the session's current data: a profile (columns/types,
    blank rates), an accurate 'last updated', and a staleness flag.

    With set_baseline=true, the current data ALSO becomes the new quality baseline — so
    after a deliberate schema change the user can re-baseline and stop /quality/check from
    alarming forever against the original upload (which would otherwise train them to
    ignore alarms — the opposite of the 'low false alarms' goal). Re-baselining resets the
    schema/blank reference only; it does NOT touch 'last updated' (freshness stays honest)."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first.", status=400)
    prof = quality.profile(entry["states"][-1]["tables"])
    baseline_set = str(set_baseline).strip().lower() in ("1", "true", "yes")
    if baseline_set:
        entry["quality_baseline"] = {"profile": prof, "captured_at": time.time()}
    updated_at = entry.get("updated_at", time.time())
    stale = quality.staleness(updated_at, time.time(), max_age_hours * 3600)
    return JSONResponse({
        "status": "ok", "profile": prof, "last_updated": updated_at, "staleness": stale,
        "baseline_set": baseline_set,
    })


@app.post("/quality/check")
async def quality_check(
    session_id: str = Form(...),
    max_age_hours: float = Form(24.0),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    """Compare the data against its baseline snapshot and report schema changes, missing-
    data spikes, and staleness. With a refreshed file attached, the new data is compared to
    the baseline, then becomes the new baseline (and 'last updated' moves to now)."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first.", status=400)
    baseline = entry.get("quality_baseline")
    if not baseline:
        return _error("No baseline snapshot for this session yet.", status=400)

    now = time.time()
    max_age = max_age_hours * 3600
    if files:
        too_big = _too_big(files)
        if too_big:
            return _error(too_big, status=413)
        uploads = [(f.filename or "upload", await f.read()) for f in files]
        too_big = _too_big_read(uploads)  # real bytes; the declared size can be absent
        if too_big:
            return _error(too_big, status=413)
        try:
            data = load_files(uploads)
        except ValueError as exc:
            return _error(str(exc), status=400)
        except Exception:
            return _error(_INTERNAL_ERROR, status=500)
        new_profile = quality.profile(dict(data.tables))
        report = quality.assess(baseline["profile"], new_profile, entry.get("updated_at", now), now, max_age)
        # The refresh becomes the new baseline; the data is now freshly updated.
        entry["quality_baseline"] = {"profile": new_profile, "captured_at": now}
        entry["updated_at"] = now
    else:
        new_profile = quality.profile(entry["states"][-1]["tables"])
        report = quality.assess(baseline["profile"], new_profile, entry.get("updated_at", now), now, max_age)

    return JSONResponse({"status": "ok", **report})


# --------------------------------------------------------------------------- #
# Live Google Sheets add-on — same Brain, different Hands.
#
# The Apps Script sidebar reads the live sheet's values, asks /sheets/plan for a plan
# (Brain), then /sheets/apply executes it (the existing trusted Hands) and hands back the
# new grid for Apps Script to write in place. All the governance still applies — PII shield,
# personalization, guardrails. Permission-awareness (view-only → suggestions) and edit
# sequencing (a document lock + a base-hash freshness check) live in the Apps Script layer;
# the base_hash check is also enforced here so a stale apply can never overwrite newer data.
# --------------------------------------------------------------------------- #
def _cell_safe(v):
    """JSON/Sheets-safe scalar: blanks→"", numpy→native, Timestamp→ISO."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v


def _sheet_df_from_values(values) -> pd.DataFrame:
    """Turn a 2D values grid (row 0 = headers) into a DataFrame. Raises a friendly
    OperationError for an empty/headerless sheet; pads/truncates ragged rows."""
    if not isinstance(values, list) or len(values) == 0:
        raise OperationError("The sheet looks empty — add a header row and some data first.")
    header = values[0]
    if not isinstance(header, list) or not any(str(h).strip() for h in header if h is not None):
        raise OperationError("I couldn't find a header row in this sheet.")
    cols: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        name = (str(h).strip() if h is not None else "") or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    width = len(cols)
    rows = []
    for r in values[1:]:
        r = list(r) if isinstance(r, list) else [r]
        r = (r + [""] * (width - len(r)))[:width]  # pad/truncate ragged rows
        rows.append(r)
    return pd.DataFrame(rows, columns=cols)


def _sheet_values_from_df(df: pd.DataFrame) -> list:
    """DataFrame → 2D values grid (header row + data), JSON/Sheets-safe."""
    out = [[str(c) for c in df.columns]]
    for _, row in df.iterrows():
        out.append([_cell_safe(v) for v in row.tolist()])
    return out


def _sheet_hash(values) -> str:
    return hashlib.sha256(
        json.dumps(values, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


@app.post("/sheets/plan")
async def sheets_plan(request: Request) -> JSONResponse:
    """Brain for the Sheets add-on: read the live grid + instruction, return a plan (or a
    SUGGESTION when the user is view-only). Same parsing, PII shield, personalization and
    guardrail assessment as the upload flow."""
    try:
        body = await request.json()
    except Exception:
        return _error("The request body wasn't valid JSON.", status=400)
    instruction = (body.get("instruction") or "").strip()
    values = body.get("values") or []
    sheet_name = body.get("sheet_name") or "Sheet1"
    team_id = body.get("team_id") or "default"
    org_id = body.get("org_id") or ""  # Phase 5.2: shared org glossary
    history = body.get("history") or ""
    can_edit = bool(body.get("can_edit", True))
    if not instruction:
        return _error("Tell Sumio what you'd like to do with the sheet.", status=400)
    try:
        df = _sheet_df_from_values(values)
    except OperationError as exc:
        return _error(str(exc), status=400)

    tables = {sheet_name: df}
    structure = summarize_tables(tables, sheet_name)
    structure, shielded = pii.redact_structure(structure, pii.scan_tables(tables))
    glossary = personalization.context(team_id, scope=org_id or None)
    safe_history = pii.redact_text(history)
    context = (glossary + "\n\n" + safe_history).strip() if glossary else safe_history

    try:
        plan = llm.parse_instruction(instruction, structure, context)
    except Exception as exc:
        plan = fallback.parse(instruction, structure, personalization.effective_definitions(team_id, org_id or None))
        if plan is None:
            unavailable = isinstance(exc, llm.ModelUnavailableError)
            return _error(
                str(exc) if unavailable else "Couldn't reach the AI service — try again in a moment.",
                status=503 if unavailable else 502,
            )

    reply = plan.get("reply")
    clarification = plan.get("clarification")
    operations = plan.get("operations") or []
    if not operations:
        if reply:
            return JSONResponse({"status": "message", "message": reply})
        if clarification:
            return JSONResponse({"status": "clarify", "clarification": clarification})
        return JSONResponse({
            "status": "message",
            "message": "I didn't understand that — try e.g. 'sort by Revenue descending'.",
        })

    operations = personalization.apply_preferences(operations, personalization.preferences(team_id))
    phantom = _missing_columns(operations, tables)
    if phantom:
        return JSONResponse(
            {"status": "clarify", "clarification": _missing_columns_clarification(phantom, tables)}
        )
    translation = (plan.get("translation") or "").strip() or _describe_plan(operations)
    confidence = plan.get("confidence")
    if not isinstance(confidence, int) or not (0 <= confidence <= 100):
        confidence = 80
    steps = plan.get("steps")
    if (not steps or len(steps) != len(operations)) and len(operations) >= 2:
        steps = _synthesize_steps(operations)
    assessment = guardrails.assess(operations, tables, sheet_name)

    return JSONResponse({
        # View-only users get a suggestion they can read; editors get an actionable plan.
        "status": "suggestion" if not can_edit else "plan",
        "can_edit": can_edit,
        "translation": translation,
        "confidence": confidence,
        "plan": {
            "operations": operations,
            "title": (plan.get("title") or "").strip() or None,
            "steps": steps or None,
        },
        "base_hash": _sheet_hash(values),
        "destructive": assessment["destructive"],
        "warnings": assessment["warnings"],
        "summary": assessment["summary"],
        "shielded_columns": _shield_columns(shielded),
    })


@app.post("/sheets/apply")
async def sheets_apply(request: Request) -> JSONResponse:
    """Hands for the Sheets add-on: run an approved plan on the grid and return the new
    grid for Apps Script to write back. Enforces the base_hash freshness check (sequencing
    human + AI edits) and the destructive-action confirmation gate."""
    try:
        body = await request.json()
    except Exception:
        return _error("The request body wasn't valid JSON.", status=400)
    values = body.get("values") or []
    plan = body.get("plan") or {}
    base_hash = body.get("base_hash")
    sheet_name = body.get("sheet_name") or "Sheet1"
    confirm = bool(body.get("confirm", False))
    operations = plan.get("operations") if isinstance(plan, dict) else None
    if not operations:
        return _error("There's nothing to apply.", status=400)
    try:
        df = _sheet_df_from_values(values)
    except OperationError as exc:
        return _error(str(exc), status=400)

    # Concurrency: the data must match what the plan was made from. If a human edited the
    # sheet in the meantime, refuse rather than silently overwrite their change.
    if base_hash and _sheet_hash(values) != base_hash:
        return JSONResponse(
            {"status": "conflict",
             "message": "The sheet changed since this plan was prepared — re-run so Sumio "
                        "works on the latest data."},
            status_code=409,
        )

    assessment = guardrails.assess(operations, {sheet_name: df}, sheet_name)
    if assessment["destructive"] and not confirm:
        return JSONResponse({
            "status": "confirm_required",
            "warnings": assessment["warnings"],
            "summary": assessment["summary"],
        })

    partial_warning = None
    completed_steps = len(operations)
    failed_step = None
    try:
        result, result_name, notes, _render = execute_multi({sheet_name: df}, sheet_name, operations)
    except MultiStepError as exc:
        result, notes = exc.partial_result, exc.notes
        completed_steps, failed_step = exc.failed_step - 1, exc.failed_step
        partial_warning = (
            f"Step {exc.failed_step} couldn't be done: {exc.reason} "
            f"The first {completed_steps} step(s) were applied."
        )
    except OperationError as exc:
        return _error(str(exc), status=422)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    if isinstance(result, dict):  # a workbook op (e.g. combine_sheets) — take the first sheet
        result = next(iter(result.values()))
    new_values = _sheet_values_from_df(result)
    return JSONResponse({
        "status": "applied",
        "values": new_values,
        "notes": notes,
        "explanation": " ".join(notes) if notes else "Done.",
        "row_count": int(len(result)),
        "new_hash": _sheet_hash(new_values),
        "partial": partial_warning is not None,
        "warning": partial_warning,
        "completed_steps": completed_steps,
        "failed_step": failed_step,
    })


# --------------------------------------------------------------------------- #
# Live database & SaaS connectors (Phase 3.2).
# --------------------------------------------------------------------------- #
def _conn_error(exc: connectors.ConnectorError) -> JSONResponse:
    return _error(str(exc), status=getattr(exc, "status", 400))


def _json_or(value: str, default):
    try:
        return json.loads(value) if (value or "").strip() else default
    except Exception:
        return default


@app.post("/connectors/create")
async def connector_create(
    name: str = Form(""),
    type: str = Form(...),
    credentials: str = Form(...),
    scopes: str = Form(""),
    read_only: str = Form("true"),
    config: str = Form(""),
) -> JSONResponse:
    """Register a data connection. Read-only by default; credentials are stored apart and
    never returned."""
    creds = _json_or(credentials, None)
    if not isinstance(creds, dict):
        return _error("Credentials must be a JSON object.", status=400)
    scope_list = _json_or(scopes, None)
    if not isinstance(scope_list, list):
        scope_list = [s.strip() for s in re.split(r"[,;\n]+", scopes) if s.strip()]
    ro = str(read_only).strip().lower() not in ("0", "false", "no")
    try:
        view = connectors.register_connection(
            name, type, creds, scope_list, read_only=ro, config=_json_or(config, {}) or {},
        )
    except connectors.ConnectorError as exc:
        return _conn_error(exc)
    return JSONResponse({"status": "ok", "connection": view})


@app.get("/connectors/list")
async def connector_list() -> JSONResponse:
    return JSONResponse({"status": "ok", "connections": connectors.list_connections()})


@app.get("/connectors/{conn_id}")
async def connector_get(conn_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "connection": connectors.get_connection(conn_id)})
    except connectors.ConnectorError as exc:
        return _conn_error(exc)


@app.post("/connectors/{conn_id}/delete")
async def connector_delete(conn_id: str) -> JSONResponse:
    try:
        connectors.delete_connection(conn_id)
    except connectors.ConnectorError as exc:
        return _conn_error(exc)
    return JSONResponse({"status": "ok"})


@app.post("/connectors/{conn_id}/query")
async def connector_query(
    conn_id: str,
    query: str = Form(...),
    page: int = Form(1),
    page_size: int = Form(100),
) -> JSONResponse:
    """Run a read-only, scope-checked, paginated query against the connection."""
    try:
        result = connectors.run_query(conn_id, query, connectors.dispatch_driver, page, page_size)
    except connectors.ConnectorError as exc:
        return _conn_error(exc)
    return JSONResponse({"status": "ok", **result})


@app.post("/connectors/{conn_id}/import")
async def connector_import(
    conn_id: str,
    query: str = Form(...),
    session_id: str = Form(...),
    page_size: int = Form(1000),
) -> JSONResponse:
    """Pull a connection's data into a session as a dataset (read-only)."""
    try:
        result = connectors.run_query(conn_id, query, connectors.dispatch_driver, 1, page_size)
    except connectors.ConnectorError as exc:
        return _conn_error(exc)
    df = pd.DataFrame(result["rows"])
    name = connectors.get_connection(conn_id)["name"][:28] or "Imported"
    _remember_session(session_id, {"tables": {name: df}, "primary": name, "exts": {name: "xlsx"}, "notes": {}})
    s = summarize_structure(df, sample_rows=5)
    return JSONResponse({"status": "ok", "tables": [{"name": name, "row_count": s["row_count"],
                         "columns": s["columns"], "sample_rows": s["sample_rows"]}], "has_more": result["has_more"]})


# --------------------------------------------------------------------------- #
# Scheduled sync & webhooks (Phase 3.3).
# --------------------------------------------------------------------------- #
def _sync_error(exc: sync.SyncError) -> JSONResponse:
    return _error(str(exc), status=getattr(exc, "status", 400))


def _sync_fetch(s: dict) -> list:
    """Pull a sync's data via its connection (read-only). Raises so run_due can retry+log."""
    res = connectors.run_query(s["connection_id"], s["query"], connectors.dispatch_driver, 1, connectors._MAX_PAGE_SIZE)
    return res["rows"]


def _sync_apply(s: dict, rows: list) -> None:
    """Load freshly-synced rows into the target session, if one is set."""
    if s.get("target_session"):
        df = pd.DataFrame(rows)
        name = (s.get("name") or "Synced")[:28] or "Synced"
        _remember_session(s["target_session"], {"tables": {name: df}, "primary": name, "exts": {name: "xlsx"}, "notes": {}})


@app.post("/sync/create")
async def sync_create(
    name: str = Form(""),
    connection_id: str = Form(...),
    query: str = Form(...),
    cadence: str = Form("manual"),
    target_session: str = Form(""),
) -> JSONResponse:
    try:
        job = sync.create_sync(name, connection_id, query, cadence, target_session or None)
    except sync.SyncError as exc:
        return _sync_error(exc)
    return JSONResponse({"status": "ok", "sync": job})


@app.get("/sync/list")
async def sync_list() -> JSONResponse:
    return JSONResponse({"status": "ok", "syncs": sync.list_syncs()})


@app.get("/sync/{sync_id}")
async def sync_get(sync_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "sync": sync.get_sync(sync_id)})
    except sync.SyncError as exc:
        return _sync_error(exc)


@app.post("/sync/{sync_id}/pause")
async def sync_pause(sync_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "sync": sync.pause_sync(sync_id)})
    except sync.SyncError as exc:
        return _sync_error(exc)


@app.post("/sync/{sync_id}/resume")
async def sync_resume(sync_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "sync": sync.resume_sync(sync_id)})
    except sync.SyncError as exc:
        return _sync_error(exc)


@app.post("/sync/{sync_id}/delete")
async def sync_delete(sync_id: str) -> JSONResponse:
    try:
        sync.delete_sync(sync_id)
    except sync.SyncError as exc:
        return _sync_error(exc)
    return JSONResponse({"status": "ok"})


@app.post("/sync/{sync_id}/run-now")
async def sync_run_now(sync_id: str) -> JSONResponse:
    try:
        report = sync.run_now(sync_id, _sync_fetch, _sync_apply)
    except sync.SyncError as exc:
        return _sync_error(exc)
    job = sync.get_sync(sync_id)
    out = {"status": "ok", "report": report, "sync": job}
    # When fresh data landed in a target session, hand back a preview so the UI can refresh.
    tgt = job.get("target_session")
    if report.get("status") == "synced" and tgt and tgt in _SESSIONS:
        st = _SESSIONS[tgt]["states"][-1]
        out["tables"] = [
            {"name": n, **summarize_structure(df, sample_rows=5)} for n, df in st["tables"].items()
        ]
    return JSONResponse(out)


@app.post("/sync/run-due")
async def sync_run_due() -> JSONResponse:
    """The scheduler tick: fire all due syncs (with retry/log/dedup built in)."""
    reports = sync.run_due(_sync_fetch, apply=_sync_apply)
    return JSONResponse({"status": "ok", "ran": reports})


@app.post("/webhook/{endpoint_id}")
async def webhook(endpoint_id: str, request: Request) -> JSONResponse:
    """Accept an external push, deduped by an idempotency key (X-Idempotency-Key header or
    ?key=) or by payload hash. Re-deliveries are acknowledged but not reprocessed."""
    try:
        payload = await request.json()
    except Exception:
        return _error("Webhook body must be JSON.", status=400)
    key = request.headers.get("X-Idempotency-Key") or request.query_params.get("key")
    entry = sync.ingest_webhook(endpoint_id, payload, key)
    return JSONResponse({"status": "ok", **entry})


@app.get("/webhook/{endpoint_id}/log")
async def webhook_log(endpoint_id: str) -> JSONResponse:
    return JSONResponse({"status": "ok", "log": sync.webhook_log(endpoint_id)})


# --------------------------------------------------------------------------- #
# Continuous learning & personalization (Phase 3.12) — view/edit/delete memory.
# --------------------------------------------------------------------------- #
@app.get("/memory")
async def memory_get(team_id: str = "default") -> JSONResponse:
    """View everything remembered for a team: definitions, preferences, templates."""
    return JSONResponse({"status": "ok", "memory": personalization.get_memory(team_id)})


@app.post("/memory/definition")
async def memory_set_definition(
    term: str = Form(...),
    definition: str = Form(""),
    formula: str = Form(""),
    team_id: str = Form("default"),
) -> JSONResponse:
    try:
        item = personalization.set_definition(team_id, term, definition, formula or None)
    except ValueError as exc:
        return _error(str(exc), status=400)
    return JSONResponse({"status": "ok", "definition": item, "memory": personalization.get_memory(team_id)})


@app.post("/memory/definition/delete")
async def memory_delete_definition(term: str = Form(...), team_id: str = Form("default")) -> JSONResponse:
    removed = personalization.delete_definition(team_id, term)
    return JSONResponse({"status": "ok", "removed": removed, "memory": personalization.get_memory(team_id)})


# Shared / org-wide AI memory (Phase 5.2) — a glossary every team in an org inherits.
@app.get("/memory/shared")
async def memory_shared_get(org_id: str = "") -> JSONResponse:
    return JSONResponse({"status": "ok", "shared": personalization.get_shared_memory(org_id)})


@app.post("/memory/shared/definition")
async def memory_set_shared_definition(
    org_id: str = Form(...),
    term: str = Form(...),
    definition: str = Form(""),
    formula: str = Form(""),
) -> JSONResponse:
    try:
        item = personalization.set_shared_definition(org_id, term, definition, formula or None)
    except ValueError as exc:
        return _error(str(exc), status=400)
    return JSONResponse({"status": "ok", "definition": item, "shared": personalization.get_shared_memory(org_id)})


@app.post("/memory/shared/definition/delete")
async def memory_delete_shared_definition(org_id: str = Form(...), term: str = Form(...)) -> JSONResponse:
    removed = personalization.delete_shared_definition(org_id, term)
    return JSONResponse({"status": "ok", "removed": removed, "shared": personalization.get_shared_memory(org_id)})


@app.post("/memory/preferences")
async def memory_set_preferences(
    currency_symbol: str = Form(None),
    date_format: str = Form(None),
    decimals: str = Form(None),
    bold_header: str = Form(None),
    team_id: str = Form("default"),
) -> JSONResponse:
    prefs: dict = {}
    if currency_symbol is not None:
        prefs["currency_symbol"] = currency_symbol or None
    if date_format is not None:
        prefs["date_format"] = date_format or None
    if decimals is not None and str(decimals).strip() != "":
        try:
            prefs["decimals"] = int(decimals)
        except ValueError:
            return _error("Decimals must be a whole number.", status=400)
    if bold_header is not None:
        prefs["bold_header"] = str(bold_header).strip().lower() in ("1", "true", "yes")
    personalization.set_preferences(team_id, **prefs)
    return JSONResponse({"status": "ok", "memory": personalization.get_memory(team_id)})


@app.post("/memory/preferences/delete")
async def memory_delete_preference(key: str = Form(...), team_id: str = Form("default")) -> JSONResponse:
    """Delete ONE remembered preference by key (currency_symbol/date_format/decimals/
    bold_header), so users can fully manage — view, edit, AND delete — every kind of
    memory, not just definitions and templates (Phase 3.7)."""
    removed = personalization.clear_preference(team_id, key)
    return JSONResponse({"status": "ok", "removed": removed, "memory": personalization.get_memory(team_id)})


@app.post("/memory/template")
async def memory_save_template(
    name: str = Form(...),
    operations: str = Form(...),
    prompt: str = Form(""),
    team_id: str = Form("default"),
) -> JSONResponse:
    try:
        ops = json.loads(operations)
        if isinstance(ops, dict):
            ops = ops.get("operations") or []
        item = personalization.save_template(team_id, name, ops, prompt or None)
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(str(exc) or "Invalid template.", status=400)
    return JSONResponse({"status": "ok", "template": item, "memory": personalization.get_memory(team_id)})


@app.post("/memory/template/delete")
async def memory_delete_template(name: str = Form(...), team_id: str = Form("default")) -> JSONResponse:
    removed = personalization.delete_template(team_id, name)
    return JSONResponse({"status": "ok", "removed": removed, "memory": personalization.get_memory(team_id)})


# --------------------------------------------------------------------------- #
# Workflow / automation builder (Phase 4.9) — save a pipeline of operations with a
# trigger, run it with per-step status, and let a scheduler tick find what's due.
# --------------------------------------------------------------------------- #
def _workflow_error(exc: "workflow.WorkflowError") -> JSONResponse:
    return _error(str(exc), status=exc.status)


@app.post("/workflow/create")
async def workflow_create(
    name: str = Form(...),
    steps: str = Form(...),
    trigger: str = Form(""),
) -> JSONResponse:
    """Save a pipeline: `steps` is an Operation Plan (JSON list, or {"operations":[…]}),
    `trigger` is optional JSON {type: manual|schedule|new_file|anomaly, …}."""
    try:
        parsed = json.loads(steps)
        ops = parsed.get("operations") if isinstance(parsed, dict) else parsed
        trig = json.loads(trigger) if trigger.strip() else None
        wf = workflow.create_workflow(name, ops, trig)
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(str(exc) or "Invalid workflow.", status=400)
    except workflow.WorkflowError as exc:
        return _workflow_error(exc)
    return JSONResponse({"status": "ok", "workflow": workflow._public(wf)})


@app.get("/workflow/list")
async def workflow_list() -> JSONResponse:
    return JSONResponse({"status": "ok", "workflows": workflow.list_workflows()})


@app.get("/workflow/{workflow_id}")
async def workflow_get(workflow_id: str) -> JSONResponse:
    try:
        wf = workflow.get_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        return _workflow_error(exc)
    return JSONResponse({"status": "ok", "workflow": workflow._public(wf), "steps": wf["steps"]})


@app.post("/workflow/{workflow_id}/pause")
async def workflow_pause(workflow_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "workflow": workflow.set_status(workflow_id, "paused")})
    except workflow.WorkflowError as exc:
        return _workflow_error(exc)


@app.post("/workflow/{workflow_id}/resume")
async def workflow_resume(workflow_id: str) -> JSONResponse:
    try:
        return JSONResponse({"status": "ok", "workflow": workflow.set_status(workflow_id, "active")})
    except workflow.WorkflowError as exc:
        return _workflow_error(exc)


@app.post("/workflow/{workflow_id}/delete")
async def workflow_delete(workflow_id: str) -> JSONResponse:
    return JSONResponse({"status": "ok", "removed": workflow.delete_workflow(workflow_id)})


@app.post("/workflow/{workflow_id}/run")
async def workflow_run(workflow_id: str, session_id: str = Form(...)) -> JSONResponse:
    """Run a workflow's pipeline on the session's current data. Returns a per-step status
    report; a failed step stops the pipeline cleanly (later steps 'skipped'), and the file
    reflects the steps that completed. Trusted Hands execute — no model call."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    try:
        report, result, result_name = workflow.run_workflow(
            workflow_id, base["tables"], base["primary"], execute_multi)
    except workflow.WorkflowError as exc:
        return _workflow_error(exc)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    payload = {"status": "ok", **report}
    # Offer the resulting file (reflecting the steps that completed) as a download.
    try:
        if isinstance(result, dict):
            out_bytes, out_name, media_type = _serialize_workbook(result, result_name, primary=result_name)
            payload["row_count"] = sum(int(len(d)) for d in result.values())
        elif result is not None:
            out_bytes, out_name, media_type = _serialize(result, result_name, base["exts"].get(result_name, "xlsx"), [])
            payload["row_count"] = int(len(result))
        else:
            out_bytes = None
        if out_bytes is not None:
            payload["download_id"] = _store_result(out_bytes, out_name, media_type)
            payload["filename"] = out_name
    except Exception:
        pass  # the report is the point; a serialization hiccup shouldn't fail the run
    return JSONResponse(payload)


@app.post("/workflow/run-due")
async def workflow_run_due(event: str = Form("")) -> JSONResponse:
    """Scheduler tick: given a trigger `event` (JSON {type, anomalies_found?}), return the
    ACTIVE workflows that should fire now — schedule cadence elapsed, a new file arrived, or
    a genuine anomaly was reported. The caller then runs each against its data source."""
    try:
        ev = json.loads(event) if event.strip() else {}
    except (ValueError, json.JSONDecodeError):
        return _error("Event must be JSON.", status=400)
    due = workflow.due_workflows(ev)
    return JSONResponse({"status": "ok", "due": [workflow._public(w) for w in due], "count": len(due)})


# --------------------------------------------------------------------------- #
# Data lineage (Phase 5.3) — trace a column's value back to its source columns, and a
# whole-workbook dependency graph, from the session's formula registry (Phase 4.8).
# --------------------------------------------------------------------------- #
def _session_columns(base: dict) -> set[str]:
    return {str(c) for df in base["tables"].values() for c in df.columns}


@app.post("/lineage")
async def lineage_column(session_id: str = Form(...), column: str = Form(...)) -> JSONResponse:
    """Trace ONE column to its sources: a value→source tree (derived columns + their
    formulas, down to source columns), plus the flat set of root sources it derives from."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    cols = _session_columns(base)
    formulas = entry.get("formulas") or {}
    column = (column or "").strip()
    if column not in cols and column not in formulas:
        return _error(f"There's no column called '{column}' in this workbook.", status=404)
    return JSONResponse({
        "status": "ok",
        "column": column,
        "derived": column in formulas,
        "trace": lineage.trace(column, formulas, cols),
        "sources": lineage.source_columns(column, formulas),
    })


@app.post("/lineage/graph")
async def lineage_workbook_graph(session_id: str = Form(...)) -> JSONResponse:
    """The whole-workbook lineage graph: nodes (columns, tagged source/derived) and edges
    (source → derived) for a visual dependency view."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    return JSONResponse({
        "status": "ok",
        "graph": lineage.graph(entry.get("formulas") or {}, _session_columns(base)),
    })


# --------------------------------------------------------------------------- #
# Knowledge graph (Phase 5.4) — entities (sheets) + inferred relationships (foreign keys),
# and relational queries without joins (name a related field, we resolve the join).
# --------------------------------------------------------------------------- #
@app.post("/kg/graph")
async def kg_graph(session_id: str = Form(...)) -> JSONResponse:
    """The workbook's knowledge graph: each sheet as an entity (columns + candidate keys),
    plus the foreign-key relationships inferred between sheets."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    return JSONResponse({"status": "ok", "graph": kg.graph(entry["states"][-1]["tables"])})


@app.post("/kg/query")
async def kg_query(
    session_id: str = Form(...),
    field: str = Form(...),
    from_table: str = Form(""),
) -> JSONResponse:
    """Relational query without a join: bring `field` from a RELATED table into `from_table`
    (default: the primary sheet). Sumio resolves the join keys from the knowledge graph and
    runs the lookup; the result is previewed and offered as a file. Declines honestly (422)
    when there's no relationship or the field is ambiguous."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    tables, primary, exts = base["tables"], base["primary"], base["exts"]
    start = (from_table or "").strip() or primary
    try:
        op, rel = kg.auto_lookup(tables, start, field.strip())
    except ValueError as exc:
        return _error(str(exc), status=422)
    try:
        result, result_name, notes, render_ops = execute_multi(tables, start, [op])
    except OperationError as exc:
        return _error(str(exc), status=422)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    payload = {
        "status": "ok",
        "field": field.strip(),
        "relationship": rel,  # from_table.from_column → to_table.to_column (+ coverage)
        "explanation": (
            f"Brought '{field.strip()}' from {rel['to_table']} into {start} by matching "
            f"{start}.{rel['from_column']} → {rel['to_table']}.{rel['to_column']}."
        ),
        "notes": notes,
        "preview": _result_preview(result, result_name),
        "row_count": int(len(result)) if not isinstance(result, dict) else None,
    }
    try:  # offer the enriched table as a download (a KG query doesn't mutate session state)
        out_bytes, out_name, media_type = _serialize(result, result_name, exts.get(result_name, "xlsx"), render_ops)
        payload["download_id"] = _store_result(out_bytes, out_name, media_type)
        payload["filename"] = out_name
    except Exception:
        pass
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# Security & compliance (Phase 5.5) — compliance profiles that widen the PII shield, a
# compliance posture scan, and the security audit trail.
# --------------------------------------------------------------------------- #
@app.get("/compliance/profiles")
async def compliance_profiles() -> JSONResponse:
    """The compliance regimes the shield can enforce (for a UI toggle)."""
    return JSONResponse({"status": "ok", "profiles": compliance.available()})


@app.post("/compliance/scan")
async def compliance_scan(
    session_id: str = Form(...),
    compliance_mode: str = Form(""),
) -> JSONResponse:
    """Report which fields are sensitive under the active compliance profiles (base PII plus
    regime-specific hints), confirming they're masked before the AI. Recorded in the audit log."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    tables = entry["states"][-1]["tables"]
    rep = compliance.report(tables, compliance_mode)
    audit.record("compliance_scan", actor=session_id,
                 detail=f"Scanned under {len(rep['profiles'])} profile(s); {rep['sensitive_count']} sensitive field(s).",
                 meta={"profiles": [p["id"] for p in rep["profiles"]], "sensitive_count": rep["sensitive_count"]})
    return JSONResponse({"status": "ok", **rep})


@app.get("/audit")
async def audit_log(limit: int = 100, action: str = "") -> JSONResponse:
    """The security audit trail, most-recent-first — what was shielded/scanned, and when."""
    return JSONResponse({"status": "ok", "events": audit.events(limit=limit, action=action or None)})


# --------------------------------------------------------------------------- #
# User roles & range-level permissions (Phase 5.6).
# --------------------------------------------------------------------------- #
@app.get("/permissions/roles")
async def permissions_roles() -> JSONResponse:
    """The data-access roles and what each may do (for a role-picker UI)."""
    return JSONResponse({"status": "ok", "roles": permissions.roles_summary()})


@app.post("/permissions/authorize")
async def permissions_authorize(
    session_id: str = Form(...),
    role: str = Form(...),
    plan: str = Form(...),
    grants: str = Form(""),
) -> JSONResponse:
    """Check whether `role` (with optional range `grants`) may run `plan` on the session's
    data. Returns {allowed, blocked:[{step, action, reason}]} — a read-only role can change
    nothing; an editor is checked per-column against its range grants."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    if not permissions.is_role(role):
        return _error(f"Unknown role '{role}'. Roles: {', '.join(permissions.ROLES)}.", status=400)
    try:
        parsed = json.loads(plan)
        operations = parsed.get("operations") if isinstance(parsed, dict) else parsed
        grant_list = json.loads(grants) if grants.strip() else []
    except (ValueError, json.JSONDecodeError):
        return _error("Plan and grants must be valid JSON.", status=400)
    result = permissions.authorize_plan(role, operations or [], entry["states"][-1]["tables"], grant_list)
    return JSONResponse({"status": "ok", "role": role, **result})


# --------------------------------------------------------------------------- #
# Self-healing workbooks (Phase 5.7) — detect formula references broken by a dropped/renamed
# column and repair them (remap to a renamed column, or restore from version history).
# --------------------------------------------------------------------------- #
def _state_columns(state: dict) -> set[str]:
    return {str(c) for df in state["tables"].values() for c in df.columns}


def _heal_history(states: list) -> list:
    """[(version_index, columns_set), …] for PAST versions, newest-first — what selfheal
    searches to restore a dropped column from."""
    return [(i, _state_columns(states[i])) for i in range(len(states) - 2, -1, -1)]


@app.post("/heal")
async def heal_diagnose(session_id: str = Form(...)) -> JSONResponse:
    """Diagnose broken formula references and propose repairs (remap / restore / unrepairable)
    — read-only; nothing is changed."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    states = entry["states"]
    formulas = entry.get("formulas") or {}
    current_cols = _state_columns(states[-1])
    broken = selfheal.broken_references(formulas, current_cols)
    repairs = selfheal.plan_repairs(formulas, current_cols, _heal_history(states))
    return JSONResponse({
        "status": "ok",
        "healthy": not broken,
        "broken_references": broken,
        "repairs": repairs,
    })


@app.post("/heal/apply")
async def heal_apply(session_id: str = Form(...)) -> JSONResponse:
    """Apply the repairs: restore dropped columns from history (when row counts still align),
    remap renamed references and recompute those formula columns. Pushes a new version."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    states = entry["states"]
    current = states[-1]
    tables, primary, exts = current["tables"], current["primary"], current["exts"]
    formulas = entry.get("formulas") or {}
    repairs = selfheal.plan_repairs(formulas, _state_columns(current), _heal_history(states))

    healed: list[dict] = []
    skipped: list[dict] = []
    new_tables = {t: df.copy() for t, df in tables.items()}

    # 1) RESTORE dropped columns from the version that still had them (positional, only when
    #    the row count matches so we never re-attach misaligned data).
    for r in [r for r in repairs if r["action"] == "restore"]:
        src = states[r["from_version"]]
        src_tbl = next((n for n, df in src["tables"].items() if r["column"] in df.columns), None)
        if src_tbl is not None and len(src["tables"][src_tbl]) == len(new_tables[primary]):
            new_tables[primary][r["column"]] = src["tables"][src_tbl][r["column"]].values
            healed.append({**r, "result": "restored"})
        else:
            skipped.append({**r, "reason": "row count changed since then — can't safely restore."})

    # 2) REMAP renamed references in the registry, then recompute those formula columns.
    new_formulas, changed = selfheal.apply_remaps(formulas, repairs)
    if changed:
        ops = [{"action": "add_formula_column", "name": col, "formula": new_formulas[col], "overwrite": True}
               for col in changed]
        try:
            result, result_name, _notes, _render = execute_multi(new_tables, primary, ops)
            new_tables = result if isinstance(result, dict) else {result_name: result}
            primary = result_name if not isinstance(result, dict) else next(iter(result))
        except (OperationError, MultiStepError) as exc:
            return _error(f"Couldn't recompute a healed formula: {exc}", status=422)
        except Exception:
            return _error(_INTERNAL_ERROR, status=500)
        for r in [r for r in repairs if r["action"] == "remap"]:
            healed.append({**r, "result": "remapped"})

    unrepairable = [r for r in repairs if r["action"] == "unrepairable"]

    # Commit the healed workbook as a new version (only if something changed).
    if healed:
        new_state = {"tables": new_tables, "primary": primary,
                     "exts": {**exts, **{k: exts.get(k, "xlsx") for k in new_tables}},
                     "label": f"Self-healed {len(healed)} broken reference(s)"}
        _push_state(session_id, new_state)
        entry["formulas"] = new_formulas
        audit.record("self_heal", actor=session_id,
                     detail=f"Repaired {len(healed)} reference(s); {len(skipped)+len(unrepairable)} unresolved.",
                     meta={"healed": [r["missing"] for r in healed], "skipped": len(skipped)})

    return JSONResponse({
        "status": "ok",
        "healed": healed,
        "skipped": skipped + unrepairable,
        "preview": _result_preview(new_tables[primary], primary),
        "remaining_broken": selfheal.broken_references(entry.get("formulas") or {}, _state_columns(_SESSIONS[session_id]["states"][-1])),
    })


# --------------------------------------------------------------------------- #
# Performance & scale (Phase 5.8) — a sampled preview for huge sheets, and result-cache stats.
# --------------------------------------------------------------------------- #
@app.post("/scale/preview")
async def scale_preview(session_id: str = Form(...), sample_rows: int = Form(1000)) -> JSONResponse:
    """A fast, representative preview of the session's current data. For a huge sheet it
    previews an evenly-spaced SAMPLE (flagged as such) instead of scanning every row."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    sample_rows = max(1, min(int(sample_rows or 1000), 50_000))
    sampled_tables, was_sampled = scale.sample_tables(base["tables"], sample_rows)
    frames = {n: sampled_tables[n] for n in sampled_tables}
    preview = [
        {"name": n, **{k: summarize_structure(df, sample_rows=min(8, len(df)))[k]
                       for k in ("row_count", "columns", "sample_rows")}}
        for n, df in frames.items()
    ]
    return JSONResponse({
        "status": "ok",
        "sampled": was_sampled,
        "total_rows": scale.total_rows(base["tables"]),
        "sample_rows": sample_rows if was_sampled else scale.total_rows(base["tables"]),
        "large": scale.is_large(base["tables"]),
        "preview": preview,
    })


@app.get("/scale/stats")
async def scale_stats() -> JSONResponse:
    """Result-cache statistics (entries, hits, misses, hit-rate) — how much recompute the
    cache is saving."""
    return JSONResponse({"status": "ok", "cache": scale.RESULT_CACHE.stats()})


# --------------------------------------------------------------------------- #
# Executive insight generator (Phase 5.9) — a board-ready narrative, every figure computed.
# --------------------------------------------------------------------------- #
@app.post("/executive/summary")
async def executive_summary(session_id: str = Form(...), title: str = Form("")) -> JSONResponse:
    """Compose a grounded executive summary of the session's current primary sheet: a
    headline plus verifiable findings (each with its raw figures) and a plain-text rendering.
    Every number is computed here — never model-generated — so nothing can be fabricated."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    base = entry["states"][-1]
    df = base["tables"].get(base["primary"])
    summary = execsummary.generate(df, title=title or None)
    return JSONResponse({"status": "ok", **summary, "text": execsummary.as_text(summary)})


# --------------------------------------------------------------------------- #
# Optimization / Solver (Phase 5.10) — constrained linear/integer optimization (scipy).
# --------------------------------------------------------------------------- #
@app.post("/solve")
async def solve(problem: str = Form(...)) -> JSONResponse:
    """Solve a constrained optimization: `problem` is JSON with objective / sense /
    constraints / bounds / integer. Returns the TRUE outcome (optimal / infeasible /
    unbounded) with the solution and objective value — never a fabricated result."""
    try:
        spec = json.loads(problem)
    except (ValueError, json.JSONDecodeError):
        return _error("The optimization problem must be valid JSON.", status=400)
    if not isinstance(spec, dict):
        return _error("The optimization problem must be a JSON object.", status=400)
    try:
        result = solver.solve_linear(
            objective=spec.get("objective") or {},
            constraints=spec.get("constraints") or [],
            bounds=spec.get("bounds"),
            sense=spec.get("sense", "max"),
            integer=spec.get("integer"),
        )
    except solver.SolverError as exc:
        return _error(str(exc), status=exc.status)
    return JSONResponse({"status": "ok", **result})


# --------------------------------------------------------------------------- #
# Ecosystem (Phase 5.11) — plugin marketplace & custom agents (sandboxed), API platform,
# admin console. A plugin is a validated pipeline of KNOWN operations, run through the same
# trusted executor — never arbitrary code.
# --------------------------------------------------------------------------- #
def _mkt_error(exc: "marketplace.MarketplaceError") -> JSONResponse:
    return _error(str(exc), status=exc.status)


@app.post("/marketplace/publish")
async def marketplace_publish(
    name: str = Form(...),
    steps: str = Form(...),
    description: str = Form(""),
    author: str = Form(""),
    kind: str = Form("plugin"),
) -> JSONResponse:
    """Publish a plugin/agent: `steps` is an Operation Plan (JSON). It's sandbox-validated —
    every step must be a known operation — before it's listed."""
    try:
        parsed = json.loads(steps)
        ops = parsed.get("operations") if isinstance(parsed, dict) else parsed
        plugin = marketplace.publish(name, ops, description, author, kind)
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(str(exc) or "Invalid plugin.", status=400)
    except marketplace.MarketplaceError as exc:
        return _mkt_error(exc)
    return JSONResponse({"status": "ok", "plugin": plugin})


@app.get("/marketplace/list")
async def marketplace_list() -> JSONResponse:
    return JSONResponse({"status": "ok", "plugins": marketplace.listing()})


@app.get("/marketplace/installed")
async def marketplace_installed(team_id: str = "default") -> JSONResponse:
    return JSONResponse({"status": "ok", "plugins": marketplace.installed(team_id)})


@app.get("/marketplace/{plugin_id}")
async def marketplace_get(plugin_id: str) -> JSONResponse:
    try:
        p = marketplace.get(plugin_id)
    except marketplace.MarketplaceError as exc:
        return _mkt_error(exc)
    return JSONResponse({"status": "ok", "plugin": marketplace._public(p), "steps": p["steps"]})


@app.post("/marketplace/{plugin_id}/install")
async def marketplace_install(plugin_id: str, team_id: str = Form("default")) -> JSONResponse:
    try:
        marketplace.install(team_id, plugin_id)
    except marketplace.MarketplaceError as exc:
        return _mkt_error(exc)
    return JSONResponse({"status": "ok", "installed": marketplace.installed(team_id)})


@app.post("/marketplace/{plugin_id}/uninstall")
async def marketplace_uninstall(plugin_id: str, team_id: str = Form("default")) -> JSONResponse:
    return JSONResponse({"status": "ok", "removed": marketplace.uninstall(team_id, plugin_id)})


@app.post("/marketplace/{plugin_id}/unpublish")
async def marketplace_unpublish(plugin_id: str) -> JSONResponse:
    return JSONResponse({"status": "ok", "removed": marketplace.unpublish(plugin_id)})


@app.post("/marketplace/{plugin_id}/run")
async def marketplace_run(plugin_id: str, session_id: str = Form(...)) -> JSONResponse:
    """Run a plugin's sandboxed pipeline on the session's data — through the SAME trusted
    executor as any instruction (caching, state history and all)."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    try:
        steps = marketplace.steps_of(plugin_id)
        name = marketplace.get(plugin_id)["name"]
    except marketplace.MarketplaceError as exc:
        return _mkt_error(exc)
    return _run_operations(session_id, entry["states"][-1], steps, name, time.time())


# --- API platform: keys -----------------------------------------------------
@app.post("/apikeys/issue")
async def apikeys_issue(team_id: str = Form("default"), label: str = Form("")) -> JSONResponse:
    """Issue an API key. The raw key is returned ONCE and never stored — save it now."""
    return JSONResponse({"status": "ok", **apikeys.issue(team_id, label)})


@app.get("/apikeys/list")
async def apikeys_list(team_id: str = "default") -> JSONResponse:
    return JSONResponse({"status": "ok", "keys": apikeys.list_keys(team_id)})


@app.post("/apikeys/{key_id}/revoke")
async def apikeys_revoke(key_id: str) -> JSONResponse:
    return JSONResponse({"status": "ok", "revoked": apikeys.revoke(key_id)})


# --- Admin console ----------------------------------------------------------
@app.get("/admin/overview")
async def admin_overview() -> JSONResponse:
    """A lightweight admin snapshot of the ecosystem: plugins, active API keys, live sessions,
    workflows, and recent audit activity."""
    return JSONResponse({
        "status": "ok",
        "plugins": marketplace.count(),
        "active_api_keys": apikeys.count(active_only=True),
        "sessions": len(_SESSIONS),
        "workflows": len(workflow.list_workflows()),
        "audit_events": len(audit.events(limit=10_000)),
    })


def _step_label(notes: list[str]) -> str:
    """A short, human-readable label for a version-history step, from the op notes
    (Phase 3.2). E.g. 'Sorted by Price descending' or 'Filtered 200 → 96 rows'."""
    text = " · ".join(n.strip().rstrip(".") for n in (notes or []) if n and n.strip())
    return (text[:70] + "…") if len(text) > 70 else (text or "Changed the data")


def _remember_session(session_id: str, state: dict) -> None:
    """Store a fresh session state, bounding both the number of sessions and the
    per-session undo stack so memory can't grow without limit. Also captures the data
    quality baseline + 'last updated' timestamp for observability (Phase 3.11)."""
    now = time.time()
    state.setdefault("label", "Uploaded")  # the base version's label (Phase 3.2)
    _SESSIONS[session_id] = {
        "states": [state],
        "redo": [],
        "updated_at": now,
        "quality_baseline": {"profile": quality.profile(state["tables"]), "captured_at": now},
        # Formula dependency registry (Phase 4.8): {column: formula} for every formula
        # column Sumio has built this session — powers guardrails' "feeds N formulas"
        # trace-precedents across steps. A fresh upload starts with none.
        "formulas": {},
    }
    while len(_SESSIONS) > config.MAX_SESSIONS:
        _SESSIONS.pop(next(iter(_SESSIONS)))  # evict the oldest (dicts keep order)


def _push_state(session_id: str, state: dict) -> None:
    """Append a new step to a session, trimming the OLDEST steps past the cap while
    always keeping states[0] (the original upload, needed for rewind + lookups)."""
    entry = _SESSIONS.setdefault(session_id, {"states": []})
    states = entry["states"]
    states.append(state)
    if len(states) > config.MAX_STATES:
        entry["states"] = [states[0]] + states[-(config.MAX_STATES - 1):]
    entry["redo"] = []  # a new forward step discards the redo branch (standard model)
    entry["updated_at"] = time.time()  # data changed → keep "last updated" accurate (3.11)


def _record_formulas(session_id: str, operations: list[dict], result) -> None:
    """Update the session's formula registry (Phase 4.8) after a successful run: remember
    each add_formula_column {name: formula}, then prune to columns that still exist in the
    result (a formula column later dropped/renamed stops being a live dependency). This is
    the honest, session-scoped precedent graph guardrails traces for 'feeds N formulas'."""
    entry = _SESSIONS.get(session_id)
    if not entry:
        return
    reg = entry.setdefault("formulas", {})
    for op in operations or []:
        if op.get("action") == "add_formula_column":
            name = (op.get("name") or "").strip()
            formula = op.get("formula") or ""
            if name and formula:
                reg[name] = formula
    live = set()
    frames = result.values() if isinstance(result, dict) else [result]
    for f in frames:
        try:
            live.update(str(c) for c in f.columns)
        except Exception:
            pass
    for k in list(reg):
        if k not in live:
            reg.pop(k, None)


def _error_body(message: str) -> dict:
    """The error BODY on its own. The async job path stores bodies rather than Responses,
    so both paths must be able to build the same shape (see _run_operations_body)."""
    return {"status": "error", "error": message}


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(_error_body(message), status_code=status)
