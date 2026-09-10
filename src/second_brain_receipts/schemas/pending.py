"""Typed database outcomes, with exact-decimal nested domain rows."""

from typing import Literal, Self

from pydantic import Field, model_validator

from second_brain_receipts.schemas.receipts import PendingReceipt, Receipt, ValidatedModel

CloseReason = Literal["cancelled", "expired", "duplicate"]


class PendingResult(ValidatedModel):
    outcome: Literal["completed", "inactive", "expired", "cancelled", "duplicate"]
    pending: PendingReceipt | None = None
    receipt: Receipt | None = None
    cleanup_allowed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def check_result(self) -> Self:
        if self.outcome == "inactive":
            if self.pending is not None or self.receipt is not None or self.cleanup_allowed:
                raise ValueError("inactive result cannot expose work")
            return self
        if self.pending is None:
            raise ValueError("terminal result requires pending row")
        expected = "failed" if self.outcome == "duplicate" else self.outcome
        if self.pending.state != expected:
            raise ValueError("pending state must match outcome")
        if self.outcome == "completed":
            if self.receipt is None or self.cleanup_allowed:
                raise ValueError("completion requires receipt without cleanup")
            authoritative = self.pending.model_dump(
                exclude={"telegram_user_id", "state", "expires_at", "created_at"}
            )
            if self.receipt.model_dump(exclude={"created_at"}) != authoritative:
                raise ValueError("receipt must match authoritative pending values")
            if (
                self.receipt.id != self.pending.id
                or self.receipt.image_storage_uri != self.pending.image_storage_uri
                or self.receipt.category != self.pending.category
            ):
                raise ValueError("completion identity mismatch")
        elif self.receipt is not None:
            raise ValueError("noncompletion cannot return receipt")
        return self
