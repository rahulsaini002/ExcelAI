# Sumio (ExcelAI)

A conversational assistant for spreadsheets. Upload a `.csv`/`.xlsx`, describe
what you want done in plain language (Hindi, English, Urdu, or any mix), and get
the result back — no formulas, no menus.

This repo is the **v1 thin slice**: upload → instruction → Gemini turns it into a
small operation plan → trusted Python executes it → download the result + a plain
explanation of what was done.

## Architecture

```
Browser (Next.js, frontend/)
   │  POST /process  (file + instruction)
   ▼
FastAPI engine (backend/)
   1. read file            (pandas)
   2. summarize structure
   3. Gemini API ──────────▶ operation-plan JSON
   4. execute plan          (pandas / openpyxl)
   5. return processed file + "here's what I did"
```

The LLM never touches the file — it only produces a small structured plan, which
trusted code validates and runs. That keeps results predictable and errors easy
to catch.

## Supported operations (v1)

- **Sort** — by one or more columns, ascending/descending
- **Remove duplicates** — by chosen column(s)
- **Add a formula column** — e.g. `Total = {Qty} * {Price}`

The action set is deliberately small so v1 is reliable enough to trust. More
operations come next, on user evidence.

## Run it locally

You need **two terminals** — one for the backend, one for the frontend.

### 1. Backend (FastAPI)

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

copy .env.example .env          # Windows  (cp on macOS/Linux)
# then edit .env and paste your Gemini API key

uvicorn app.main:app --reload --port 8000
```

Get an API key from <https://aistudio.google.com/apikey>. The default model is
`gemini-2.5-flash` (a fast, low-cost tier — instruction parsing is an easy task).
Change it via `SUMIO_MODEL` in `.env`.

### 2. Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000>. The frontend talks to the backend at the URL in
`frontend/.env.local` (`http://localhost:8000` by default).

## Project layout

```
backend/          FastAPI engine
  app/
    main.py         /process + /health endpoints
    reader.py       read file, summarize structure
    llm.py          the single Gemini wrapper (swappable)
    executor.py     runs the operation plan with pandas
    config.py       env-driven settings
frontend/         Next.js app (upload UI, results, download)
  app/page.tsx      the main screen
  lib/api.ts        backend client
```

## Notes

- All LLM calls go through `backend/app/llm.py::parse_instruction` — swap the
  model or provider there without touching anything else.
- v1 reads the first sheet of an `.xlsx` and writes results as values. Writing
  live Excel formulas, multi-sheet lookups, and the rest of the PRD's operation
  set are the next steps.
