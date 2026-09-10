"""One backend Supabase client per application lifetime, with explicit HTTP cleanup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from supabase import AsyncClient, create_async_client
from supabase.lib.client_options import AsyncClientOptions

from second_brain_receipts.core.config import Settings
from second_brain_receipts.repositories.errors import (
    InvalidPersistenceResponseError,
    PersistenceError,
    PersistenceUnavailableError,
)


async def _require_table_response(response: httpx.Response) -> None:
    if response.status_code >= 500 or response.status_code in (408, 429):
        raise PersistenceUnavailableError()
    # PostgREST SDK otherwise treats an empty HTTP body as an empty result list.
    if response.is_success and response.request.url.path.startswith("/rest/v1/"):
        await response.aread()
        if not response.content:
            raise InvalidPersistenceResponseError()


@asynccontextmanager
async def open_supabase_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[AsyncClient]:
    if settings.supabase_url is None or settings.supabase_service_role_key is None:
        raise PersistenceError()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=20.0,
        event_hooks={"response": [_require_table_response]},
    ) as http:
        client = await create_async_client(
            str(settings.supabase_url),
            settings.supabase_service_role_key.get_secret_value(),
            options=AsyncClientOptions(
                httpx_client=http,
                auto_refresh_token=False,
                persist_session=False,
            ),
        )
        yield client
