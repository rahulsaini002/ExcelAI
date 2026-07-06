"""Trusted operation implementations — ONE module per operation.

The executor ("the Hands") dispatches each step of a plan to the matching function
in here. Every operation is a small, pure function:

    name(df, op) -> (new_df, note)

It validates its own inputs and carries out exactly one transformation with pandas,
returning a plain-language `note` describing what actually happened (with real
counts), so the user always sees an honest account.

Shared helpers (column checks, value parsing, the `OperationError` type) live in
`base.py`. Operation modules import from `base` — never from the executor — so the
import graph stays acyclic (executor -> operations -> base).

To add a new operation (filter, dedupe, …): drop a new `<name>.py` here with the
same `name(df, op) -> (df, note)` shape, then dispatch to it from the executor.
`sort.py` is the reference example.
"""
