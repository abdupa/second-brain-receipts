"""Validated extraction and persisted post-extraction domain models."""

from datetime import date as Date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from second_brain_receipts.domain.category import Category
from second_brain_receipts.domain.storage import validate_storage_reference
from second_brain_receipts.domain.vendor import normalize_vendor_name

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TelegramID = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]


def reject_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("money must be Decimal, a decimal string, or an integer")
    return value


def two_places(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


Money = Annotated[
    Decimal,
    BeforeValidator(reject_float),
    Field(max_digits=12, decimal_places=2, allow_inf_nan=False, le=Decimal("9999999999.99")),
    AfterValidator(two_places),
]


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class WorkflowState(StrEnum):
    PROCESSING = "processing"
    AWAITING_CATEGORY = "awaiting_category"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ValidatedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ReceiptValues(ValidatedModel):
    vendor_name: Text
    total_amount: Annotated[Money, Field(gt=0)]
    vat_amount: Annotated[Money, Field(ge=0)] | None = None
    category: Text | None = None
    confidence_score: Confidence

    @model_validator(mode="after")
    def validate_vat(self) -> Self:
        if self.vat_amount is not None and self.vat_amount > self.total_amount:
            raise ValueError("VAT must not exceed total amount")
        return self


class ReceiptExtraction(ReceiptValues):
    date: Date


class ReceiptDomain(ReceiptValues):
    normalized_vendor_name: Text
    receipt_date: Date
    image_storage_key: Text
    image_storage_uri: Text

    @model_validator(mode="after")
    def validate_vendor_identity(self) -> Self:
        validate_storage_reference(self.image_storage_key, self.image_storage_uri)
        if self.normalized_vendor_name != normalize_vendor_name(self.vendor_name):
            raise ValueError("normalized vendor name must match vendor name")
        return self


class Receipt(ReceiptDomain):
    id: UUID
    telegram_chat_id: TelegramID
    category: Category
    created_at: AwareDatetime


class PendingReceipt(ReceiptDomain):
    """A validated extraction retained for a later category reply, not raw ingress."""

    id: UUID
    telegram_chat_id: TelegramID
    telegram_user_id: TelegramID
    category: Category | None = None
    state: WorkflowState
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expiry must be after creation")
        if self.state == WorkflowState.COMPLETED and self.category is None:
            raise ValueError("completed pending receipts require a category")
        return self
