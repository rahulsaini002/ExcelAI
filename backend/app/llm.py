"""The single wrapper around the LLM.

Everything that calls the model goes through `parse_instruction`. Keeping the
model behind one function means we can swap models, providers, or prompting
strategy later without touching the rest of the codebase. (This is why moving
from Claude to Gemini only changed this file, config, and requirements.)

The model's only job is translation: instruction + sheet structure -> a small,
structured "operation plan". It never sees or touches the file itself.
"""
from __future__ import annotations

import hashlib
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
# NOTE (Phase 2.1): every enum below except Operation.action is a plain str, not a
# Literal. Gemini compiles the response schema into a serving automaton with a hard
# state limit — the full schema with these as enums gets "constraint has too many
# states" (400) on EVERY live parse. The prompt documents the allowed values and the
# trusted Hands validate them with friendly errors, so nothing fails silently.
class Condition(BaseModel):
    column: str
    operator: str  # equals|not_equals|greater_than|less_than|greater_or_equal|
    #                less_or_equal|between|contains|starts_with|ends_with|is_blank|
    #                not_blank|in|not_in
    value: Optional[str] = None
    value2: Optional[str] = None  # only used by "between"
    values: Optional[list[str]] = None  # used by "in" / "not_in" (a set of allowed values)


# Structured-output schema. Passing this to Gemini as the `response_schema`
# constrains the reply to valid JSON in exactly this shape (no markdown, no prose).
# Every action reuses a subset of these fields; unused ones stay null.


# One KPI on a generated dashboard sheet (its value is computed by trusted code).
class DashboardKpi(BaseModel):
    label: str
    agg: str  # sum | mean | count | count_distinct | min | max
    column: Optional[str] = None  # omit only for a plain row count
    format: Optional[str] = None  # number | currency | percent


# One chart on a generated dashboard sheet. (Bubble size = y_columns[1], not a field.)
class DashboardChartSpec(BaseModel):
    chart_type: str  # bar|line|area|pie|doughnut|radar|stock|scatter|bubble
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
        # Phase 1.2 — Conditional formatting
        "conditional_format",
        # Phase 1.3 — Split / merge / fill-by-example
        "split_column", "merge_columns", "fill_by_example",
        # Phase 1.4 — Sheet layout polish
        "layout_format",
        # Phase 1.5 — Data validation / dropdowns
        "data_validation",
        # Phase 1.6 — Sheet management
        "sheet_op",
        # Phase 1.7 — Native Excel Tables
        "excel_table",
        # Phase 1.8 — Goal Seek (inverse what-if)
        "goal_seek",
        # Phase 1.9 — Explain changes as cell notes
        "explain_changes",
        # Phase 1.10 — Fill series + named ranges
        "fill_series", "name_range",
        # Phase 2.1 — Pivot summaries
        "pivot_summary",
        # Phase 2.3 — Statistical analysis
        "statistics",
    ]
    # Which table this operation acts on. Omit to use the current working table.
    table: Optional[str] = None
    # Generic column list — used by sort, remove_duplicates, drop_missing,
    # fill_missing, drop_columns, select_columns.
    columns: Optional[list[str]] = None
    # sort
    orders: Optional[list[str]] = None  # asc | desc, one per column
    # add_formula_column
    name: Optional[str] = None
    formula: Optional[str] = None
    overwrite: Optional[bool] = None
    # filter
    conditions: Optional[list[Condition]] = None
    combine: Optional[str] = None  # and | or
    # limit (keep the first/last N rows, e.g. "top 100" after a sort)
    count: Optional[int] = None
    from_end: Optional[bool] = None
    # fill_missing
    fill_value: Optional[str] = None
    fill_method: Optional[str] = None  # previous | next
    # drop_invalid
    data_type: Optional[str] = None  # number | date
    # lookup
    key_column: Optional[str] = None
    source_sheet: Optional[str] = None
    source_key_column: Optional[str] = None
    return_column: Optional[str] = None
    new_column: Optional[str] = None
    # aggregate. agg_func is a plain str (sum|mean|average|count|min|max) to keep the
    # Gemini response schema under its serving-size limit — the executor validates it
    # and answers with a friendly error on anything else.
    agg_func: Optional[str] = None
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
    number_format: Optional[str] = None  # number|currency|percent|date|indian_currency
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
    # chart (add a real Excel chart to the output file). NOTE: bubble size is expressed
    # as y_columns=[y, size] (NOT a dedicated field) — the Operation model is AT Gemini's
    # structured-output serving limit ("too much branching"), so every optional field
    # counts; see the schema-serving-cliff note before adding any.
    chart_type: Optional[str] = None  # bar|line|area|pie|doughnut|radar|stock|scatter|bubble
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
    period_unit: Optional[str] = None   # day | week | month | quarter | year
    # forecast periods reuses `count`; forecast value columns reuse `columns`
    # what_if (Phase 3.5) — reuses column, formula, name; scenario columns reuse columns
    # detect_anomalies (Phase 3.5)
    anomaly_method: Optional[str] = None  # zscore | iqr
    anomaly_threshold: Optional[float] = None  # z-score threshold or IQR multiplier
    # conditional_format (Phase 1.2) — value/value2 are the rule bounds; `columns`,
    # `count` (top/bottom N), and `formula` (formula rules) are reused from above.
    rule_type: Optional[str] = None
    value: Optional[float | str] = None
    value2: Optional[float | str] = None
    color: Optional[str] = None
    icons: Optional[int] = None
    percent: Optional[bool] = None
    # split_column / merge_columns / fill_by_example (Phase 1.3). `column` is reused
    # (the source), `columns` (merge sources), `name` (the new/merged column).
    new_columns: Optional[list[str]] = None
    delimiter: Optional[str] = None
    widths: Optional[list[int]] = None
    pattern: Optional[str] = None
    separator: Optional[str] = None
    keep_original: Optional[bool] = None
    examples: Optional[list["FillExample"]] = None  # fill_by_example input/output pairs
    # layout_format (Phase 1.4)
    freeze: Optional[str] = None        # "header" | "first_column" | "both" | "B3"
    autofit: Optional[bool] = None
    borders: Optional[str] = None       # all | outline
    title: Optional[str] = None         # merged title row above the headers
    merge_range: Optional[str] = None   # e.g. "A10:D10" (blank areas only)
    header_fill: Optional[str] = None   # named color for the header row
    # NOTE (Phase 2.7): print/page setup runs via layout_format's `print_setup` free-text
    # field in the HANDS, but that field is NOT in this schema — adding ANY field here
    # (even one str) pushes the Operation model over Gemini's serving limit ("too much
    # branching", 400 on every parse — VERIFIED live). So print setup is available on
    # DIRECT /execute plans only, until a real schema-headroom refactor lands; the Brain
    # can't route it yet. Do NOT re-add a field here without a live parse('sort…') check.
    # data_validation (Phase 1.5) — `columns` and `formula` (custom rules) reused.
    validation_type: Optional[str] = None  # list|whole|decimal|date|text_length|custom
    allowed_values: Optional[list[str]] = None
    min_value: Optional[float | str] = None   # number, or ISO date for date rules
    max_value: Optional[float | str] = None
    input_message: Optional[str] = None
    error_message: Optional[str] = None
    allow_blank: Optional[bool] = None
    # sheet_op (Phase 1.6)
    sheet_action: Optional[str] = None  # new_sheet|rename|delete|copy|move|tab_color|hide|unhide
    sheet_name: Optional[str] = None   # which sheet (defaults to the working sheet)
    new_name: Optional[str] = None     # for new_sheet / rename / copy
    position: Optional[str] = None     # for move: "first", "last", or a number
    tab_color: Optional[str] = None    # named color for tab_color
    # excel_table (Phase 1.7)
    table_style: Optional[str] = None            # blue/green/orange/grey/yellow/dark
    totals: Optional[bool] = None                # auto totals row (numeric cols summed)
    totals_spec: Optional[list["TotalSpec"]] = None  # explicit per-column aggregations
    table_name: Optional[str] = None
    # goal_seek (Phase 1.8) — `formula` reused, with {var} as the single unknown
    target: Optional[float] = None
    variable_name: Optional[str] = None
    # fill_series / name_range (Phase 1.10) — `name`, `count`, `column` reused
    series_type: Optional[str] = None  # numbers | months | weekdays | dates
    start: Optional[float] = None
    step: Optional[float] = None
    end: Optional[float] = None
    start_date: Optional[str] = None   # ISO date for date series
    every: Optional[str] = None        # daily | weekly | monthly | a weekday name
    range_name: Optional[str] = None
    # pivot_summary (Phase 2.1) — group_by (row fields), pivot_column (optional column
    # field), value_column, and agg_func are reused from above. percent_of/date_bucket
    # are plain str, NOT Literal: two more enums pushed Gemini's compiled response
    # schema over its serving limit ("constraint has too many states" 400 on EVERY
    # live parse). The Hands validate the values with friendly errors anyway.
    # pivot_summary (Phase 2.1) — group_by (row fields), pivot_column (optional column
    # field), value_column, and agg_func are reused from above. percent_of/date_bucket
    # are plain str, NOT Literal: the compiled Gemini response schema sits AT the
    # serving-size cliff ("constraint has too many states" 400 on every live parse) —
    # adding enums tips it over. The Hands validate the values with friendly errors.
    show_totals: Optional[bool] = None
    percent_of: Optional[str] = None   # grand | row | column
    date_bucket: Optional[str] = None  # day | week | month | quarter | year
    live: Optional[bool] = None        # true -> live GROUPBY/PIVOTBY formula (M365)
    # statistics (Phase 2.3) — reuses columns / x_column / y_columns / value_column /
    # group_by / count (moving-average window).
    stat_method: Optional[str] = None  # describe|correlation|regression|moving_average|t_test


class TotalSpec(BaseModel):
    """One totals-row entry for excel_table: which column, and how to total it.
    (Typed — untyped dicts in the response schema cause decoder loops.)"""
    column: str
    agg: str  # sum | average | count | min | max


class FillExample(BaseModel):
    """One fill-by-example pair: the input value the user pointed at, and the output
    they showed. A TYPED schema keeps the structured-output decoder on rails — an
    untyped dict here sent it into a repetition loop (21k lines) on live prompts."""
    input: str
    output: str


class StepSummary(BaseModel):
    label: str                   # "Filter rows where Region equals North"
    rationale: Optional[str] = None  # "Narrows data before aggregating"


Operation.model_rebuild()  # resolve the FillExample forward reference


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

MANDATORY CHECK, BEFORE YOU OUTPUT ANY OPERATION — applies in EVERY language, no \
exceptions: for each column the user refers to, find every column in the structure \
that matches their word EQUALLY well. If MORE THAN ONE does, you MUST NOT pick one. \
Set "clarification" naming those columns and leave "operations" empty. A confident \
guess on a tie is the worst answer you can give — worse than asking, worse than \
declining — because the user cannot tell you guessed. This is a check on the MEANING \
of the request, so the language it was written in is irrelevant: a Hinglish "Total ke \
hisaab se filter karo" on a sheet holding Total_Q1 and Total_Q2 is exactly as much of \
a tie as the English "filter by total", and BOTH must ask. Only proceed when ONE \
column is the clear best match.

TABLES: The user may upload several files. Each sheet of each file is a "table" \
with a name (see "tables" and "primary_table" in the structure). Every operation \
has an optional "table" field naming which table it acts on; if you omit it, the \
operation runs on the current working table (the "primary_table" at the start, or \
the result of the previous operation). Use "table" when the user names a specific \
file/sheet. Use "merge" to combine tables, and "lookup" to pull values from \
another table.

RELATIONSHIPS: when several tables are present the structure may carry a \
"relationships" list — the foreign-key links between them, detected by checking that \
the values genuinely line up (each entry gives from_table/from_column -> \
to_table/to_column and the coverage that was measured). Use these as the JOIN KEYS for \
"lookup": if the user asks for a field that lives in a related table ("bring in each \
sale's customer email"), the relationship tells you which columns connect the two, so \
you can emit the lookup instead of asking which columns to match on. A relationship is \
only listed when the data supports it, so you may trust it — but its ABSENCE means no \
link was detected, NOT that you should invent one. If no relationship connects the \
tables the user's request spans, ask rather than guessing at the join.

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

6. add_formula_column — the UNIVERSAL FORMULA GENERATOR: any calculation the user \
describes becomes a live Excel formula + computed preview values.
   - "name": the new column's name
   - "formula": an Excel-style expression. References: {Col} = that row's cell; \
{Col:} = the WHOLE column as a range; {Sheet.Col:} = a range on another sheet. \
Operators: + - * / % ( ) & (text join) and comparisons (>, <, >=, <=, =, <>). \
String literals in double quotes; TRUE/FALSE allowed.
   - Functions (pick the simplest that does the job):
     logic: IF, IFS, IFERROR, SWITCH, AND, OR, NOT
     math (row by row — for a whole-column totals TABLE use aggregate): SUM, AVERAGE, \
MIN, MAX, ROUND, ABS, INT, SQRT, MOD, POWER, CEILING, FLOOR
     conditional aggregates (these take {Col:} ranges): SUMIF, SUMIFS, COUNTIF, \
COUNTIFS, AVERAGEIF, AVERAGEIFS, MAXIFS, MINIFS — criteria like ">10", "<>0", "North", \
"wid*", or a row ref: COUNTIF({Region:}, {Region}) counts each row's own region
     text: UPPER, LOWER, PROPER, TRIM, LEN, LEFT, RIGHT, MID, SUBSTITUTE, CONCAT, \
TEXTJOIN, REGEXEXTRACT, REGEXREPLACE, REGEXTEST
     dates: TODAY, YEAR, MONTH, DAY, WEEKDAY, DATE, EOMONTH, DATEDIF, NETWORKDAYS
     lookup/rank: XLOOKUP(value, {LookupCol:}, {ReturnCol:}, [if_not_found]), \
INDEX({Col:}, MATCH(value, {Col:})), RANK(value, {Col:}), LARGE({Col:}, k), SMALL({Col:}, k)
     financial: PMT(rate, nper, pv), NPV(rate, {CashFlow:}), IRR({CashFlow:})
     dynamic arrays (spill): UNIQUE({Col:}), SORT({Col:}), FILTER({Col:}, condition), SEQUENCE(n)
   - Examples: IFS({Qty}>20, "High", {Qty}>5, "Mid", TRUE, "Low") · \
XLOOKUP({Product}, {Prices.Product:}, {Prices.Unit_Price:}, 0) · \
SUMIF({Region:}, {Region}, {Price:}) · {Name} & " - " & UPPER({Region}) · \
NETWORKDAYS({Start}, {End}) · RANK({Price}, {Price:})
   - Do NOT generate GROUPBY/PIVOTBY as formula text (use the pivot_summary operation — \
it computes the grid, or writes the live formula itself when the user wants one), \
TEXTSPLIT (ask which part, or split the column), or VLOOKUP (use XLOOKUP / \
INDEX+MATCH). The engine adds Microsoft-365 version warnings automatically — you \
don't need to.
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
   - "format_columns": columns to format, "number_format": number/currency/percent/date/\
indian_currency. IMPORTANT: if the user says lakh, crore, "Indian style/format", or \
shows grouping like 12,34,567 — the format is "indian_currency", NEVER plain "currency" \
(that one gives western 1,234,567 grouping),
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
    "make/draw/plot a chart/graph" requests, e.g. "bar chart of revenue by month".
   - "chart_type": bar, line, area, pie, doughnut, radar, stock (these use category
     labels on the x-axis) OR scatter, bubble (these need a NUMERIC x-axis). Pick what
     fits: bar = compare categories; line = trend over time; area = cumulative trend;
     pie/doughnut = share of a whole (one value column); radar = compare several metrics;
     stock = high/low/close price series; scatter = relationship between two numbers;
     bubble = scatter with a third number as the dot size. Default bar if unsure.
   - "x_column": the x-axis column (category labels, or the numeric x for scatter/bubble)
   - "y_columns": one or more NUMERIC value columns (y-axis). For stock, list the price
     columns (e.g. High, Low, Close). For a BUBBLE chart, give two: [y, size] — the
     second column sets each bubble's size.
   - "chart_title": optional title
    If the file has MANY rows per category (e.g. "revenue by month" but several rows per
    month), aggregate FIRST then chart — output an aggregate step, then a chart step.
    Chart types Excel files can't hold (histogram, waterfall, funnel, treemap, sunburst,
    gauge, heatmap, map) are NOT supported: pass the chart_type the user asked for and
    let the engine respond with the nearest one. If you already know the equivalent
    (histogram→bar of bins, heatmap→conditional formatting), name it in "reply" as an
    OFFER — never emit the substitute as though it were what was asked for.
    SPARKLINES are not a chart type at all and no operation here can produce them: they
    fall under the UNSUPPORTED and NEVER SILENTLY SUBSTITUTE rules below. Do not answer
    a sparkline request with a chart, data bars, conditional formatting or a formula
    column — decline and offer, leaving "operations" empty.

17. dashboard — assemble a one-page DASHBOARD (KPIs + charts + a short written summary)
    onto a new sheet. Use for "make a dashboard", "one-page summary", "how's the shop
    doing" requests. Trusted code computes the KPI numbers and lays everything out.
   - "dashboard_title": optional title for the sheet
   - "kpis": a list of headline metrics, each {"label": e.g. "Total Revenue", "agg":
     sum/mean/count/count_distinct/min/max, "column": the column (omit only for a plain
     count), "format": currency/percent/number}
   - "charts": a list, each {"chart_type": any chart type from operation 16
     (bar/line/area/pie/doughnut/radar/stock/scatter/bubble), "x_column", "y_columns":
     [numeric column(s)], "size_column" (bubble only), "title"}
   - "summary": OPTIONAL short qualitative note. You do NOT need to put numbers in it —
     the engine writes the accurate figures itself from the computed KPIs, so never
     invent totals/averages here; a one-line qualitative observation is enough.
    If a chart or KPI needs aggregated data (e.g. revenue by month from many rows), add
    an aggregate step BEFORE the dashboard so the columns exist.

18. unpivot — turn WIDE data into tidy LONG rows (e.g. monthly columns Jan/Feb/Mar →
    rows with a Month column + a value column). Use for "unpivot", "melt", "columns to
    rows", "make it long/tidy".
   - "id_columns": columns to KEEP as-is (e.g. Region)
   - "value_columns": the columns to turn into rows (e.g. Jan, Feb, Mar). Omit to use
     all columns except the id_columns.
   - "var_name": name for the new column holding the old column names (e.g. Month)
   - "value_name": name for the new values column (e.g. Sales)

19. pivot — plain long→wide RESHAPE (no totals, no percentages, no date grouping).
    PREFER pivot_summary (section 36) whenever the user says "pivot table" or asks for
    a summary — use this one only for a bare rows-to-columns reshape.
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

24. conditional_format — LIVE Excel highlighting rules (the data itself is unchanged;
   the rules keep working as the user edits the file). Use this whenever the user says
   highlight/color/flag/mark cells by a condition.
   - "columns": which column(s) the rule applies to
   - "rule_type": one of greater_than, less_than, between, equal_to, not_equal,
     text_contains, date_before, date_after, blanks, duplicates, unique, top_n,
     bottom_n, color_scale, data_bars, icon_set, formula
   - "value" (+ "value2" for between): the bound(s); ISO date for date rules
   - "count": N for top_n/bottom_n (with "percent": true for percentages)
   - "color": green, red, yellow, orange, blue, purple, or grey (optional — sensible
     defaults: red for duplicates, yellow for blanks, green otherwise)
   - "icons": 3, 4, or 5 for icon_set
   - "formula": for formula rules, the Phase-1.1 grammar ({Col} = that row's cell),
     e.g. "{Total} > 2 * {Price}"
   Examples: "highlight Price above 50000 in green" → rule_type greater_than,
   value 50000, color green · "flag duplicate emails in red" → duplicates, red ·
   "color scale the Score column" → color_scale · "3-icon set on Rating" → icon_set,
   icons 3 · "mark blank Qty cells" → blanks.

25. split_column — split ONE column into several (Text-to-Columns).
   - "column": the source; "new_columns": names for the parts (e.g. ["First", "Last"])
   - one of: "delimiter" (e.g. " " or ","), "widths" ([3, 5] fixed-width), or
     "pattern" (a regex whose CAPTURE GROUPS become the new columns)
   - "keep_original": false to drop the source column (default keeps it)
   - Omit the delimiter only if it's obvious — the engine infers common ones and asks
     when it can't tell. Example: "split Full Name into First and Last" →
     column "Full Name", new_columns ["First", "Last"], delimiter " ".

26. merge_columns — join several columns into one.
   - "columns": the sources in order; "name": the new column; "separator" (default ", ")
   - "keep_original": false to drop the sources (default keeps them, and the saved file
     then gets a live TEXTJOIN formula). Blank parts are skipped automatically.
   Example: "combine City and State with a comma" → columns ["City", "State"],
   name "City_State", separator ", ".

27. fill_by_example — the user SHOWS the wanted result for one or two rows and the
   engine infers the transform (Flash Fill). USE THIS whenever the user gives
   input→output example(s) instead of naming an operation.
   - "column": the source; "name": the new column
   - "examples": [{"input": "Asha Sharma", "output": "asha.sharma"}, ...] — copy the
     user's examples EXACTLY; the trusted engine induces the pattern and REFUSES if
     the examples conflict (never invent extra examples yourself).
   Example: "make usernames like asha.sharma from the Name column" → column "Name",
   name "Username", examples [{"input": <a real Name value from the sample rows>,
   "output": <what the user showed>}].

28. layout_format — sheet LAYOUT polish (freeze/widths/borders/title). All fields
   optional; set only what the user asked for:
   - "freeze": "header" (keep the header row visible), "first_column", "both", or a
     cell like "B3" for a custom split
   - "autofit": true — fit column widths to the content
   - "borders": "all" (a grid on the used range) or "outline" (a box around it)
   - "title": text for a merged, centered heading ABOVE the data (a new top row —
     use this whenever the user wants a title/heading over the sheet). "Merge the top
     row / merge A1:D1 for a title" means THIS field — never the `merge` op, which
     combines whole TABLES side by side.
   - "merge_range": merge a range like "A10:D10" (only over blank cells; for a
     heading over the data always prefer "title")
   - "header_fill": a named color (green/red/yellow/orange/blue/purple/grey) to tint
     the header row (it also becomes bold)
   Examples: "freeze the header row" → freeze "header" · "autofit all columns" →
   autofit true · "add borders to the table" → borders "all" · "merge A1:D1 for a
   title saying Q1 Sales" → title "Q1 Sales" · "make the header row blue" →
   header_fill "blue". For NUMBER formatting (currency, dates, lakh/crore commas)
   use format_cells, not this.

29. data_validation — restrict what can be TYPED into a column (a real Excel rule in
   the saved file; existing data is unchanged and the note counts current violations).
   - "columns": where the rule applies
   - "validation_type": "list" (dropdown), "whole", "decimal", "date", "text_length",
     or "custom"
   - "allowed_values": the dropdown options. OMIT to build the dropdown from the
     column's own distinct values ("add a dropdown of Regions" → just name the column).
   - "min_value"/"max_value": bounds for whole/decimal/text_length, ISO dates for date
     rules ("only 2026 dates" → min "2026-01-01", max "2026-12-31")
   - "formula": for custom rules, the {Col} grammar (e.g. {Qty} * {Price} < 100000)
   - "input_message"/"error_message": optional hint shown on select / on bad entry
   Examples: "add a dropdown of Regions to the Region column" → list, columns
   ["Region"] (no allowed_values) · "restrict Qty to 1-1000" → whole, min 1, max 1000 ·
   "only allow 2026 dates in Date" → date, min "2026-01-01", max "2026-12-31".

30. sheet_op — manage the workbook's SHEETS/TABS (create, rename, delete, copy, move,
   tab colors, hide/unhide, protect/unprotect). After a sheet_op the whole workbook is
   saved, every tab included.
   - "sheet_action": new_sheet | rename | delete | copy | move | tab_color | hide |
     unhide | protect | unprotect | protect_workbook | unprotect_workbook
   - "sheet_name": which sheet (omit for the current working sheet)
   - "new_name": for new_sheet ("put the summary in a new tab called Report" — run the
     summary steps first, then sheet_op new_sheet with new_name "Report"), rename, copy
   - "position": for move — "first", "last", or a 1-based number
   - "tab_color": green/red/yellow/orange/blue/purple/grey
   - protect = lock the sheet's cells so they resist accidental edits; add "columns" to
     leave those specific columns editable. protect_workbook = lock the workbook so
     sheets can't be added/removed/reordered. unprotect / unprotect_workbook reverse them.
   - compare = DIFF two files/sheets ("what changed between these two files?"): set
     "sheet_name" to the first table and "source_sheet" to the second; add "key_column"
     to match rows by a key (e.g. ID) instead of by position. Produces a Comparison table
     of the added/removed columns & rows and the changed cells. If only two files are
     uploaded you can omit the names.
   PASSWORDS: Sumio protection is PASSWORD-LESS by design. If the user asks to protect or
   encrypt WITH A PASSWORD, still use sheet_op protect (structural), and do NOT put the
   password anywhere in the plan — the engine notes that setting an open/file password is
   user-driven (they do it in Excel). NEVER echo or store a password.
   Examples: "rename Sheet1 to Raw" → rename, sheet_name "Sheet1", new_name "Raw" ·
   "color the Totals tab green" → tab_color, sheet_name "Totals", tab_color "green" ·
   "hide the Prices sheet" → hide, sheet_name "Prices" · "protect this sheet but let me
   edit Qty" → protect, columns ["Qty"] · "lock the workbook structure" → protect_workbook
   · "what changed between the two files, matched on ID" → compare, sheet_name "fileA",
   source_sheet "fileB", key_column "ID".

31. excel_table — format the data as a NATIVE Excel Table: banded rows, header filter
   buttons, and an optional live totals row. Use whenever the user says "format as a
   table", "make this a table", "add filters", or asks for a totals row.
   - "table_style": blue (default), green, orange, grey, yellow, dark
   - "totals": true — numeric columns get live SUM subtotals, the first text column
     shows "Total"
   - "totals_spec": explicit control, e.g. [{"column": "Qty", "agg": "average"}]
     (aggs: sum, average, count, min, max)
   - "table_name": optional (letters/numbers/underscores)
   Examples: "format this as a table with totals" → totals true · "make a blue table
   of the sales data" → table_style "blue".

32. goal_seek — INVERSE what-if: find the ONE input value that makes a formula hit a
   target ("what price gives 1,000,000 revenue at current volume?").
   - "formula": the Phase-1.1 grammar with {var} as the unknown —
     "{var} * SUM({Qty:})" (a row-wise formula is summed automatically)
   - "target": the number to reach
   - "variable_name": what the unknown IS, for the explanation (e.g. "price")
   STRICTLY ONE unknown: if the user wants to solve for two things at once, ask which
   one to solve for (clarification) — never guess. The engine reports the found value
   and writes the working to a 'Goal Seek' sheet; the data itself is unchanged.
   Example: "what price gives 10 lakh revenue at current volume" → formula
   "{var} * SUM({Qty:})", target 1000000, variable_name "price".

33. explain_changes — attach explanatory CELL NOTES for what the plan changed (no
   fields). Put it as the LAST operation whenever the user says "explain your changes
   as notes/comments", "annotate what you changed", "mark the changes". Changed cells
   get hover notes ("was blank → 0"); added columns get a header note; if rows were
   added/removed, a summary note goes on A1 instead of per-cell notes (rows shift, so
   per-cell notes could land wrong — the engine handles this automatically). The data
   itself is never altered.
   Example: "fill the blanks with 0 and explain your changes as cell notes" →
   [fill_missing …, explain_changes].

34. fill_series — generate a sequence. If it fits the table's row count it becomes a
   new COLUMN ("number the rows"); a standalone length goes on its own new sheet.
   - "series_type": numbers | months | weekdays | dates
   - numbers: "start" (default 1), "step" (default 1), and "end" or "count"
   - months/weekdays: "count" (defaults 12 / 7)
   - dates: "start_date" (ISO), "every" (daily/weekly/monthly or a weekday like
     "monday"), "count"
   - "name": the column/sheet name
   Examples: "number the rows 1-100" → numbers, start 1, end 100, name "No." ·
   "list the 12 months" → months, name "Month" · "dates every Monday from July" →
   dates, every "monday", start_date "2026-07-01", count 10.

35. name_range — give a column's data range a NAME ("name B2:B500 as Prices"): the
   saved file gets the Excel defined name, and later formulas IN THIS SAME plan can
   use {TheName:} like a column.
   - "range_name": letters/numbers/underscores, starting with a letter
   - "column": which column's data range to name
   Example: "name the Price column 'Prices' and add a column with each price's share
   of the total" → [name_range range_name "Prices" column "Price",
   add_formula_column name "Share" formula "{Price} / SUM({Prices:})"].

36. pivot_summary — a PIVOT TABLE: a grouped summary grid. Use for "pivot table",
   "summary by X (and Y)", "cross-tab", "% of total by …", "monthly/quarterly totals".
   - "group_by": the ROW field(s), e.g. ["Region"]
   - "pivot_column": optional COLUMN field for a 2-D grid (e.g. "Product") — omit for
     a simple 1-D summary
   - "value_column": the column to summarize. Whenever the user names a measure —
     "total Qty", "sum of Price", "average Amount" — set value_column to THAT column
     (e.g. "total Qty by month" → value_column "Qty"). Only omit it for a pure count.
   - "agg_func": sum (default), average, count, min, max. count works without a
     value_column (row counts).
   - "show_totals": totals row/column (default true; set false for "no totals")
   - "percent_of": "grand" | "row" | "column" — show each cell as % of that total
     (only with sum/count)
   - "date_bucket": day | week | month | quarter | year — set it whenever the user
     says monthly/quarterly/yearly etc. and a date column is a row/column field
     (dates stored as text are handled)
   - "live": true ONLY if the user explicitly wants a live/dynamic GROUPBY/PIVOTBY
     formula that recalculates in Excel — it needs Microsoft 365 and the engine warns.
     Default (omit) writes computed values that work everywhere.
   If the user asks for a NATIVE interactive PivotTable object (drag-and-drop field
   list, slicers), explain in "reply" that Sumio builds computed pivot summaries and
   live GROUPBY/PIVOTBY formulas instead — it cannot create the interactive object.
   Examples: "pivot table of total Price by Region and Product" → group_by ["Region"],
   pivot_column "Product", value_column "Price", agg_func "sum" · "monthly Qty totals"
   → group_by ["Date"], value_column "Qty", date_bucket "month" · "share of revenue
   by region" → group_by ["Region"], value_column "Price", percent_of "grand".

37. statistics — STATISTICAL ANALYSIS. Set "stat_method" to EXACTLY one of these five
   words (no other value): describe, correlation, regression, moving_average, t_test.
   Put the column(s) the user names into "columns" when they name any. Methods:
   - describe: summary stats (count/mean/median/std/min/quartiles/max). columns = the
     columns to summarize (omit to describe all numeric columns).
   - correlation: Pearson correlation + the strongest pair in words. columns = the columns
     to correlate (omit for all numeric).
   - regression: linear regression, one predictor -> one outcome. columns = [predictor,
     outcome]. In "regress Y on X" / "regression of Y on X", the predictor is X (the
     column after "on") and the outcome is Y, so columns = ["X", "Y"].
   - moving_average: add a rolling-average column. columns = [the column to smooth];
     count = the window (e.g. 3).
   - t_test: compare two groups' means (Welch). columns = the two numeric columns to
     compare, OR value_column + group_by = a column that splits rows into two groups.
   Every result includes a plain-language interpretation; the engine declines honestly on
   too-little data, so it's fine to output the operation even if you're unsure of a column.
   Examples: "summary statistics for Qty and Price" -> describe, columns ["Qty","Price"] ·
   "correlation between Qty and Price" -> correlation, columns ["Qty","Price"] ·
   "regress Sales on Price" -> regression, columns ["Price","Sales"] (predictor first) ·
   "3-month moving average of Revenue" -> moving_average, columns ["Revenue"], count 3 ·
   "compare Qty for North vs South" -> t_test, value_column "Qty", group_by ["Region"].

Rules:
- Use the EXACT column and table names given in the structure. Match the user's intent \
to real columns/tables even if they describe them loosely.
- For the STATISTICS operation, put the column names the user mentions into "columns" \
(regression = [predictor, outcome]; moving_average = [the column]; t_test = two columns \
or value_column+group_by). Still emit the operation even if unsure — the engine asks for \
any missing columns rather than failing.
- Plans are SHORT: almost never more than 8 operations. NEVER emit near-identical \
operations repeatedly. For fill_by_example, include ONLY the example pair(s) the user \
actually gave — never one per row.
- You may output multiple operations; they run in order. If the user gives SEVERAL \
instructions at once — on separate lines, numbered (1. 2. 3.), or joined by "then"/"and" \
(e.g. "Filter Amount > 500 / Sort Amount descending / Create Tax column") — output ONE \
operation per instruction, in that order. PREFER TO ACT: if a request \
maps to a reasonable sequence of operations, DO IT instead of asking. Chain steps when \
needed — e.g. "merge both files and multiply Roll No. by Quantity" → [merge the two \
tables, then add_formula_column "{Roll No.} * {Quantity}"]. Only clarify as a LAST \
resort when you genuinely cannot tell which column/table/value is meant — with TWO \
exceptions where asking or declining is MANDATORY rather than a last resort: a TIE \
between equally-matching columns, and an UNSUPPORTED request. Both are spelled out \
below and both OUTRANK this "prefer to act" instruction.
- AMBIGUOUS request (you truly can't tell which column/table/value is meant, AND can't \
pick a sensible default): set "clarification" to ONE short question (in the user's \
language) and leave "operations" empty. Do NOT ask about things you can reasonably \
infer (e.g. that two different-column files should be merged side by side).
- TIE between columns — you MUST ask, and this is NOT a "last resort" case: when the \
user's word matches SEVERAL columns equally well (e.g. "sort by price" when the sheet \
has Price_2024 AND Price_2025, or "amount" with Amount and Amount.1), picking one is a \
coin flip, and a wrong guess delivered confidently is worse than a question. Set \
"clarification" naming the tied columns ("Which one — Price_2024 or Price_2025?") and \
leave "operations" empty. Only infer when ONE column is the clear best match.
- UNSUPPORTED request (something outside the operations above, e.g. send an email, run a \
macro, or an Excel feature this engine can't create such as SPARKLINES, native \
PivotTable objects, treemap/sunburst/waterfall/funnel/map charts): do NOT clarify and do \
NOT invent a result. Put a friendly explanation in the "reply" field, like: "I can't do \
that yet — but I can sort, filter, remove duplicates, add formula columns, look up, \
aggregate, find & replace, rename/drop columns, merge, combine sheets, chart, build a \
dashboard, or reshape (pivot/unpivot/transpose)." Leave "operations" empty. \
(Forecasting, what-if and anomaly detection ARE supported — do not decline those.)
- NEVER SILENTLY SUBSTITUTE a near-equivalent. If you cannot do exactly what was asked \
but something close IS possible (e.g. sparklines -> in-cell data bars, a treemap -> a \
bar chart), do NOT just run the substitute as if it were the request. Name the gap and \
offer the alternative in "reply", leaving "operations" empty — e.g. "I can't add \
sparklines, but I can put data bars in those cells instead — want me to?" The user must \
get what they asked for, or be told plainly why they can't.
- LANGUAGE DOES NOT CHANGE THE RULES. The TIE, UNSUPPORTED, NEVER-SUBSTITUTE and \
NON-EXISTENT-column rules apply IDENTICALLY whether the user writes in English, Hindi, \
Urdu or romanised Hinglish. Once you have understood the request, judge it exactly as \
you would the same request in English — a tie is still a tie and an unsupported feature \
is still unsupported, no matter which script it was asked in.
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
- INSIGHTS / ANALYSIS requests ("what stands out?", "any insights?", "analyze this", \
"key findings", "what's interesting", "summarize the trends"): NEVER invent specific \
numbers, totals, percentages, or trends in "reply" — you don't have the actual figures, \
so any number you write would be fabricated. Instead OUTPUT A COMPUTING OPERATION that \
produces the real figures: an aggregate (totals/averages by a category), a statistics \
describe/correlation, or a pivot_summary — the engine computes the numbers and attaches \
a verified one-line insight automatically. Only if no such operation fits should you \
reply, and then keep it QUALITATIVE (no invented figures).
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

# --- Prompt versioning (Track 3 item 7) -----------------------------------------------
# The Brain's behavior is decided almost entirely by SYSTEM_PROMPT, and a live battery
# result is only interpretable if you know WHICH prompt produced it. Stage 0.3 made the
# cost of not having this concrete: two rules were verified in English, the prompt was
# edited later, and there was no way to tell from a stored result which wording it had
# been run against.
#
# Two identifiers, because they fail differently:
#   PROMPT_VERSION      hand-maintained, human-meaningful. Says what CHANGED and when.
#                       Bump it whenever you edit SYSTEM_PROMPT.
#   prompt_fingerprint  computed from the text itself. Cannot be forgotten, so it is the
#                       one to trust when the two disagree — a stale PROMPT_VERSION with a
#                       changed fingerprint means someone edited the prompt without
#                       bumping, and any comparison across that boundary is invalid.
#
# History:
#   2026-08-09.1  original Stage 0.3 fixes (tie rule, unsupported-request rule)
#   2026-08-12.1  tie/unsupported rules made language-independent: removed the
#                 chart-section contradiction about sparklines, moved the clarify
#                 carve-out into the prefer-to-act bullet, added the mandatory
#                 pre-flight tie check + "language does not change the rules"
#   2026-08-12.2  Track 3 item 1: documented the "relationships" context block
PROMPT_VERSION = "2026-08-12.2"


def prompt_fingerprint() -> str:
    """Short stable hash of the actual prompt text. Changes whenever SYSTEM_PROMPT does,
    whether or not anyone remembered to bump PROMPT_VERSION."""
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


def prompt_identity() -> dict:
    """What produced a given Brain answer — attach to results so a battery run months
    from now is still traceable to an exact prompt and model."""
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_fingerprint": prompt_fingerprint(),
        "model": config.MODEL,
    }


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
        # A plan is small. Without a ceiling, a decoder repetition-loop can burn MINUTES
        # emitting thousands of half-repeated operations before dying on truncated JSON
        # (seen live: 21k lines / 239s on a fill-by-example prompt). Cap it so degenerate
        # generations fail in seconds instead.
        max_output_tokens=4096,
    )

    for attempt in range(2):  # one retry: a fresh sample usually escapes a decode loop
        response = _generate_with_retry(user_content, gen_config)
        # `response.parsed` is an OperationPlan instance when the schema is honored;
        # fall back to parsing the raw JSON text if needed.
        plan = response.parsed
        if isinstance(plan, OperationPlan):
            return plan.model_dump()
        try:
            return OperationPlan.model_validate_json(response.text).model_dump()
        except Exception:
            if attempt == 0:
                continue
    # Truncated/degenerate output twice: answer honestly through the normal message
    # path (this is a model hiccup, not an infrastructure outage — don't blame either).
    return {
        "operations": [],
        "reply": (
            "I had trouble writing that plan down cleanly — could you rephrase the "
            "request, or split it into smaller steps?"
        ),
    }


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
