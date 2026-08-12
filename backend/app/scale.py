"""Performance & scale layer (Phase 5.8).

Two engine-side levers for big data — the ones a single-process Python service can honestly
provide (true multi-node "distributed processing" is an infra concern out of this engine's
scope; here we make the SAME work cheaper and skippable):

  • RESULT CACHE — the same plan on the same data always yields the same output, so memoize
    it. A repeat run is a cache hit that skips the (potentially expensive) recompute. Keyed by
    a cheap, stable fingerprint of the data + the operations.

  • SAMPLING — for a huge sheet, a representative SAMPLE (evenly spaced rows, not just the
    head) lets the UI preview a transform fast, clearly flagged as a sample, before committing
    to the full run.

Everything here is pure and cheap: the fingerprint hashes shape + column names + a bounded
row sample (never a full million-row scan), so keying the cache costs O(1)-ish, not O(rows).
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

import pandas as pd

DEFAULT_LARGE_ROWS = 100_000  # a table past this is "large" enough to sample for previews
_FP_SAMPLE = 50               # rows sampled per table when fingerprinting


def total_rows(tables: dict) -> int:
    return sum(len(df) for df in tables.values())


def is_large(tables: dict, threshold: int = DEFAULT_LARGE_ROWS) -> bool:
    return any(len(df) > threshold for df in tables.values())


def fingerprint(tables: dict) -> str:
    """A cheap, stable content hash: table names, shapes, column names, and a hash of up to
    _FP_SAMPLE evenly-spaced rows per table — enough to tell 'same data' from 'changed data'
    without scanning millions of rows."""
    parts = []
    for name in sorted(tables, key=str):
        df = tables[name]
        n = len(df)
        cols = [str(c) for c in df.columns]
        sample = df.iloc[:: max(1, n // _FP_SAMPLE)].head(_FP_SAMPLE) if n else df
        try:
            vh = int(pd.util.hash_pandas_object(sample, index=False).sum())
        except Exception:
            vh = hash(sample.to_csv(index=False))
        parts.append([str(name), n, cols, vh])
    return hashlib.sha1(json.dumps(parts, default=str).encode("utf-8")).hexdigest()


def plan_signature(tables: dict, operations: list) -> str:
    """Cache key for (this data, this plan)."""
    h = hashlib.sha1()
    h.update(fingerprint(tables).encode("utf-8"))
    h.update(json.dumps(operations, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


class ResultCache:
    """A tiny bounded LRU. Values are whatever the caller memoizes (here: an execute_multi
    result tuple). Kept small on purpose — caching a couple of big results is the point;
    caching everything would defeat the memory savings sampling is meant to give."""

    def __init__(self, max_entries: int = 16):
        self._d: "OrderedDict[str, object]" = OrderedDict()
        self.max = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        if key in self._d:
            self._d.move_to_end(key)
            self.hits += 1
            return self._d[key]
        self.misses += 1
        return None

    def put(self, key: str, value) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._d), "max_entries": self.max,
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


# The process-wide cache used by the execution path.
RESULT_CACHE = ResultCache()


def run_cached(tables: dict, primary: str, operations: list, runner, cache: ResultCache = RESULT_CACHE):
    """Memoize `runner(tables, primary, operations)` by (data, plan) signature. Returns
    (result, cached: bool). `runner` is execute_multi-compatible."""
    key = plan_signature(tables, operations)
    hit = cache.get(key)
    if hit is not None:
        return hit, True
    result = runner(tables, primary, operations)
    cache.put(key, result)
    return result, False


def sample_tables(tables: dict, max_rows: int = 1000) -> tuple[dict, bool]:
    """Return a representative sample of every oversized table (evenly-spaced rows so the
    preview reflects the whole sheet, not just the top). Returns (sampled_tables, was_sampled)."""
    out: dict = {}
    sampled = False
    for name, df in tables.items():
        n = len(df)
        if n > max_rows:
            step = max(1, n // max_rows)
            out[name] = df.iloc[::step].head(max_rows).copy()
            sampled = True
        else:
            out[name] = df
    return out, sampled
