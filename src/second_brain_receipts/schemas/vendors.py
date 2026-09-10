"""Validated vendor write and persisted row models."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, model_validator

from second_brain_receipts.domain.category import Category
from second_brain_receipts.domain.vendor import normalize_vendor_name
from second_brain_receipts.schemas.receipts import Text, ValidatedModel


class VendorWrite(ValidatedModel):
    normalized_name: Text
    display_name: Text
    category: Category

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if self.normalized_name != normalize_vendor_name(self.display_name):
            raise ValueError("normalized vendor identity does not match display name")
        return self


class Vendor(VendorWrite):
    id: UUID
    created_at: AwareDatetime
