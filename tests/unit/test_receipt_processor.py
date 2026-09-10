import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from second_brain_receipts.core.config import Settings
from second_brain_receipts.domain.ingress import (
    IncomingPhotoMessage,
    IncomingTextMessage,
    UnsupportedMessage,
)
from second_brain_receipts.main import create_app
from second_brain_receipts.providers.vision import (
    ReceiptExtractionError,
    VisionAuthenticationError,
    VisionRateLimitError,
    VisionResponseError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from second_brain_receipts.repositories.errors import (
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.schemas.receipts import Confidence, Receipt, ReceiptExtraction
from second_brain_receipts.schemas.vendors import Vendor
from second_brain_receipts.services.image_service import (
    ImageErrorCode,
    ImageService,
    ImageValidationError,
)
from second_brain_receipts.services.receipt_messages import (
    SYSTEM_FAILURE,
    UNREADABLE,
    UNSUPPORTED,
    duplicate_summary,
)
from second_brain_receipts.services.receipt_processor import (
    ProcessingOutcome,
    ReceiptDuplicateLookupFailure,
    ReceiptNotificationFailure,
    ReceiptPersistenceFailure,
    ReceiptProcessor,
    ReceiptStorageFailure,
    ReceiptVendorFailure,
    ReceiptVisionFailure,
)
from second_brain_receipts.services.telegram_service import TelegramIngressService
from second_brain_receipts.storage.base import (
    StorageDeleteError,
    StorageUnavailableError,
    StorageUploadError,
    StoredReceiptImage,
)
from second_brain_receipts.telegram.client import HTTPTelegramClient
from second_brain_receipts.telegram.errors import (
    TelegramRateLimitError,
    TelegramTimeoutError,
    TelegramUnavailableError,
)


@pytest.fixture
def rig():
    config = Settings(
        app_env="test",
        _env_file=None,
        telegram_allowed_user_id=123,
        telegram_webhook_secret="fake-webhook-secret",
        telegram_bot_token="123:fake-token",
    )
    output = BytesIO()
    Image.new("RGB", (256, 256), "white").save(output, "PNG")
    raw = output.getvalue()
    event = IncomingPhotoMessage(7, 123, 123, 9, datetime.now(UTC), "photo-id", raw)
    extraction = ReceiptExtraction(
        vendor_name="  ＡＢＣ  Hardware ",
        date="2026-09-10",
        total_amount="1280.00",
        vat_amount="137.14",
        category="AI category",
        confidence_score="High",
    )
    vendor = Vendor(
        id=uuid4(),
        normalized_name="abc hardware",
        display_name="ABC Hardware",
        category="Maintenance",
        created_at=datetime.now(UTC),
    )
    images = SimpleNamespace(process=Mock(side_effect=ImageService(config).process))
    vision = SimpleNamespace(extract=AsyncMock(return_value=extraction))
    vendors = SimpleNamespace(
        find_by_normalized_name=AsyncMock(return_value=vendor), upsert_vendor=AsyncMock()
    )
    receipts = SimpleNamespace(
        find_duplicate=AsyncMock(return_value=None),
        create_receipt=AsyncMock(side_effect=lambda row: row),
    )
    stored = StoredReceiptImage(
        "receipts/2026/09/opaque.jpg",
        "s3://test-bucket/receipts/2026/09/opaque.jpg",
        "test-bucket",
        100,
    )
    storage = SimpleNamespace(
        store=AsyncMock(return_value=stored), delete=AsyncMock(), create_download_url=AsyncMock()
    )
    telegram = SimpleNamespace(
        send_message=AsyncMock(),
        get_file=AsyncMock(return_value="photos/a.jpg"),
        download_file=AsyncMock(return_value=raw),
    )
    pending = SimpleNamespace(
        before_photo=AsyncMock(return_value=False), create=AsyncMock(), reply=AsyncMock()
    )
    processor = ReceiptProcessor(
        config, images, vision, vendors, receipts, storage, telegram, pending
    )
    trace = Mock()
    for name, method in [
        ("image", images.process),
        ("vision", vision.extract),
        ("vendor", vendors.find_by_normalized_name),
        ("duplicate", receipts.find_duplicate),
        ("upload", storage.store),
        ("insert", receipts.create_receipt),
        ("send", telegram.send_message),
    ]:
        trace.attach_mock(method, name)
    return SimpleNamespace(**locals())


def run(rig):
    return asyncio.run(rig.processor.process(rig.event))


def assert_no_writes(rig):
    rig.storage.store.assert_not_awaited()
    rig.receipts.create_receipt.assert_not_awaited()
    rig.vendors.upsert_vendor.assert_not_awaited()


@pytest.mark.parametrize("confidence", [Confidence.HIGH, Confidence.MEDIUM])
@pytest.mark.parametrize("ai_category", ["AI category", None])
def test_known_vendor_happy_path(rig, confidence, ai_category):
    rig.vision.extract.return_value = rig.extraction.model_copy(
        update={"confidence_score": confidence, "category": ai_category}
    )
    assert run(rig) == ProcessingOutcome.SAVED
    rig.images.process.assert_called_once_with(rig.raw)
    processed = rig.vision.extract.call_args.args[0]
    assert processed.mime_type == "image/jpeg" and processed.data != rig.raw
    assert rig.storage.store.call_args.args[0] is processed
    rig.vendors.find_by_normalized_name.assert_awaited_once_with("abc hardware")
    rig.receipts.find_duplicate.assert_awaited_once_with(
        "abc hardware", date(2026, 9, 10), Decimal("1280.00")
    )
    receipt = rig.receipts.create_receipt.call_args.args[0]
    assert isinstance(receipt, Receipt) and receipt.category == "Maintenance"
    assert receipt.total_amount == Decimal("1280.00") and receipt.vat_amount == Decimal("137.14")
    assert receipt.receipt_date == date(2026, 9, 10) and receipt.telegram_chat_id == 123
    assert (
        receipt.normalized_vendor_name == "abc hardware" and receipt.created_at.tzinfo is not None
    )
    assert (
        receipt.image_storage_key == rig.stored.object_key
        and receipt.image_storage_uri == rig.stored.storage_uri
    )
    rig.vendors.upsert_vendor.assert_not_awaited()
    rig.storage.delete.assert_not_awaited()
    rig.storage.create_download_url.assert_not_awaited()
    message = rig.telegram.send_message.call_args.args[1]
    assert "*Date*: `10/09/2026`" in message and "*Total*: `PHP 1,280.00`" in message
    assert "*VAT*: `PHP 137.14`" in message and "*Category*: `Maintenance`" in message
    assert "AI category" not in message and "s3://" not in message
    # The execution summary is the one message that opts into Telegram MarkdownV2.
    assert rig.telegram.send_message.call_args.kwargs["parse_mode"] == "MarkdownV2"
    assert [call[0] for call in rig.trace.mock_calls] == [
        "image",
        "vision",
        "vendor",
        "duplicate",
        "upload",
        "insert",
        "send",
    ]


def test_low_confidence_stops_before_vendor_and_storage(rig):
    rig.vision.extract.return_value = rig.extraction.model_copy(
        update={"confidence_score": Confidence.LOW}
    )
    assert run(rig) == ProcessingOutcome.UNREADABLE
    assert_no_writes(rig)
    rig.vendors.find_by_normalized_name.assert_not_awaited()
    rig.telegram.send_message.assert_awaited_once_with(123, UNREADABLE)


@pytest.mark.parametrize("reason", list(ImageErrorCode))
def test_image_validation_errors_are_user_correctable(rig, reason):
    rig.images.process.side_effect = ImageValidationError(reason)
    assert run(rig) == ProcessingOutcome.UNREADABLE
    rig.vision.extract.assert_not_awaited()
    assert_no_writes(rig)
    rig.telegram.send_message.assert_awaited_once_with(123, UNREADABLE)


def test_real_corrupted_image_does_not_reach_vision(rig):
    rig.event = IncomingPhotoMessage(7, 123, 123, 9, datetime.now(UTC), "id", b"not-an-image")
    assert run(rig) == ProcessingOutcome.UNREADABLE
    rig.vision.extract.assert_not_awaited()
    assert_no_writes(rig)


def test_unextractable_is_handled(rig):
    rig.vision.extract.side_effect = ReceiptExtractionError()
    assert run(rig) == ProcessingOutcome.UNREADABLE
    assert_no_writes(rig)
    rig.telegram.send_message.assert_awaited_once_with(123, UNREADABLE)


@pytest.mark.parametrize(
    "failure",
    [
        VisionTimeoutError(),
        VisionUnavailableError(),
        VisionAuthenticationError(),
        VisionRateLimitError(),
        VisionResponseError(),
    ],
)
def test_vision_system_errors_classified(rig, failure):
    rig.vision.extract.side_effect = failure
    with pytest.raises(ReceiptVisionFailure) as exc:
        run(rig)
    assert exc.value.failure is failure
    assert_no_writes(rig)
    rig.telegram.send_message.assert_awaited_once_with(123, SYSTEM_FAILURE)


def test_unknown_vendor_delegates_after_duplicate_check(rig):
    rig.vendors.find_by_normalized_name.return_value = None
    assert run(rig) == ProcessingOutcome.AWAITING_CATEGORY
    assert_no_writes(rig)
    rig.receipts.find_duplicate.assert_awaited_once()
    rig.pending.create.assert_awaited_once()
    assert rig.pending.create.call_args.args[1] is rig.vision.extract.call_args.args[0]


def test_precheck_duplicate_no_upload(rig):
    rig.receipts.find_duplicate.return_value = object()
    assert run(rig) == ProcessingOutcome.DUPLICATE
    assert_no_writes(rig)
    rig.telegram.send_message.assert_awaited_once_with(
        123, duplicate_summary(rig.extraction, "PHP")
    )


def test_duplicate_race_cleanup_and_same_notification(rig):
    rig.receipts.create_receipt.side_effect = DuplicateReceiptPersistenceError()
    assert run(rig) == ProcessingOutcome.DUPLICATE
    rig.storage.delete.assert_awaited_once_with(rig.stored.object_key)
    rig.receipts.create_receipt.assert_awaited_once()
    rig.telegram.send_message.assert_awaited_once_with(
        123, duplicate_summary(rig.extraction, "PHP")
    )


def test_insert_rejection_preserved_after_cleanup(rig):
    failure = PersistenceError()
    rig.receipts.create_receipt.side_effect = failure
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        run(rig)
    assert exc.value.failure is failure and not exc.value.cleanup_failed
    rig.storage.delete.assert_awaited_once_with(rig.stored.object_key)
    assert exc.value.receipt_id == rig.receipts.create_receipt.call_args.args[0].id
    rig.telegram.send_message.assert_awaited_once_with(123, SYSTEM_FAILURE)


@pytest.mark.parametrize("duplicate", [False, True])
def test_cleanup_failure_preserves_insert_reason_safe_log(rig, duplicate, caplog):
    caplog.set_level(logging.INFO)
    failure = DuplicateReceiptPersistenceError() if duplicate else PersistenceError()
    rig.receipts.create_receipt.side_effect = failure
    cleanup = StorageDeleteError(object_key=rig.stored.object_key)
    cleanup.args = ("secret-cloud-value",)
    rig.storage.delete.side_effect = cleanup
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        run(rig)
    assert exc.value.failure is failure and exc.value.cleanup_failed
    assert exc.value.object_key == rig.stored.object_key
    assert "receipt_cleanup_failed" in caplog.text and "secret-cloud-value" not in caplog.text
    assert rig.stored.object_key not in caplog.text
    assert "secret-cloud-value" not in str(exc.value)
    text = rig.telegram.send_message.call_args.args[1]
    assert text == (duplicate_summary(rig.extraction, "PHP") if duplicate else SYSTEM_FAILURE)
    assert rig.telegram.send_message.await_count == 1


@pytest.mark.parametrize(
    "method,error_type",
    [("vendor", ReceiptVendorFailure), ("duplicate", ReceiptDuplicateLookupFailure)],
)
def test_repository_read_failures_no_upload(rig, method, error_type):
    failure = PersistenceUnavailableError()
    target = (
        rig.vendors.find_by_normalized_name if method == "vendor" else rig.receipts.find_duplicate
    )
    target.side_effect = failure
    with pytest.raises(error_type) as exc:
        run(rig)
    assert exc.value.failure is failure
    assert_no_writes(rig)
    rig.telegram.send_message.assert_awaited_once_with(123, SYSTEM_FAILURE)


@pytest.mark.parametrize("failure", [StorageUploadError(), StorageUnavailableError()])
def test_upload_failure_no_insert(rig, failure):
    rig.storage.store.side_effect = failure
    with pytest.raises(ReceiptStorageFailure) as exc:
        run(rig)
    assert exc.value.failure is failure
    rig.receipts.create_receipt.assert_not_awaited()
    rig.storage.delete.assert_not_awaited()
    rig.telegram.send_message.assert_awaited_once_with(123, SYSTEM_FAILURE)


@pytest.mark.parametrize(
    "failure", [TelegramTimeoutError(), TelegramUnavailableError(), TelegramRateLimitError(3)]
)
def test_success_send_failure_keeps_committed_receipt_and_image(rig, failure):
    rig.telegram.send_message.side_effect = failure
    with pytest.raises(ReceiptNotificationFailure) as exc:
        run(rig)
    assert exc.value.failure is failure and exc.value.committed
    rig.receipts.create_receipt.assert_awaited_once()
    rig.storage.delete.assert_not_awaited()
    assert rig.telegram.send_message.await_count == 1


def test_system_failure_plus_failed_error_notification_preserves_original(rig):
    failure = VisionTimeoutError()
    rig.vision.extract.side_effect = failure
    rig.telegram.send_message.side_effect = TelegramTimeoutError()
    with pytest.raises(ReceiptVisionFailure) as exc:
        run(rig)
    assert exc.value.failure is failure and exc.value.notification_failed
    assert rig.telegram.send_message.await_count == 1


@pytest.mark.parametrize(
    "failure", [PersistenceUnavailableError(), InvalidPersistenceResponseError()]
)
def test_ambiguous_insert_confirmed_committed_by_identity(rig, failure):
    committed = []

    async def insert(receipt):
        committed.append(receipt)
        raise failure

    async def lookup(*args):
        return committed[0] if committed else None

    rig.receipts.create_receipt.side_effect = insert
    rig.receipts.find_duplicate.side_effect = lookup
    assert run(rig) == ProcessingOutcome.SAVED
    rig.receipts.create_receipt.assert_awaited_once()
    rig.storage.delete.assert_not_awaited()
    assert "Receipt Processed" in rig.telegram.send_message.call_args.args[1]


@pytest.mark.parametrize("lookup", ["absent", "unavailable", "different_row", "different_key"])
def test_ambiguous_insert_unresolved_retains_image_for_reconciliation(rig, lookup):
    rig.receipts.create_receipt.side_effect = PersistenceUnavailableError()
    calls = 0

    async def find(*args):
        nonlocal calls
        calls += 1
        if calls == 1 or lookup == "absent":
            return None
        if lookup == "unavailable":
            raise PersistenceUnavailableError()
        receipt = rig.receipts.create_receipt.call_args.args[0]
        return receipt.model_copy(
            update={"id": uuid4()}
            if lookup == "different_row"
            else {"image_storage_key": "other.jpg"}
        )

    rig.receipts.find_duplicate.side_effect = find
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        run(rig)
    assert exc.value.uncertain and exc.value.object_key == rig.stored.object_key
    rig.storage.delete.assert_not_awaited()
    rig.receipts.create_receipt.assert_awaited_once()


def test_invalid_storage_reference_cleanup_before_insert(rig):
    rig.storage.store.return_value = StoredReceiptImage(
        "opaque.jpg", "https://secret.invalid", "test-bucket", 100
    )
    with pytest.raises(ReceiptPersistenceFailure):
        run(rig)
    rig.receipts.create_receipt.assert_not_awaited()
    rig.storage.delete.assert_awaited_once_with("opaque.jpg")


def test_image_runs_off_event_loop_and_timings_safe(rig, caplog):
    caplog.set_level(logging.INFO)
    threads = []
    image_service = ImageService(rig.config)

    def image(raw):
        threads.append(threading.get_ident())
        return image_service.process(raw)

    rig.images.process.side_effect = image
    run(rig)
    assert threads[0] != threading.get_ident()
    stages = {record.stage for record in caplog.records if hasattr(record, "stage")}
    assert {
        "image_processing",
        "vision",
        "vendor_lookup",
        "duplicate_lookup",
        "s3_upload",
        "database_insert",
    } <= stages
    assert all(
        record.duration_ms >= 0 for record in caplog.records if hasattr(record, "duration_ms")
    )
    assert any(hasattr(record, "total_processing_ms") for record in caplog.records)
    assert (
        "1280" not in caplog.text
        and "Maintenance" not in caplog.text
        and "Hardware" not in caplog.text
    )


@pytest.mark.parametrize("vat", [None, Decimal("0.00")])
def test_summary_optional_vat_currency_and_plain_text(rig, vat):
    rig.vision.extract.return_value = rig.extraction.model_copy(
        update={"vat_amount": vat, "vendor_name": "_[Vendor]*\nTotal: fake"}
    )
    rig.vendor = rig.vendor.model_copy(update={"category": "[Maintenance]*"})
    rig.vendors.find_by_normalized_name.return_value = rig.vendor
    rig.processor = ReceiptProcessor(
        rig.config.model_copy(update={"default_currency": "USD"}),
        rig.images,
        rig.vision,
        rig.vendors,
        rig.receipts,
        rig.storage,
        rig.telegram,
        rig.pending,
    )
    run(rig)
    message = rig.telegram.send_message.call_args.args[1]
    assert "*Total*: `USD 1,280.00`" in message
    assert ("*VAT*:" in message) == (vat is not None)
    assert "\nTotal: fake" not in message and "\nCategory: forged" not in message
    # The vendor name contains MarkdownV2 syntax characters and display_value already
    # collapsed its embedded newline. Inside a code span it stays literal, and is also
    # exempt from Telegram's own URL and hashtag detection.
    assert "`_[Vendor]* Total: fake`" in message
    assert rig.telegram.send_message.call_args.kwargs["parse_mode"] == "MarkdownV2"


def test_long_vendor_category_fit_one_message(rig):
    rig.vision.extract.return_value = rig.extraction.model_copy(
        update={"vendor_name": "Vendor" * 1000}
    )
    rig.vendors.find_by_normalized_name.return_value = rig.vendor.model_copy(
        update={"category": "Category" * 10}
    )
    run(rig)
    assert len(rig.telegram.send_message.call_args.args[1]) < 4096


@pytest.mark.parametrize("kind", [IncomingTextMessage, UnsupportedMessage])
def test_nonphoto_no_category_behavior(rig, kind):
    common = (7, 123, 123, 9, datetime.now(UTC))
    event = kind(*common, "Maintenance") if kind is IncomingTextMessage else kind(*common)
    asyncio.run(rig.processor.accept(event))
    rig.images.process.assert_not_called()
    assert_no_writes(rig)
    if kind is IncomingTextMessage:
        rig.pending.reply.assert_awaited_once_with(event)
    else:
        rig.telegram.send_message.assert_awaited_once_with(123, UNSUPPORTED)


class Updates:
    def __init__(self):
        self.rows = {}

    async def claim_update(self, update_id):
        if update_id in self.rows:
            return False
        self.rows[update_id] = "received"
        return True

    async def finish_update(self, update_id, status):
        self.rows[update_id] = status


def payload():
    return {
        "update_id": 7,
        "message": {
            "message_id": 9,
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "photo": [{"file_id": "photo-id", "width": 256, "height": 256}],
        },
    }


def webhook(rig, *, body=None, repeat=False):
    updates = Updates()
    service = TelegramIngressService(rig.config, updates, rig.telegram, rig.processor)

    async def run_request():
        app = create_app(rig.config, service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
        ) as client:
            kwargs = {
                "json": body or payload(),
                "headers": {"X-Telegram-Bot-Api-Secret-Token": "fake-webhook-secret"},
            }
            first = await client.post("/webhooks/telegram", **kwargs)
            second = await client.post("/webhooks/telegram", **kwargs) if repeat else None
            return first, second

    first, second = asyncio.run(run_request())
    return first, second, updates


def test_webhook_to_saved_receipt_and_duplicate_delivery(rig, caplog):
    caplog.set_level(logging.INFO)
    first, second, updates = webhook(rig, repeat=True)
    assert first.json() == {"status": "handled"} and second.json() == {"status": "duplicate"}
    assert updates.rows == {7: "handled"}
    rig.telegram.get_file.assert_awaited_once_with("photo-id")
    rig.telegram.download_file.assert_awaited_once_with("photos/a.jpg")
    rig.receipts.create_receipt.assert_awaited_once()
    assert any(getattr(record, "stage", None) == "telegram_download" for record in caplog.records)


@pytest.mark.parametrize(
    "branch", ["low", "unreadable", "unknown", "duplicate", "bad_image", "unsupported", "text"]
)
def test_user_outcomes_mark_handled(rig, branch):
    body = payload()
    if branch == "low":
        rig.vision.extract.return_value = rig.extraction.model_copy(
            update={"confidence_score": Confidence.LOW}
        )
    elif branch == "unreadable":
        rig.vision.extract.side_effect = ReceiptExtractionError()
    elif branch == "unknown":
        rig.vendors.find_by_normalized_name.return_value = None
    elif branch == "duplicate":
        rig.receipts.find_duplicate.return_value = object()
    elif branch == "bad_image":
        rig.telegram.download_file.return_value = b"bad"
    else:
        body["message"].pop("photo")
        body["message"].update({"text": "Category"} if branch == "text" else {"sticker": {}})
    first, _, updates = webhook(rig, body=body)
    assert first.status_code == 200 and updates.rows == {7: "handled"}
    assert_no_writes(rig)
    if branch not in ("unknown", "text"):
        rig.telegram.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    "stage", ["download", "vision", "vendor", "duplicate", "upload", "insert", "send"]
)
def test_internal_failures_mark_failed_and_no_replay(rig, stage):
    targets = {
        "download": (rig.telegram.download_file, TelegramUnavailableError()),
        "vision": (rig.vision.extract, VisionTimeoutError()),
        "vendor": (rig.vendors.find_by_normalized_name, PersistenceUnavailableError()),
        "duplicate": (rig.receipts.find_duplicate, PersistenceUnavailableError()),
        "upload": (rig.storage.store, StorageUploadError()),
        "insert": (rig.receipts.create_receipt, PersistenceError()),
        "send": (rig.telegram.send_message, TelegramTimeoutError()),
    }
    mock, failure = targets[stage]
    mock.side_effect = failure
    first, second, updates = webhook(rig, repeat=True)
    assert first.json() == {"status": "failed"} and second.json() == {"status": "duplicate"}
    assert updates.rows == {7: "failed"} and mock.await_count == 1
    if stage != "send":
        rig.telegram.send_message.assert_awaited_once_with(123, SYSTEM_FAILURE)
    if stage == "insert":
        rig.storage.delete.assert_awaited_once_with(rig.stored.object_key)


def test_http_adapter_download_to_processor_and_send(rig):
    requests = []

    def respond(req):
        requests.append(req)
        if req.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        if req.url.path.endswith("photos/a.jpg"):
            return httpx.Response(200, content=rig.raw)
        assert req.url.path.endswith("sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

    async def run_request():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            telegram = HTTPTelegramClient(client, rig.config)
            processor = ReceiptProcessor(
                rig.config,
                rig.images,
                rig.vision,
                rig.vendors,
                rig.receipts,
                rig.storage,
                telegram,
                rig.pending,
            )
            updates = Updates()
            service = TelegramIngressService(rig.config, updates, telegram, processor)
            app = create_app(rig.config, service)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
            ) as http:
                response = await http.post(
                    "/webhooks/telegram",
                    json=payload(),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "fake-webhook-secret"},
                )
                assert response.json() == {"status": "handled"}
            assert updates.rows == {7: "handled"}

    asyncio.run(run_request())
    assert len(requests) == 3
    sent = json.loads(requests[-1].content)
    # Real end-to-end proof (through the actual HTTP body, not a mock) that the
    # execution summary reaches Telegram with MarkdownV2 formatting requested.
    assert sent["parse_mode"] == "MarkdownV2" and sent["chat_id"] == 123
    assert "Receipt Processed" in sent["text"]
    assert rig.storage.store.call_args.args[0] is rig.vision.extract.call_args.args[0]


def test_two_different_updates_competing_for_same_receipt(rig):
    objects = []
    inserts = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def store(image):
        key = f"receipts/2026/09/{uuid4()}.jpg"
        result = StoredReceiptImage(key, f"s3://test-bucket/{key}", "test-bucket", len(image.data))
        objects.append(result)
        return result

    async def insert(receipt):
        if inserts:
            release.set()
            raise DuplicateReceiptPersistenceError()
        inserts.append(receipt)
        entered.set()
        await release.wait()
        return receipt

    rig.storage.store.side_effect = store
    rig.receipts.create_receipt.side_effect = insert

    async def run_both():
        first = asyncio.create_task(rig.processor.process(rig.event))
        await entered.wait()
        second_event = IncomingPhotoMessage(8, 123, 123, 10, datetime.now(UTC), "another", rig.raw)
        second = asyncio.create_task(rig.processor.process(second_event))
        return await asyncio.wait_for(asyncio.gather(first, second), 3)

    assert asyncio.run(run_both()) == [ProcessingOutcome.SAVED, ProcessingOutcome.DUPLICATE]
    assert len(inserts) == 1 and len(objects) == 2
    rig.storage.delete.assert_awaited_once_with(objects[1].object_key)
    assert inserts[0].image_storage_key == objects[0].object_key


def test_cleanup_and_duplicate_notification_both_fail_preserves_duplicate(rig):
    failure = DuplicateReceiptPersistenceError()
    rig.receipts.create_receipt.side_effect = failure
    rig.storage.delete.side_effect = StorageDeleteError()
    rig.telegram.send_message.side_effect = TelegramTimeoutError()
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        run(rig)
    assert (
        exc.value.failure is failure and exc.value.cleanup_failed and exc.value.notification_failed
    )
    assert not exc.value.user_notified and rig.telegram.send_message.await_count == 1


def test_cancellation_during_insert_does_not_delete_potentially_committed_image(rig):
    rig.receipts.create_receipt.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(rig)
    rig.storage.delete.assert_not_awaited()
    rig.telegram.send_message.assert_not_awaited()


def test_failed_status_write_after_success_keeps_image_and_does_not_replay(rig):
    from second_brain_receipts.schemas.telegram import TelegramUpdate

    updates = Updates()

    async def fail(*args):
        raise PersistenceUnavailableError()

    updates.finish_update = fail
    service = TelegramIngressService(rig.config, updates, rig.telegram, rig.processor)

    async def run_twice():
        with pytest.raises(PersistenceUnavailableError):
            await service.handle(TelegramUpdate.model_validate(payload()))
        assert await service.handle(TelegramUpdate.model_validate(payload())) == "duplicate"

    asyncio.run(run_twice())
    assert updates.rows == {7: "received"}
    rig.receipts.create_receipt.assert_awaited_once()
    rig.telegram.send_message.assert_awaited_once()
    rig.storage.delete.assert_not_awaited()


def test_pending_creation_notification_failure_is_failed_not_handled(rig):
    rig.vendors.find_by_normalized_name.return_value = None
    rig.pending.create.side_effect = ReceiptNotificationFailure(TelegramTimeoutError())
    first, _, updates = webhook(rig)
    assert first.json() == {"status": "failed"} and updates.rows == {7: "failed"}
    assert_no_writes(rig)


def test_complete_application_factory_wires_known_vendor_processor(rig, monkeypatch):
    import second_brain_receipts.main as main

    @asynccontextmanager
    async def database(config):
        yield object()

    @asynccontextmanager
    async def telegram(config):
        yield rig.telegram

    @asynccontextmanager
    async def vision(config):
        yield object()

    @asynccontextmanager
    async def storage(config):
        yield rig.storage

    updates = Updates()
    monkeypatch.setattr(main, "open_supabase_client", database)
    monkeypatch.setattr(main, "open_telegram_client", telegram)
    monkeypatch.setattr(main, "create_openai_client", vision)
    monkeypatch.setattr(main, "open_s3_storage", storage)
    monkeypatch.setattr(main, "OpenAIReceiptVisionProvider", lambda *args: rig.vision)
    monkeypatch.setattr(main, "SupabaseVendorRepository", lambda *args: rig.vendors)
    monkeypatch.setattr(main, "SupabaseReceiptRepository", lambda *args: rig.receipts)
    monkeypatch.setattr(main, "SupabaseUpdateRepository", lambda *args: updates)
    monkeypatch.setattr(main, "PendingWorkflow", lambda *args: rig.pending)
    app = main.create_app(rig.config)

    async def use_app():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
            ) as client,
        ):
            response = await client.post(
                "/webhooks/telegram",
                json=payload(),
                headers={"X-Telegram-Bot-Api-Secret-Token": "fake-webhook-secret"},
            )
            assert response.json() == {"status": "handled"}
            assert (await client.get("/health")).json() == {"status": "ok"}

    asyncio.run(use_app())
    assert updates.rows == {7: "handled"}
    assert rig.receipts.create_receipt.call_args.args[0].category == "Maintenance"
    assert rig.storage.store.call_args.args[0] is rig.vision.extract.call_args.args[0]


def test_startup_disables_http_financial_url_logging(rig, caplog):
    from second_brain_receipts.core.logging import configure_logging

    caplog.set_level(logging.DEBUG)
    configure_logging(rig.config)
    logging.getLogger("httpx").info("request vendor=secret_vendor&total_amount=1280.00")
    logging.getLogger("botocore.endpoint").debug("body secret_image")
    assert "secret_vendor" not in caplog.text and "secret_image" not in caplog.text
