"""Reusable, bounded Telegram HTTP adapter.

Formatting is caller-selected, not adapter policy: send_message defaults to literal
plain text (no parse_mode) and only opts into Telegram's strict MarkdownV2 when a
caller explicitly passes parse_mode. Callers that opt in are responsible for
escaping any untrusted/dynamic text first (see services.receipt_messages); this
adapter never infers or rewrites formatting itself.

Idempotent reads (getFile, download) retry transient failures under the bounded
policy in core.retry. Sending is not idempotent: a network failure or deadline mid-send
never retries transparently (it could double-send); it surfaces as
TelegramAmbiguousSendError instead. A definitive rate-limit rejection (ok=false) is
safe to retry, since it confirms nothing was sent.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, Protocol

import httpx

from second_brain_receipts.core.config import Settings
from second_brain_receipts.core.retry import retry
from second_brain_receipts.telegram.errors import (
    TelegramAmbiguousSendError,
    TelegramAuthenticationError,
    TelegramDownloadError,
    TelegramError,
    TelegramRateLimitError,
    TelegramRejectedError,
    TelegramResponseError,
    TelegramTimeoutError,
    TelegramUnavailableError,
)


class TelegramClient(Protocol):
    async def get_file(self, file_id: str) -> str: ...
    async def download_file(self, file_path: str) -> bytes: ...
    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: Literal["MarkdownV2"] | None = None
    ) -> None: ...


class _SafeHTTPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX INFO includes token-bearing URLs. Low-level tracing is intentionally
        # suppressed process-wide; it is unsuitable for this secret-bearing app.
        return (
            not record.name.startswith("httpcore") and "api.telegram.org" not in record.getMessage()
        )


def _protect_http_logs() -> None:
    for name in (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _SafeHTTPLogs) for item in logger.filters):
            logger.addFilter(_SafeHTTPLogs())


def _file_path(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,1024}", value):
        raise TelegramResponseError()
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise TelegramResponseError()
    return value


class HTTPTelegramClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        if settings.telegram_bot_token is None:
            raise TelegramAuthenticationError()
        _protect_http_logs()
        self._client = client
        self._token = settings.telegram_bot_token.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", self._token):
            raise TelegramAuthenticationError()
        self._limit = settings.max_upload_bytes
        self._timeout = settings.telegram_timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        download: bool = False,
    ) -> bytes:
        url = f"https://api.telegram.org/{'file/' if download else ''}bot{self._token}/{path}"
        limit = self._limit if download else 64 * 1024
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    method,
                    url,
                    json=body,
                    timeout=httpx.Timeout(self._timeout, connect=5),
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise TelegramAuthenticationError()
                    if download and (status == 408 or status >= 500):
                        raise TelegramUnavailableError()
                    if download and status != 200 and status != 429:
                        raise TelegramDownloadError()
                    if 300 <= status < 400:
                        raise TelegramResponseError()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise TelegramResponseError()
                    length = response.headers.get("content-length")
                    if length is not None:
                        if not length.isdecimal():
                            raise TelegramResponseError()
                        if len(length) > 20 or int(length) > limit:
                            raise TelegramDownloadError() if download else TelegramResponseError()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > limit:
                            raise TelegramDownloadError() if download else TelegramResponseError()
                        data.extend(chunk)
                    if status == 429:
                        retry_after = None
                        try:
                            payload = json.loads(data)
                            hint = payload.get("parameters", {}).get("retry_after")
                            if type(hint) is int and hint > 0:
                                retry_after = hint
                        except (ValueError, AttributeError, TypeError):
                            pass
                        raise TelegramRateLimitError(retry_after)
                    if download:
                        if not data:
                            raise TelegramDownloadError()
                        return bytes(data)
                    if status == 408 or status >= 500:
                        try:
                            rejected = json.loads(data)
                        except (ValueError, TypeError):
                            rejected = None
                        if isinstance(rejected, dict) and rejected.get("ok") is False:
                            raise TelegramRejectedError()
                        raise TelegramUnavailableError()
                    if status != 200:
                        raise TelegramResponseError()
                    return bytes(data)
        except (httpx.TimeoutException, TimeoutError):
            raise TelegramTimeoutError() from None
        except httpx.TransportError:
            raise TelegramUnavailableError() from None

    async def _api(self, method: str, body: dict[str, object]) -> dict[str, object]:
        data = await self._request("POST", method, body=body)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise TelegramResponseError()
            if payload.get("ok") is not True:
                code = payload.get("error_code")
                if code in (401, 403):
                    raise TelegramAuthenticationError()
                if code == 429:
                    parameters = payload.get("parameters")
                    hint = parameters.get("retry_after") if isinstance(parameters, dict) else None
                    raise TelegramRateLimitError(hint if type(hint) is int and hint > 0 else None)
                if type(code) is int and (code == 408 or code >= 500):
                    raise TelegramRejectedError()
                raise TelegramResponseError()
            result = payload.get("result")
            if not isinstance(result, dict):
                raise TelegramResponseError()
            return result
        except (ValueError, TypeError):
            raise TelegramResponseError() from None

    async def get_file(self, file_id: str) -> str:
        result = await retry(
            lambda: self._api("getFile", {"file_id": file_id}),
            retryable=lambda exc: isinstance(exc, TelegramError) and exc.transient,
            retry_after=lambda exc: getattr(exc, "retry_after", None),
        )
        size = result.get("file_size")
        if type(size) is int and size > self._limit:
            raise TelegramDownloadError()
        return _file_path(result.get("file_path"))

    async def download_file(self, file_path: str) -> bytes:
        path = _file_path(file_path)
        return await retry(
            lambda: self._request("GET", path, download=True),
            retryable=lambda exc: isinstance(exc, TelegramError) and exc.transient,
            retry_after=lambda exc: getattr(exc, "retry_after", None),
        )

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: Literal["MarkdownV2"] | None = None
    ) -> None:
        if not text.strip() or len(text) > 4096:
            raise TelegramResponseError()
        body: dict[str, object] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        try:
            retryable_send_errors = (TelegramRateLimitError, TelegramRejectedError)
            result = await retry(
                lambda: self._api("sendMessage", body),
                retryable=lambda exc: isinstance(exc, retryable_send_errors),
                retry_after=lambda exc: getattr(exc, "retry_after", None),
            )
        except (TelegramTimeoutError, TelegramUnavailableError) as exc:
            if isinstance(exc, TelegramRejectedError):
                raise
            raise TelegramAmbiguousSendError() from None
        if type(result.get("message_id")) is not int:
            raise TelegramResponseError()


@asynccontextmanager
async def open_telegram_client(settings: Settings) -> AsyncIterator[HTTPTelegramClient]:
    async with httpx.AsyncClient(trust_env=False) as client:
        yield HTTPTelegramClient(client, settings)
