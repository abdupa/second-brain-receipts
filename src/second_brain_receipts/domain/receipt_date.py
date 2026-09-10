"""Deterministic reading of an ambiguous printed date.

A numeric date like 08/09/2026 means 8 September in most of the world and 9 August in
the United States. The image alone cannot settle it, and a vision model will simply
pick one, confidently. Observed behavior with gpt-4o is that it picks month-first even
when the receipt carries obvious non-US regional clues, and still reports High
confidence, so the wrong day can reach a financial record unflagged.

So the model is asked to transcribe the date exactly as printed, and this module
decides what it means, using configured policy rather than inference. That keeps the
rule in code where it can be read and tested, consistent with the wider design: the
model reads, the application decides.

Only genuinely ambiguous values are reinterpreted. When the printed form settles the
question on its own, such as 25/12/2026 or a spelled-out month, the model's reading is
kept as-is.
"""

import re
from datetime import date
from typing import Literal

DateOrder = Literal["auto", "day_first", "month_first"]

# Two 1-2 digit parts and a 4-digit year, separated consistently by / - or .
# Year-first and spelled-out forms are unambiguous by construction and not matched.
_NUMERIC_DATE = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{4})$")


def resolve_receipt_date(date_text: str | None, extracted: date, order: DateOrder) -> date:
    """Return the date to record, preferring configured policy over the model's guess.

    `extracted` is the model's own reading and is the fallback whenever policy does not
    apply: no transcription, an unrecognized format, an unambiguous date, `auto` order,
    or a combination that is not a real calendar date.
    """
    if order == "auto" or not date_text:
        return extracted

    match = _NUMERIC_DATE.match(date_text.strip())
    if match is None:
        return extracted

    first, _, second, year = match.groups()
    first, second, year = int(first), int(second), int(year)

    # If either part exceeds 12 it can only be the day, so the printed form is already
    # unambiguous and there is nothing for policy to decide.
    if first > 12 or second > 12:
        return extracted

    day, month = (first, second) if order == "day_first" else (second, first)
    try:
        return date(year, month, day)
    except ValueError:
        # e.g. 30/02/2026. Prefer the model's reading over inventing a date.
        return extracted
