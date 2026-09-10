import asyncio
import threading
from contextlib import closing
from io import BytesIO
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError
from botocore.stub import ANY, Stubber
from PIL import Image

from second_brain_receipts.core.config import Settings
from second_brain_receipts.services.image_service import ImageService
from second_brain_receipts.storage.base import (
    StorageAmbiguousError,
    StorageAuthenticationError,
    StorageDeleteError,
    StorageError,
    StoragePresignError,
    StorageUnavailableError,
    StorageUploadError,
)
from second_brain_receipts.storage.s3 import KEY_PATTERN, S3ReceiptImageStorage, _make_client

KEY = "receipts/2026/09/00000000-0000-4000-8000-000000000001.jpg"
SECRET = "fake-aws-secret-never-real"


@pytest.fixture
def settings():
    return Settings(
        app_env="test",
        _env_file=None,
        aws_region="ap-southeast-1",
        s3_bucket_name="test-receipt-bucket",
    )


@pytest.fixture
def image(settings):
    stream = BytesIO()
    with Image.new("RGB", (320, 240), "white") as source:
        source.save(stream, "PNG")
    return ImageService(settings).process(stream.getvalue())


def test_s3_store_with_real_sdk_stub(settings, image):
    with (
        closing(
            boto3.client(
                "s3",
                region_name="ap-southeast-1",
                aws_access_key_id="test-key",
                aws_secret_access_key=SECRET,
            )
        ) as client,
        Stubber(client) as stub,
    ):
        stub.add_response(
            "put_object",
            {"ETag": '"synthetic"'},
            {
                "Bucket": settings.s3_bucket_name,
                "Key": ANY,
                "Body": image.data,
                "ContentLength": len(image.data),
                "ContentType": "image/jpeg",
                "ServerSideEncryption": "AES256",
                "IfNoneMatch": "*",
            },
        )
        result = asyncio.run(S3ReceiptImageStorage(client, settings).store(image))
        assert KEY_PATTERN.fullmatch(result.object_key)
        assert result.storage_uri == f"s3://{settings.s3_bucket_name}/{result.object_key}"
        assert result.size_bytes == len(image.data)
        assert result.content_type == "image/jpeg"
        assert result.bucket == settings.s3_bucket_name
        stub.assert_no_pending_responses()
        # Stubber checks the exact parameters: no ACL or financial metadata allowed.


def test_generated_keys_are_distinct_and_opaque(settings, image):
    client = Mock()
    storage = S3ReceiptImageStorage(client, settings)
    first = asyncio.run(storage.store(image))
    second = asyncio.run(storage.store(image))
    assert first.object_key != second.object_key
    for result in (first, second):
        assert KEY_PATTERN.fullmatch(result.object_key)
        assert not any(word in result.object_key for word in ("vendor", "telegram", "username"))


def test_presign_exact_get_intent(settings):
    client = Mock()
    client.generate_presigned_url.return_value = "https://example.invalid/object?secret=fake"
    storage = S3ReceiptImageStorage(client, settings)
    assert asyncio.run(storage.create_download_url(KEY)).startswith("https://")
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": KEY},
        ExpiresIn=300,
        HttpMethod="GET",
    )


def test_custom_expiry_and_real_offline_signing():
    settings = Settings(
        app_env="test",
        _env_file=None,
        s3_bucket_name="test-bucket",
        aws_region="ap-southeast-1",
        s3_presigned_url_ttl_seconds=60,
        aws_access_key_id="test-key",
        aws_secret_access_key=SECRET,
    )
    client = _make_client(settings)
    try:
        assert client.meta.config.retries["total_max_attempts"] == 1
        url = asyncio.run(S3ReceiptImageStorage(client, settings).create_download_url(KEY))
        from urllib.parse import parse_qs, urlsplit

        assert parse_qs(urlsplit(url).query)["X-Amz-Expires"] == ["60"]
        assert urlsplit(url).scheme == "https"
    finally:
        client.close()


@pytest.mark.parametrize("missing", [False, True])
def test_delete_and_missing_object(settings, missing):
    client = Mock()
    if missing:
        client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "DeleteObject"
        )
    assert asyncio.run(S3ReceiptImageStorage(client, settings).delete(KEY)) is None
    client.delete_object.assert_called_once_with(Bucket=settings.s3_bucket_name, Key=KEY)


@pytest.mark.parametrize(
    ("operation", "code", "error"),
    [
        ("store", "AccessDenied", StorageAuthenticationError),
        ("store", "InvalidAccessKeyId", StorageAuthenticationError),
        ("store", "ExpiredToken", StorageAuthenticationError),
        ("store", "SlowDown", StorageUnavailableError),
        ("store", "InternalError", StorageUnavailableError),
        # PreconditionFailed means the opaque key already exists; a HEAD that then finds
        # nothing there is a genuine paradox (delete race/consistency lag), not a permanent
        # rejection. Reconciliation classifies that as unavailable/retryable, not upload-fatal.
        ("store", "PreconditionFailed", StorageUnavailableError),
        ("store", "NoSuchBucket", StorageUploadError),
        ("delete", "NoSuchBucket", StorageDeleteError),
        ("create_download_url", "UnknownFailure", StoragePresignError),
    ],
)
def test_client_error_mapping(settings, image, operation, code, error, caplog):
    client = Mock()
    sdk_method = {
        "store": "put_object",
        "delete": "delete_object",
        "create_download_url": "generate_presigned_url",
    }[operation]
    getattr(client, sdk_method).side_effect = ClientError(
        {"Error": {"Code": code, "Message": SECRET}}, sdk_method
    )
    # A transient/collision store() failure triggers reconciliation via HEAD; simulate a
    # real S3 client confirming nothing was actually written under this opaque key.
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    storage = S3ReceiptImageStorage(client, settings)
    with pytest.raises(error) as exc:
        asyncio.run(getattr(storage, operation)(image if operation == "store" else KEY))
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert str(image.data) not in caplog.text
    assert exc.value.object_key and KEY_PATTERN.fullmatch(exc.value.object_key)
    assert exc.value.__suppress_context__


def test_collision_reconciliation_succeeds_when_content_matches(settings, image):
    # IfNoneMatch=* rejects a retried upload that already landed under this opaque key.
    # Confirming identical content already there makes store() idempotent, not fatal.
    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed", "Message": SECRET}}, "PutObject"
    )
    client.head_object.return_value = {
        "ContentLength": image.processed_byte_size,
        "ContentType": image.mime_type,
    }
    storage = S3ReceiptImageStorage(client, settings)
    result = asyncio.run(storage.store(image))
    assert result.size_bytes == image.processed_byte_size
    assert KEY_PATTERN.fullmatch(result.object_key)


def test_collision_reconciliation_ambiguous_when_content_differs(settings, image):
    # A different object already occupies this opaque key: never silently treat that as
    # success, and never overwrite it either.
    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed", "Message": SECRET}}, "PutObject"
    )
    client.head_object.return_value = {
        "ContentLength": image.processed_byte_size + 1,
        "ContentType": image.mime_type,
    }
    storage = S3ReceiptImageStorage(client, settings)
    with pytest.raises(StorageAmbiguousError) as exc:
        asyncio.run(storage.store(image))
    assert exc.value.object_key and KEY_PATTERN.fullmatch(exc.value.object_key)


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("store", StorageUploadError),
        ("delete", StorageDeleteError),
        ("create_download_url", StoragePresignError),
    ],
)
def test_botocore_errors(settings, image, method, error):
    client = Mock()
    sdk_method = {
        "store": "put_object",
        "delete": "delete_object",
        "create_download_url": "generate_presigned_url",
    }[method]
    getattr(client, sdk_method).side_effect = ParamValidationError(report=SECRET)
    with pytest.raises(error):
        asyncio.run(
            getattr(S3ReceiptImageStorage(client, settings), method)(
                image if method == "store" else KEY
            )
        )


def test_missing_credentials(settings, image):
    client = Mock()
    client.put_object.side_effect = NoCredentialsError()
    with pytest.raises(StorageAuthenticationError):
        asyncio.run(S3ReceiptImageStorage(client, settings).store(image))


@pytest.mark.parametrize(
    "key", ["../secret", "other.jpg", "https://example.invalid/x", KEY + "?x=1"]
)
def test_only_generated_key_namespace_allowed(settings, key):
    client = Mock()
    storage = S3ReceiptImageStorage(client, settings)
    for method in (storage.delete, storage.create_download_url):
        with pytest.raises(StorageError):
            asyncio.run(method(key))
    assert not client.mock_calls


def test_operations_run_off_event_loop(settings, image):
    main_thread = threading.get_ident()
    client = Mock()

    def check(*args, **kwargs):
        assert threading.get_ident() != main_thread
        return "https://example.invalid/object?temporary=authorization"

    client.put_object.side_effect = check
    client.delete_object.side_effect = check
    client.generate_presigned_url.side_effect = check

    async def run():
        storage = S3ReceiptImageStorage(client, settings)
        await storage.store(image)
        await storage.delete(KEY)
        await storage.create_download_url(KEY)

    asyncio.run(run())


def test_standard_credential_chain_is_not_replaced(monkeypatch, settings):
    session = Mock()
    factory = Mock(return_value=session)
    monkeypatch.setattr(boto3, "Session", factory)
    _make_client(settings)
    factory.assert_called_once_with(
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        region_name="ap-southeast-1",
    )


def test_non_https_presign_rejected(settings):
    client = Mock()
    client.generate_presigned_url.return_value = "http://example.invalid/object"
    with pytest.raises(StoragePresignError):
        asyncio.run(S3ReceiptImageStorage(client, settings).create_download_url(KEY))


def test_storage_context_closes_client(monkeypatch, settings):
    from second_brain_receipts.storage import s3

    client = Mock()
    monkeypatch.setattr(s3, "_make_client", lambda config: client)

    async def run():
        async with s3.open_s3_storage(settings) as storage:
            assert isinstance(storage, S3ReceiptImageStorage)

    asyncio.run(run())
    client.close.assert_called_once()
