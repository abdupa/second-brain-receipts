"""Supabase/PostgREST repositories. Each write is one statement, not workflow completion."""

from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import httpx
from postgrest import APIError
from postgrest._async.request_builder import AsyncQueryRequestBuilder, AsyncRPCFilterRequestBuilder
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from supabase import AsyncClient

from second_brain_receipts.core.retry import retry
from second_brain_receipts.domain.category import validate_category
from second_brain_receipts.repositories.errors import (
    AmbiguousPersistenceError,
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PendingExpiredError,
    PendingReceiptConflictError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.schemas.pending import CloseReason, PendingResult
from second_brain_receipts.schemas.receipts import (
    Money,
    PendingReceipt,
    Receipt,
    Text,
    WorkflowState,
)
from second_brain_receipts.schemas.vendors import Vendor, VendorWrite


def _columns(model: type[BaseModel]) -> str:
    # PostgreSQL emits exact strings, bypassing the SDK's default JSON float decoder.
    return ",".join(
        f"{name}::text" if name in ("total_amount", "vat_amount") else name
        for name in model.model_fields
    )


async def _read[T: BaseModel](query: AsyncQueryRequestBuilder, model: type[T]) -> list[T]:
    return await retry(
        lambda: _rows(query, model),
        retryable=lambda exc: isinstance(exc, PersistenceUnavailableError),
    )


async def _rows[T: BaseModel](
    query: AsyncQueryRequestBuilder | AsyncRPCFilterRequestBuilder,
    model: type[T],
) -> list[T]:
    try:
        result = await query.retry(False).execute()
        if not isinstance(result.data, list):
            raise InvalidPersistenceResponseError()
        return [model.model_validate(row) for row in result.data]
    except APIError as exc:
        # A primary-key conflict is not automatically a business duplicate.
        detail = f"{exc.message} {exc.details}"
        code = str(exc.code or "")
        if code == "P0002":
            raise PendingExpiredError() from None
        if code == "23505":
            if '"receipts_vendor_date_total_key"' in detail:
                raise DuplicateReceiptPersistenceError() from None
            if '"pending_receipts_one_question_idx"' in detail:
                raise PendingReceiptConflictError() from None
        if code in {
            "503",
            "504",
            "520",
            "PGRST000",
            "PGRST001",
            "PGRST002",
            "PGRST003",
            "40001",
            "40P01",
            "53300",
            "57P01",
        } or code.startswith("08"):
            raise PersistenceUnavailableError() from None
        if code == "200":
            raise InvalidPersistenceResponseError() from None
        raise PersistenceError() from None
    except httpx.TransportError:
        raise PersistenceUnavailableError() from None
    except (ValueError, TypeError, ValidationError):
        raise InvalidPersistenceResponseError() from None


def _one[T](rows: list[T]) -> T:
    if len(rows) != 1:
        raise InvalidPersistenceResponseError()
    return rows[0]


def _optional[T](rows: list[T]) -> T | None:
    return _one(rows) if rows else None


async def _insert_reconciled[T: BaseModel](
    write: Callable[[], Awaitable[T]],
    read: Callable[[], Awaitable[T | None]],
    expected: T,
) -> T:
    uncertain = False

    async def attempt() -> T:
        nonlocal uncertain
        try:
            saved = await write()
            if saved != expected:
                raise InvalidPersistenceResponseError()
            return saved
        except PersistenceError as exc:
            if not uncertain and not isinstance(
                exc, (PersistenceUnavailableError, InvalidPersistenceResponseError)
            ):
                raise
            uncertain = True
            try:
                found = await read()
            except PersistenceError:
                raise AmbiguousPersistenceError() from None
            if found == expected:
                return expected
            if found is not None or not isinstance(exc, PersistenceUnavailableError):
                raise AmbiguousPersistenceError() from None
            raise PersistenceUnavailableError() from None

    try:
        return await retry(
            attempt,
            retryable=lambda exc: type(exc) is PersistenceUnavailableError,
        )
    except PersistenceUnavailableError:
        raise AmbiguousPersistenceError() from None


class SupabaseVendorRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def find_by_normalized_name(self, normalized_name: str) -> Vendor | None:
        query = (
            self._client.table("vendors")
            .select(_columns(Vendor))
            .eq(
                "normalized_name",
                normalized_name,
            )
            .limit(2)
        )
        return _optional(await _read(query, Vendor))

    async def upsert_vendor(self, vendor: VendorWrite) -> Vendor:
        vendor = VendorWrite.model_validate(vendor.model_dump())
        query = (
            self._client.table("vendors")
            .upsert(
                vendor.model_dump(mode="json"),
                on_conflict="normalized_name",
            )
            .select(_columns(Vendor))
        )
        return _one(await _rows(query, Vendor))


class SupabaseReceiptRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def find_duplicate(
        self,
        normalized_vendor_name: str,
        receipt_date: date,
        total_amount: Decimal,
    ) -> Receipt | None:
        amount: Decimal = TypeAdapter(Annotated[Money, Field(gt=0)]).validate_python(total_amount)
        query = (
            self._client.table("receipts")
            .select(_columns(Receipt))
            .eq(
                "normalized_vendor_name",
                normalized_vendor_name,
            )
            .eq("receipt_date", receipt_date.isoformat())
            .eq(
                "total_amount",
                format(amount, ".2f"),
            )
            .limit(2)
        )
        return _optional(await _read(query, Receipt))

    async def create_receipt(self, receipt: Receipt) -> Receipt:
        receipt = Receipt.model_validate(receipt.model_dump())
        query = (
            self._client.table("receipts")
            .insert(
                receipt.model_dump(mode="json"),
            )
            .select(_columns(Receipt))
        )
        return await _insert_reconciled(
            lambda: self._insert_receipt(query),
            lambda: self.find_duplicate(
                receipt.normalized_vendor_name, receipt.receipt_date, receipt.total_amount
            ),
            receipt,
        )

    async def _insert_receipt(self, query: AsyncQueryRequestBuilder) -> Receipt:
        return _one(await _rows(query, Receipt))

    async def get_by_id(self, receipt_id: UUID) -> Receipt | None:
        query = (
            self._client.table("receipts")
            .select(_columns(Receipt))
            .eq("id", str(receipt_id))
            .limit(2)
        )
        return _optional(await _read(query, Receipt))


class SupabasePendingReceiptRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def create_pending(self, receipt: PendingReceipt) -> PendingReceipt:
        receipt = PendingReceipt.model_validate(receipt.model_dump())
        query = (
            self._client.table("pending_receipts")
            .insert(
                receipt.model_dump(mode="json"),
            )
            .select(_columns(PendingReceipt))
        )
        return await _insert_reconciled(
            lambda: self._insert_pending(query), lambda: self.get_by_id(receipt.id), receipt
        )

    async def _insert_pending(self, query: AsyncQueryRequestBuilder) -> PendingReceipt:
        return _one(await _rows(query, PendingReceipt))

    async def get_by_id(self, receipt_id: UUID) -> PendingReceipt | None:
        query = (
            self._client.table("pending_receipts")
            .select(_columns(PendingReceipt))
            .eq(
                "id",
                str(receipt_id),
            )
            .limit(2)
        )
        return _optional(await _read(query, PendingReceipt))

    async def get_awaiting_for_chat_user(self, chat_id: int, user_id: int) -> PendingReceipt | None:
        query = (
            self._client.table("pending_receipts")
            .select(_columns(PendingReceipt))
            .eq(
                "telegram_chat_id",
                chat_id,
            )
            .eq("telegram_user_id", user_id)
            .eq("state", WorkflowState.AWAITING_CATEGORY.value)
            .limit(2)
        )
        # Expiry is returned as data; orchestration owns the expiry decision.
        return _optional(await _read(query, PendingReceipt))

    async def update_state(
        self,
        receipt_id: UUID,
        expected_state: WorkflowState,
        new_state: WorkflowState,
        *,
        category: str | None = None,
    ) -> PendingReceipt:
        values = {"state": new_state.value}
        if category is not None:
            values["category"] = TypeAdapter(Text).validate_python(category)
        query = (
            self._client.table("pending_receipts")
            .update(values)
            .eq(
                "id",
                str(receipt_id),
            )
            .eq("state", expected_state.value)
            .select(_columns(PendingReceipt))
        )
        rows = await _rows(query, PendingReceipt)
        if not rows:
            raise PendingReceiptConflictError()
        return _one(rows)


class SupabasePendingWorkflowRepository(SupabasePendingReceiptRepository):
    async def complete(
        self, pending_id: UUID, chat_id: int, user_id: int, category: str
    ) -> PendingResult:
        category = validate_category(category)
        uncertain = False

        async def attempt() -> PendingResult:
            nonlocal uncertain
            try:
                result = _one(
                    await _rows(
                        self._client.rpc(
                            "complete_pending_receipt",
                            {
                                "p_pending_id": str(pending_id),
                                "p_chat_id": chat_id,
                                "p_user_id": user_id,
                                "p_category": category,
                            },
                        ),
                        PendingResult,
                    )
                )
                result = self._verify_result(result, pending_id, chat_id, user_id)
                if not uncertain or result.outcome != "inactive":
                    return result
            except PersistenceUnavailableError:
                # Only a genuine availability failure means we don't know whether the
                # RPC's write committed. A response that was actually received but is
                # malformed, mismatched or unsafe (InvalidPersistenceResponseError) is a
                # deterministic contract violation, not a retryable/ambiguous outcome:
                # let it propagate immediately instead of masking it as "ambiguous".
                uncertain = True
            if uncertain:
                try:
                    pending = await self.get_by_id(pending_id)
                    owned = (
                        pending is not None
                        and pending.telegram_chat_id == chat_id
                        and pending.telegram_user_id == user_id
                    )
                    if not owned:
                        raise AmbiguousPersistenceError()
                    assert pending is not None
                    if pending.state == WorkflowState.COMPLETED:
                        receipts = SupabaseReceiptRepository(self._client)
                        vendors = SupabaseVendorRepository(self._client)
                        receipt = await receipts.get_by_id(pending_id)
                        vendor = await vendors.find_by_normalized_name(
                            pending.normalized_vendor_name
                        )
                        memory_matches = vendor is not None and vendor.category == pending.category
                        if receipt is None or not memory_matches:
                            raise AmbiguousPersistenceError()
                        try:
                            return PendingResult(
                                outcome="completed", pending=pending, receipt=receipt
                            )
                        except ValidationError:
                            raise AmbiguousPersistenceError() from None
                    if pending.state == WorkflowState.AWAITING_CATEGORY:
                        raise PersistenceUnavailableError()
                except PersistenceError as exc:
                    if type(exc) is PersistenceUnavailableError:
                        raise
                    raise AmbiguousPersistenceError() from None
            raise AmbiguousPersistenceError()

        try:
            return await retry(
                attempt, retryable=lambda exc: type(exc) is PersistenceUnavailableError
            )
        except PersistenceUnavailableError:
            raise AmbiguousPersistenceError() from None

    async def close(
        self, pending_id: UUID, chat_id: int, user_id: int, reason: CloseReason
    ) -> PendingResult:
        if reason not in ("cancelled", "expired", "duplicate"):
            raise ValueError("invalid close reason")
        result = _one(
            await _rows(
                self._client.rpc(
                    "close_pending_receipt",
                    {
                        "p_pending_id": str(pending_id),
                        "p_chat_id": chat_id,
                        "p_user_id": user_id,
                        "p_reason": reason,
                    },
                ),
                PendingResult,
            )
        )
        return self._verify_result(result, pending_id, chat_id, user_id)

    @staticmethod
    def _verify_result(
        result: PendingResult, pending_id: UUID, chat_id: int, user_id: int
    ) -> PendingResult:
        if result.pending is not None and (
            result.pending.id != pending_id
            or result.pending.telegram_chat_id != chat_id
            or result.pending.telegram_user_id != user_id
        ):
            raise InvalidPersistenceResponseError()
        return result
