"""Known-vendor application workflow with explicit compensation, not a distributed transaction."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from second_brain_receipts.core.config import Settings
from second_brain_receipts.domain.ingress import (
    IncomingEvent,
    IncomingPhotoMessage,
    IncomingTextMessage,
)
from second_brain_receipts.domain.vendor import normalize_vendor_name
from second_brain_receipts.providers.vision import (
    ReceiptExtractionError,
    ReceiptVisionProvider,
    VisionProviderError,
)
from second_brain_receipts.repositories.errors import (
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.repositories.protocols import ReceiptRepository, VendorRepository
from second_brain_receipts.schemas.receipts import Confidence, Receipt, ReceiptExtraction
from second_brain_receipts.services.image_service import ImageValidationError, ProcessedImage
from second_brain_receipts.services.receipt_messages import (
    MARKDOWN_V2,
    SYSTEM_FAILURE,
    UNREADABLE,
    UNSUPPORTED,
    duplicate_summary,
    success_summary,
)
from second_brain_receipts.storage.base import ReceiptImageStorage, StorageError
from second_brain_receipts.telegram.client import TelegramClient
from second_brain_receipts.telegram.errors import TelegramError

logger = logging.getLogger(__name__)


class ImageProcessor(Protocol):
    def process(self, raw_bytes: bytes) -> ProcessedImage: ...


class ProcessingOutcome(StrEnum):
    SAVED = "saved"
    DUPLICATE = "duplicate"
    UNREADABLE = "unreadable"
    AWAITING_CATEGORY = "awaiting_category"


class ReceiptProcessingError(Exception):
    """Safe typed failure; operational metadata is available without logging content."""

    code = "receipt_processing_failed"

    def __init__(
        self,
        failure: Exception,
        *,
        cleanup_failed: bool = False,
        object_key: str | None = None,
        receipt_id: UUID | None = None,
        uncertain: bool = False,
        committed: bool = False,
        user_notified: bool = False,
    ) -> None:
        super().__init__(self.code)
        self.failure = failure
        self.cleanup_failed = cleanup_failed
        self.object_key = object_key
        self.receipt_id = receipt_id
        self.uncertain = uncertain
        self.committed = committed
        self.user_notified = user_notified
        self.notification_failed = False


class ReceiptVisionFailure(ReceiptProcessingError):
    code = "receipt_vision_failed"


class ReceiptVendorFailure(ReceiptProcessingError):
    code = "receipt_vendor_lookup_failed"


class ReceiptDuplicateLookupFailure(ReceiptProcessingError):
    code = "receipt_duplicate_lookup_failed"


class ReceiptStorageFailure(ReceiptProcessingError):
    code = "receipt_storage_failed"


class ReceiptPersistenceFailure(ReceiptProcessingError):
    code = "receipt_persistence_failed"


class ReceiptNotificationFailure(ReceiptProcessingError):
    code = "receipt_notification_failed"


@contextmanager
def stage_timing(update_id: int, stage: str) -> Iterator[None]:
    start = perf_counter()
    try:
        yield
    finally:
        duration = round((perf_counter() - start) * 1000, 3)
        logger.info(
            "receipt_stage update_id=%d stage=%s duration_ms=%.3f",
            update_id,
            stage,
            duration,
            extra={
                "update_id": update_id,
                "stage": stage,
                "duration_ms": duration,
            },
        )


class PendingHandler(Protocol):
    async def before_photo(self, chat_id: int, user_id: int, update_id: int) -> bool: ...
    async def create(
        self, event: IncomingPhotoMessage, image: ProcessedImage, extraction: ReceiptExtraction
    ) -> None: ...
    async def reply(self, event: IncomingTextMessage) -> None: ...


class ReceiptProcessor:
    def __init__(
        self,
        settings: Settings,
        images: ImageProcessor,
        vision: ReceiptVisionProvider,
        vendors: VendorRepository,
        receipts: ReceiptRepository,
        storage: ReceiptImageStorage,
        telegram: TelegramClient,
        pending: PendingHandler,
    ) -> None:
        self._pending = pending
        self._currency = settings.default_currency
        self._images, self._vision = images, vision
        self._vendors, self._receipts = vendors, receipts
        self._storage, self._telegram = storage, telegram
        # Pillow warning filters are process-global in this Python runtime. Serializing
        # this app's image stage avoids overlapping its warning contexts while offloading CPU.
        self._image_lock = asyncio.Lock()

    async def before_photo(self, chat_id: int, user_id: int, update_id: int) -> bool:
        return await self._pending.before_photo(chat_id, user_id, update_id)

    async def accept(self, event: IncomingEvent) -> None:
        if isinstance(event, IncomingPhotoMessage):
            await self.process(event)
        elif isinstance(event, IncomingTextMessage):
            await self._pending.reply(event)
        else:
            await self._send(event.chat_id, UNSUPPORTED)

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        receipt_id: UUID | None = None,
        parse_mode: Literal["MarkdownV2"] | None = None,
    ) -> None:
        kwargs = {"parse_mode": parse_mode} if parse_mode is not None else {}
        try:
            await self._telegram.send_message(chat_id, text, **kwargs)
        except TelegramError as exc:
            raise ReceiptNotificationFailure(
                exc, committed=receipt_id is not None, receipt_id=receipt_id
            ) from None

    async def process(self, event: IncomingPhotoMessage) -> ProcessingOutcome:
        start = perf_counter()
        outcome = "interrupted"
        try:
            result = await self._process(event)
            outcome = result.value
            return result
        except ReceiptProcessingError as exc:
            outcome = "failed"
            logger.warning(
                "receipt_failure update_id=%d code=%s cleanup_failed=%s uncertain=%s committed=%s",
                event.update_id,
                exc.code,
                exc.cleanup_failed,
                exc.uncertain,
                exc.committed,
                extra={
                    "update_id": event.update_id,
                    "error_code": exc.code,
                    "cleanup_failed": exc.cleanup_failed,
                    "uncertain": exc.uncertain,
                    "committed": exc.committed,
                },
            )
            if (
                not isinstance(exc, ReceiptNotificationFailure)
                and not exc.user_notified
                and not exc.notification_failed
            ):
                try:
                    await self._telegram.send_message(event.chat_id, SYSTEM_FAILURE)
                except TelegramError:
                    exc.notification_failed = True
                    logger.warning(
                        "receipt_failure_notification_failed", extra={"update_id": event.update_id}
                    )
            raise
        finally:
            duration = round((perf_counter() - start) * 1000, 3)
            logger.info(
                "receipt_processing update_id=%d outcome=%s total_processing_ms=%.3f",
                event.update_id,
                outcome,
                duration,
                extra={
                    "update_id": event.update_id,
                    "outcome": outcome,
                    "total_processing_ms": duration,
                },
            )

    async def _process(self, event: IncomingPhotoMessage) -> ProcessingOutcome:
        try:
            with stage_timing(event.update_id, "image_processing"):
                async with self._image_lock:
                    worker = asyncio.create_task(
                        asyncio.to_thread(self._images.process, event.image_bytes)
                    )
                    try:
                        image = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # Keep the lock until the non-cancellable Pillow thread exits.
                        await asyncio.gather(worker, return_exceptions=True)
                        raise
            with stage_timing(event.update_id, "vision"):
                extraction = await self._vision.extract(image)
        except (ImageValidationError, ReceiptExtractionError):
            await self._send(event.chat_id, UNREADABLE)
            return ProcessingOutcome.UNREADABLE
        except VisionProviderError as exc:
            raise ReceiptVisionFailure(exc) from None
        if extraction.confidence_score == Confidence.LOW:
            await self._send(event.chat_id, UNREADABLE)
            return ProcessingOutcome.UNREADABLE
        normalized = normalize_vendor_name(extraction.vendor_name)
        try:
            with stage_timing(event.update_id, "vendor_lookup"):
                vendor = await self._vendors.find_by_normalized_name(normalized)
        except PersistenceError as exc:
            raise ReceiptVendorFailure(exc) from None
        try:
            with stage_timing(event.update_id, "duplicate_lookup"):
                duplicate = await self._receipts.find_duplicate(
                    normalized, extraction.date, extraction.total_amount
                )
        except PersistenceError as exc:
            raise ReceiptDuplicateLookupFailure(exc) from None
        if duplicate is not None:
            await self._send(event.chat_id, duplicate_summary(extraction, self._currency))
            return ProcessingOutcome.DUPLICATE
        if vendor is None:
            await self._pending.create(event, image, extraction)
            return ProcessingOutcome.AWAITING_CATEGORY
        try:
            with stage_timing(event.update_id, "s3_upload"):
                stored = await self._storage.store(image)
        except StorageError as exc:
            # Upload timeouts can leave an object even without a successful return.
            raise ReceiptStorageFailure(exc, object_key=exc.object_key, uncertain=True) from None
        receipt_id = uuid4()
        try:
            receipt = Receipt(
                id=receipt_id,
                telegram_chat_id=event.chat_id,
                vendor_name=extraction.vendor_name,
                normalized_vendor_name=normalized,
                receipt_date=extraction.date,
                total_amount=extraction.total_amount,
                vat_amount=extraction.vat_amount,
                category=vendor.category,
                confidence_score=extraction.confidence_score,
                image_storage_key=stored.object_key,
                image_storage_uri=stored.storage_uri,
                created_at=datetime.now(UTC),
            )
        except ValidationError:
            # No insert has been attempted, so cleanup is safe.
            failed = not await self._cleanup(event.update_id, stored.object_key)
            raise ReceiptPersistenceFailure(
                InvalidPersistenceResponseError(),
                cleanup_failed=failed,
                object_key=stored.object_key,
                receipt_id=receipt_id,
            ) from None
        try:
            with stage_timing(event.update_id, "database_insert"):
                await self._receipts.create_receipt(receipt)
        except DuplicateReceiptPersistenceError as exc:
            failed = not await self._cleanup(event.update_id, stored.object_key)
            if failed:
                failure = ReceiptPersistenceFailure(
                    exc, cleanup_failed=True, object_key=stored.object_key, receipt_id=receipt_id
                )
                try:
                    await self._telegram.send_message(
                        event.chat_id, duplicate_summary(extraction, self._currency)
                    )
                    failure.user_notified = True
                except TelegramError:
                    failure.notification_failed = True
                    # One failed notification attempt is enough; do not send another error.
                raise failure from None
            await self._send(event.chat_id, duplicate_summary(extraction, self._currency))
            return ProcessingOutcome.DUPLICATE
        except (PersistenceUnavailableError, InvalidPersistenceResponseError) as exc:
            # A failed response does not establish a failed transaction. One read
            # may positively identify our committed row; absence is not proof that
            # an in-flight INSERT cannot still commit. Retain the object if uncertain.
            try:
                with stage_timing(event.update_id, "insert_reconciliation"):
                    committed = await self._receipts.find_duplicate(
                        normalized, extraction.date, extraction.total_amount
                    )
            except PersistenceError:
                committed = None
            if (
                committed is None
                or committed.id != receipt.id
                or (
                    committed.image_storage_key != stored.object_key
                    or committed.image_storage_uri != stored.storage_uri
                )
            ):
                raise ReceiptPersistenceFailure(
                    exc, object_key=stored.object_key, receipt_id=receipt_id, uncertain=True
                ) from None
            receipt = committed
        except PersistenceError as exc:
            failed = not await self._cleanup(event.update_id, stored.object_key)
            raise ReceiptPersistenceFailure(
                exc, cleanup_failed=failed, object_key=stored.object_key, receipt_id=receipt_id
            ) from None
        await self._send(
            event.chat_id,
            success_summary(receipt, self._currency),
            receipt_id=receipt.id,
            parse_mode=MARKDOWN_V2,
        )
        return ProcessingOutcome.SAVED

    async def _cleanup(self, update_id: int, object_key: str) -> bool:
        try:
            with stage_timing(update_id, "s3_cleanup"):
                await self._storage.delete(object_key)
            return True
        except StorageError:
            logger.error("receipt_cleanup_failed", extra={"update_id": update_id})
            return False
