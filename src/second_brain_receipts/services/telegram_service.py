"""Authorization, durable claiming and transport normalization; no receipt rules."""

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal, Protocol

from second_brain_receipts.core.config import Settings
from second_brain_receipts.domain.ingress import (
    IncomingEvent,
    IncomingPhotoMessage,
    IncomingTextMessage,
    UnsupportedMessage,
)
from second_brain_receipts.repositories.telegram_updates import UpdateRepository
from second_brain_receipts.schemas.telegram import TelegramUpdate
from second_brain_receipts.services.receipt_messages import SYSTEM_FAILURE
from second_brain_receipts.services.receipt_processor import ReceiptProcessingError, stage_timing
from second_brain_receipts.telegram.client import TelegramClient
from second_brain_receipts.telegram.errors import TelegramDownloadError, TelegramError

logger = logging.getLogger(__name__)
IngressOutcome = Literal["handled", "unsupported", "ignored", "duplicate", "failed"]


class IngressHandlingError(Exception):
    def __init__(self) -> None:
        super().__init__("ingress_handler_failed")


class IngressHandler(Protocol):
    async def before_photo(self, chat_id: int, user_id: int, update_id: int) -> bool: ...

    async def accept(self, event: IncomingEvent) -> None: ...


class TelegramIngressService:
    def __init__(
        self,
        settings: Settings,
        updates: UpdateRepository,
        telegram: TelegramClient,
        handler: IngressHandler,
    ) -> None:
        if settings.telegram_allowed_user_id is None:
            raise ValueError("Telegram user authorization must be configured")
        self._settings = settings
        self._updates = updates
        self._telegram = telegram
        self._handler = handler

    async def handle(self, update: TelegramUpdate) -> IngressOutcome:
        started = perf_counter()
        outcome = "interrupted"
        try:
            result = await self._handle(update)
            outcome = result
            return result
        finally:
            duration = round((perf_counter() - started) * 1000, 3)
            logger.info(
                "telegram_processing update_id=%d outcome=%s total_processing_ms=%.3f",
                update.update_id,
                outcome,
                duration,
                extra={
                    "update_id": update.update_id,
                    "outcome": outcome,
                    "total_processing_ms": duration,
                },
            )

    async def _handle(self, update: TelegramUpdate) -> IngressOutcome:
        message = update.message
        if message is None:
            return "unsupported"
        if (
            message.sender is None
            or message.sender.id != self._settings.telegram_allowed_user_id
            or message.chat.type != "private"
            or message.chat.id != message.sender.id
        ):
            return "ignored"
        if not await self._updates.claim_update(update.update_id):
            return "duplicate"
        outcome: IngressOutcome = "handled"
        try:
            received_at = datetime.now(UTC)
            event: IncomingEvent
            common = (
                update.update_id,
                message.chat.id,
                message.sender.id,
                message.message_id,
                received_at,
            )
            if message.photo and await self._handler.before_photo(
                message.chat.id, message.sender.id, update.update_id
            ):
                await self._updates.finish_update(update.update_id, "handled")
                return "handled"
            if message.photo:
                suitable = [
                    photo
                    for photo in message.photo
                    if photo.file_size is None or photo.file_size <= self._settings.max_upload_bytes
                ]
                if not suitable:
                    raise TelegramDownloadError()
                photo = max(
                    suitable, key=lambda item: (item.width * item.height, item.file_size or 0)
                )
                with stage_timing(update.update_id, "telegram_download"):
                    async with asyncio.timeout(self._settings.telegram_timeout_seconds):
                        path = await self._telegram.get_file(photo.file_id)
                        data = await self._telegram.download_file(path)
                event = IncomingPhotoMessage(*common, photo.file_id, data)
            elif message.text is not None and not message.text.lstrip().startswith("/"):
                event = IncomingTextMessage(*common, message.text)
            else:
                event = UnsupportedMessage(*common)
                outcome = "unsupported"
            await self._handler.accept(event)
        except ReceiptProcessingError as exc:
            await self._updates.finish_update(update.update_id, "failed")
            logger.warning(
                "telegram_receipt_failed",
                extra={"update_id": update.update_id, "error_code": exc.code},
            )
            return "failed"
        except (TelegramError, TimeoutError):
            try:
                await self._telegram.send_message(message.chat.id, SYSTEM_FAILURE)
            except TelegramError:
                logger.warning(
                    "telegram_download_notification_failed", extra={"update_id": update.update_id}
                )
            await self._updates.finish_update(update.update_id, "failed")
            logger.warning("telegram_ingress update_id=%d outcome=failed", update.update_id)
            return "failed"
        except Exception:
            # Future handler failures remain retry-ineligible claims too. Preserve
            # cancellation (BaseException) and never log exception/provider payloads.
            await self._updates.finish_update(update.update_id, "failed")
            logger.error("telegram_ingress update_id=%d outcome=handler_failed", update.update_id)
            raise IngressHandlingError() from None
        await self._updates.finish_update(update.update_id, "handled")
        logger.info("telegram_ingress update_id=%d outcome=%s", update.update_id, outcome)
        return outcome
