from datetime import date

import pytest

from second_brain_receipts.domain.receipt_date import resolve_receipt_date

# What a US-leaning model returns for "08/09/2026": 9 August.
MODEL_GUESS = date(2026, 8, 9)


@pytest.mark.parametrize("separator", ["/", "-", "."])
def test_day_first_reinterprets_ambiguous_date(separator):
    text = f"08{separator}09{separator}2026"
    assert resolve_receipt_date(text, MODEL_GUESS, "day_first") == date(2026, 9, 8)


@pytest.mark.parametrize("separator", ["/", "-", "."])
def test_month_first_keeps_us_reading(separator):
    text = f"08{separator}09{separator}2026"
    assert resolve_receipt_date(text, MODEL_GUESS, "month_first") == date(2026, 8, 9)


def test_auto_defers_entirely_to_the_model():
    assert resolve_receipt_date("08/09/2026", MODEL_GUESS, "auto") == MODEL_GUESS


@pytest.mark.parametrize(
    "text",
    [
        "25/12/2026",  # 25 cannot be a month, so the printed form settles it
        "12/25/2026",  # likewise, in the other order
    ],
)
def test_unambiguous_numeric_dates_are_left_to_the_model(text):
    # Policy only exists to break a genuine tie. When the printed value already
    # identifies the day, reinterpreting it could only introduce an error.
    guess = date(2026, 12, 25)
    assert resolve_receipt_date(text, guess, "day_first") == guess
    assert resolve_receipt_date(text, guess, "month_first") == guess


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "September 8, 2026",  # spelled out, unambiguous by construction
        "2026-09-08",  # year first, unambiguous by position
        "08/09/26",  # two-digit year, deliberately not guessed at
        "8 Sept 2026",
        "08/09",  # no year
        "not a date",
        "08/09/2026 14:32",  # trailing time is not the bare form we accept
        "08//09/2026",
        "08/09-2026",  # mismatched separators
    ],
)
def test_unrecognized_or_missing_text_falls_back_to_the_model(text):
    assert resolve_receipt_date(text, MODEL_GUESS, "day_first") == MODEL_GUESS


def test_impossible_calendar_date_falls_back_rather_than_inventing_one():
    # 02/30 under day-first would be 30 February. Prefer the model's reading over
    # fabricating a date that does not exist.
    guess = date(2026, 2, 3)
    assert resolve_receipt_date("30/02/2026", guess, "day_first") == guess


def test_surrounding_whitespace_is_tolerated():
    assert resolve_receipt_date("  08/09/2026  ", MODEL_GUESS, "day_first") == date(2026, 9, 8)


def test_leap_day_resolves_under_day_first():
    assert resolve_receipt_date("29/02/2024", date(2024, 2, 29), "day_first") == date(2024, 2, 29)


def test_single_digit_parts_are_handled():
    assert resolve_receipt_date("8/9/2026", date(2026, 8, 9), "day_first") == date(2026, 9, 8)
