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


# Structured-output schema. Passing this to Gemini as the `response_schema`
# constrains the reply to valid JSON in exactly this shape (no markdown, no prose).
class Operation(BaseModel):
    action: Literal["sort", "remove_duplicates", "add_formula_column"]
    # sort
    columns: Optional[list[str]] = None
    orders: Optional[list[Literal["asc", "desc"]]] = None
    # add_formula_column
    name: Optional[str] = None
    formula: Optional[str] = None


class OperationPlan(BaseModel):
    operations: list[Operation]
    # Set when the instruction is too ambiguous to act on. When present, we ask
    # the user instead of guessing, and `operations` should be empty.
    clarification: Optional[str] = None


SYSTEM_PROMPT = """\
You are the parsing brain of a conversational spreadsheet assistant. You convert a \
user's plain-language instruction into a small, structured operation plan. You do \
NOT execute anything — trusted code runs your plan.

The user may write in Hindi, English, Urdu, or any mix of them. Interpret \
code-switched instructions naturally (e.g. "Email ke basis pe duplicate rows hata do" \
means remove duplicate rows based on the Email column).

You can ONLY use these operations:

1. sort
   - "columns": list of column names to sort by (in priority order)
   - "orders": list of "asc" or "desc", one per column (default "asc" if unsure)

2. remove_duplicates
   - "columns": list of column names that define a duplicate. Omit to consider all columns.

3. add_formula_column
   - "name": the new column's name
   - "formula": an arithmetic expression over existing columns, written with each \
column name wrapped in curly braces, e.g. "{Qty} * {Price}". Allowed operators: + - * / and parentheses.

Rules:
- Use the EXACT column names given in the sheet structure. Match the user's intent to \
real columns even if they describe them loosely.
- You may output multiple operations; they run in order.
- If the instruction is genuinely ambiguous or asks for something outside the \
operations above, set "clarification" to a short question (in the user's language if \
possible) and return an empty "operations" list.
- Otherwise leave "clarification" empty/null.
"""


def _client() -> genai.Client:
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy backend/.env.example to "
            "backend/.env and add your key."
        )
    return genai.Client(api_key=config.GEMINI_API_KEY)


def parse_instruction(instruction: str, structure: dict) -> dict:
    """Translate a plain-language instruction into an operation plan dict.

    Returns a dict shaped like OperationPlan (operations + clarification).
    """
    user_content = (
        "Sheet structure:\n"
        f"{json.dumps(structure, ensure_ascii=False, indent=2)}\n\n"
        f"Instruction:\n{instruction}"
    )

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
        f"The model ({config.MODEL}) is busy right now (high demand). "
        "Please try again in a few seconds."
    ) from last_exc
