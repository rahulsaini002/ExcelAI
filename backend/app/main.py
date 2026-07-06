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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from openpyxl import Workbook
from openpyxl.chart import AreaChart, BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Matches {ColumnName} placeholders inside a formula template.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

from . import (
    auth, collab, config, connectors, digest, distribution, exports, fallback, guardrails,
    llm, oidc, org, personalization, pii, quality, slack, store, sync,
)
from .db import init_db, session_scope
from .executor import MultiStepError, OperationError, execute_multi
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
    fwd = request.headers.get("x-forwarded-for")  # set by Render/Vercel/most proxies
    if fwd:
        return fwd.split(",")[0].strip()
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
    _prune_results()
    _save_results_index()
    return rid


_load_results_index()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": config.MODEL}


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
    for i, b in enumerate(block_list):
        metric = metrics.get(i)
        if not metric:
            continue
        t = b.get("type")
        if t == "kpi":
            v = _compute_kpi(df, metric)
            if v is not None:
                b["value"] = v
                b["delta"] = None
        elif t == "chart" and metric.get("group_by"):
            s = _compute_series(df, metric)
            if s:
                b["data"] = s
        elif t == "table" and metric.get("group_by"):
            tbl = _compute_table(df, metric)
            if tbl:
                b["columns"] = tbl["columns"]
                b["rows"] = tbl["rows"]

    return JSONResponse({"status": "ok", "blocks": block_list})


@app.api_route("/download/{result_id}", methods=["GET", "HEAD"])
def download(result_id: str):
    """Stream a generated result file from disk (the browser saves it straight to disk,
    so a large file never lives in the page's memory; survives a server restart).
    HEAD is supported so the frontend can check a file still exists before downloading."""
    meta = _RESULTS.get(result_id)
    path = _RESULTS_DIR / result_id
    if not meta or not path.exists():
        return _error("That download has expired — please re-run the step.", status=404)
    return FileResponse(path, media_type=meta["media_type"], filename=meta["filename"])


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
        "can_undo": True,  # we just appended a step
        "can_redo": len(redo_stack) > 0,
    })


@app.post("/inspect")
async def inspect(
    files: list[UploadFile] = File(...),
    session_id: str = Form(""),
) -> JSONResponse:
    """Read uploaded file(s) and return their structure (sheets, columns + types,
    row count, sample rows) so the UI can show a preview BEFORE any operation.

    If a `session_id` is given, the loaded data is ALSO remembered for that session
    so the two-phase flow (/parse then /execute) can reuse it without re-uploading."""
    if not files:
        return _error("Please upload a spreadsheet.", status=400)
    too_big = _too_big(files)
    if too_big:
        return _error(too_big, status=413)
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    try:
        data = load_files(uploads)
    except ValueError as exc:
        return _error(str(exc), status=400)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

    # Remember the upload for the session so /parse + /execute can use it.
    if session_id:
        _remember_session(
            session_id,
            {
                "tables": dict(data.tables),
                "primary": data.primary,
                "exts": dict(data.exts),
                "notes": dict(data.notes),
            },
        )

    tables = []
    for name, df in data.tables.items():
        s = summarize_structure(df, sample_rows=5)
        rc = s["row_count"]
        note_parts = []
        ocr_note = data.notes.get(name, "")
        if ocr_note:
            note_parts.append(ocr_note)
        if rc == 0:
            note_parts.append("This sheet has no data rows.")
        elif rc > 50_000:
            note_parts.append(f"Large file ({rc:,} rows) — preview shows the first 5 rows.")
        tables.append(
            {
                "name": name,
                "row_count": rc,
                "columns": s["columns"],
                "sample_rows": s["sample_rows"],
                "note": " ".join(note_parts) if note_parts else None,
            }
        )
    return JSONResponse({"status": "ok", "tables": tables})


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
    structure = summarize_tables(tables, primary)
    # PII shield (3.9): mask sensitive sample values + history BEFORE the model sees them.
    structure, shielded = pii.redact_structure(structure, pii.scan_tables(tables))
    history = pii.redact_text(history)
    # Personalization (3.12): prepend the team's learned glossary + preferences to the
    # context so definitions are applied consistently (kept inside `history` so the call
    # signature is unchanged for callers/mocks).
    glossary = personalization.context(team_id)
    context = (glossary + "\n\n" + history).strip() if glossary else history

    # Translate via the Brain (falling back to the deterministic parser if it's down).
    try:
        plan = llm.parse_instruction(instruction, structure, context)
    except Exception as exc:
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()
        plan = fallback.parse(instruction, structure, personalization.definitions(team_id))
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

    clarification = plan.get("clarification")
    reply = plan.get("reply")
    operations = plan.get("operations") or []
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

    return JSONResponse(
        {
            "status": "plan",
            "translation": translation,
            "confidence": confidence,
            # Sensitive columns masked before the model saw them (Phase 3.9).
            "shielded_columns": _shield_columns(shielded),
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
    session_id: str, base: dict, operations: list[dict], ai_title, started_at: float
) -> JSONResponse:
    """Run an operation plan on a base state, push the new state, serialize, and build
    the OK response. Shared shape with /process (deltas, formulas, preview, partial
    warnings, streamed download). Returns a friendly 422 on an expected step failure or
    500 on an unexpected bug. Trusted code runs the plan — the model never executes."""
    tables, primary, exts = base["tables"], base["primary"], base["exts"]

    partial_warning = None
    completed_steps = len(operations)  # all steps ran unless a later one fails
    failed_step = None
    shield_cols: list[str] = []  # /execute runs a pre-approved plan — no AI call, nothing to shield
    try:
        result, result_name, notes, render_ops = execute_multi(tables, primary, operations)
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
    except OperationError as exc:
        return _error(str(exc), status=422)
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

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
        _push_state(session_id, new_state)

    biggest = max((len(t) for t in tables.values()), default=0)
    if biggest > 50_000:
        notes = [
            f"Heads up: this is a large file (~{biggest:,} rows) — it still processed, "
            "but big files can take a little longer."
        ] + notes

    try:
        if isinstance(result, dict):
            out_bytes, out_name, media_type = _serialize_workbook(result, result_name)
            row_count = sum(int(len(d)) for d in result.values())
        else:
            out_ext, upgrade_note = _output_ext(exts.get(result_name, "xlsx"), render_ops)
            if upgrade_note:
                notes = notes + [upgrade_note]
            out_bytes, out_name, media_type = _serialize(result, result_name, out_ext, render_ops)
            row_count = int(len(result))
    except Exception:
        return _error(_INTERNAL_ERROR, status=500)

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

    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Please upload a spreadsheet to start.", status=400)
    states = entry["states"]
    if 0 <= rewind < len(states):
        del states[rewind + 1:]  # Retry/Edit: branch from an earlier step
    base = states[-1]

    try:
        parsed = json.loads(plan)
    except Exception:
        return _error("That plan couldn't be read — please try running again.", status=400)
    operations = parsed.get("operations") or []
    if not operations:
        return _error("There's nothing to run.", status=400)
    ai_title = (parsed.get("title") or "").strip() or None

    # Guardrails (3.10): warn before destructive actions, with concrete impact. Opt-in via
    # `guard` so non-UI callers keep the immediate behaviour; bypassed once `confirm`ed.
    want_guard = str(guard).strip().lower() in ("1", "true", "yes")
    confirmed = str(confirm).strip().lower() in ("1", "true", "yes")
    if want_guard and not confirmed:
        assessment = guardrails.assess(operations, base["tables"], base["primary"])
        if assessment["destructive"]:
            return JSONResponse({
                "status": "confirm_required",
                "warnings": assessment["warnings"],
                "summary": assessment["summary"],
            })

    resp = _run_operations(session_id, base, operations, ai_title, started_at)

    # Record the run for the weekly digest — ONLY for a signed-in user and ONLY on success
    # (a 200; _run_operations returns 4xx/5xx on failure). Best-effort: digest.record_run
    # swallows its own errors, and _extract_row_count guards the body parse, so nothing here
    # can turn a successful task into an error for the user.
    if user is not None and resp.status_code == 200:
        summary = ai_title or ", ".join(dict.fromkeys(
            op.get("action") for op in operations if op.get("action"))) or None
        digest.record_run(user.id, summary, _extract_row_count(resp))

    return resp


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
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    started_at = time.time()
    instruction = (instruction or "").strip()
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

    # 2. Summarize all tables for the model so it can plan across files.
    structure = summarize_tables(tables, primary)
    # PII shield (3.9): mask sensitive sample values + history BEFORE the model sees them.
    structure, shielded = pii.redact_structure(structure, pii.scan_tables(tables))
    history = pii.redact_text(history)
    # Personalization (3.12): prepend the team's learned glossary + preferences to the
    # context so definitions are applied consistently (kept inside `history`).
    glossary = personalization.context(team_id)
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
        plan = fallback.parse(instruction, structure, personalization.definitions(team_id))
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

    clarification = plan.get("clarification")
    reply = plan.get("reply")
    ai_title = (plan.get("title") or "").strip() or None
    operations = plan.get("operations") or []
    if not operations:
        # No action to take: answer a data question, ask for clarity, or nudge.
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
        _push_state(session_id, new_state)

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

    # 6. Serialize. The result is either one table or a multi-sheet workbook.
    try:
        if isinstance(result, dict):
            out_bytes, out_name, media_type = _serialize_workbook(result, result_name)
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
            if len(grouped) >= 2:
                top = str(grouped.idxmax())
                top_val = float(grouped.max())
                share = round(top_val / total * 100)
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


def _serialize_workbook(sheets: dict, base_name: str) -> tuple[bytes, str, str]:
    """Write several tables into ONE .xlsx, each on its own sheet/tab."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        taken: set[str] = set()
        for name, d in sheets.items():
            sheet_name = _safe_sheet_name(name, taken)
            taken.add(sheet_name)
            d.to_excel(writer, index=False, sheet_name=sheet_name)
            _disarm_injection(writer.sheets[sheet_name])
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
    for directive in render_ops:
        kind = directive.get("type")
        if kind == "format":
            _apply_format(ws, df, directive)
        elif kind == "formula":
            _apply_formula(ws, df, directive)
        elif kind == "highlight":
            _apply_highlight(ws, df, directive)
        elif kind == "lookup":
            _apply_lookup(writer, ws, df, directive)
        elif kind == "chart":
            _apply_chart(ws, df, directive)
        elif kind == "dashboard":
            _apply_dashboard(writer, main_name, df, directive)


_CHART_CLASSES = {"bar": BarChart, "line": LineChart, "pie": PieChart, "area": AreaChart}


def _build_chart(data_ws, df, chart_type, x_col, y_cols, title, height=8, width=16):
    """Build an openpyxl chart that references `data_ws`'s cells, so it always reflects
    the current data. Returns the chart, or None if the request can't be charted.
    `data_ws` may differ from the chart's host sheet (the dashboard charts the data
    sheet from the Dashboard sheet)."""
    columns = list(df.columns)
    y_cols = [c for c in (y_cols or []) if c in columns]
    n = len(df)
    if x_col not in columns or not y_cols or n == 0:
        return None
    chart = _CHART_CLASSES.get(chart_type, BarChart)()
    if chart_type == "bar":
        chart.type = "col"  # vertical columns
    if title:
        chart.title = title
    chart.height = height
    chart.width = width
    # Values include the header row so each series is named (titles_from_data).
    for col in y_cols:
        idx = columns.index(col) + 1
        chart.add_data(Reference(data_ws, min_col=idx, min_row=1, max_row=n + 1), titles_from_data=True)
    # Categories are the x-axis labels — data rows only (skip the header).
    x_idx = columns.index(x_col) + 1
    chart.set_categories(Reference(data_ws, min_col=x_idx, min_row=2, max_row=n + 1))
    return chart


def _apply_chart(ws, df, directive: dict) -> None:
    """Add a chart to the data sheet itself (the chart operation, 2.2)."""
    chart = _build_chart(
        ws, df, directive.get("chart_type"), directive.get("x_column"),
        directive.get("y_columns"), directive.get("title"),
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
        )
        if chart is None:
            continue
        dash.add_chart(chart, f"E{anchor_row}")
        anchor_row += 16  # vertical spacing so charts never overlap


def _safe_sheet_name(base: str, taken: set[str]) -> str:
    """A valid, unique Excel sheet name (<=31 chars, no : \\ / ? * [ ])."""
    name = re.sub(r"[:\\/?*\[\]]", " ", str(base)).strip()[:28] or "Lookup"
    candidate = name
    i = 2
    while candidate in taken:
        candidate = f"{name} {i}"[:31]
        i += 1
    return candidate


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
    for i in range(len(df)):
        r = i + 2
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


def _apply_formula(ws, df, directive: dict) -> None:
    """Write LIVE Excel formulas (e.g. =B2*C2) down the formula column, translating
    {ColumnName} placeholders into real cell references from the final layout."""
    columns = list(df.columns)
    name = directive.get("column")
    template = directive.get("formula") or ""
    if name not in columns:
        return
    referenced = _PLACEHOLDER.findall(template)
    # If any referenced column is gone (renamed/dropped later), keep the computed
    # values rather than writing a broken formula.
    if any(r not in columns for r in referenced):
        return
    target_idx = columns.index(name) + 1
    for i in range(len(df)):
        excel_row = i + 2  # row 1 is the header
        cell_formula = _PLACEHOLDER.sub(
            lambda m: f"{get_column_letter(columns.index(m.group(1)) + 1)}{excel_row}",
            template,
        )
        ws.cell(row=excel_row, column=target_idx, value="=" + cell_formula)


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


def _too_big(files) -> str | None:
    """Friendly message if the combined upload exceeds the size limit, else None.
    Guards against a single huge upload exhausting server memory."""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    total = sum((getattr(f, "size", None) or 0) for f in files)
    if total > limit:
        return (
            f"That upload is too large (~{total / 1024 / 1024:.0f} MB). "
            f"Please keep files under {config.MAX_UPLOAD_MB} MB."
        )
    return None


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
) -> JSONResponse:
    """Observability snapshot of the session's current data: a profile (columns/types,
    blank rates), an accurate 'last updated', and a staleness flag."""
    entry = _SESSIONS.get(session_id) if session_id else None
    if not entry or not entry.get("states"):
        return _error("Upload a spreadsheet first.", status=400)
    prof = quality.profile(entry["states"][-1]["tables"])
    updated_at = entry.get("updated_at", time.time())
    stale = quality.staleness(updated_at, time.time(), max_age_hours * 3600)
    return JSONResponse({
        "status": "ok", "profile": prof, "last_updated": updated_at, "staleness": stale,
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
    glossary = personalization.context(team_id)
    safe_history = pii.redact_text(history)
    context = (glossary + "\n\n" + safe_history).strip() if glossary else safe_history

    try:
        plan = llm.parse_instruction(instruction, structure, context)
    except Exception as exc:
        plan = fallback.parse(instruction, structure, personalization.definitions(team_id))
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


def _remember_session(session_id: str, state: dict) -> None:
    """Store a fresh session state, bounding both the number of sessions and the
    per-session undo stack so memory can't grow without limit. Also captures the data
    quality baseline + 'last updated' timestamp for observability (Phase 3.11)."""
    now = time.time()
    _SESSIONS[session_id] = {
        "states": [state],
        "redo": [],
        "updated_at": now,
        "quality_baseline": {"profile": quality.profile(state["tables"]), "captured_at": now},
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


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message}, status_code=status)
