"""Narrow persistence operations, not workflow decisions or transactions."""

from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from second_brain_receipts.schemas.pending import CloseReason, PendingResult
from second_brain_receipts.schemas.receipts import PendingReceipt, Receipt, WorkflowState
from second_brain_receipts.schemas.vendors import Vendor, VendorWrite


class VendorRepository(Protocol):
    async def find_by_normalized_name(self, normalized_name: str) -> Vendor | None: ...
    async def upsert_vendor(self, vendor: VendorWrite) -> Vendor: ...


class ReceiptRepository(Protocol):
    async def find_duplicate(
        self,
        normalized_vendor_name: str,
        receipt_date: date,
        total_amount: Decimal,
    ) -> Receipt | None: ...
    async def create_receipt(self, receipt: Receipt) -> Receipt: ...


class PendingReceiptRepository(Protocol):
    async def create_pending(self, receipt: PendingReceipt) -> PendingReceipt: ...
    async def get_by_id(self, receipt_id: UUID) -> PendingReceipt | None: ...
    async def get_awaiting_for_chat_user(
        self,
        chat_id: int,
        user_id: int,
    ) -> PendingReceipt | None: ...
    async def update_state(
        self,
        receipt_id: UUID,
        expected_state: WorkflowState,
        new_state: WorkflowState,
        *,
        category: str | None = None,
    ) -> PendingReceipt: ...


class PendingWorkflowRepository(PendingReceiptRepository, Protocol):
    async def complete(
        self, pending_id: UUID, chat_id: int, user_id: int, category: str
    ) -> "PendingResult": ...
    async def close(
        self, pending_id: UUID, chat_id: int, user_id: int, reason: "CloseReason"
    ) -> "PendingResult": ...
