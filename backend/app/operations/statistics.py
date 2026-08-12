"""Engine Phase 2.3 — statistical analysis (Area 11).

One `statistics` operation, chosen by `stat_method`:

  describe          per-column summary (count, mean, median, std, min/quartiles/max…)
  correlation       Pearson correlation matrix + the strongest relationship, in words
  regression        simple linear regression y ~ x: slope, intercept, R², p-value
  moving_average    add a rolling-mean column (trend smoothing)
  t_test            compare two groups' means (Welch's two-sample t-test)

Every result comes with a PLAIN-LANGUAGE interpretation, and every method declines
honestly when there isn't enough data (a statistic computed on 2 points is noise, and
saying "r = 1.00" there would be a confident lie).

No SciPy/statsmodels: the maths is pure NumPy, and the one special function needed —
the Student-t tail probability for p-values — is the regularized incomplete beta,
implemented here (Numerical Recipes' continued fraction) and unit-tested against known
values. This matches the codebase's dependency-light, pure-NumPy `forecast`.

describe / correlation return a results TABLE (it becomes the download); moving_average
adds a column to the data; regression / t_test leave the data untouched and write their
working to a small summary sheet (like Goal Seek).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .base import OperationError, require_columns

_METHODS = ("describe", "correlation", "regression", "moving_average", "t_test")


# --------------------------------------------------------------------------- #
# Student-t two-sided p-value via the regularized incomplete beta (no SciPy).
# --------------------------------------------------------------------------- #
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (modified Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_two_sided_p(t: float, df: float) -> float:
    """Two-sided p-value P(|T| > |t|) for a Student-t with `df` degrees of freedom."""
    if df <= 0 or not math.isfinite(t):
        return float("nan")
    if t == 0:
        return 1.0
    return _betai(0.5 * df, 0.5, df / (df + t * t))


def _significance(p: float) -> str:
    if not math.isfinite(p):
        return "significance can't be assessed"
    if p < 0.001:
        return "statistically significant (p < 0.001)"
    if p < 0.05:
        return f"statistically significant at the 5% level (p = {p:.3f})"
    return f"NOT statistically significant at the 5% level (p = {p:.3f})"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _blank_mask(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype(str).str.strip() == "")


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    """Coerce a column to numeric (numbers-stored-as-text included), NaNs dropped."""
    return pd.to_numeric(df[col].astype(object), errors="coerce").dropna()


def _numeric_columns(df: pd.DataFrame, requested: list[str] | None) -> list[str]:
    """Which columns to analyze: the ones asked for (validated), else every column
    that is at least 60% numeric (so an ID/label text column isn't dragged in)."""
    if requested:
        require_columns(df, requested)
        cols = requested
    else:
        cols = [c for c in df.columns
                if pd.to_numeric(df[c].astype(object), errors="coerce").notna().mean() >= 0.6]
    return cols


def _fmt(v: float) -> str:
    if not math.isfinite(v):
        return "—"
    a = abs(v)
    if a != 0 and (a < 0.01 or a >= 1e7):
        return f"{v:.3g}"
    return f"{v:,.2f}"


# --------------------------------------------------------------------------- #
# The five methods
# --------------------------------------------------------------------------- #
def _describe(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, None]:
    cols = _numeric_columns(df, op.get("columns"))
    numeric = {}
    for c in cols:
        s = _numeric(df, c)
        if len(s) > 0:
            numeric[c] = s
    if not numeric:
        raise OperationError(
            "I couldn't find any numeric columns to describe — describe works on "
            "numbers (counts, amounts, scores…)."
        )
    stats = ["count", "mean", "median", "std", "min", "25%", "50%", "75%", "max"]
    out = {"Statistic": [*stats, "range"]}
    for c, s in numeric.items():
        q = s.quantile([0.25, 0.5, 0.75])
        col_vals = [
            float(len(s)), float(s.mean()), float(s.median()),
            float(s.std(ddof=1)) if len(s) > 1 else 0.0,
            float(s.min()), float(q.loc[0.25]), float(q.loc[0.5]),
            float(q.loc[0.75]), float(s.max()),
        ]
        col_vals.append(col_vals[-1] - col_vals[4])  # range = max - min
        out[c] = [round(v, 6) for v in col_vals]
    result = pd.DataFrame(out)
    first = next(iter(numeric))
    s0 = numeric[first]
    note = (
        f"Described {len(numeric)} column{'s' if len(numeric) != 1 else ''} "
        f"({', '.join(numeric)}). For example, '{first}' averages {_fmt(float(s0.mean()))} "
        f"(median {_fmt(float(s0.median()))}, min {_fmt(float(s0.min()))}, "
        f"max {_fmt(float(s0.max()))}, over {len(s0)} values)."
    )
    return result, note, None


def _correlation(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, None]:
    cols = _numeric_columns(df, op.get("columns"))
    work = pd.DataFrame({c: pd.to_numeric(df[c].astype(object), errors="coerce") for c in cols})
    work = work.dropna(how="any")
    numeric_cols = [c for c in cols if work[c].nunique() > 1] if len(work) else []
    if len(numeric_cols) < 2:
        raise OperationError(
            "Correlation needs at least two numeric columns that vary — I couldn't find "
            "two here (constant or non-numeric columns can't be correlated)."
        )
    if len(work) < 3:
        raise OperationError(
            f"Only {len(work)} complete row{'s' if len(work) != 1 else ''} have values in "
            "every column — correlation needs at least 3 to mean anything."
        )
    corr = work[numeric_cols].corr(method="pearson").round(4)
    result = corr.reset_index().rename(columns={"index": ""})
    result.columns = [str(c) for c in result.columns]

    # Strongest off-diagonal pair, described in words.
    best = (None, None, 0.0)
    for i, a in enumerate(numeric_cols):
        for b in numeric_cols[i + 1:]:
            r = corr.loc[a, b]
            if pd.notna(r) and abs(r) >= abs(best[2]):
                best = (a, b, float(r))
    a, b, r = best
    note = (
        f"Correlation of {len(numeric_cols)} columns over {len(work)} complete rows. "
        f"Strongest relationship: '{a}' and '{b}' with r = {r:+.2f} "
        f"({_describe_r(r)}). r ranges -1…+1; near 0 means little linear relationship. "
        "Correlation is not causation."
    )
    return result, note, None


def _describe_r(r: float) -> str:
    a = abs(r)
    strength = ("negligible" if a < 0.2 else "weak" if a < 0.4 else
                "moderate" if a < 0.7 else "strong")
    if a < 0.2:
        return "almost no linear relationship"
    return f"a {strength} {'positive' if r > 0 else 'negative'} relationship"


def _regression(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict]:
    x_col = op.get("x_column")
    y_col = (op.get("y_columns") or [None])[0] or op.get("value_column")
    # Fallback: the Brain often supplies the two columns in the generic `columns` list
    # ([predictor, outcome]) instead of x_column/value_column — accept that too.
    if (not x_col or not y_col):
        pair_cols = [c for c in (op.get("columns") or []) if c]
        if len(pair_cols) == 2:
            x_col = x_col or pair_cols[0]
            y_col = y_col or pair_cols[1]
    if not x_col or not y_col:
        avail = ", ".join(
            c for c in df.columns
            if pd.to_numeric(df[c].astype(object), errors="coerce").notna().mean() >= 0.6
        )
        raise OperationError(
            "Regression needs a predictor and an outcome column — say which is which, "
            f"e.g. 'regress Sales on Price' (Price predicts Sales). Numeric columns: {avail}."
        )
    require_columns(df, [x_col, y_col])
    if x_col == y_col:
        raise OperationError("The predictor and the outcome must be different columns.")
    pair = pd.DataFrame({
        "x": pd.to_numeric(df[x_col].astype(object), errors="coerce"),
        "y": pd.to_numeric(df[y_col].astype(object), errors="coerce"),
    }).dropna()
    n = len(pair)
    if n < 3:
        raise OperationError(
            f"Regression needs at least 3 rows with numbers in both '{x_col}' and "
            f"'{y_col}' — I found {n}."
        )
    x, y = pair["x"].to_numpy(float), pair["y"].to_numpy(float)
    if np.ptp(x) == 0:
        raise OperationError(
            f"'{x_col}' has the same value in every row, so it can't predict anything."
        )
    slope, intercept = np.polyfit(x, y, 1)
    yhat = slope * x + intercept
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    # Standard error of the slope and its t-test (H0: slope == 0).
    dfree = n - 2
    se_slope = math.sqrt((ss_res / dfree) / np.sum((x - x.mean()) ** 2)) if dfree > 0 and ss_res > 0 else 0.0
    t = slope / se_slope if se_slope > 0 else float("inf")
    p = student_t_two_sided_p(t, dfree) if se_slope > 0 else 0.0

    sig = _significance(p)
    direction = "increases" if slope > 0 else "decreases"
    rows = [
        ["Linear regression", ""],
        ["Model", f"{y_col} ≈ slope × {x_col} + intercept"],
        ["Predictor (x)", x_col],
        ["Outcome (y)", y_col],
        ["Rows used", n],
        ["Slope", round(float(slope), 6)],
        ["Intercept", round(float(intercept), 6)],
        ["R-squared", round(float(r2), 6)],
        ["Slope p-value", round(float(p), 6) if math.isfinite(p) else "n/a"],
        ["Interpretation", f"{sig}; explains {r2*100:.0f}% of the variance"],
    ]
    note = (
        f"Regression of '{y_col}' on '{x_col}' ({n} rows): {y_col} {direction} by about "
        f"{_fmt(abs(float(slope)))} for each 1-unit rise in {x_col} "
        f"(intercept {_fmt(float(intercept))}). R² = {r2:.2f} — the line explains "
        f"{r2*100:.0f}% of the variation; the slope is {sig}. Details on the "
        "'Regression' sheet; your data is unchanged."
    )
    directive = {"type": "stats_sheet", "sheet_name": "Regression", "rows": rows}
    return df, note, directive


def _numeric_col_names(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns
            if pd.to_numeric(df[c].astype(object), errors="coerce").notna().mean() >= 0.6]


def _moving_average(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, None]:
    col = op.get("column") or (op.get("columns") or [None])[0] or op.get("value_column")
    if not col:
        # The Brain often gives just stat_method with no column. If there's exactly ONE
        # numeric column, it's unambiguous — use it. Otherwise ask (never guess between
        # several).
        numcols = _numeric_col_names(df)
        if len(numcols) == 1:
            col = numcols[0]
        else:
            raise OperationError(
                "Which column should I average? e.g. 'add a 3-month moving average of "
                f"Sales'. Numeric columns: {', '.join(numcols) or '(none)'}."
            )
    require_columns(df, [col])
    window = op.get("count") or op.get("window") or 3
    try:
        window = int(window)
    except (TypeError, ValueError):
        raise OperationError("The moving-average window must be a whole number (e.g. 3).")
    if window < 2:
        raise OperationError("A moving average needs a window of at least 2 periods.")
    if window > len(df):
        raise OperationError(
            f"The window ({window}) is larger than the table ({len(df)} rows) — "
            "pick a smaller window."
        )
    nums = pd.to_numeric(df[col].astype(object), errors="coerce")
    if nums.notna().sum() == 0:
        raise OperationError(f"Can't average '{col}' — it looks like text, not numbers.")
    df = df.copy()
    new_col = f"{col}_MA{window}"
    df[new_col] = nums.rolling(window=window, min_periods=window).mean().round(6)
    note = (
        f"Added '{new_col}', a {window}-period moving average of '{col}'. The first "
        f"{window - 1} row{'s' if window - 1 != 1 else ''} are blank (not enough earlier "
        "values yet). A moving average smooths short-term noise to show the trend."
    )
    return df, note, None


def _t_test(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict]:
    cols = [c for c in (op.get("columns") or []) if c]
    value_col = op.get("value_column")
    group_col = (op.get("group_by") or [None])[0] or op.get("group_column")

    if len(cols) >= 2:  # two numeric columns compared directly
        require_columns(df, cols[:2])
        a_name, b_name = cols[0], cols[1]
        a, b = _numeric(df, a_name), _numeric(df, b_name)
    elif value_col and group_col:  # one value column split by a 2-group column
        require_columns(df, [value_col, group_col])
        groups = [g for g in df[group_col].dropna().astype(str).unique()]
        groups = [g for g in groups if g.strip() != ""]
        if len(groups) != 2:
            raise OperationError(
                f"A t-test compares exactly TWO groups, but '{group_col}' has "
                f"{len(groups)} — filter to two groups, or name two columns to compare."
            )
        a_name, b_name = groups
        av = pd.to_numeric(df.loc[df[group_col].astype(str) == a_name, value_col].astype(object), errors="coerce").dropna()
        bv = pd.to_numeric(df.loc[df[group_col].astype(str) == b_name, value_col].astype(object), errors="coerce").dropna()
        a, b = av, bv
        a_name, b_name = f"{group_col}={a_name}", f"{group_col}={b_name}"
    else:
        # No columns given (the Brain often omits them). If the data has EXACTLY two
        # numeric columns, comparing them is unambiguous (order doesn't affect the
        # result) — do it. Otherwise ask; never pick arbitrarily among several.
        numcols = _numeric_col_names(df)
        if len(numcols) == 2:
            a_name, b_name = numcols
            a, b = _numeric(df, a_name), _numeric(df, b_name)
        else:
            raise OperationError(
                "For a t-test, name two numeric columns to compare, or a value column "
                "plus a column that splits the rows into two groups. Numeric columns: "
                f"{', '.join(numcols) or '(none)'}."
            )

    if len(a) < 2 or len(b) < 2:
        raise OperationError(
            f"Each group needs at least 2 values (got {len(a)} and {len(b)}) — "
            "a t-test can't run on a single point."
        )
    m1, m2 = float(a.mean()), float(b.mean())
    v1, v2 = float(a.var(ddof=1)), float(b.var(ddof=1))
    n1, n2 = len(a), len(b)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        raise OperationError(
            "Both groups have no variation (every value identical), so a t-test isn't "
            "meaningful here."
        )
    t = (m1 - m2) / se
    # Welch–Satterthwaite degrees of freedom.
    dfree = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = student_t_two_sided_p(t, dfree)
    sig = _significance(p)
    rows = [
        ["Two-sample t-test (Welch)", ""],
        ["Group A", f"{a_name} (n={n1}, mean {_fmt(m1)})"],
        ["Group B", f"{b_name} (n={n2}, mean {_fmt(m2)})"],
        ["Difference (A − B)", round(m1 - m2, 6)],
        ["t-statistic", round(t, 6)],
        ["Degrees of freedom", round(dfree, 3)],
        ["p-value", round(p, 6) if math.isfinite(p) else "n/a"],
        ["Conclusion", sig],
    ]
    verdict = ("The difference is " + ("real (unlikely to be chance)."
               if math.isfinite(p) and p < 0.05 else "within what chance could explain."))
    note = (
        f"t-test comparing '{a_name}' (mean {_fmt(m1)}, n={n1}) and '{b_name}' "
        f"(mean {_fmt(m2)}, n={n2}): difference {_fmt(m1 - m2)}, t = {t:.2f}, {sig}. "
        f"{verdict} Details on the 'T-Test' sheet; your data is unchanged."
    )
    directive = {"type": "stats_sheet", "sheet_name": "T-Test", "rows": rows}
    return df, note, directive


def statistics(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict | None]:
    if len(df) == 0:
        raise OperationError("There's no data to analyze yet.")
    # stat_method is a free str (the schema can't afford an enum — see the serving-cliff
    # note), so the model sometimes invents verbose values like
    # "correlation_matrix_all_numeric_columns". Match on the KEY WORD, priority-ordered,
    # so those still route correctly.
    m = (op.get("stat_method") or "").strip().lower()
    if "correl" in m:
        return _correlation(df, op)
    if "regress" in m:
        return _regression(df, op)
    if any(k in m for k in ("moving", "rolling", "movavg", "movingaverage", "smooth")):
        return _moving_average(df, op)
    if "ttest" in m or "t_test" in m or "t-test" in m:
        return _t_test(df, op)
    if any(k in m for k in ("describ", "descriptive", "summar", "statistic", "stats", "summary")):
        return _describe(df, op)
    raise OperationError(
        f"I don't know the analysis '{m or '(none)'}' — I can describe, correlate, "
        "run a linear regression, add a moving average, or run a t-test."
    )
