from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from second_brain_receipts.schemas.receipts import (
    Confidence,
    PendingReceipt,
    Receipt,
    ReceiptExtraction,
    WorkflowState,
)


@pytest.fixture
def extraction():
    return dict(
        vendor_name="  ABC Hardware  ",
        date="2026-09-10",
        total_amount="100.50",
        vat_amount="10.00",
        category=" Maintenance ",
        confidence_score="High",
    )


@pytest.fixture
def pending(extraction):
    values = dict(extraction)
    values["receipt_date"] = values.pop("date")
    return dict(
        **values,
        id=uuid4(),
        telegram_chat_id=1,
        telegram_user_id=2,
        normalized_vendor_name="abc hardware",
        image_storage_key="receipts/opaque.jpg",
        image_storage_uri="s3://test-bucket/receipts/opaque.jpg",
        state="awaiting_category",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


def test_valid_extraction(extraction):
    result = ReceiptExtraction(**extraction)
    assert result.vendor_name == "ABC Hardware"
    assert result.category == "Maintenance"
    assert result.date == date(2026, 9, 10)
    assert result.total_amount == Decimal("100.50")
    assert isinstance(result.total_amount, Decimal)
    assert result.confidence_score is Confidence.HIGH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor_name", " \t"),
        ("category", " "),
        ("total_amount", "0"),
        ("total_amount", "-1"),
        ("vat_amount", "-0.01"),
        ("vat_amount", "100.51"),
        ("confidence_score", "high"),
        ("date", "2026-02-30"),
        ("total_amount", 1.2),
        ("vat_amount", 1.0),
        ("total_amount", True),
        ("total_amount", "1.001"),
        ("total_amount", "10000000000.00"),
        ("total_amount", "NaN"),
        ("total_amount", "Infinity"),
        ("vat_amount", "NaN"),
    ],
)
def test_invalid_extraction(extraction, field, value):
    with pytest.raises(ValidationError):
        ReceiptExtraction(**{**extraction, field: value})


@pytest.mark.parametrize("confidence", ["High", "Medium", "Low"])
def test_all_confidence_values(extraction, confidence):
    assert ReceiptExtraction(**{**extraction, "confidence_score": confidence}).confidence_score


def test_optional_fields_and_arbitrary_category(extraction):
    extraction.pop("vat_amount")
    extraction.pop("category")
    assert ReceiptExtraction(**extraction).vat_amount is None
    assert ReceiptExtraction(**extraction).category is None
    assert ReceiptExtraction(**extraction, category="Custom expense").category == "Custom expense"


@pytest.mark.parametrize("amount", [1, "1", Decimal("1"), "9999999999.99"])
def test_amount_scale(extraction, amount):
    model = ReceiptExtraction(**{**extraction, "total_amount": amount, "vat_amount": "0"})
    assert model.total_amount.as_tuple().exponent == -2


def test_json_money_is_exact(extraction):
    model = ReceiptExtraction(**extraction)
    assert ReceiptExtraction.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        ReceiptExtraction.model_validate_json(model.model_dump_json().replace('"100.50"', "100.50"))


def test_extra_fields_rejected(extraction):
    with pytest.raises(ValidationError):
        ReceiptExtraction(**extraction, unexpected="ignored?")


@pytest.mark.parametrize("state", list(WorkflowState))
def test_valid_pending(pending, state):
    model = PendingReceipt(**{**pending, "state": state})
    assert model.receipt_date == date(2026, 9, 10)
    assert model.total_amount == Decimal("100.50")


def test_pending_category_can_be_absent(pending):
    assert PendingReceipt(**{**pending, "category": None}).category is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "queued"),
        ("total_amount", "0"),
        ("vat_amount", "101"),
        ("normalized_vendor_name", "different"),
        ("image_storage_key", " "),
        ("telegram_chat_id", 2**63),
        ("telegram_user_id", True),
        ("created_at", datetime(2026, 1, 1)),
    ],
)
def test_invalid_pending(pending, field, value):
    with pytest.raises(ValidationError):
        PendingReceipt(**{**pending, field: value})


def test_expiry_and_completed_category(pending):
    with pytest.raises(ValidationError):
        PendingReceipt(**{**pending, "expires_at": pending["created_at"]})
    with pytest.raises(ValidationError):
        PendingReceipt(**{**pending, "state": "completed", "category": None})


def test_final_receipt_requires_category(pending):
    for key in ("state", "expires_at", "telegram_user_id"):
        pending.pop(key)
    assert Receipt(**pending).category == "Maintenance"
    with pytest.raises(ValidationError):
        Receipt(**{**pending, "category": None})
