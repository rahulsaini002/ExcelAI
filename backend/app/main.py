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

import pandas as pd
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, llm
from .executor import OperationError, execute_plan
from .reader import load_spreadsheet, summarize_structure

app = FastAPI(title="Sumio API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": config.MODEL}


@app.post("/process")
async def process(
    file: UploadFile = File(...),
    instruction: str = Form(...),
) -> JSONResponse:
    instruction = (instruction or "").strip()
    if not instruction:
        return _error("Please describe what you'd like done to the data.", status=400)

    # 1. Read the file.
    raw = await file.read()
    try:
        sheet = load_spreadsheet(raw, file.filename or "upload")
    except ValueError as exc:
        return _error(str(exc), status=400)

    # 2. Summarize structure for the model.
    structure = summarize_structure(sheet.df)

    # 3. Translate the instruction into an operation plan.
    try:
        plan = llm.parse_instruction(instruction, structure)
    except llm.ModelUnavailableError as exc:  # overloaded / rate-limited
        return _error(str(exc), status=503)
    except RuntimeError as exc:  # missing API key, etc.
        return _error(str(exc), status=500)
    except Exception as exc:  # network / parsing failure
        return _error(f"Couldn't understand the instruction: {exc}", status=502)

    clarification = plan.get("clarification")
    operations = plan.get("operations") or []
    if clarification and not operations:
        # Ambiguous — ask rather than guess.
        return JSONResponse({"status": "clarify", "clarification": clarification})

    # 4. Execute the plan.
    try:
        result_df, notes = execute_plan(sheet.df, operations)
    except OperationError as exc:
        return _error(str(exc), status=422)

    # 5. Serialize the result back to the original format and return it.
    out_bytes, out_name, media_type = _serialize(result_df, sheet.filename, sheet.ext)

    return JSONResponse(
        {
            "status": "ok",
            "explanation": " ".join(notes) if notes else "No changes were needed.",
            "notes": notes,
            "row_count": int(len(result_df)),
            "filename": out_name,
            "media_type": media_type,
            "file_base64": base64.b64encode(out_bytes).decode("ascii"),
        }
    )


def _serialize(df, original_name: str, ext: str) -> tuple[bytes, str, str]:
    stem = (original_name or "result").rsplit(".", 1)[0]
    buf = io.BytesIO()
    if ext == "csv":
        df.to_csv(buf, index=False)
        return buf.getvalue(), f"{stem}_sumio.csv", "text/csv"
    # xlsx via openpyxl
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return (
        buf.getvalue(),
        f"{stem}_sumio.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": "error", "error": message}, status_code=status)
