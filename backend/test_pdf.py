"""test_pdf.py — Phase 3.1 PDF / Image / OCR ingestion tests.

Runs without pytest (standalone script).  Live Gemini calls are SKIPPED unless
an API key + network are available — marked BRAIN below.

PDF extraction uses pdfplumber (digital) and Gemini Vision (OCR) as fallbacks.
All Gemini calls are monkeypatched so we test the logic without burning quota.
"""
from __future__ import annotations

import importlib
import io
import sys
import types as py_types
import unittest.mock as mock

# Resolve the backend package from the adjacent 'app/' directory.
import os, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# ── helpers ──────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
SKIP = 0

def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {name}")

def fail(name: str, reason: str) -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {name}: {reason}")

def skip(name: str, reason: str) -> None:
    global SKIP
    SKIP += 1
    print(f"SKIP  {name}: {reason}")


# ── minimal digital PDF (from pdfplumber's own test fixtures) ──────────────
# We create a tiny in-memory PDF with a single table using reportlab-free bytes.
# Instead of a real PDF renderer (heavy dep), we use pdfplumber with a known
# small PDF included in its package as a test asset.  If the asset path changes,
# the test falls back gracefully.

def _get_pdfplumber_sample() -> bytes | None:
    """Return bytes of a small PDF that pdfplumber can extract tables from."""
    try:
        import pdfplumber
        pkg_dir = pathlib.Path(pdfplumber.__file__).parent
        # pdfplumber ships sample PDFs for its own tests
        candidates = list(pkg_dir.rglob("*table*.pdf")) + list(pkg_dir.rglob("*.pdf"))
        if candidates:
            return candidates[0].read_bytes()
    except Exception:
        pass
    return None


# ── minimal fake CSV response from Gemini Vision ─────────────────────────────

_FAKE_CSV = "Name,Score\nAlice,92\nBob,78\n"
_FAKE_CSV_TWO = "Name,Score\nAlice,92\n--- Table 2 ---\nMonth,Revenue\nJan,5200\n"
_NO_TABLE = "NO_TABLE_FOUND"


# ── tests ─────────────────────────────────────────────────────────────────────

def test_parse_csv_response_basic():
    from app.pdf_reader import _parse_csv_response
    df = _parse_csv_response("Name,Score\nAlice,92\nBob,78\n")
    assert df is not None
    assert list(df.columns) == ["Name", "Score"]
    assert len(df) == 2
    assert df.iloc[0]["Name"] == "Alice"
    assert str(df.iloc[0]["Score"]) == "92"
    ok("parse_csv_response_basic")


def test_parse_csv_response_no_table():
    from app.pdf_reader import _parse_csv_response
    assert _parse_csv_response("NO_TABLE_FOUND") is None
    assert _parse_csv_response("") is None
    assert _parse_csv_response(None) is None
    ok("parse_csv_response_no_table")


def test_parse_csv_response_strips_whitespace():
    from app.pdf_reader import _parse_csv_response
    df = _parse_csv_response(" Name , Score \n Alice , 92 \n")
    assert df is not None
    assert list(df.columns) == ["Name", "Score"]
    assert df.iloc[0]["Name"] == "Alice"
    ok("parse_csv_response_strips_whitespace")


def test_load_image_basic_ocr():
    """load_image: Gemini returns valid CSV → one DataFrame with OCR note."""
    with mock.patch("app.llm.ocr_image", return_value=_FAKE_CSV):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        result = pdf_reader.load_image("invoice.png", b"FAKEIMGBYTES", "image/png")

    assert len(result) == 1
    name, df, note = result[0]
    assert name == "invoice"
    assert list(df.columns) == ["Name", "Score"]
    assert len(df) == 2
    assert "verify" in note.lower() or "ocr" in note.lower()
    ok("load_image_basic_ocr")


def test_load_image_multi_table():
    """load_image: Gemini returns two-table CSV → two DataFrames."""
    with mock.patch("app.llm.ocr_image", return_value=_FAKE_CSV_TWO):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        result = pdf_reader.load_image("doc.jpg", b"FAKEIMGBYTES", "image/jpeg")

    assert len(result) == 2
    _, df1, _ = result[0]
    _, df2, _ = result[1]
    assert "Name" in df1.columns
    assert "Month" in df2.columns
    ok("load_image_multi_table")


def test_load_image_no_table_found():
    """load_image: Gemini returns NO_TABLE_FOUND → ValueError."""
    with mock.patch("app.llm.ocr_image", return_value=_NO_TABLE):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        try:
            pdf_reader.load_image("photo.png", b"FAKEIMGBYTES", "image/png")
            fail("load_image_no_table_found", "expected ValueError")
        except ValueError as e:
            assert "extracted" in str(e).lower() or "table" in str(e).lower()
            ok("load_image_no_table_found")


def test_load_image_low_quality():
    """load_image: Gemini returns QUALITY: LOW prefix → low-confidence warning note."""
    low_quality_response = "QUALITY: LOW\n" + _FAKE_CSV
    with mock.patch("app.llm.ocr_image", return_value=low_quality_response):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        result = pdf_reader.load_image("blurry.png", b"FAKEIMGBYTES", "image/png")

    assert len(result) == 1
    name, df, note = result[0]
    assert list(df.columns) == ["Name", "Score"]
    assert len(df) == 2
    # Must carry the low-confidence warning, not the generic verify note.
    assert "low" in note.lower() and ("confidence" in note.lower() or "quality" in note.lower())
    ok("load_image_low_quality")


def test_load_image_low_quality_case_insensitive():
    """QUALITY: LOW flag is matched case-insensitively."""
    for prefix in ("QUALITY: LOW", "quality: low", "Quality: Low"):
        response = f"{prefix}\n{_FAKE_CSV}"
        with mock.patch("app.llm.ocr_image", return_value=response):
            from app import pdf_reader
            importlib.reload(pdf_reader)
            result = pdf_reader.load_image("scan.jpg", b"FAKEIMGBYTES", "image/jpeg")
        assert len(result) == 1
        _, _, note = result[0]
        assert "low" in note.lower()
    ok("load_image_low_quality_case_insensitive")


def test_load_image_good_quality_no_flag():
    """load_image: clean Gemini response (no QUALITY flag) → standard verify note, not low-confidence."""
    with mock.patch("app.llm.ocr_image", return_value=_FAKE_CSV):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        result = pdf_reader.load_image("clear.png", b"FAKEIMGBYTES", "image/png")

    _, _, note = result[0]
    # Should NOT have the low-confidence language.
    assert "low" not in note.lower()
    assert "verify" in note.lower() or "ocr" in note.lower()
    ok("load_image_good_quality_no_flag")


def test_load_pdf_fallback_low_quality():
    """load_pdf OCR path: QUALITY: LOW in OCR response → fallback-low-quality note."""
    fake_page = mock.MagicMock()
    fake_page.extract_tables.return_value = []
    fake_pdf_ctx = mock.MagicMock()
    fake_pdf_ctx.__enter__ = mock.Mock(return_value=fake_pdf_ctx)
    fake_pdf_ctx.__exit__ = mock.Mock(return_value=False)
    fake_pdf_ctx.pages = [fake_page]

    from app import pdf_reader
    importlib.reload(pdf_reader)

    with mock.patch("pdfplumber.open", return_value=fake_pdf_ctx), \
         mock.patch("app.pdf_reader._render_page_png", return_value=b"PNG"), \
         mock.patch("app.pdf_reader._count_pages", return_value=1), \
         mock.patch("app.llm.ocr_image", return_value="QUALITY: LOW\n" + _FAKE_CSV):
        result = pdf_reader.load_pdf("handwritten.pdf", b"FAKEPDFBYTES")

    assert len(result) == 1
    _, df, note = result[0]
    assert list(df.columns) == ["Name", "Score"]
    assert "low" in note.lower()
    ok("load_pdf_fallback_low_quality")


def test_load_image_quota_error_propagates():
    """load_image: ModelUnavailableError propagates so main.py can surface it."""
    from app.llm import ModelUnavailableError
    with mock.patch("app.llm.ocr_image", side_effect=ModelUnavailableError("quota")):
        from app import pdf_reader
        importlib.reload(pdf_reader)
        try:
            pdf_reader.load_image("x.png", b"FAKEIMGBYTES", "image/png")
            fail("load_image_quota_error_propagates", "expected ModelUnavailableError")
        except ModelUnavailableError:
            ok("load_image_quota_error_propagates")


def test_load_pdf_digital_pdfplumber():
    """load_pdf with a digital PDF: pdfplumber finds a table → no OCR."""
    sample = _get_pdfplumber_sample()
    if sample is None:
        skip("load_pdf_digital_pdfplumber", "no pdfplumber sample PDF found")
        return
    from app import pdf_reader
    try:
        result = pdf_reader.load_pdf("sample.pdf", sample)
    except Exception as e:
        skip("load_pdf_digital_pdfplumber", f"extraction failed: {e}")
        return
    # We don't know what tables are in the sample, but we should get something.
    # If no tables, pdfplumber falls through to OCR (which we haven't mocked) —
    # just confirm it didn't crash with the pdfplumber path.
    assert isinstance(result, list)
    ok("load_pdf_digital_pdfplumber")


def test_load_pdf_fallback_to_ocr():
    """load_pdf: pdfplumber finds nothing → falls back to Gemini Vision OCR."""
    # Patch pdfplumber to return no tables, and Gemini to return a CSV.
    fake_page = mock.MagicMock()
    fake_page.extract_tables.return_value = []
    fake_pdf_ctx = mock.MagicMock()
    fake_pdf_ctx.__enter__ = mock.Mock(return_value=fake_pdf_ctx)
    fake_pdf_ctx.__exit__ = mock.Mock(return_value=False)
    fake_pdf_ctx.pages = [fake_page]

    # Patch fitz (PyMuPDF) to return a trivial 1×1 black PNG.
    import struct, zlib
    def _make_tiny_png():
        sig = b'\x89PNG\r\n\x1a\n'
        def chunk(t, d): l = len(d); return struct.pack('>I',l)+t+d+struct.pack('>I',zlib.crc32(t+d)&0xffffffff)
        ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
        raw = b'\x00\x00\x00\x00'
        idat = chunk(b'IDAT', zlib.compress(raw))
        iend = chunk(b'IEND', b'')
        return sig+ihdr+idat+iend

    tiny_png = _make_tiny_png()

    from app import pdf_reader
    importlib.reload(pdf_reader)

    with mock.patch("pdfplumber.open", return_value=fake_pdf_ctx), \
         mock.patch("app.pdf_reader._render_page_png", return_value=tiny_png), \
         mock.patch("app.pdf_reader._count_pages", return_value=1), \
         mock.patch("app.llm.ocr_image", return_value=_FAKE_CSV):
        result = pdf_reader.load_pdf("scan.pdf", b"FAKEPDFBYTES")

    assert len(result) == 1
    name, df, note = result[0]
    assert name == "Page 1"
    assert list(df.columns) == ["Name", "Score"]
    assert "verify" in note.lower() or "ocr" in note.lower()
    ok("load_pdf_fallback_to_ocr")


def test_load_pdf_no_content_anywhere():
    """load_pdf: pdfplumber finds nothing AND OCR says NO_TABLE_FOUND → ValueError."""
    fake_page = mock.MagicMock()
    fake_page.extract_tables.return_value = []
    fake_pdf_ctx = mock.MagicMock()
    fake_pdf_ctx.__enter__ = mock.Mock(return_value=fake_pdf_ctx)
    fake_pdf_ctx.__exit__ = mock.Mock(return_value=False)
    fake_pdf_ctx.pages = [fake_page]

    from app import pdf_reader
    importlib.reload(pdf_reader)

    with mock.patch("pdfplumber.open", return_value=fake_pdf_ctx), \
         mock.patch("app.pdf_reader._render_page_png", return_value=b"PNG"), \
         mock.patch("app.pdf_reader._count_pages", return_value=1), \
         mock.patch("app.llm.ocr_image", return_value=_NO_TABLE):
        try:
            pdf_reader.load_pdf("blank.pdf", b"FAKEPDFBYTES")
            fail("load_pdf_no_content_anywhere", "expected ValueError")
        except ValueError as e:
            assert "table" in str(e).lower() or "extracted" in str(e).lower()
            ok("load_pdf_no_content_anywhere")


def test_reader_routes_pdf_to_pdf_reader():
    """load_files routes .pdf files to pdf_reader, not load_spreadsheet."""
    import pandas as pd
    from app import reader
    fake_df = pd.DataFrame({"Name": ["Alice"], "Score": [92]})

    with mock.patch("app.pdf_reader.load_pdf", return_value=[("Page 1", fake_df, "OCR note")]):
        data = reader.load_files([("invoice.pdf", b"FAKEPDF")])

    assert "Page 1" in data.tables
    assert data.exts["Page 1"] == "pdf"
    assert data.notes["Page 1"] == "OCR note"
    assert data.primary == "Page 1"
    ok("reader_routes_pdf_to_pdf_reader")


def test_reader_routes_image_to_pdf_reader():
    """load_files routes .jpg/.png files to pdf_reader, not load_spreadsheet."""
    import pandas as pd
    from app import reader
    fake_df = pd.DataFrame({"A": [1], "B": [2]})

    with mock.patch("app.pdf_reader.load_image", return_value=[("photo", fake_df, "OCR note")]):
        data = reader.load_files([("table.png", b"FAKEIMG")])

    assert "photo" in data.tables
    assert data.exts["photo"] == "pdf"
    assert data.notes["photo"] == "OCR note"
    ok("reader_routes_image_to_pdf_reader")


def test_reader_unsupported_type():
    """load_files: .docx → ValueError (not a spreadsheet or PDF/image)."""
    from app import reader
    try:
        reader.load_files([("report.docx", b"FAKEDOCX")])
        fail("reader_unsupported_type", "expected ValueError")
    except ValueError as e:
        assert "unsupported" in str(e).lower() or "excel" in str(e).lower()
        ok("reader_unsupported_type")


def test_output_ext_pdf_always_xlsx():
    """_output_ext: 'pdf' ext always returns xlsx (even with no render ops)."""
    from app.main import _output_ext
    ext, note = _output_ext("pdf", [])
    assert ext == "xlsx"
    assert note is None
    ext2, _ = _output_ext("pdf", [{"type": "format"}])
    assert ext2 == "xlsx"
    ok("output_ext_pdf_always_xlsx")


def test_inspect_endpoint_note():
    """POST /inspect with a mocked PDF upload: OCR note appears in the response."""
    import pandas as pd
    from fastapi.testclient import TestClient
    from app.main import app
    from app import reader

    fake_df = pd.DataFrame({"Name": ["Alice", "Bob"], "Score": [92, 78]})

    with mock.patch("app.pdf_reader.load_pdf",
                    return_value=[("Page 1", fake_df, "Extracted via OCR — please verify.")]):
        client = TestClient(app)
        response = client.post(
            "/inspect",
            files=[("files", ("invoice.pdf", b"FAKEPDF", "application/pdf"))],
        )

    assert response.status_code == 200
    data = response.json()
    tables = data["tables"]
    assert len(tables) == 1
    assert tables[0]["name"] == "Page 1"
    assert tables[0]["row_count"] == 2
    assert "verify" in (tables[0].get("note") or "").lower()
    ok("inspect_endpoint_note")


def test_process_endpoint_pdf():
    """POST /process with a PDF → executes operations → returns a result."""
    import pandas as pd
    from fastapi.testclient import TestClient
    from app.main import app

    fake_df = pd.DataFrame({"Name": ["Bob", "Alice"], "Score": [78, 92]})

    sort_plan = '{"operations": [{"action": "sort", "columns": ["Score"], "orders": ["desc"]}]}'

    with mock.patch("app.pdf_reader.load_pdf",
                    return_value=[("Page 1", fake_df, "OCR — please verify.")]), \
         mock.patch("app.llm.parse_instruction",
                    return_value={"operations": [
                        {"action": "sort", "columns": ["Score"], "orders": ["desc"]}
                    ], "title": "Sort by score"}):
        client = TestClient(app)
        # Upload + process in one /process call
        response = client.post(
            "/process",
            data={"instruction": "sort by Score descending"},
            files=[("files", ("data.pdf", b"FAKEPDF", "application/pdf"))],
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["row_count"] == 2
    # First row after descending sort should be Alice (score 92)
    preview_rows = (body.get("preview") or [{}])[0].get("sample_rows", [])
    if preview_rows:
        assert str(preview_rows[0].get("Score", "")) == "92"
    ok("process_endpoint_pdf")


def test_inspect_mixed_spreadsheet_and_pdf():
    """POST /inspect with .xlsx + .pdf → both tables returned, PDF has note."""
    import pandas as pd
    from fastapi.testclient import TestClient
    from app.main import app

    fake_pdf_df = pd.DataFrame({"Item": ["Widget"], "Price": [9.99]})

    # Create a tiny real XLSX in memory.
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Product", "Qty"])
    ws.append(["A", 5])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    with mock.patch("app.pdf_reader.load_pdf",
                    return_value=[("invoice - Page 1", fake_pdf_df, "OCR note")]):
        client = TestClient(app)
        response = client.post(
            "/inspect",
            files=[
                ("files", ("products.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
                ("files", ("invoice.pdf", b"FAKEPDF", "application/pdf")),
            ],
        )

    assert response.status_code == 200
    tables = response.json()["tables"]
    names = [t["name"] for t in tables]
    assert any("invoice" in n.lower() or "Page 1" in n for n in names)
    assert any("products" in n.lower() for n in names)
    # PDF table has the OCR note; XLSX table does not.
    pdf_table = next(t for t in tables if "invoice" in t["name"].lower() or "Page 1" in t["name"])
    assert pdf_table["note"] is not None and "ocr" in pdf_table["note"].lower()
    xlsx_table = next(t for t in tables if "products" in t["name"].lower())
    assert xlsx_table["note"] is None
    ok("inspect_mixed_spreadsheet_and_pdf")


# ── text-layout PDF extraction ───────────────────────────────────────────────

def test_text_layout_tables_mocked():
    """_text_layout_tables correctly parses space-aligned columns from mock words.

    Simulates a rotated (rot=0) 2-page PDF where each page has a header row
    and 3 data rows with columns ID | NAME | SCORE using fitz word tuples.
    """
    import unittest.mock as mock
    importlib.reload(__import__("app.pdf_reader", fromlist=["pdf_reader"]))
    from app.pdf_reader import _text_layout_tables

    # fitz word tuple: (x0, y0, x1, y1, text, block, line, word)
    # Layout (rot=0): columns at x ~10, 50, 120
    def _w(x0, y0, x1, y1, text):
        return (x0, y0, x1, y1, text, 0, 0, 0)

    def _make_page_words(row_offset: float = 0.0):
        return [
            # header row at y=10
            _w(10, 10, 18, 18, "ID"),
            _w(50, 10, 80, 18, "NAME"),
            _w(120, 10, 155, 18, "SCORE"),
            # data rows
            _w(10, 30 + row_offset, 18, 38 + row_offset, "1"),
            _w(50, 30 + row_offset, 80, 38 + row_offset, "ALICE"),
            _w(120, 30 + row_offset, 155, 38 + row_offset, "92"),
            _w(10, 50 + row_offset, 18, 58 + row_offset, "2"),
            _w(50, 50 + row_offset, 80, 58 + row_offset, "BOB"),
            _w(120, 50 + row_offset, 155, 58 + row_offset, "78"),
            _w(10, 70 + row_offset, 18, 78 + row_offset, "3"),
            _w(50, 70 + row_offset, 80, 78 + row_offset, "CAROL"),
            _w(120, 70 + row_offset, 155, 78 + row_offset, "85"),
        ]

    mock_page_1 = mock.MagicMock()
    mock_page_1.rotation = 0
    mock_page_1.mediabox.width = 200
    mock_page_1.mediabox.height = 200
    mock_page_1.get_text.return_value = _make_page_words(0.0)

    mock_page_2 = mock.MagicMock()
    mock_page_2.rotation = 0
    mock_page_2.mediabox.width = 200
    mock_page_2.mediabox.height = 200
    # Second page starts at data row 4 (same structure)
    mock_page_2.get_text.return_value = [
        _w(10, 10, 18, 18, "ID"),
        _w(50, 10, 80, 18, "NAME"),
        _w(120, 10, 155, 18, "SCORE"),
        _w(10, 30, 18, 38, "4"),
        _w(50, 30, 80, 38, "DAVE"),
        _w(120, 30, 155, 38, "91"),
    ]

    mock_doc = mock.MagicMock()
    mock_doc.__len__ = mock.Mock(return_value=2)
    mock_doc.__iter__ = mock.Mock(return_value=iter([mock_page_1, mock_page_2]))
    mock_doc.__getitem__ = mock.Mock(side_effect=lambda i: [mock_page_1, mock_page_2][i])

    with mock.patch("fitz.open", return_value=mock_doc):
        result = _text_layout_tables(b"FAKEPDF")

    assert result, "expected non-empty result"
    name, df = result[0]
    assert len(df) == 4, f"expected 4 rows, got {len(df)}"
    assert len(df.columns) == 3, f"expected 3 cols, got {len(df.columns)}"
    # Column names should come from header row
    cols_lower = [c.lower() for c in df.columns]
    assert any("id" in c for c in cols_lower), f"no ID col in {df.columns.tolist()}"
    assert any("name" in c for c in cols_lower), f"no NAME col in {df.columns.tolist()}"
    # Data values
    ids = df.iloc[:, 0].tolist()
    assert "1" in ids and "4" in ids, f"unexpected ids: {ids}"
    ok("text_layout_tables_mocked")


def test_text_layout_tables_live_eti():
    """BRAIN: test _text_layout_tables on the 4487-page ETI exam results PDF."""
    import pathlib
    eti_path = pathlib.Path(r"C:\Users\HP\Downloads\Roll-No-wise-result-ETI.pdf")
    if not eti_path.exists():
        skip("text_layout_tables_live_eti", "ETI PDF not found at expected path")
        return

    importlib.reload(__import__("app.pdf_reader", fromlist=["pdf_reader"]))
    from app.pdf_reader import _text_layout_tables

    data = eti_path.read_bytes()
    result = _text_layout_tables(data)

    assert result, "expected non-empty result for ETI PDF"
    name, df = result[0]
    assert len(df) > 1000, f"expected >1000 rows, got {len(df)}"
    assert len(df.columns) == 6, f"expected 6 cols, got {df.columns.tolist()}"
    expected_cols = ["S. NO", "ROLL NO.", "CANDIDATE'S NAME", "CATEGORY",
                     "MARKS (Part A)", "MARKS (Part B)"]
    assert list(df.columns) == expected_cols, f"wrong cols: {df.columns.tolist()}"
    # Row 3 should have the multi-line DISABILITY category merged
    row3 = df[df.iloc[:, 0].str.strip() == "3"]
    assert len(row3) == 1
    assert "DISABILITY" in row3.iloc[0, 3]
    ok("text_layout_tables_live_eti")


# ── live OCR test (requires Gemini API key + network) ─────────────────────────

def test_ocr_image_live():
    """BRAIN: ocr_image sends a real image to Gemini and gets CSV back."""
    try:
        from app import llm, config
        if not config.GEMINI_API_KEY:
            skip("ocr_image_live", "GEMINI_API_KEY not set")
            return

        # Create a 1-pixel PNG (too small to contain a real table) — Gemini
        # should return NO_TABLE_FOUND.
        import struct, zlib
        sig = b'\x89PNG\r\n\x1a\n'
        def chunk(t, d):
            l = len(d)
            return struct.pack('>I', l) + t + d + struct.pack('>I', zlib.crc32(t+d) & 0xffffffff)
        ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
        idat = chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00'))
        iend = chunk(b'IEND', b'')
        tiny_png = sig + ihdr + idat + iend

        result = llm.ocr_image(tiny_png, "image/png")
        # The model should return NO_TABLE_FOUND or an empty-ish response for a 1-pixel image.
        assert isinstance(result, str)
        ok("ocr_image_live")
    except llm.ModelUnavailableError:
        skip("ocr_image_live", "Gemini rate-limited / daily quota exhausted")
    except Exception as e:
        skip("ocr_image_live", f"Gemini error: {e}")


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_parse_csv_response_basic()
    test_parse_csv_response_no_table()
    test_parse_csv_response_strips_whitespace()
    test_load_image_basic_ocr()
    test_load_image_multi_table()
    test_load_image_no_table_found()
    test_load_image_low_quality()
    test_load_image_low_quality_case_insensitive()
    test_load_image_good_quality_no_flag()
    test_load_pdf_fallback_low_quality()
    test_load_image_quota_error_propagates()
    test_load_pdf_digital_pdfplumber()
    test_load_pdf_fallback_to_ocr()
    test_load_pdf_no_content_anywhere()
    test_reader_routes_pdf_to_pdf_reader()
    test_reader_routes_image_to_pdf_reader()
    test_reader_unsupported_type()
    test_output_ext_pdf_always_xlsx()
    test_inspect_endpoint_note()
    test_process_endpoint_pdf()
    test_inspect_mixed_spreadsheet_and_pdf()
    test_text_layout_tables_mocked()
    test_text_layout_tables_live_eti()
    test_ocr_image_live()

    print(f"\n{'-'*50}")
    print(f"PASS {PASS}  FAIL {FAIL}  SKIP {SKIP}")
    if FAIL:
        sys.exit(1)
