"""Authenticate before streaming a bounded body; keep receipt logic out of HTTP."""

import asyncio
import hmac
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from second_brain_receipts.core.config import Settings
from second_brain_receipts.repositories.errors import PersistenceError
from second_brain_receipts.schemas.telegram import TelegramUpdate
from second_brain_receipts.services.telegram_service import (
    IngressHandlingError,
    TelegramIngressService,
)


def telegram_router(settings: Settings, service: Callable[[], TelegramIngressService]) -> APIRouter:
    if settings.telegram_webhook_secret is None:
        raise ValueError("Telegram webhook secret must be configured")
    secret = settings.telegram_webhook_secret.get_secret_value().encode()
    router = APIRouter()

    @router.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> dict[str, str]:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode()
        if not hmac.compare_digest(supplied, secret):
            raise HTTPException(401, "unauthorized")
        limit = settings.telegram_webhook_max_bytes
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdecimal():
                raise HTTPException(400, "invalid request")
            if len(length) > 20 or int(length) > limit:
                raise HTTPException(413, "request too large")
        body = bytearray()
        try:
            async with asyncio.timeout(settings.telegram_timeout_seconds):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > limit:
                        raise HTTPException(413, "request too large")
                    body.extend(chunk)
            update = TelegramUpdate.model_validate_json(body)
        except (ValidationError, ValueError, ClientDisconnect):
            raise HTTPException(400, "invalid update") from None
        except TimeoutError:
            raise HTTPException(408, "request timeout") from None
        try:
            outcome = await service().handle(update)
        except PersistenceError:
            # Includes failed status writes: never disguise database failure as a duplicate.
            raise HTTPException(503, "ingress unavailable") from None
        except IngressHandlingError:
            raise HTTPException(500, "ingress failed") from None
        return {"status": outcome}

    return router
