"""PDF and image ingestion: extract tables and return them as DataFrames.

Three extraction paths:
  1. Digital PDF (grid tables)  — pdfplumber extracts tables directly; fast and
     accurate, no Gemini quota used.
  1b. Digital PDF (text layout) — PyMuPDF word-position analysis detects columns
     from consistent x-gaps across rows; handles government/exam-result PDFs that
     use space-aligned columns instead of PDF table grids.  Fast, no quota used.
  2. Scanned PDF / image files — PyMuPDF renders each page to PNG, which is
     sent to Gemini Vision for OCR.  The CSV response is parsed into a DataFrame.
     Every OCR-extracted table carries a verification note shown to the user.

The caller (reader.load_files) gets back a list of (table_name, df, note).
"""
from __future__ import annotations

import io
import re

import pandas as pd

# --- constants ----------------------------------------------------------------

# Shown when a table was produced by Gemini Vision OCR (good quality).
_OCR_NOTE = (
    "Extracted from an image/scan via OCR — please verify the values before "
    "using them. Double-click any cell in the preview to correct misreads."
)

# Shown when OCR confidence is low (blurry / handwritten scan).
_OCR_LOW_QUALITY_NOTE = (
    "Low OCR confidence — the scan may be blurry or handwritten. "
    "Check every value carefully and double-click any cell to correct misreads."
)

# Same as _OCR_NOTE but for the 'no pdfplumber table found → fell back to OCR' path.
_OCR_FALLBACK_NOTE = (
    "No formatted table was found; data was extracted via image recognition. "
    "Please verify the values and double-click to correct any misreads."
)

# Fallback path + low OCR confidence.
_OCR_FALLBACK_LOW_QUALITY_NOTE = (
    "Low OCR confidence — no text table found and the scan may be blurry or "
    "handwritten. Check every value carefully and double-click to correct misreads."
)

# Gemini Vision prompt asking for CSV output from an image.
_OCR_PROMPT = """\
Extract the table(s) from this image as CSV.

Rules:
- The FIRST ROW of each table must be the header row with column names.
- Use commas to separate values. Quote any cell containing a comma with double quotes.
- Strip leading and trailing whitespace from every cell.
- If there are MULTIPLE separate tables in the image, separate them with a line
  that reads exactly:  --- Table N ---  (where N is 1, 2, 3 …)
- If NO table is visible (the image contains only paragraphs, charts, logos, or
  is too blurry to extract reliably): output exactly the string: NO_TABLE_FOUND
- If the image IS blurry, the text is handwritten, or you are uncertain about
  some cell values, add this line as the very first line of your output:
  QUALITY: LOW
- Output ONLY the optional quality flag, the CSV data, and any separator lines.
  No other prose, no markdown.

Clear-scan example (no quality flag):
Name,Score,Grade
Alice,92,A
Bob,78,B+

Poor-scan / handwritten example:
QUALITY: LOW
Name,Score,Grade
Alice,92,A
Bob,78,B+

Two-table example:
--- Table 1 ---
Name,Score
Alice,92
--- Table 2 ---
Month,Revenue
Jan,5200
"""

# Max PDF pages to send through OCR (keeps Gemini quota in check).
_MAX_OCR_PAGES = 5

# Max pages for word-position text-layout extraction (fast — no API calls).
_MAX_TEXT_PAGES = 1000

# Max pages to scan with pdfplumber for grid tables.  If none found in the
# first N pages we assume the PDF has no grid tables and skip to text layout.
_MAX_PDFPLUMBER_SCAN = 20

# Shown when a table was recovered via word-position analysis of text-layout PDF.
_TEXT_LAYOUT_NOTE = (
    "Extracted from PDF text content - values should be accurate. "
    "Double-click any cell to correct any misreads."
)


# --- helpers ------------------------------------------------------------------

_QUALITY_FLAG = "QUALITY: LOW"


def _strip_quality_flag(text: str) -> tuple[str, bool]:
    """Remove the optional QUALITY: LOW first line Gemini adds for uncertain scans.

    Returns (csv_text_without_flag, is_low_quality).
    """
    t = (text or "").strip()
    if t.upper().startswith(_QUALITY_FLAG):
        return t[len(_QUALITY_FLAG):].lstrip("\r\n"), True
    return t, False


def _parse_csv_response(csv_text: str) -> pd.DataFrame | None:
    """Parse one CSV block (from a Gemini Vision reply) into a DataFrame.

    Returns None if the text is empty, the NO_TABLE_FOUND sentinel, or
    unparseable as a CSV with at least one header and one data row.
    """
    text = (csv_text or "").strip()
    if not text or text == "NO_TABLE_FOUND":
        return None
    try:
        df = pd.read_csv(io.StringIO(text))
        if df.empty or len(df.columns) == 0:
            return None
        # Normalise string cells: strip whitespace, drop all-blank rows.
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all").reset_index(drop=True)
        return df if len(df) > 0 else None
    except Exception:
        return None


def _pdfplumber_tables(pdf_data: bytes) -> list[tuple[str, pd.DataFrame]]:
    """Try to extract tables from a digital PDF using pdfplumber.

    Returns (table_name, df) pairs for every table found across all pages.
    Returns an empty list when no tables are found (scanned PDF or plain text).
    """
    import pdfplumber  # imported here so ImportError is localised

    results: list[tuple[str, pd.DataFrame]] = []
    with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
        pages = pdf.pages[:_MAX_PDFPLUMBER_SCAN]
        for page_num, page in enumerate(pages, 1):
            try:
                page_tables = page.extract_tables()
            except Exception:
                continue
            if not page_tables:
                continue
            for tbl_idx, raw in enumerate(page_tables):
                if not raw or not raw[0]:
                    continue
                header = [
                    str(h).strip() if h else f"Col {i + 1}"
                    for i, h in enumerate(raw[0])
                ]
                rows = [
                    [str(c).strip() if c is not None else "" for c in row]
                    for row in raw[1:]
                    if any(c for c in row)
                ]
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=header).replace("", pd.NA)
                suffix = f" Table {tbl_idx + 1}" if len(page_tables) > 1 else ""
                results.append((f"Page {page_num}{suffix}", df))
    return results


def _fitz_visual_word(
    w: tuple, mw: float, mh: float, rot: int
) -> tuple[float, float, float, str]:
    """Return (lx0, lx1, ly0, text) in the page's visual coordinate system.

    PyMuPDF word tuples are (x0, y0, x1, y1, text, …) in raw PDF space.
    Many PDFs store pages in portrait orientation with a 90° rotation tag so
    they display as landscape.  We undo that rotation here.
    """
    x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
    if rot == 90:
        return (mh - y1, mh - y0, x0, text)   # lx0, lx1, ly0
    if rot == 270:
        return (y0, y1, mw - x1, text)
    if rot == 180:
        return (mw - x1, mw - x0, mh - y1, text)
    return (x0, x1, y0, text)                  # rot == 0


def _detect_text_col_slices(
    sorted_rows: list[tuple[int, list[tuple[float, float, str]]]],
    data_rows: list[tuple[int, list[tuple[float, float, str]]]],
) -> tuple[list[tuple[float, float]] | None, list[str] | None]:
    """Detect column (lo, hi) x-ranges from consistent inter-word gaps.

    A column boundary exists at an x-position where a gap ≥ 10pt between the
    right edge of word[i] and the left edge of word[i+1] occurs in ≥25 % of
    data rows.  Returns (slices, col_names) or (None, None) if not found.
    """
    from collections import defaultdict

    gap_counts: dict[int, int] = defaultdict(int)
    for _, ws in data_rows:
        seen: set[int] = set()
        for i in range(1, len(ws)):
            gap = ws[i][0] - ws[i - 1][1]      # lx0_next - lx1_prev
            if gap >= 10.0:
                xkey = int(ws[i][0] / 5) * 5   # floor to nearest-5 (keeps boundary ≤ col start)
                if xkey not in seen:
                    gap_counts[xkey] += 1
                    seen.add(xkey)

    min_count = max(2, int(len(data_rows) * 0.25))
    candidates = sorted(x for x, c in gap_counts.items() if c >= min_count)

    # Merge nearby boundaries (within 15pt)
    merged: list[float] = []
    for x in candidates:
        if merged and x - merged[-1] < 15:
            merged[-1] = (merged[-1] + x) / 2
        else:
            merged.append(float(x))

    if not merged:
        return None, None

    slices: list[tuple[float, float]] = [(0.0, merged[0])]
    for i in range(len(merged) - 1):
        slices.append((merged[i], merged[i + 1]))
    slices.append((merged[-1], 1e9))

    # Header rows: pick the pre-data row that starts closest to the left data
    # edge and spans the widest x-range.  Then supplement empty right-side cols
    # from any other pre-data row (catches sub-headers like "MARKS (Part A)").
    first_data_y = data_rows[0][0]
    leftmost_data_x = min(ws[0][0] for _, ws in data_rows if ws)
    pre_data = [
        (y, ws) for y, ws in sorted_rows
        if y < first_data_y and ws and ws[0][0] <= leftmost_data_x + 35
    ]
    col_names: list[str] | None = None
    if pre_data:
        best_y, best_ws = max(
            pre_data,
            key=lambda r: r[1][-1][0] - r[1][0][0] if len(r[1]) >= 2 else 0.0,
        )
        # Assign words to columns with a small left-side tolerance (header words
        # sometimes align a few pts to the left of the data column boundary).
        HEADER_TOL = 10.0
        parts: list[list[str]] = [[] for _ in slices]

        def _assign_header_word(lx0: float) -> int:
            """Return the column index for a header word at lx0.

            Scans slices right-to-left so that a word positioned just below a
            column boundary (e.g. 128.3 vs boundary at 130) is correctly assigned
            to the column to the right, not the one to the left.
            """
            for i in range(len(slices) - 1, -1, -1):
                if slices[i][0] - HEADER_TOL <= lx0:
                    return i
            return 0

        # Step 1: fill from the best header row (allow multiple words per col)
        for lx0, lx1, text in best_ws:
            parts[_assign_header_word(lx0)].append(text)

        # Step 2: supplement EMPTY columns from other pre-data rows
        for src_y, src_ws in pre_data:
            if src_y == best_y:
                continue
            for lx0, lx1, text in src_ws:
                col_i = _assign_header_word(lx0)
                if not parts[col_i]:
                    parts[col_i].append(text)

        col_names = [" ".join(p).strip() or f"Col {i + 1}" for i, p in enumerate(parts)]

    return slices, col_names


def _words_to_col_record(
    ws: list[tuple[float, float, str]], slices: list[tuple[float, float]]
) -> list[str]:
    """Join words in each column slice into a single string."""
    cols: list[list[str]] = [[] for _ in slices]
    for lx0, lx1, text in ws:
        for i, (lo, hi) in enumerate(slices):
            if lo <= lx0 < hi:
                cols[i].append(text)
                break
    return [' '.join(c).strip() for c in cols]


def _text_layout_tables(pdf_data: bytes) -> list[tuple[str, pd.DataFrame]]:
    """Extract structured tables from text-layout PDFs via word-position analysis.

    Used when pdfplumber finds no grid tables but the PDF has a real text layer
    (e.g. government exam-result sheets, bank statements with space-aligned cols).

    Strategy:
      1. Use PyMuPDF's fast word extraction (handles page rotation).
      2. Group words into visual rows via 8pt y-quantization.
      3. Detect column x-boundaries from inter-word gaps consistent across rows.
      4. Parse every data row (first word is a digit) into column records.
      5. Append multi-line category/field continuations to the preceding record.
      6. Return a single combined DataFrame across all pages.

    Returns [] if the PDF has no detectable columnar structure.
    """
    import fitz  # PyMuPDF

    all_records: list[list[str]] = []
    col_slices: list[tuple[float, float]] | None = None
    col_names: list[str] | None = None

    doc = fitz.open("pdf", pdf_data)
    try:
        for pg_idx in range(min(len(doc), _MAX_TEXT_PAGES)):
            page = doc[pg_idx]
            rot = page.rotation
            mw, mh = page.mediabox.width, page.mediabox.height

            raw_words = page.get_text("words")
            if not raw_words:
                continue

            # Convert to visual coords and group into rows by y (8pt tolerance).
            row_map: dict[int, list[tuple[float, float, str]]] = {}
            for lx0, lx1, ly0, text in [_fitz_visual_word(w, mw, mh, rot) for w in raw_words]:
                row_y = int(ly0 / 8 + 0.5) * 8
                row_map.setdefault(row_y, []).append((lx0, lx1, text))

            sorted_rows = [
                (y, sorted(ws, key=lambda w: w[0]))
                for y, ws in sorted(row_map.items())
            ]

            # Detect columns on the first page that has enough data rows
            if col_slices is None:
                data_rows = [
                    (y, ws) for y, ws in sorted_rows
                    if ws and ws[0][2].isdigit()
                ]
                if len(data_rows) < 3:
                    continue
                col_slices, col_names = _detect_text_col_slices(sorted_rows, data_rows)
                if not col_slices or len(col_slices) < 2:
                    col_slices = None
                    continue

            # Parse rows into records
            n_col = len(col_slices)
            prev: list[str] | None = None

            for _, ws in sorted_rows:
                record = _words_to_col_record(ws, col_slices)
                sno = record[0].strip()

                if sno and sno.isdigit():
                    if prev is not None:
                        all_records.append(prev)
                    prev = record
                elif prev is not None and not sno:
                    # Continuation line: merge only the category/description column
                    # (3rd from right).  Require the name column (one to the left)
                    # to be empty — if it has text the row is a footer/note, not a
                    # genuine field continuation, and should be skipped.
                    cat_col = max(2, n_col - 3)
                    name_col = max(0, cat_col - 1)
                    extra = record[cat_col].strip()
                    if (
                        extra
                        and not record[0].strip()
                        and not record[name_col].strip()
                    ):
                        prev[cat_col] = (prev[cat_col] + " " + extra).strip()

            if prev is not None:
                all_records.append(prev)

    finally:
        doc.close()

    if not all_records or col_slices is None:
        return []

    n_col = len(col_slices)
    headers = (
        col_names if col_names and len(col_names) == n_col
        else [f"Col {i + 1}" for i in range(n_col)]
    )
    padded = [r[:n_col] + [""] * max(0, n_col - len(r)) for r in all_records]
    df = pd.DataFrame(padded, columns=headers)
    df = df.replace("", pd.NA).dropna(how="all").reset_index(drop=True)
    return [("Sheet1", df)] if len(df) > 0 else []


def _render_page_png(pdf_data: bytes, page_num: int, dpi: int = 150) -> bytes:
    """Render a single PDF page (0-based index) to PNG bytes."""
    import fitz  # PyMuPDF

    doc = fitz.open("pdf", pdf_data)
    try:
        page = doc[page_num]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def _count_pages(pdf_data: bytes) -> int:
    """Return the number of pages in a PDF without loading all of them."""
    import fitz

    doc = fitz.open("pdf", pdf_data)
    n = len(doc)
    doc.close()
    return n


# --- public API ---------------------------------------------------------------

def load_pdf(
    filename: str, pdf_data: bytes
) -> list[tuple[str, pd.DataFrame, str]]:
    """Load a PDF file into one or more named DataFrames.

    Returns a list of (table_name, df, note).  `note` is non-empty when
    OCR was used and the user should verify the extracted values.

    Raises ValueError with a user-friendly message on unrecoverable failure.
    Propagates llm.ModelUnavailableError so main.py can surface the quota message.
    """
    from . import llm  # deferred import avoids a circular dependency

    # Path 1 — digital PDF: try pdfplumber first (no AI quota used).
    try:
        tables = _pdfplumber_tables(pdf_data)
    except ImportError:
        tables = []  # pdfplumber not installed → fall through to OCR
    except Exception as exc:
        raise ValueError(
            f"Couldn't read '{filename}' as a PDF — it may be corrupted or "
            "password-protected. Try opening it and re-saving."
        ) from exc

    if tables:
        return [(name, df, "") for name, df in tables]

    # Path 1b — text-layout PDF: word-position column detection (fast, no quota).
    try:
        text_tables = _text_layout_tables(pdf_data)
    except Exception:
        text_tables = []

    if text_tables:
        return [(name, df, _TEXT_LAYOUT_NOTE) for name, df in text_tables]

    # Path 2 — no text tables found: render each page → Gemini Vision OCR.
    try:
        n_pages = min(_count_pages(pdf_data), _MAX_OCR_PAGES)
    except Exception as exc:
        raise ValueError(
            f"Couldn't open '{filename}' as a PDF. "
            "It may be corrupted or not a valid PDF file."
        ) from exc

    results: list[tuple[str, pd.DataFrame, str]] = []
    for page_num in range(n_pages):
        try:
            img_bytes = _render_page_png(pdf_data, page_num)
            csv_text = llm.ocr_image(img_bytes, "image/png")
            clean_text, low_quality = _strip_quality_flag(csv_text or "")
            df = _parse_csv_response(clean_text)
            note = _OCR_FALLBACK_LOW_QUALITY_NOTE if low_quality else _OCR_FALLBACK_NOTE
        except llm.ModelUnavailableError:
            raise  # propagate so main.py returns the friendly quota message
        except Exception:
            df = None
            note = _OCR_FALLBACK_NOTE

        if df is not None:
            results.append((f"Page {page_num + 1}", df, note))

    if not results:
        raise ValueError(
            f"No table data could be extracted from '{filename}'. "
            "The file may be image-only, contain only text / charts (no grid tables), "
            "or the scan quality may be too low. "
            "Try copying the table into Excel or CSV and uploading that instead."
        )
    return results


def load_image(
    filename: str, img_data: bytes, mime_type: str
) -> list[tuple[str, pd.DataFrame, str]]:
    """OCR a single image file and return a list of (name, df, note) tuples.

    Multiple tables separated by '--- Table N ---' markers in the Gemini response
    are each returned as a separate entry.

    Raises ValueError with a user-friendly message when no table is detected.
    Propagates llm.ModelUnavailableError for main.py to handle.
    """
    from . import llm

    try:
        csv_text = llm.ocr_image(img_data, mime_type)
    except llm.ModelUnavailableError:
        raise

    # Strip the optional QUALITY: LOW confidence flag before splitting tables.
    raw_text, low_quality = _strip_quality_flag(csv_text or "")
    note = _OCR_LOW_QUALITY_NOTE if low_quality else _OCR_NOTE

    segments = re.split(r"(?m)^---\s*Table\s*\d+\s*---\s*$", raw_text)
    base = filename.rsplit(".", 1)[0] if "." in filename else filename

    results: list[tuple[str, pd.DataFrame, str]] = []
    for i, seg in enumerate(segments, 1):
        df = _parse_csv_response(seg)
        if df is not None:
            name = base if len(segments) == 1 else f"{base} - Table {i}"
            results.append((name, df, note))

    if not results:
        raise ValueError(
            f"No table data could be extracted from '{filename}'. "
            "The image may not contain a recognizable table, or the quality "
            "may be too low. Try exporting the data to Excel or CSV instead."
        )
    return results
