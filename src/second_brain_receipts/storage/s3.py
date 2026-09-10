"""SSE-S3 encrypted receipt objects; boto3 I/O runs off the event loop."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from second_brain_receipts.core.config import Settings
from second_brain_receipts.core.retry import retry
from second_brain_receipts.services.image_service import ProcessedImage
from second_brain_receipts.storage.base import (
    StorageAmbiguousError,
    StorageAuthenticationError,
    StorageDeleteError,
    StorageError,
    StoragePresignError,
    StorageUnavailableError,
    StorageUploadError,
    StoredObjectInfo,
    StoredReceiptImage,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

KEY_PATTERN = re.compile(
    r"receipts/\d{4}/(?:0[1-9]|1[0-2])/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
    r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.jpg"
)


def _map_error(
    exc: BotoCoreError | ClientError, fallback: type[StorageError], key: str | None = None
) -> StorageError:
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError)):
        return StorageAuthenticationError(object_key=key)
    if isinstance(exc, (ConnectionError, ReadTimeoutError)):
        return StorageUnavailableError(object_key=key)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
            "ExpiredToken",
            "InvalidToken",
            "TokenRefreshRequired",
        }:
            return StorageAuthenticationError(object_key=key)
        if code in {"SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable"} or (
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
        ):
            return StorageUnavailableError(object_key=key)
    return fallback(object_key=key)


def _make_client(settings: Settings) -> S3Client:
    try:
        # None allows boto3's standard role/profile/environment credential chain.
        session = boto3.Session(
            aws_access_key_id=(
                settings.aws_access_key_id.get_secret_value()
                if settings.aws_access_key_id
                else None
            ),
            aws_secret_access_key=(
                settings.aws_secret_access_key.get_secret_value()
                if settings.aws_secret_access_key
                else None
            ),
            aws_session_token=(
                settings.aws_session_token.get_secret_value()
                if settings.aws_session_token
                else None
            ),
            region_name=settings.aws_region,
        )
        return session.client(
            "s3",
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=20,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
    except (BotoCoreError, ClientError) as exc:
        raise _map_error(exc, StorageError) from None


@asynccontextmanager
async def open_s3_storage(settings: Settings) -> AsyncIterator[S3ReceiptImageStorage]:
    if settings.s3_bucket_name is None or settings.aws_region is None:
        raise StorageError()
    client = await asyncio.to_thread(_make_client, settings)
    try:
        yield S3ReceiptImageStorage(client, settings)
    finally:
        await asyncio.to_thread(client.close)


class S3ReceiptImageStorage:
    def __init__(self, client: S3Client, settings: Settings) -> None:
        if settings.s3_bucket_name is None:
            raise StorageError()
        self._client = client
        self._bucket = settings.s3_bucket_name
        self._ttl = settings.s3_presigned_url_ttl_seconds

    @staticmethod
    def _validate_key(key: str) -> None:
        if not KEY_PATTERN.fullmatch(key):
            raise StorageError()

    async def store(self, image: ProcessedImage) -> StoredReceiptImage:
        key = f"receipts/{datetime.now(UTC):%Y/%m}/{uuid4()}.jpg"

        async def upload() -> None:
            try:
                await asyncio.to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=key,
                    Body=image.data,
                    ContentLength=image.processed_byte_size,
                    ContentType=image.mime_type,
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                )
            except (BotoCoreError, ClientError) as exc:
                error = _map_error(exc, StorageUploadError, key)
                collision = isinstance(exc, ClientError) and exc.response.get("Error", {}).get(
                    "Code"
                ) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}
                if not error.transient and not collision:
                    raise error from None
                try:
                    found = await self.inspect(key)
                except StorageError:
                    raise StorageAmbiguousError(object_key=key) from None
                if found is not None:
                    matches = (
                        found.size_bytes == image.processed_byte_size
                        and found.content_type == image.mime_type
                    )
                    if matches:
                        return
                    raise StorageAmbiguousError(object_key=key) from None
                raise StorageUnavailableError(object_key=key) from None

        await retry(upload, retryable=lambda exc: isinstance(exc, StorageUnavailableError))
        return StoredReceiptImage(
            object_key=key,
            storage_uri=f"s3://{self._bucket}/{key}",
            bucket=self._bucket,
            size_bytes=image.processed_byte_size,
        )

    async def inspect(self, object_key: str) -> StoredObjectInfo | None:
        self._validate_key(object_key)

        async def head() -> StoredObjectInfo | None:
            try:
                result = await asyncio.to_thread(
                    self._client.head_object, Bucket=self._bucket, Key=object_key
                )
                length, mime = result.get("ContentLength"), result.get("ContentType")
                if type(length) is not int or not isinstance(mime, str):
                    raise StorageAmbiguousError(object_key=object_key)
                return StoredObjectInfo(object_key, length, mime)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise _map_error(exc, StorageError, object_key) from None
            except BotoCoreError as exc:
                raise _map_error(exc, StorageError, object_key) from None

        return await retry(head, retryable=lambda exc: isinstance(exc, StorageUnavailableError))

    async def delete(self, object_key: str) -> None:
        await retry(
            lambda: self._delete_once(object_key),
            retryable=lambda exc: isinstance(exc, StorageUnavailableError),
        )

    async def _delete_once(self, object_key: str) -> None:
        self._validate_key(object_key)
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
                return
            raise _map_error(exc, StorageDeleteError, object_key) from None
        except BotoCoreError as exc:
            raise _map_error(exc, StorageDeleteError, object_key) from None

    async def create_download_url(self, object_key: str) -> str:
        # Authorization belongs to the caller; this method binds access to one key/bucket.
        self._validate_key(object_key)
        try:
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=self._ttl,
                HttpMethod="GET",
            )
            if urlsplit(url).scheme != "https" or not urlsplit(url).netloc:
                raise StoragePresignError(object_key=object_key)
            return url
        except (BotoCoreError, ClientError) as exc:
            raise _map_error(exc, StoragePresignError, object_key) from None
        except ValueError:
            raise StoragePresignError(object_key=object_key) from None
