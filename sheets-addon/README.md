# Sumio — Google Sheets add-on

An alternative path for Sumio: instead of upload → download, edit a **live Google Sheet**
from a sidebar. **Same Brain** (the Sumio FastAPI backend), **different Hands** — Apps Script
reads the sheet and writes results back in place. No operation logic is re-implemented here;
all the intelligence and governance (PII shield, guardrails, personalization, multi-step
planning) stay in the backend.

## How it works

```
Sidebar (Sidebar.html)
   │  instruction + live grid
   ▼
Code.gs ──POST /sheets/plan──▶  Backend "Brain": parse → plan (+ PII shield, glossary,
   │                            guardrail assessment, base_hash for concurrency)
   │  show plan / suggestion
   ▼
Code.gs ──POST /sheets/apply─▶  Backend "Hands": run the trusted executor on the grid →
   │   (inside a document lock,   return the new grid (+ confirm gate, conflict check)
   │    re-reading the sheet)
   ▼
writes the new grid back to the sheet
```

## The four key behaviours

| Requirement | Where it's enforced |
|---|---|
| **Operations apply live** | `/sheets/apply` returns the transformed grid; `Code.gs` writes it back with `setValues`. |
| **View-only → suggestions, not edits** | `Code.gs._canEdit_()` probes write access; view-only users get `status:"suggestion"` and **no Apply button**, and `applyRequest` refuses to write. |
| **Human + AI edits sequenced safely** | `applyRequest` takes a `LockService` document lock, **re-reads** the sheet, and sends a `base_hash`; the backend returns `409 conflict` if the data changed since the plan, so an AI edit never overwrites a human's. |
| **Broken / empty sheet handled** | The backend returns friendly errors for empty/headerless sheets and pads ragged rows; `_writeValues_` normalizes rows. |

These are covered by `ExcelAI/backend/test_sheets.py` (27 checks) at the API boundary;
the Apps Script layer adds the lock + view-only write guard on top.

## Setup

1. In a Google Sheet: **Extensions → Apps Script**.
2. Create files matching this folder: `Code.gs`, `Sidebar.html`, and the `appsscript.json`
   manifest (Project Settings → "Show appsscript.json"). Or push with
   [`clasp`](https://github.com/google/clasp): `clasp push`.
3. **Project Settings → Script properties** → add `SUMIO_API_URL` = your backend base URL.
   It must be **HTTPS and reachable by Google's servers** (a deployed instance or a tunnel
   like `ngrok` / Cloud Run — `localhost` won't work from Apps Script).
4. Reload the sheet → **Sumio** menu → **Open Sumio**. Authorize when prompted.

## Notes

- OAuth scopes are minimal: `spreadsheets.currentonly` (only the open sheet),
  `script.external_request` (call the backend), `script.container.ui` (the sidebar).
- The backend's existing CORS config doesn't matter here — `UrlFetchApp` runs server-side
  in Google's infra, not in a browser.
- This is **not a separate product**: it shares the same `/parse`-equivalent Brain and the
  same executor as the web app. The web upload/download flow is unchanged.
