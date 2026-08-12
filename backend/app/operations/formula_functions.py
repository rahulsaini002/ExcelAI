"""Engine Phase 1.1 — the Universal Formula Generator's function registry.

The Brain writes a formula TEMPLATE (e.g.  IFS({Qty}>20, "High", {Qty}>5, "Mid", TRUE, "Low")
or  XLOOKUP({Product}, {Prices.Product:}, {Prices.Unit_Price:}, "?") ). The trusted
executor parses it with a strict AST whitelist and evaluates it here in pandas so the
preview shows REAL computed values; the same template is separately rendered into a live
Excel formula by the serializer (main._apply_formula), which turns:

    {Col}          -> this row's cell            (B2)
    {Col:}         -> the column's data range    ($B$2:$B$201)
    {Sheet.Col:}   -> another sheet's range      (Prices!$B$2:$B$6)

Design rules:
  * NEVER a wrong preview: if we can't compute a function faithfully, we raise a plain
    OperationError (the Brain is told to prefer supported forms) — a missing feature
    beats a confident lie.
  * Ranges arrive as _Range (full/other-sheet Series); row refs arrive as plain Series
    aligned to the frame. Spill functions return _Spill (shorter Series) which the
    executor pads and the serializer writes as a single spilling formula.
  * M365/2019-only functions are listed in M365_FUNCS so callers can warn about
    older-Excel compatibility (#NAME?) while still writing the formula.
"""
from __future__ import annotations

import datetime as _dt
import re

import numpy as np
import pandas as pd

from .base import OperationError


class _Range:
    """A whole-column (possibly other-sheet) reference used as a function argument."""

    __slots__ = ("series", "sheet", "column")

    def __init__(self, series: pd.Series, sheet: str | None, column: str):
        self.series = series
        self.sheet = sheet
        self.column = column


class _Spill(pd.Series):
    """Marker type: a dynamic-array result (UNIQUE/SORT/FILTER/SEQUENCE) whose length is
    independent of the frame — the executor pads it and marks the directive 'spill'."""

    @property
    def _constructor(self):
        return pd.Series


# Function -> minimum Excel that evaluates it. Everything else works in Excel 2016+.
M365_FUNCS: dict[str, str] = {
    "XLOOKUP": "Excel 2021 / Microsoft 365",
    "XMATCH": "Excel 2021 / Microsoft 365",
    "IFS": "Excel 2019",
    "SWITCH": "Excel 2019",
    "TEXTJOIN": "Excel 2019",
    "CONCAT": "Excel 2016",
    "MAXIFS": "Excel 2019",
    "MINIFS": "Excel 2019",
    "FILTER": "Microsoft 365",
    "SORT": "Microsoft 365",
    "UNIQUE": "Microsoft 365",
    "SEQUENCE": "Microsoft 365",
    "REGEXEXTRACT": "Microsoft 365 (2024)",
    "REGEXREPLACE": "Microsoft 365 (2024)",
    "REGEXTEST": "Microsoft 365 (2024)",
}

# Asked-for but deliberately NOT generated, with the honest alternative. (A spilled
# multi-column result doesn't fit a formula column; the engine has real ops for these.)
REDIRECTS: dict[str, str] = {
    "GROUPBY": "ask for a pivot summary (e.g. 'total Price by Region') — Sumio computes "
               "the grouped table, or writes a live GROUPBY if you ask for a live formula",
    "PIVOTBY": "ask for a pivot table (e.g. 'pivot table of Qty by Region and Product') — "
               "Sumio computes it, or writes a live PIVOTBY if you ask for a live formula",
    "TEXTSPLIT": "tell me which part you want (LEFT/MID/RIGHT can grab it), or ask me to "
                 "'split the column' so each part gets its own column",
    "VLOOKUP": "say what to look up in plain language — Sumio writes XLOOKUP or "
               "INDEX/MATCH, which don't break when columns move",
}


def _s(v, ctx) -> pd.Series:
    """Broadcast a scalar/row value to a frame-aligned Series."""
    if isinstance(v, _Range):
        return v.series
    if isinstance(v, pd.Series):
        return v
    return pd.Series([v] * ctx["n"], index=ctx["index"])


def _num(v, ctx) -> pd.Series:
    return pd.to_numeric(_s(v, ctx), errors="coerce")


def _txt(v, ctx) -> pd.Series:
    s = _s(v, ctx)
    out = s.astype("string").fillna("")
    # Integral floats read from Excel ("2.0") should join/concat as "2".
    if pd.api.types.is_numeric_dtype(s):
        out = s.map(lambda x: "" if pd.isna(x) else (str(int(x)) if float(x).is_integer() else str(x)))
    return out


_DATE_NOTE = ("this date column has values stored as text (not real Excel dates) — the "
              "preview is computed correctly, but the live formula may show #VALUE! for "
              "those rows in Excel (ask me to 'fix the dates' first)")


def _dates(v, ctx) -> pd.Series:
    s = _s(v, ctx)
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    # pandas parses far more date shapes than Excel does: even when every value parses
    # HERE, text-stored dates make the LIVE formula fail in Excel — say so honestly.
    if (s.dtype == object or str(s.dtype) == "string") and (
        (s.notna() & (s.astype(str).str.strip() != "")).any()
    ) and _DATE_NOTE not in ctx["notes"]:
        ctx["notes"].append(_DATE_NOTE)
    return pd.to_datetime(s, errors="coerce", format="mixed", dayfirst=False)


def _range_arg(v, fname: str, argname: str) -> _Range:
    if not isinstance(v, _Range):
        raise OperationError(
            f"{fname}() needs a column range for its {argname} — write it as "
            "{Column:} (or {Sheet.Column:} for another sheet)."
        )
    return v


_CRIT = re.compile(r"^(<=|>=|<>|<|>|=)?(.*)$", re.S)


def _criteria_mask(rng: _Range, crit, ctx) -> pd.Series:
    """Excel criteria semantics: '>10', '<>0', 'North', 'wid*' — or a row-aligned Series
    (e.g. COUNTIF({Region:}, {Region}) counts each row's own region)."""
    hay = rng.series
    if isinstance(crit, (pd.Series, _Range)):  # per-row criteria -> membership counts later
        return crit.series if isinstance(crit, _Range) else crit
    text = str(crit)
    op, rest = _CRIT.match(text).groups()
    if op in (">", "<", ">=", "<=") or (op in ("=", "<>") and rest.replace(".", "", 1).lstrip("-").isdigit()):
        nums = pd.to_numeric(hay, errors="coerce")
        val = float(rest)
        return {"<": nums < val, ">": nums > val, "<=": nums <= val, ">=": nums >= val,
                "=": nums == val, "<>": nums != val}[op]
    target = rest if op else text
    vals = hay.astype("string").str.strip().str.lower()
    if "*" in target or "?" in target:
        pat = "^" + re.escape(target.strip().lower()).replace("\\*", ".*").replace("\\?", ".") + "$"
        m = vals.str.match(pat, na=False)
    else:
        m = vals == target.strip().lower()
    return ~m if op == "<>" else m


def _cond_agg(fname: str, args, ctx, agg: str):
    """Shared SUMIF/SUMIFS/COUNTIF(S)/AVERAGEIF(S)/MAXIFS/MINIFS engine."""
    plural = fname.endswith("S") and fname != "COUNTIFS" or fname in ("SUMIFS", "AVERAGEIFS", "MAXIFS", "MINIFS")
    if fname in ("SUMIFS", "AVERAGEIFS", "MAXIFS", "MINIFS"):
        value = _range_arg(args[0], fname, "value range")
        pairs = args[1:]
    elif fname == "COUNTIFS":
        value, pairs = None, args
    else:  # SUMIF / COUNTIF / AVERAGEIF: (range, criteria, [sum_range])
        rng = _range_arg(args[0], fname, "criteria range")
        crit = args[1]
        value = _range_arg(args[2], fname, "sum range") if len(args) > 2 else (None if fname == "COUNTIF" else rng)
        pairs = [rng, crit]
    if len(pairs) < 2 or len(pairs) % 2:
        raise OperationError(f"{fname}() takes (range, criteria) pairs.")

    per_row = None  # a Series criteria makes the result per-row
    mask = None
    for i in range(0, len(pairs), 2):
        rng = _range_arg(pairs[i], fname, f"criteria range {i // 2 + 1}")
        crit = pairs[i + 1]
        got = _criteria_mask(rng, crit, ctx)
        if isinstance(crit, (pd.Series, _Range)):
            per_row = (rng, got) if per_row is None else per_row
            continue
        mask = got if mask is None else (mask & got)

    if per_row is not None:
        rng, keys = per_row
        base = rng.series
        sub_ok = pd.Series(True, index=base.index) if mask is None else mask
        vals = pd.to_numeric(value.series, errors="coerce") if value is not None else None
        frame = pd.DataFrame({"k": base.astype("string").str.strip().str.lower()})
        frame = frame[sub_ok.reindex(frame.index, fill_value=False)]
        if agg == "count":
            table = frame.groupby("k").size()
        else:
            frame["v"] = vals.reindex(frame.index)
            table = getattr(frame.groupby("k")["v"], agg)()
        lut = keys.astype("string").str.strip().str.lower()
        return lut.map(table).fillna(0 if agg in ("count", "sum") else np.nan)

    if mask is None:
        raise OperationError(f"{fname}() needs at least one criteria.")
    if agg == "count":
        return int(mask.sum())
    vals = pd.to_numeric(value.series, errors="coerce")[mask]
    if agg == "sum":
        return float(vals.sum())
    if agg == "mean":
        return float(vals.mean()) if len(vals) else np.nan
    if agg == "max":
        return float(vals.max()) if len(vals) else np.nan
    return float(vals.min()) if len(vals) else np.nan


def _pmt(args, ctx):
    if len(args) < 3:
        raise OperationError("PMT() needs (rate, nper, pv) — e.g. PMT(0.01, 60, 500000).")
    rate, nper, pv = (_num(a, ctx) for a in args[:3])
    fv = _num(args[3], ctx) if len(args) > 3 else 0
    with np.errstate(all="ignore"):
        flat = -(pv + fv) / nper.replace(0, np.nan)
        grow = -(pv * (1 + rate) ** nper + fv) * rate / (((1 + rate) ** nper - 1))
    out = grow.where(rate != 0, flat)
    return out


def _npv(args, ctx):
    if len(args) != 2:
        raise OperationError("NPV() needs (rate, {CashFlow:}) — a rate and one cash-flow column.")
    rate = args[0]
    rate = float(rate) if not isinstance(rate, (pd.Series, _Range)) else float(_s(rate, ctx).iloc[0])
    cf = pd.to_numeric(_range_arg(args[1], "NPV", "cash-flow range").series, errors="coerce").dropna()
    if cf.empty:
        raise OperationError("NPV(): the cash-flow column has no numbers.")
    return float(sum(v / (1 + rate) ** (i + 1) for i, v in enumerate(cf)))


def _irr(args, ctx):
    cf = pd.to_numeric(_range_arg(args[0], "IRR", "cash-flow range").series, errors="coerce").dropna().to_numpy()
    if len(cf) < 2 or not ((cf > 0).any() and (cf < 0).any()):
        raise OperationError(
            "IRR() needs a cash-flow column with at least one negative (outflow) and one "
            "positive (inflow) value."
        )
    roots = np.roots(cf[::-1])
    real = [r.real for r in roots if abs(r.imag) < 1e-9 and r.real > 0]
    rates = sorted(1 / r - 1 for r in real if r != 0)
    rates = [r for r in rates if r > -1]
    if not rates:
        raise OperationError("IRR(): no solution exists for these cash flows.")
    return float(min(rates, key=abs))


def _apply(fname: str, args: list, ctx: dict):
    """Evaluate one whitelisted function. ctx = {n, index, notes}."""
    A = args

    # ---------- range aggregates ----------
    # SUM/AVERAGE/... with a {Col:} RANGE argument are true whole-column aggregates
    # (the legacy row-wise SUM(a, b) form is dispatched before the registry and never
    # reaches here with ranges).
    if fname in ("SUM", "AVERAGE", "AVG", "MEAN", "MIN", "MAX", "COUNT"):
        pieces = []
        for a in A:
            if isinstance(a, _Range):
                pieces.append(pd.to_numeric(a.series, errors="coerce").dropna())
            elif isinstance(a, pd.Series):
                pieces.append(pd.to_numeric(a, errors="coerce").dropna())
            else:
                pieces.append(pd.Series([pd.to_numeric(a, errors="coerce")]).dropna())
        allv = pd.concat(pieces) if pieces else pd.Series(dtype=float)
        if fname == "COUNT":
            return int(len(allv))
        if allv.empty:
            raise OperationError(f"{fname}(): the range has no numeric values.")
        if fname == "SUM":
            return float(allv.sum())
        if fname in ("AVERAGE", "AVG", "MEAN"):
            return float(allv.mean())
        return float(allv.min() if fname == "MIN" else allv.max())

    # ---------- logical ----------
    if fname == "IFS":
        if len(A) < 2 or len(A) % 2:
            raise OperationError('IFS() takes condition/value pairs — e.g. IFS({Qty}>20,"High",TRUE,"Low").')
        out = pd.Series([np.nan] * ctx["n"], index=ctx["index"], dtype=object)
        done = pd.Series(False, index=ctx["index"])
        for i in range(0, len(A), 2):
            cond = _s(A[i], ctx).astype(bool) & ~done
            val = _s(A[i + 1], ctx)
            out[cond] = val[cond]
            done |= cond
        return out
    if fname == "IFERROR":
        if len(A) != 2:
            raise OperationError("IFERROR() needs (value, value_if_error).")
        val = _num(A[0], ctx) if not isinstance(A[0], str) else _s(A[0], ctx)
        bad = ~np.isfinite(pd.to_numeric(val, errors="coerce").fillna(np.inf))
        fb = _s(A[1], ctx)
        return _s(A[0], ctx).where(~bad, fb)
    if fname == "SWITCH":
        if len(A) < 3:
            raise OperationError('SWITCH() needs (value, match, result, ... [default]).')
        expr = _txt(A[0], ctx).str.strip().str.lower()
        rest = A[1:]
        default = rest[-1] if len(rest) % 2 else np.nan
        pairs = rest[: len(rest) - 1] if len(rest) % 2 else rest
        out = _s(default, ctx).copy() if not np.isscalar(default) or not pd.isna(default) else pd.Series(
            [np.nan] * ctx["n"], index=ctx["index"], dtype=object)
        for i in range(0, len(pairs), 2):
            key = str(pairs[i]).strip().lower()
            out = out.mask(expr == key, _s(pairs[i + 1], ctx))
        return out
    if fname == "AND":
        out = _s(A[0], ctx).astype(bool)
        for a in A[1:]:
            out &= _s(a, ctx).astype(bool)
        return out
    if fname == "OR":
        out = _s(A[0], ctx).astype(bool)
        for a in A[1:]:
            out |= _s(a, ctx).astype(bool)
        return out
    if fname == "NOT":
        return ~_s(A[0], ctx).astype(bool)

    # ---------- math ----------
    if fname == "MOD":
        b = _num(A[1], ctx).replace(0, np.nan)
        return _num(A[0], ctx) % b
    if fname == "POWER":
        return _num(A[0], ctx) ** _num(A[1], ctx).clip(-100, 100)
    if fname in ("CEILING", "FLOOR"):
        sig = _num(A[1], ctx) if len(A) > 1 else 1
        sig = sig.replace(0, np.nan) if isinstance(sig, pd.Series) else (sig or 1)
        fn = np.ceil if fname == "CEILING" else np.floor
        return fn(_num(A[0], ctx) / sig) * sig

    # ---------- text ----------
    if fname in ("UPPER", "LOWER", "PROPER", "TRIM", "LEN"):
        t = _txt(A[0], ctx)
        if fname == "UPPER":
            return t.str.upper()
        if fname == "LOWER":
            return t.str.lower()
        if fname == "PROPER":
            return t.str.title()
        if fname == "TRIM":  # Excel TRIM also collapses interior runs of spaces
            return t.str.strip().str.replace(r" {2,}", " ", regex=True)
        return t.str.len()
    if fname in ("LEFT", "RIGHT"):
        n = int(A[1]) if len(A) > 1 else 1
        t = _txt(A[0], ctx)
        return t.str[:n] if fname == "LEFT" else t.str[-n:] if n else t.str[:0]
    if fname == "MID":
        if len(A) != 3:
            raise OperationError("MID() needs (text, start, length) — start counts from 1.")
        start, ln = int(A[1]) - 1, int(A[2])
        return _txt(A[0], ctx).str[start:start + ln]
    if fname == "SUBSTITUTE":
        if len(A) < 3:
            raise OperationError("SUBSTITUTE() needs (text, old, new).")
        return _txt(A[0], ctx).str.replace(str(A[1]), str(A[2]), regex=False)
    if fname in ("CONCAT", "CONCATENATE"):
        out = _txt(A[0], ctx)
        for a in A[1:]:
            out = out + _txt(a, ctx)
        return out
    if fname == "TEXTJOIN":
        if len(A) < 3:
            raise OperationError('TEXTJOIN() needs (delimiter, ignore_empty, text1, ...).')
        delim = str(A[0])
        ignore = bool(A[1]) if not isinstance(A[1], pd.Series) else True
        parts = [_txt(a, ctx) for a in A[2:]]
        rows = zip(*[p.tolist() for p in parts])
        joined = [delim.join([p for p in r if p != ""] if ignore else list(r)) for r in rows]
        return pd.Series(joined, index=ctx["index"])
    if fname in ("REGEXEXTRACT", "REGEXREPLACE", "REGEXTEST"):
        try:
            pat = re.compile(str(A[1]))
        except re.error as exc:
            raise OperationError(f"{fname}(): that pattern isn't a valid regular expression ({exc}).")
        t = _txt(A[0], ctx)
        if fname == "REGEXTEST":
            return t.str.contains(pat, na=False)
        if fname == "REGEXREPLACE":
            return t.str.replace(pat, str(A[2]) if len(A) > 2 else "", regex=True)
        got = t.str.extract("(" + pat.pattern + ")" if pat.groups == 0 else pat.pattern, expand=False)
        return got if isinstance(got, pd.Series) else got.iloc[:, 0]

    # ---------- dates ----------
    if fname == "TODAY":
        return pd.Timestamp.today().normalize()
    if fname in ("YEAR", "MONTH", "DAY", "WEEKDAY"):
        d = _dates(A[0], ctx)
        if fname == "WEEKDAY":  # Excel default: 1 = Sunday … 7 = Saturday
            return ((d.dt.dayofweek + 1) % 7) + 1
        return getattr(d.dt, fname.lower())
    if fname == "DATE":
        if len(A) != 3:
            raise OperationError("DATE() needs (year, month, day).")
        return pd.to_datetime(
            {"year": _num(A[0], ctx), "month": _num(A[1], ctx), "day": _num(A[2], ctx)}, errors="coerce")
    if fname == "EOMONTH":
        months = int(A[1]) if len(A) > 1 else 0
        d = _dates(A[0], ctx)
        return d + pd.DateOffset(months=months) + pd.offsets.MonthEnd(0)
    if fname == "DATEDIF":
        if len(A) != 3:
            raise OperationError('DATEDIF() needs (start, end, "Y"|"M"|"D").')
        a, b, unit = _dates(A[0], ctx), _dates(A[1], ctx), str(A[2]).strip().upper()
        if unit == "D":
            return (b - a).dt.days
        yrs = b.dt.year - a.dt.year
        mos = yrs * 12 + (b.dt.month - a.dt.month) - (b.dt.day < a.dt.day).astype(int)
        if unit == "M":
            return mos
        if unit == "Y":
            return mos // 12
        raise OperationError('DATEDIF(): the unit must be "Y", "M", or "D".')
    if fname == "NETWORKDAYS":
        a = _dates(A[0], ctx)
        b = _dates(A[1], ctx)
        av, bv = a.to_numpy("datetime64[D]"), b.to_numpy("datetime64[D]")
        ok = ~(pd.isna(a) | pd.isna(b))
        out = np.zeros(len(a))
        # busday_count excludes the end date; Excel includes it — add one day.
        out[ok.to_numpy()] = np.busday_count(av[ok.to_numpy()], (bv + np.timedelta64(1, "D"))[ok.to_numpy()])
        return pd.Series(np.where(ok, out, np.nan), index=ctx["index"])

    # ---------- lookup / rank ----------
    if fname == "XLOOKUP":
        if len(A) < 3:
            raise OperationError("XLOOKUP() needs (value, {LookupCol:}, {ReturnCol:}, [if_not_found]).")
        keys = _txt(A[0], ctx).str.strip().str.lower()
        lk = _range_arg(A[1], "XLOOKUP", "lookup range")
        rt = _range_arg(A[2], "XLOOKUP", "return range")
        lut_keys = lk.series.astype("string").str.strip().str.lower()
        lut = pd.Series(rt.series.values, index=lut_keys)
        lut = lut[~lut.index.duplicated(keep="first")]
        out = keys.map(lut)
        if len(A) > 3:
            out = out.where(out.notna(), _s(A[3], ctx))
        return out
    if fname == "MATCH":
        if len(A) < 2:
            raise OperationError("MATCH() needs (value, {Range:}) — exact match.")
        keys = _txt(A[0], ctx).str.strip().str.lower()
        rng = _range_arg(A[1], "MATCH", "range")
        vals = rng.series.astype("string").str.strip().str.lower().tolist()
        first = {}
        for i, v in enumerate(vals):
            first.setdefault(v, i + 1)
        return keys.map(first)
    if fname == "INDEX":
        if len(A) != 2:
            raise OperationError("INDEX() needs ({Range:}, position) — pair it with MATCH().")
        rng = _range_arg(A[0], "INDEX", "range")
        pos = _num(A[1], ctx)
        vals = rng.series.reset_index(drop=True)
        return pos.map(lambda p: vals.iloc[int(p) - 1] if pd.notna(p) and 1 <= int(p) <= len(vals) else np.nan)
    if fname == "RANK":
        rng = _range_arg(A[1], "RANK", "range") if len(A) > 1 else None
        if rng is None:
            raise OperationError("RANK() needs (value, {Range:}, [order]) — 0/omitted = highest first.")
        asc = bool(int(A[2])) if len(A) > 2 and not isinstance(A[2], pd.Series) else False
        nums = pd.to_numeric(rng.series, errors="coerce")
        ranked = nums.rank(ascending=asc, method="min")
        lut = pd.Series(ranked.values, index=nums.values)
        lut = lut[~pd.Series(lut.index).duplicated(keep="first").values]
        return _num(A[0], ctx).map(lut)
    if fname in ("LARGE", "SMALL"):
        rng = _range_arg(A[0], fname, "range")
        k = int(A[1]) if len(A) > 1 else 1
        nums = pd.to_numeric(rng.series, errors="coerce").dropna().sort_values(ascending=fname == "SMALL")
        if not 1 <= k <= len(nums):
            raise OperationError(f"{fname}(): k={k} is outside the {len(nums)} numeric values.")
        return float(nums.iloc[k - 1])

    # ---------- conditional aggregates ----------
    if fname in ("SUMIF", "SUMIFS"):
        return _cond_agg(fname, A, ctx, "sum")
    if fname in ("COUNTIF", "COUNTIFS"):
        return _cond_agg(fname, A, ctx, "count")
    if fname in ("AVERAGEIF", "AVERAGEIFS"):
        return _cond_agg(fname, A, ctx, "mean")
    if fname == "MAXIFS":
        return _cond_agg(fname, A, ctx, "max")
    if fname == "MINIFS":
        return _cond_agg(fname, A, ctx, "min")

    # ---------- financial ----------
    if fname == "PMT":
        return _pmt(A, ctx)
    if fname == "NPV":
        return _npv(A, ctx)
    if fname == "IRR":
        return _irr(A, ctx)

    # ---------- dynamic arrays (spill) ----------
    if fname == "UNIQUE":
        rng = _range_arg(A[0], "UNIQUE", "range")
        vals = rng.series.dropna()
        return _Spill(pd.unique(vals))
    if fname == "SORT":
        rng = _range_arg(A[0], "SORT", "range")
        desc = len(A) > 2 and str(A[2]).strip() in ("-1", "-1.0")
        nums = pd.to_numeric(rng.series, errors="coerce")
        key = nums if nums.notna().any() else rng.series.astype("string")
        return _Spill(rng.series.iloc[key.sort_values(ascending=not desc, na_position="last").index].reset_index(drop=True))
    if fname == "FILTER":
        if len(A) < 2:
            raise OperationError('FILTER() needs ({Range:}, condition) — e.g. FILTER({Price:}, {Region:}="North").')
        rng = _range_arg(A[0], "FILTER", "range")
        cond = A[1].series if isinstance(A[1], _Range) else _s(A[1], ctx)
        picked = rng.series[cond.astype(bool).reindex(rng.series.index, fill_value=False)]
        if picked.empty and len(A) > 2:
            return _Spill(pd.Series([A[2]]))
        return _Spill(picked.reset_index(drop=True))
    if fname == "SEQUENCE":
        n = int(A[0]) if A else 0
        if not 1 <= n <= 100_000:
            raise OperationError("SEQUENCE() needs a count between 1 and 100,000.")
        start = float(A[1]) if len(A) > 1 else 1
        step = float(A[2]) if len(A) > 2 else 1
        return _Spill(pd.Series(np.arange(n) * step + start))

    raise OperationError(f"The function {fname}() isn't supported yet.")


SUPPORTED: frozenset[str] = frozenset({
    "SUM", "AVERAGE", "AVG", "MEAN", "MIN", "MAX", "COUNT",  # range-aggregate forms
    "IFS", "IFERROR", "SWITCH", "AND", "OR", "NOT",
    "MOD", "POWER", "CEILING", "FLOOR",
    "UPPER", "LOWER", "PROPER", "TRIM", "LEN", "LEFT", "RIGHT", "MID",
    "SUBSTITUTE", "CONCAT", "CONCATENATE", "TEXTJOIN",
    "REGEXEXTRACT", "REGEXREPLACE", "REGEXTEST",
    "TODAY", "YEAR", "MONTH", "DAY", "WEEKDAY", "DATE", "EOMONTH", "DATEDIF", "NETWORKDAYS",
    "XLOOKUP", "MATCH", "INDEX", "RANK", "LARGE", "SMALL",
    "SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS", "MAXIFS", "MINIFS",
    "PMT", "NPV", "IRR",
    "UNIQUE", "SORT", "FILTER", "SEQUENCE",
})
