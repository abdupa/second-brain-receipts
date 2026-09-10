"""Persistent pause/resume; each invocation finishes without waiting for a reply."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from second_brain_receipts.core.config import Settings
from second_brain_receipts.domain.category import validate_category
from second_brain_receipts.domain.ingress import IncomingPhotoMessage, IncomingTextMessage
from second_brain_receipts.domain.vendor import normalize_vendor_name
from second_brain_receipts.repositories.errors import (
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PendingExpiredError,
    PendingReceiptConflictError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.repositories.protocols import PendingWorkflowRepository
from second_brain_receipts.schemas.pending import PendingResult
from second_brain_receipts.schemas.receipts import PendingReceipt, ReceiptExtraction, WorkflowState
from second_brain_receipts.services.image_service import ProcessedImage
from second_brain_receipts.services.receipt_messages import (
    CANCELLED,
    EXPIRED,
    INVALID_CATEGORY,
    MARKDOWN_V2,
    NO_PENDING,
    SYSTEM_FAILURE,
    WAITING,
    category_prompt,
    duplicate_summary,
    remembered_summary,
)
from second_brain_receipts.services.receipt_processor import (
    ReceiptNotificationFailure,
    ReceiptPersistenceFailure,
    ReceiptProcessingError,
    ReceiptStorageFailure,
    stage_timing,
)
from second_brain_receipts.storage.base import ReceiptImageStorage, StorageError
from second_brain_receipts.telegram.client import TelegramClient
from second_brain_receipts.telegram.errors import TelegramError

logger = logging.getLogger(__name__)


class PendingWorkflow:
    def __init__(
        self,
        settings: Settings,
        pending: PendingWorkflowRepository,
        storage: ReceiptImageStorage,
        telegram: TelegramClient,
    ) -> None:
        self._settings, self._pending = settings, pending
        self._storage, self._telegram = storage, telegram

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        committed: bool = False,
        parse_mode: Literal["MarkdownV2"] | None = None,
    ) -> None:
        kwargs = {"parse_mode": parse_mode} if parse_mode is not None else {}
        try:
            await self._telegram.send_message(chat_id, text, **kwargs)
        except TelegramError as exc:
            raise ReceiptNotificationFailure(exc, committed=committed) from None

    async def _report(self, chat_id: int, error: ReceiptProcessingError) -> None:
        if (
            error.user_notified
            or error.notification_failed
            or isinstance(error, ReceiptNotificationFailure)
        ):
            return
        try:
            await self._telegram.send_message(chat_id, SYSTEM_FAILURE)
            error.user_notified = True
        except TelegramError:
            error.notification_failed = True

    async def before_photo(self, chat_id: int, user_id: int, update_id: int) -> bool:
        """True blocks the photo, including expiry notification; user can resend."""
        try:
            row = await self._pending.get_awaiting_for_chat_user(chat_id, user_id)
            if row is None:
                return False
            if row.expires_at <= datetime.now(UTC):
                result = await self._pending.close(row.id, chat_id, user_id, "expired")
                await self._terminal(result, chat_id, update_id)
            else:
                await self._send(chat_id, WAITING)
            return True
        except PersistenceError as exc:
            error = ReceiptPersistenceFailure(exc)
            await self._report(chat_id, error)
            raise error from None

    async def create(
        self, event: IncomingPhotoMessage, image: ProcessedImage, extraction: ReceiptExtraction
    ) -> None:
        # Called only after confidence/vendor/duplicate gates. Recheck the slot after
        # extraction too; the DB partial unique index remains the concurrent defense.
        if await self.before_photo(event.chat_id, event.user_id, event.update_id):
            return
        try:
            with stage_timing(event.update_id, "s3_upload"):
                stored = await self._storage.store(image)
        except StorageError as exc:
            raise ReceiptStorageFailure(exc, object_key=exc.object_key, uncertain=True) from None
        now = datetime.now(UTC)
        try:
            row = PendingReceipt(
                id=uuid4(),
                telegram_chat_id=event.chat_id,
                telegram_user_id=event.user_id,
                vendor_name=extraction.vendor_name,
                normalized_vendor_name=normalize_vendor_name(extraction.vendor_name),
                receipt_date=extraction.date,
                total_amount=extraction.total_amount,
                vat_amount=extraction.vat_amount,
                confidence_score=extraction.confidence_score,
                category=None,
                image_storage_key=stored.object_key,
                image_storage_uri=stored.storage_uri,
                state=WorkflowState.AWAITING_CATEGORY,
                created_at=now,
                expires_at=now + timedelta(minutes=self._settings.pending_receipt_ttl_minutes),
            )
        except ValidationError:
            failed = not await self._delete(stored.object_key, event.update_id)
            raise ReceiptPersistenceFailure(
                InvalidPersistenceResponseError(),
                cleanup_failed=failed,
                object_key=stored.object_key,
            ) from None
        try:
            with stage_timing(event.update_id, "pending_insert"):
                await self._pending.create_pending(row)
        except PendingReceiptConflictError as exc:
            failed = not await self._delete(stored.object_key, event.update_id)
            if failed:
                error = ReceiptPersistenceFailure(
                    exc, cleanup_failed=True, object_key=stored.object_key
                )
                try:
                    await self._telegram.send_message(event.chat_id, WAITING)
                    error.user_notified = True
                except TelegramError:
                    error.notification_failed = True
                raise error from None
            await self._send(event.chat_id, WAITING)
            return
        except (PersistenceUnavailableError, InvalidPersistenceResponseError) as exc:
            try:
                saved = await self._pending.get_by_id(row.id)
            except PersistenceError:
                saved = None
            if saved != row:
                raise ReceiptPersistenceFailure(
                    exc, uncertain=True, object_key=stored.object_key, receipt_id=row.id
                ) from None
        except PersistenceError as exc:
            failed = not await self._delete(stored.object_key, event.update_id)
            raise ReceiptPersistenceFailure(
                exc, cleanup_failed=failed, object_key=stored.object_key, receipt_id=row.id
            ) from None
        # Notification failure never deletes already-persisted work.
        await self._send(event.chat_id, category_prompt(row.vendor_name), committed=True)

    async def reply(self, event: IncomingTextMessage) -> None:
        try:
            await self._reply(event)
        except PersistenceError as exc:
            error = ReceiptPersistenceFailure(exc)
            await self._report(event.chat_id, error)
            raise error from None
        except ReceiptProcessingError as exc:
            await self._report(event.chat_id, exc)
            raise

    async def _reply(self, event: IncomingTextMessage) -> None:
        row = await self._pending.get_awaiting_for_chat_user(event.chat_id, event.user_id)
        if row is None:
            await self._send(event.chat_id, NO_PENDING)
            return
        if row.expires_at <= datetime.now(UTC):
            result = await self._pending.close(row.id, event.chat_id, event.user_id, "expired")
        elif event.text.strip().casefold() == "cancel":
            result = await self._pending.close(row.id, event.chat_id, event.user_id, "cancelled")
        else:
            try:
                category = validate_category(event.text)
            except ValidationError:
                await self._send(event.chat_id, INVALID_CATEGORY)
                return
            try:
                with stage_timing(event.update_id, "pending_completion"):
                    result = await self._pending.complete(
                        row.id, event.chat_id, event.user_id, category
                    )
            except DuplicateReceiptPersistenceError:
                # Separate transaction, after the completion attempt rolled back.
                result = await self._pending.close(
                    row.id, event.chat_id, event.user_id, "duplicate"
                )
            except PendingExpiredError:
                result = await self._pending.close(row.id, event.chat_id, event.user_id, "expired")
        if result.outcome == "completed":
            assert result.receipt is not None  # Validated by PendingResult.
            await self._send(
                event.chat_id,
                remembered_summary(result.receipt, self._settings.default_currency),
                committed=True,
                parse_mode=MARKDOWN_V2,
            )
        else:
            await self._terminal(result, event.chat_id, event.update_id)

    async def _terminal(self, result: PendingResult, chat_id: int, update_id: int) -> None:
        if result.outcome == "inactive":
            await self._send(chat_id, NO_PENDING)
            return
        row = result.pending
        assert row is not None
        failed = result.cleanup_allowed and not await self._delete(row.image_storage_key, update_id)
        if result.outcome == "duplicate":
            extraction = ReceiptExtraction(
                vendor_name=row.vendor_name,
                date=row.receipt_date,
                total_amount=row.total_amount,
                vat_amount=row.vat_amount,
                confidence_score=row.confidence_score,
            )
            message = duplicate_summary(extraction, self._settings.default_currency)
        else:
            message = EXPIRED if result.outcome == "expired" else CANCELLED
        if failed:
            error = ReceiptStorageFailure(
                StorageError(),
                cleanup_failed=True,
                object_key=row.image_storage_key,
                receipt_id=row.id,
            )
            try:
                await self._telegram.send_message(chat_id, message)
                error.user_notified = True
            except TelegramError:
                error.notification_failed = True
            raise error
        await self._send(chat_id, message)

    async def _delete(self, key: str, update_id: int) -> bool:
        try:
            await self._storage.delete(key)
            return True
        except StorageError:
            logger.warning("pending_cleanup_failed update_id=%d", update_id)
            return False
