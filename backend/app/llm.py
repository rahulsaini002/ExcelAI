"""The single wrapper around the LLM.

Everything that calls the model goes through `parse_instruction`. Keeping the
model behind one function means we can swap models, providers, or prompting
strategy later without touching the rest of the codebase. (This is why moving
from Claude to Gemini only changed this file, config, and requirements.)

The model's only job is translation: instruction + sheet structure -> a small,
structured "operation plan". It never sees or touches the file itself.
"""
from __future__ import annotations

import json
import random
import time
from typing import Literal, Optional

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from . import config

# HTTP codes worth retrying: rate limits and transient server overload.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class ModelUnavailableError(Exception):
    """Raised when the model is overloaded/rate-limited after retries.

    Distinct from other failures so the API can return a friendly "try again"
    message instead of a raw error dump.
    """


# A set of columns across tables that mean the same thing, to be unified on merge.
class ColumnGroup(BaseModel):
    name: str  # the unified output column name
    aliases: list[str]  # the column names (in any table) that mean this


# One condition inside a filter (e.g. "Revenue greater_than 5000").
class Condition(BaseModel):
    column: str
    operator: Literal[
        "equals", "not_equals", "greater_than", "less_than",
        "greater_or_equal", "less_or_equal", "between",
        "contains", "starts_with", "ends_with", "is_blank", "not_blank",
        "in", "not_in",
    ]
    value: Optional[str] = None
    value2: Optional[str] = None  # only used by "between"
    values: Optional[list[str]] = None  # used by "in" / "not_in" (a set of allowed values)


# Structured-output schema. Passing this to Gemini as the `response_schema`
# constrains the reply to valid JSON in exactly this shape (no markdown, no prose).
# Every action reuses a subset of these fields; unused ones stay null.


# One KPI on a generated dashboard sheet (its value is computed by trusted code).
class DashboardKpi(BaseModel):
    label: str
    agg: Literal["sum", "mean", "count", "count_distinct", "min", "max"]
    column: Optional[str] = None  # omit only for a plain row count
    format: Optional[Literal["number", "currency", "percent"]] = None


# One chart on a generated dashboard sheet.
class DashboardChartSpec(BaseModel):
    chart_type: Literal["bar", "line", "pie", "area"]
    x_column: str
    y_columns: list[str]
    title: Optional[str] = None


class Operation(BaseModel):
    action: Literal[
        "sort", "filter", "limit", "remove_duplicates", "fill_missing", "drop_missing",
        "drop_invalid", "trim", "flag_missing", "add_formula_column", "lookup",
        "aggregate", "find_replace", "rename_columns", "drop_columns", "select_columns",
        "format_cells", "merge", "combine_sheets", "chart", "dashboard",
        "unpivot", "pivot", "transpose",
        # Phase 3.5 — Predictive analytics
        "forecast", "what_if", "detect_anomalies",
    ]
    # Which table this operation acts on. Omit to use the current working table.
    table: Optional[str] = None
    # Generic column list — used by sort, remove_duplicates, drop_missing,
    # fill_missing, drop_columns, select_columns.
    columns: Optional[list[str]] = None
    # sort
    orders: Optional[list[Literal["asc", "desc"]]] = None
    # add_formula_column
    name: Optional[str] = None
    formula: Optional[str] = None
    overwrite: Optional[bool] = None
    # filter
    conditions: Optional[list[Condition]] = None
    combine: Optional[Literal["and", "or"]] = None
    # limit (keep the first/last N rows, e.g. "top 100" after a sort)
    count: Optional[int] = None
    from_end: Optional[bool] = None
    # fill_missing
    fill_value: Optional[str] = None
    fill_method: Optional[Literal["previous", "next"]] = None
    # drop_invalid
    data_type: Optional[Literal["number", "date"]] = None
    # lookup
    key_column: Optional[str] = None
    source_sheet: Optional[str] = None
    source_key_column: Optional[str] = None
    return_column: Optional[str] = None
    new_column: Optional[str] = None
    # aggregate
    agg_func: Optional[Literal["sum", "mean", "average", "count", "min", "max"]] = None
    agg_column: Optional[str] = None
    group_by: Optional[list[str]] = None
    count_value: Optional[str] = None
    # find_replace
    find: Optional[str] = None
    replace: Optional[str] = None
    column: Optional[str] = None
    match_case: Optional[bool] = None
    whole_cell: Optional[bool] = None
    # rename_columns (two parallel lists: rename_from[i] -> rename_to[i])
    rename_from: Optional[list[str]] = None
    rename_to: Optional[list[str]] = None
    # format_cells
    format_columns: Optional[list[str]] = None
    number_format: Optional[Literal["number", "currency", "percent", "date"]] = None
    decimals: Optional[int] = None
    currency_symbol: Optional[str] = None
    date_format: Optional[str] = None
    bold_header: Optional[bool] = None
    # merge (combine several tables into one by stacking rows)
    merge_tables: Optional[list[str]] = None
    new_table: Optional[str] = None
    # combine_sheets (put each table on its own sheet/tab in one workbook)
    sheet_tables: Optional[list[str]] = None
    # Synonym groups: unify differently-named columns that mean the same thing.
    column_groups: Optional[list[ColumnGroup]] = None
    # chart (add a real Excel chart to the output file)
    chart_type: Optional[Literal["bar", "line", "pie", "area"]] = None
    x_column: Optional[str] = None
    y_columns: Optional[list[str]] = None
    chart_title: Optional[str] = None
    # dashboard (assemble KPIs + charts + a summary onto one sheet)
    dashboard_title: Optional[str] = None
    kpis: Optional[list[DashboardKpi]] = None
    charts: Optional[list[DashboardChartSpec]] = None
    summary: Optional[str] = None
    # unpivot (wide → long)
    id_columns: Optional[list[str]] = None
    value_columns: Optional[list[str]] = None
    var_name: Optional[str] = None
    value_name: Optional[str] = None
    # pivot (long → wide); reuses agg_func
    index_columns: Optional[list[str]] = None
    pivot_column: Optional[str] = None
    value_column: Optional[str] = None
    # transpose
    header_column: Optional[str] = None
    # forecast (Phase 3.5)
    date_column: Optional[str] = None   # time-axis column; omit to use row index
    period_unit: Optional[Literal["day", "week", "month", "quarter", "year"]] = None
    # forecast periods reuses `count`; forecast value columns reuse `columns`
    # what_if (Phase 3.5) — reuses column, formula, name; scenario columns reuse columns
    # detect_anomalies (Phase 3.5)
    anomaly_method: Optional[Literal["zscore", "iqr"]] = None
    anomaly_threshold: Optional[float] = None  # z-score threshold or IQR multiplier


class StepSummary(BaseModel):
    label: str                   # "Filter rows where Region equals North"
    rationale: Optional[str] = None  # "Narrows data before aggregating"


class OperationPlan(BaseModel):
    operations: list[Operation]
    # Set when the instruction is too ambiguous to act on. When present, we ask
    # the user instead of guessing, and `operations` should be empty.
    clarification: Optional[str] = None
    # A direct, plain-text answer when the user asks ABOUT the data (e.g. "what
    # columns are there?") rather than requesting an operation. operations empty.
    reply: Optional[str] = None
    # A short 3-6 word English title summarizing the task, used to name the session
    # in the sidebar (e.g. "Sort sales by revenue").
    title: Optional[str] = None
    # A one-line plain-language restatement of what the plan does, shown to the user
    # BEFORE running (the "AI Translation" preview), e.g. "Filter Class equals 1,
    # then sort by Amount (high to low)".
    translation: Optional[str] = None
    # How confident you are in this interpretation, an integer 0-100.
    confidence: Optional[int] = None
    # Phase 3.4 — Agentic plan: one entry per operation (same order), giving
    # a human-readable label and an optional rationale for each step.
    steps: Optional[list[StepSummary]] = None
    # One sentence explaining the overall approach, e.g. "Filter first to reduce
    # the dataset, then aggregate for a focused summary." Omit for single-op plans.
    plan_rationale: Optional[str] = None


# --- Dashboard generation -------------------------------------------------------

# How to COMPUTE a widget's number(s) from the real data (filled in by trusted code,
# not the model — the model only chooses the agg + columns).
class WidgetMetric(BaseModel):
    agg: Literal["sum", "mean", "count", "count_distinct", "min", "max"]
    column: Optional[str] = None  # column to aggregate (omit for a plain row count)
    group_by: Optional[str] = None  # charts: aggregate per value of this column
    format: Optional[Literal["number", "currency", "percent"]] = None


# One widget on a generated dashboard. Mirrors the frontend Widget shape.
class DashboardWidget(BaseModel):
    type: Literal["kpi", "chart", "table"]
    title: str
    value: Optional[str] = None  # kpi headline, e.g. "₹4.82M"
    delta: Optional[str] = None  # kpi change, e.g. "+12%"
    chart_type: Optional[
        Literal[
            "bar", "line", "area", "pie", "scatter", "heatmap",
            "waterfall", "pareto", "treemap", "gauge",
        ]
    ] = None
    span: Optional[int] = 1  # 1 = half width, 2 = full width (frontend clamps)
    # How to compute this widget from the data (kpi + chart). The backend uses this to
    # fill in real numbers when a data file is provided.
    metric: Optional[WidgetMetric] = None


class DashboardSpec(BaseModel):
    widgets: list[DashboardWidget]
    title: Optional[str] = None  # short 3-6 word dashboard name


# Maps one existing report block (by its position) to how its number(s) are computed.
class ReportBlockMetric(BaseModel):
    index: int
    metric: Optional[WidgetMetric] = None


class ReportMetricsPlan(BaseModel):
    items: list[ReportBlockMetric]


SYSTEM_PROMPT = """\
You are the parsing brain of a conversational spreadsheet assistant. You convert a \
user's plain-language instruction into a small, structured operation plan. You do \
NOT execute anything — trusted code runs your plan.

The user may write in Hindi, English, Urdu, or any mix of them. Interpret \
code-switched instructions naturally (e.g. "Email ke basis pe duplicate rows hata do" \
means remove duplicate rows based on the Email column).

TABLES: The user may upload several files. Each sheet of each file is a "table" \
with a name (see "tables" and "primary_table" in the structure). Every operation \
has an optional "table" field naming which table it acts on; if you omit it, the \
operation runs on the current working table (the "primary_table" at the start, or \
the result of the previous operation). Use "table" when the user names a specific \
file/sheet. Use "merge" to combine tables, and "lookup" to pull values from \
another table.

You can ONLY use these operations:

1. sort
   - "columns": list of column names to sort by (in priority order)
   - "orders": list of "asc" or "desc", one per column (default "asc" if unsure)

2. filter — keep only rows matching one or more conditions
   - "conditions": a list, each with "column", "operator", and "value".
     Operators: equals, not_equals, greater_than, less_than, greater_or_equal, \
less_or_equal, between (uses "value" and "value2"), contains, starts_with, \
ends_with, is_blank, not_blank, in, not_in.
     For "in"/"not_in" give "values": a list of allowed values (e.g. Status in \
Completed/Pending → operator "in", values ["Completed","Pending"]).
   - "combine": "and" (default) or "or" when there are multiple conditions.

2b. limit — keep only the first N rows (use AFTER a sort for "top N" / "highest N").
   - "count": how many rows to keep (e.g. top 100 → count 100)
   - "from_end": true to keep the LAST N instead. Example: "sort by Revenue desc and \
keep the top 100" → [{sort Revenue desc}, {limit count 100}].

3. remove_duplicates
   - "columns": list of column names that define a duplicate. Omit to consider all columns.

4. fill_missing — fill blank cells
   - "columns": which columns (omit for all).
   - "fill_value": a FIXED value to put in blanks (e.g. "Unknown", 0), OR
   - "fill_method": "previous" to copy the value above down, or "next" to copy the value
     below up (use this for "fill with the previous value" / "carry forward"). Use either
     fill_value OR fill_method, not both.

5. drop_missing — remove rows that have blank cells
   - "columns": blanks in any of these drop the row (omit to check all columns).

5a. drop_invalid — remove rows whose value isn't a valid number (or date). Use for
    "remove rows with invalid revenue", "delete bad/garbage values in <column>".
   - "columns": the column(s) that must be valid.
   - "data_type": "number" (default) or "date". Blank cells are NOT dropped here
     (that's drop_missing) — this targets bad DATA like "ABC" in a number column.

5c. trim — clean whitespace in text cells (strip leading/trailing + collapse internal
    double spaces, like Excel TRIM). Use for "trim spaces", "clean extra spaces".
   - "columns": which text columns to trim (omit to trim ALL text columns).

5b. flag_missing — highlight blank cells (yellow) WITHOUT changing the data
   - "columns": which columns to check (omit for all). Use this when the user wants
     to "highlight"/"mark"/"show" blanks rather than fill or remove them.

6. add_formula_column
   - "name": the new column's name
   - "formula": a per-row expression over existing columns, each column name wrapped in \
curly braces, e.g. "{Qty} * {Price}". Operators: + - * / ( ). You may also use Excel \
functions IF, SUM, AVERAGE, MIN, MAX, ROUND, ABS and comparisons (>, <, >=, <=, =, <>), \
e.g. "IF({Qty} > 10, {Price} * 0.9, {Price})" or "ROUND({A} / {B}, 2)". SUM/AVERAGE here \
combine the listed columns ROW BY ROW (for a whole-column total use aggregate, not this).
   - If the new column name ALREADY EXISTS, ask the user whether to overwrite it or use a \
new name (clarification). Only set "overwrite": true if they confirm overwriting.

7. lookup — bring a value from ANOTHER table/sheet/file (like VLOOKUP/XLOOKUP)
   - "key_column": the matching column in the current (or "table") table
   - "source_sheet": the name of the table to look in (any name from "tables")
   - "source_key_column": the matching column in that table
   - "return_column": the column to bring back
   - "new_column": optional name for the new column (defaults to the return column)

8. aggregate — totals/averages/counts, optionally grouped
   - "agg_func": sum, average, count, min, or max
   - "agg_column": the column to aggregate (not needed for a plain count)
   - "group_by": optional list of columns to group by (produces a summary table)
   - "count_value": with agg_func "count", count only cells in agg_column equal to
     this value (e.g. count how many responses were "Yes": agg_func=count,
     agg_column=Response, count_value=Yes). Matching ignores case and extra spaces.

9. find_replace — replace text
   - "find", "replace", optional "column" (omit for whole sheet), \
"match_case" (true/false), "whole_cell" (true/false)

10. rename_columns — "rename_from": [old names], "rename_to": [new names] (same length, in order)

11. drop_columns — "columns": columns to remove

12. select_columns — "columns": the only columns to keep

13. format_cells — change how values look (does not change the data)
   - "format_columns": columns to format, "number_format": number/currency/percent/date,
     optional "decimals", "currency_symbol", and "bold_header" (true to bold the header row).
   - "date_format": with number_format "date", the desired style, e.g. "dd-mm-yyyy"
     (default), "yyyy-mm-dd", "mm/dd/yyyy", or "dd-mmm-yyyy" (09-Jun-2026).

14. merge — combine several tables into ONE table. If the tables SHARE column names
    their rows are STACKED (one big list; missing columns left blank). If the tables
    have NO columns in common (completely different columns), they are placed SIDE BY
    SIDE, aligned by row position (row 1 with row 1, etc.) — so after merging you can
    compute ACROSS the two files (e.g. multiply a column from file A by a column from
    file B). Use merge when the user wants the data combined.
   - "merge_tables": the list of table names to combine
   - "new_table": optional name for the combined table (defaults to "merged")
   - "column_groups": IMPORTANT for files with inconsistent headers. When columns in
     different tables MEAN THE SAME THING but are named differently (e.g. "Customer_ID",
     "client_id", "cust_no"), unify them: give a list where each item is
     {"name": "<unified name>", "aliases": ["<each differently-named column>", ...]}.
     Only group columns that truly mean the same thing; leave genuinely different
     columns out. Columns differing only in case/spacing are unified automatically.

15. combine_sheets — combine several files/tables into ONE Excel file with EACH on
    its OWN separate SHEET/TAB (the data stays separate, not stacked). Use this when
    the user says "in different sheets/tabs", "separate sheets", "each file on its
    own tab", or similar. This is DIFFERENT from merge (which stacks into one table).
   - "sheet_tables": the list of table names to put on separate sheets
   - "new_table": optional name for the output file (defaults to "combined")

16. chart — add a REAL chart to the output file (it does NOT change the data). Use for
    "make/draw/plot a chart/graph" requests, e.g. "bar chart of revenue by month". Pick
    the type that fits: bar = compare categories/rankings; line = a trend over time;
    pie = share of a whole (one value column); area = cumulative trend.
   - "chart_type": bar, line, pie, or area
   - "x_column": the label/category column (x-axis), e.g. Month
   - "y_columns": one or more NUMERIC value columns to plot (y-axis), e.g. Revenue
   - "chart_title": optional title
    If the file has MANY rows per category (e.g. "revenue by month" but several rows per
    month), aggregate FIRST then chart — output an aggregate step, then a chart step.
    Only bar/line/pie/area are supported; for any other chart type, decline via "reply".

17. dashboard — assemble a one-page DASHBOARD (KPIs + charts + a short written summary)
    onto a new sheet. Use for "make a dashboard", "one-page summary", "how's the shop
    doing" requests. Trusted code computes the KPI numbers and lays everything out.
   - "dashboard_title": optional title for the sheet
   - "kpis": a list of headline metrics, each {"label": e.g. "Total Revenue", "agg":
     sum/mean/count/count_distinct/min/max, "column": the column (omit only for a plain
     count), "format": currency/percent/number}
   - "charts": a list, each {"chart_type": bar/line/pie/area, "x_column", "y_columns":
     [numeric column(s)], "title"}
   - "summary": a short (1-3 sentence) plain-language summary of the data
    If a chart needs aggregated data (e.g. revenue by month from many rows), add an
    aggregate step BEFORE the dashboard so the chart's columns exist.

18. unpivot — turn WIDE data into tidy LONG rows (e.g. monthly columns Jan/Feb/Mar →
    rows with a Month column + a value column). Use for "unpivot", "melt", "columns to
    rows", "make it long/tidy".
   - "id_columns": columns to KEEP as-is (e.g. Region)
   - "value_columns": the columns to turn into rows (e.g. Jan, Feb, Mar). Omit to use
     all columns except the id_columns.
   - "var_name": name for the new column holding the old column names (e.g. Month)
   - "value_name": name for the new values column (e.g. Sales)

19. pivot — summarize LONG data into a WIDE grid (e.g. rows of Region/Month/Sales → a
    Region × Month grid of summed Sales). Use for "pivot", "pivot table", "rows to
    columns", "summary grid".
   - "index_columns": the row groups (e.g. Region)
   - "pivot_column": the column whose values become new columns (e.g. Month)
   - "value_column": the column to aggregate (e.g. Sales)
   - "agg_func": sum (default), mean, count, min, or max

20. transpose — flip the whole table: rows become columns and columns become rows.
   - "header_column": optional — the column whose values become the new headers.

21. forecast — extrapolate future values using a linear trend (requires ≥ 5 data points).
    Use for "predict next N months", "forecast sales", "what will revenue be next quarter".
   - "columns": the numeric column(s) to forecast (required)
   - "date_column": the date/time column to use as the time axis (optional; omit to use
     row order). Use the actual column name from the structure.
   - "count": how many future periods to forecast (default 3)
   - "period_unit": "day", "week", "month", "quarter", or "year" — the unit for the
     generated future period labels (infer from the date column or the user's request)
   Output adds forecast rows with "{col}_Forecast", "{col}_Lower95", "{col}_Upper95" columns.
   NEVER use this for less than 5 rows — decline via "reply" if the data is too small.

22. what_if — apply a hypothetical change to one column and show the impact on dependent
    columns. Use for "what if price increases by 10%?", "simulate a 20% discount", "what
    if I double marketing spend?".
   - "column": the column whose value changes hypothetically (e.g. "Price")
   - "formula": the expression for the new value of that column, using {column} for the
     original value — e.g. "{Price} * 1.1" (10% increase), "{Cost} + 50"
   - "name": label for the scenario column, e.g. "Price (Scenario)"
   - "columns": optional list of dependent columns to recompute in the scenario
     (each needs a matching formula in a follow-up add_formula_column if complex);
     omit for a simple single-column scenario.
   Output adds scenario columns showing before/after; a note summarises the impact.

23. detect_anomalies — flag rows whose numeric values are unusually high or low.
    Use for "find anomalies", "highlight outliers", "flag unusual values", "audit for
    inconsistencies" in numeric columns.
   - "columns": the numeric column(s) to check for anomalies (required)
   - "anomaly_method": "zscore" (default, flags |z| > threshold) or "iqr" (flags
     values outside Q1 − multiplier×IQR … Q3 + multiplier×IQR)
   - "anomaly_threshold": z-score cutoff (default 3.0) or IQR multiplier (default 1.5)
   Output adds "Is_Anomaly" (True/False) and "Anomaly_Note" (which column + direction)
   columns. Rows that are perfectly normal show False / blank.
   NEVER use this for less than 5 rows — decline via "reply" if the data is too small.

Rules:
- Use the EXACT column and table names given in the structure. Match the user's intent \
to real columns/tables even if they describe them loosely.
- You may output multiple operations; they run in order. If the user gives SEVERAL \
instructions at once — on separate lines, numbered (1. 2. 3.), or joined by "then"/"and" \
(e.g. "Filter Amount > 500 / Sort Amount descending / Create Tax column") — output ONE \
operation per instruction, in that order. PREFER TO ACT: if a request \
maps to a reasonable sequence of operations, DO IT instead of asking. Chain steps when \
needed — e.g. "merge both files and multiply Roll No. by Quantity" → [merge the two \
tables, then add_formula_column "{Roll No.} * {Quantity}"]. Only clarify as a LAST \
resort when you genuinely cannot tell which column/table/value is meant.
- AMBIGUOUS request (you truly can't tell which column/table/value is meant, AND can't \
pick a sensible default): set "clarification" to ONE short question (in the user's \
language) and leave "operations" empty. Do NOT ask about things you can reasonably \
infer (e.g. that two different-column files should be merged side by side).
- UNSUPPORTED request (something outside the operations above, e.g. predict/forecast \
sales, send an email): do NOT clarify and do NOT invent a \
result. Put a friendly explanation in the "reply" field, like: "I can't do that yet — \
but I can sort, filter, remove duplicates, add formula columns, look up, aggregate, \
find & replace, rename/drop columns, merge, combine sheets, chart, build a dashboard, \
or reshape (pivot/unpivot/transpose)." Leave "operations" empty.
- NON-EXISTENT column/table: if the user names a column or table that isn't in the \
structure (even loosely), do NOT invent it. Ask in "clarification" and list the real \
column/table names so they can pick (e.g. "I don't see a 'Profit' column — did you mean \
Revenue or Cost?").
- fill_missing only supports a FIXED value (the "fill_value"). If the user asks to fill \
blanks with a STATISTIC (average/mean/median/mode/interpolation/regression), do NOT do \
it — put a friendly decline in "reply": "Filling blanks with an average isn't supported \
yet — try a fixed value like 0 or 'Unknown'." and leave operations empty.
- When your clarification asks the user to CHOOSE a column (e.g. which column to sort \
by), ALWAYS list the available column names from the structure in the question, so the \
user can pick. Example: "Which column should I sort by? Available: Name, Roll No., product".
- If the user ASKS ABOUT the data instead of requesting an action (e.g. "what columns \
are there?", "name the columns", "how many rows?"), do NOT treat it as an operation: \
put a direct answer in the "reply" field and leave "operations"/"clarification" empty. \
When there are MULTIPLE tables, answer for EVERY table, grouped by table name. \
Example: "Testing 1: Name, Roll No.  •  testing: product, Quantity". If the user names \
a specific table, answer just that one.
- CONVERSATION CONTEXT: you may be given "Recent conversation". A new instruction can \
be a fragment that DEPENDS on a previous one — combine them to get the full intent. \
E.g. previous "name the columns", new "in the testing table" → answer the columns of \
the 'testing' table. Previous "remove duplicates", new "now sort by date" → sort. If \
the conversation shows YOU asked a clarifying question and this message is the user's \
ANSWER (e.g. you asked "did you mean Amount?" and they reply "Amount" or "yes Amount"), \
carry out the ORIGINAL request using that answer. BUT if this message is clearly a NEW, \
self-contained instruction (it names its own action/column), treat it on its OWN — do \
NOT re-apply an earlier unfinished request or re-ask its question. Only fall back to a \
clarification if it's still unclear after using the conversation.
- TEAM GLOSSARY: you may be given a "Team glossary" with the team's own definitions \
(e.g. ARR = {MRR} * 12) and formatting preferences. When the user uses a defined term, \
APPLY that meaning consistently — e.g. "add ARR" with ARR defined as {MRR} * 12 means \
add_formula_column name "ARR" formula "{MRR} * 12". Honour stated formatting preferences.
- TITLE: whenever you output operations, ALSO set "title" to a short 3-6 word English \
title that names the task, for the session list (e.g. "Sort sales by revenue", "Remove \
duplicate emails", "Add profit column"). Keep it concise; no quotes, no trailing period.
- TRANSLATION + CONFIDENCE: whenever you output operations, ALSO set "translation" to a \
ONE-LINE plain-language restatement (in English) of what you will do, so the user can \
confirm before it runs — e.g. "Filter rows where Class equals 1, then sort by Amount \
(high to low)". And set "confidence" to an integer 0-100 for how sure you are of this \
interpretation: high (90+) when the columns and intent are unambiguous, lower when you \
had to guess which column or value was meant.
- STEPS: whenever you output operations, ALSO populate "steps" — a parallel list, one \
entry per operation in the SAME ORDER. Each entry has: "label" — a plain-English \
phrase (under 12 words) describing what that step does, e.g. "Filter rows where Region \
equals North", "Sort by Revenue, highest first", "Remove duplicate rows on Email"; and \
"rationale" — one sentence (under 15 words) explaining WHY this step is needed in the \
plan, e.g. "Narrows to the target region before aggregating", "Puts the most important \
results first". Omit "rationale" only when the reason is entirely obvious from the label.
- PLAN_RATIONALE: when you output 2 or more operations, also set "plan_rationale" to \
one sentence explaining the OVERALL approach, e.g. "Filter first to reduce the dataset, \
then aggregate for a focused summary." Omit for single-operation plans.
- Otherwise leave "clarification" and "reply" empty/null.
"""


def _client() -> genai.Client:
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy backend/.env.example to "
            "backend/.env and add your key."
        )
    return genai.Client(api_key=config.GEMINI_API_KEY)


def parse_instruction(instruction: str, structure: dict, history: str = "") -> dict:
    """Translate a plain-language instruction into an operation plan dict.

    `history` is recent conversation text (and, prepended by the caller, the team's
    learned glossary + preferences — Phase 3.12) so a follow-up instruction can be
    interpreted in context. Returns a dict shaped like OperationPlan.
    """
    parts = [
        "Available tables and their structure:\n"
        + json.dumps(structure, ensure_ascii=False, indent=2)
    ]
    if history.strip():
        parts.append(
            "Recent conversation (the new instruction may depend on it):\n" + history.strip()
        )
    parts.append(f"Instruction:\n{instruction}")
    user_content = "\n\n".join(parts)

    gen_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0,
        response_mime_type="application/json",
        response_schema=OperationPlan,
    )
    response = _generate_with_retry(user_content, gen_config)

    # `response.parsed` is an OperationPlan instance when the schema is honored;
    # fall back to parsing the raw JSON text if needed.
    plan = response.parsed
    if isinstance(plan, OperationPlan):
        return plan.model_dump()
    return OperationPlan.model_validate_json(response.text).model_dump()


DASHBOARD_SYSTEM_PROMPT = """\
You design a small analytics dashboard for a conversational spreadsheet app. Given the \
user's request and the COLUMNS available in their data, return a set of 4-6 dashboard \
widgets that best answer the request.

Widget types:
- "kpi": a single headline number. Set "title" (e.g. "Total Revenue"), a short "value" \
(e.g. "₹4.82M", "18,204", "92%") and an optional "delta" (e.g. "+12%", "-3%"). Base the \
metric on a REAL column when one fits; the value/delta are illustrative sample figures.
- "chart": set "title" and a "chart_type" from: bar, line, area, pie, scatter, heatmap, \
waterfall, pareto, treemap, gauge. Pick the type that suits the data (trend over time -> \
line; share of a whole -> pie; ranking -> bar/pareto; correlation -> scatter; \
part-to-whole hierarchy -> treemap; a single rate -> gauge).
- "table": a detail breakdown. Set "title".

METRICS — for every "kpi" and "chart" widget, ALSO set "metric" describing HOW to compute \
it from the REAL columns (trusted code computes the actual numbers):
- "agg": one of sum, mean, count, count_distinct, min, max.
- "column": the column to aggregate. Omit it only for a plain row count (agg "count").
- "group_by": CHARTS ONLY — the column to group rows by, so the chart shows the aggregate \
per group (e.g. a "Revenue by Region" bar chart -> agg sum, column Revenue, group_by Region).
- "format": how to show a KPI — "currency" (money columns), "percent", or "number".
Examples: "Total Revenue" -> {agg: sum, column: Revenue, format: currency}; "Orders" -> \
{agg: count, format: number}; "Avg Order Value" -> {agg: mean, column: Amount, format: \
currency}; "Customers" -> {agg: count_distinct, column: Customer}. Only use columns that \
EXIST in the structure; pick numeric columns for sum/mean/min/max.

Rules:
- Use the user's ACTUAL column names in titles where it makes sense (e.g. if there is a \
"Region" column, "Revenue by Region"). If no columns are given, design a sensible generic \
dashboard for the request and you may omit "metric".
- Start with 2-3 KPI cards, then 2-3 charts, optionally 1 table.
- "span" is 1 (half width) or 2 (full width). Use 2 for a primary trend chart or a wide \
table; 1 otherwise.
- Also set the dashboard "title": a short 3-6 word name.
- Return ONLY the structured fields. Do not invent spreadsheet operations or prose.
"""


def generate_dashboard(prompt: str, structure: dict) -> dict:
    """Design a dashboard (a set of widgets) from a prompt + the data's columns.

    Returns a dict shaped like DashboardSpec. Raises ModelUnavailableError when the
    model is rate-limited, so the caller can fall back to a local template.
    """
    if structure:
        context = "Columns available in the user's data:\n" + json.dumps(
            structure, ensure_ascii=False, indent=2
        )
    else:
        context = "No specific columns were provided; design a sensible generic dashboard."
    user_content = f"{context}\n\nDashboard request:\n{prompt}"

    gen_config = types.GenerateContentConfig(
        system_instruction=DASHBOARD_SYSTEM_PROMPT,
        temperature=0.4,
        response_mime_type="application/json",
        response_schema=DashboardSpec,
    )
    response = _generate_with_retry(user_content, gen_config)

    spec = response.parsed
    if isinstance(spec, DashboardSpec):
        return spec.model_dump()
    return DashboardSpec.model_validate_json(response.text).model_dump()


REPORT_METRICS_SYSTEM_PROMPT = """\
You map each block of a business report to a metric computed from the user's data columns.
For each KPI, CHART, or TABLE block, return its "index" and a "metric":
- "agg": sum, mean, count, count_distinct, min, max.
- "column": the column to aggregate. Omit only for a plain row count (agg "count").
- "group_by": for CHART and TABLE blocks, the column to group rows by (e.g. Region).
- "format": currency (money), percent, or number — how to show a KPI.
Base each metric on the block's TITLE: e.g. a "Revenue by Region" table/chart -> agg sum, \
column Revenue, group_by Region; "Total Orders" KPI -> agg count; "Avg Deal Size" -> agg \
mean, column Amount, format currency. Use ONLY columns that exist in the structure; pick \
numeric columns for sum/mean/min/max. For NARRATIVE blocks, omit them (no metric). Return \
one item per block that should show a real number.
"""


def assign_report_metrics(blocks: list[dict], structure: dict) -> dict:
    """Map each report block (by index) to a metric over the real columns. Returns a
    dict shaped like ReportMetricsPlan. Raises ModelUnavailableError if rate-limited."""
    summary = [
        {
            "index": i,
            "type": b.get("type"),
            "title": b.get("title"),
            "chart_type": b.get("chartType"),
        }
        for i, b in enumerate(blocks)
    ]
    user_content = (
        "Report blocks:\n"
        + json.dumps(summary, ensure_ascii=False, indent=2)
        + "\n\nData columns:\n"
        + json.dumps(structure, ensure_ascii=False, indent=2)
    )
    gen_config = types.GenerateContentConfig(
        system_instruction=REPORT_METRICS_SYSTEM_PROMPT,
        temperature=0,
        response_mime_type="application/json",
        response_schema=ReportMetricsPlan,
    )
    response = _generate_with_retry(user_content, gen_config)

    plan = response.parsed
    if isinstance(plan, ReportMetricsPlan):
        return plan.model_dump()
    return ReportMetricsPlan.model_validate_json(response.text).model_dump()


_OCR_PROMPT = """\
Extract the table(s) from this image as CSV.

Rules:
- The FIRST ROW of each table must be the header row with column names.
- Use commas to separate values. Quote any cell containing a comma with double quotes.
- Strip leading and trailing whitespace from every cell.
- If there are MULTIPLE separate tables, separate them with a line that reads exactly:
  --- Table N ---  (where N is 1, 2, 3 …)
- If NO table is visible (only text paragraphs, charts, logos, or too blurry):
  output exactly: NO_TABLE_FOUND
- Output ONLY the CSV data (and any separator lines). No prose, no markdown.

Example — one table:
Name,Score,Grade
Alice,92,A
Bob,78,B+

Example — two tables:
--- Table 1 ---
Name,Score
--- Table 2 ---
Month,Revenue
Jan,5200
"""


def ocr_image(image_bytes: bytes, mime_type: str) -> str:
    """Use Gemini Vision to extract table data from an image.

    Returns a CSV string (possibly multi-table with '--- Table N ---' separators)
    or the sentinel string 'NO_TABLE_FOUND' when no table is visible.
    Raises ModelUnavailableError when the model is rate-limited after retries.
    """
    client = _client()
    parts = [
        types.Part.from_text(text=_OCR_PROMPT),
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
    ]
    gen_config = types.GenerateContentConfig(temperature=0)
    last_exc: errors.APIError | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.models.generate_content(
                model=config.MODEL,
                contents=parts,
                config=gen_config,
            )
            return response.text or "NO_TABLE_FOUND"
        except errors.APIError as exc:
            if getattr(exc, "code", None) not in _RETRYABLE_CODES:
                raise
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2**attempt + random.uniform(0, 0.5))

    raise ModelUnavailableError(
        "The AI service is rate-limited right now (the free tier has a usage cap). "
        "This isn't a problem with your file or instruction — please wait a bit and try "
        "again. If it keeps happening, the daily free limit may be used up."
    ) from last_exc


def _generate_with_retry(user_content: str, gen_config: types.GenerateContentConfig):
    """Call Gemini, retrying transient overload/rate-limit errors with backoff."""
    client = _client()
    last_exc: errors.APIError | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            return client.models.generate_content(
                model=config.MODEL,
                contents=user_content,
                config=gen_config,
            )
        except errors.APIError as exc:
            if getattr(exc, "code", None) not in _RETRYABLE_CODES:
                raise  # non-transient (bad request, auth, etc.) — surface it
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                # Exponential backoff with jitter: ~1s, 2s, 4s.
                time.sleep(2**attempt + random.uniform(0, 0.5))

    raise ModelUnavailableError(
        "The AI service is rate-limited right now (the free tier has a usage cap). "
        "This isn't a problem with your file or instruction — please wait a bit and try "
        "again. If it keeps happening, the daily free limit may be used up."
    ) from last_exc
