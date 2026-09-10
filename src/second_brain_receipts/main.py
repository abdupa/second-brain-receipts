"""Explicit ASGI factory. Import has no configuration, network or registration side effects."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from second_brain_receipts.api.telegram import telegram_router
from second_brain_receipts.core.config import Settings
from second_brain_receipts.core.logging import configure_logging
from second_brain_receipts.providers.openai_vision import (
    OpenAIReceiptVisionProvider,
    create_openai_client,
)
from second_brain_receipts.repositories.supabase import (
    SupabasePendingWorkflowRepository,
    SupabaseReceiptRepository,
    SupabaseVendorRepository,
)
from second_brain_receipts.repositories.supabase_client import open_supabase_client
from second_brain_receipts.repositories.telegram_updates import SupabaseUpdateRepository
from second_brain_receipts.services.image_service import ImageService
from second_brain_receipts.services.pending_workflow import PendingWorkflow
from second_brain_receipts.services.receipt_processor import ReceiptProcessor
from second_brain_receipts.services.telegram_service import (
    TelegramIngressService,
)
from second_brain_receipts.storage.s3 import open_s3_storage
from second_brain_receipts.telegram.client import open_telegram_client


def create_app(
    settings: Settings | None = None, ingress: TelegramIngressService | None = None
) -> FastAPI:
    settings = settings if settings is not None else Settings()
    configure_logging(settings)
    if settings.telegram_allowed_user_id is None:
        raise ValueError("Telegram user authorization must be configured")
    current = ingress

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal current
        if ingress is not None:
            yield
            return
        async with (
            open_supabase_client(settings) as database,
            open_telegram_client(settings) as api,
            create_openai_client(settings) as vision_client,
            open_s3_storage(settings) as storage,
        ):
            current = TelegramIngressService(
                settings,
                SupabaseUpdateRepository(database),
                api,
                ReceiptProcessor(
                    settings,
                    ImageService(settings),
                    OpenAIReceiptVisionProvider(vision_client, settings),
                    SupabaseVendorRepository(database),
                    SupabaseReceiptRepository(database),
                    storage,
                    api,
                    PendingWorkflow(
                        settings, SupabasePendingWorkflowRepository(database), storage, api
                    ),
                ),
            )
            try:
                yield
            finally:
                current = None

    def service() -> TelegramIngressService:
        if current is None:
            raise RuntimeError("Application lifespan has not started")
        return current

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(telegram_router(settings, service))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
