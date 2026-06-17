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
import io
import json
import re
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# Matches {ColumnName} placeholders inside a formula template.
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

from . import config, fallback, llm
from .executor import MultiStepError, OperationError, execute_multi
from .reader import load_files, summarize_structure, summarize_tables

app = FastAPI(title="Sumio API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # Open for local dev so the browser is never blocked by CORS regardless of
    # whether the page is served from localhost, 127.0.0.1, or the LAN IP.
    allow_origin_regex=".*",
    allow_methods=["*"],
    allow_headers=["*"],
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
    """Undo the last successful step: pop the most recent state so the next instruction
    builds on the step before it. Returns how many steps remain."""
    entry = _SESSIONS.get(session_id)
    states = entry["states"] if entry else []
    if len(states) <= 1:  # states[0] is the original upload — nothing to undo
        return _error("There's nothing to undo yet.", status=400)
    states.pop()
    cur = states[-1]
    biggest = max((len(t) for t in cur["tables"].values()), default=0)
    return JSONResponse({
        "status": "ok",
        "steps_remaining": len(states) - 1,  # not counting the original upload
        "row_count": biggest,
        "primary": cur["primary"],
    })


@app.post("/inspect")
async def inspect(files: list[UploadFile] = File(...)) -> JSONResponse:
    """Read uploaded file(s) and return their structure (sheets, columns + types,
    row count, sample rows) so the UI can show a preview BEFORE any operation."""
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

    tables = []
    for name, df in data.tables.items():
        s = summarize_structure(df, sample_rows=5)
        rc = s["row_count"]
        note = None
        if rc == 0:
            note = "This sheet has no data rows."
        elif rc > 50_000:
            note = f"Large file ({rc:,} rows) — preview shows the first 5 rows."
        tables.append(
            {
                "name": name,
                "row_count": rc,
                "columns": s["columns"],
                "sample_rows": s["sample_rows"],
                "note": note,
            }
        )
    return JSONResponse({"status": "ok", "tables": tables})


@app.post("/process")
async def process(
    instruction: str = Form(...),
    session_id: str = Form(""),
    rewind: int = Form(-1),
    history: str = Form(""),
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
            }
        else:
            base = {"tables": dict(data.tables), "primary": data.primary, "exts": dict(data.exts)}
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

    # 3. Translate the instruction into an operation plan.
    try:
        plan = llm.parse_instruction(instruction, structure, history)
    except Exception as exc:
        # The Brain is unavailable (rate limit / quota / outage / parse error). Try a
        # deterministic fallback for simple commands so basic work still happens. The
        # fallback result is shown like any normal result (no "AI unavailable" notice —
        # that reads as a bad/uncertain experience to the user).
        unavailable = isinstance(exc, llm.ModelUnavailableError)
        key_missing = isinstance(exc, RuntimeError) and not unavailable
        if not (unavailable or key_missing):
            traceback.print_exc()  # log the real cause; never blame the instruction
        plan = fallback.parse(instruction, structure)
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

    # 4. Execute the plan across all tables.
    partial_warning = None
    try:
        result, result_name, notes, render_ops = execute_multi(tables, primary, operations)
    except MultiStepError as exc:
        # A later step failed: keep the file reflecting the steps that completed and
        # tell the user exactly which step failed and why (PRD MS-b).
        result, result_name = exc.partial_result, exc.partial_name
        notes, render_ops = exc.notes, exc.format_ops
        done = exc.failed_step - 1
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
            "row_count": row_count,
            "rows_before": int(len(tables[primary])) if primary in tables else None,
            "preview": _result_preview(result, result_name),
            "actions": list(dict.fromkeys(op.get("action") for op in operations)),
            "ai_title": ai_title,
            "partial": partial_warning is not None,
            "warning": partial_warning,
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
    """Choose the download extension. A .csv can't carry live formulas or formatting,
    so when an operation produced any render directive (formula/lookup/format/
    highlight) we upgrade the download to .xlsx — that's what makes the 1.13 promise
    ("formulas live, formatting intact") actually hold. Returns (ext, optional note)."""
    if in_ext == "csv" and render_ops:
        return "xlsx", (
            "Saved as .xlsx so the live formulas/formatting are preserved "
            "(a .csv file can't hold them)."
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


def _remember_session(session_id: str, state: dict) -> None:
    """Store a fresh session state, bounding both the number of sessions and the
    per-session undo stack so memory can't grow without limit."""
    _SESSIONS[session_id] = {"states": [state]}
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


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message}, status_code=status)
