import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from test_receipt_processor import Updates, payload
from test_receipt_processor import rig as base_fixture

from second_brain_receipts.domain.category import validate_category
from second_brain_receipts.domain.ingress import IncomingTextMessage
from second_brain_receipts.main import create_app
from second_brain_receipts.repositories.errors import (
    DuplicateReceiptPersistenceError,
    PendingReceiptConflictError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.schemas.pending import PendingResult
from second_brain_receipts.schemas.receipts import (
    Confidence,
    Receipt,
    WorkflowState,
)
from second_brain_receipts.schemas.vendors import Vendor
from second_brain_receipts.services.pending_workflow import PendingWorkflow
from second_brain_receipts.services.receipt_messages import (
    CANCELLED,
    EXPIRED,
    INVALID_CATEGORY,
    NO_PENDING,
    WAITING,
)
from second_brain_receipts.services.receipt_processor import (
    ReceiptNotificationFailure,
    ReceiptPersistenceFailure,
    ReceiptProcessor,
    ReceiptStorageFailure,
)
from second_brain_receipts.services.telegram_service import TelegramIngressService
from second_brain_receipts.storage.base import StorageDeleteError
from second_brain_receipts.telegram.errors import TelegramTimeoutError

rig = base_fixture


@pytest.fixture
def pending_rig(request):
    rig = request.getfixturevalue("rig")
    repo = SimpleNamespace(
        create_pending=AsyncMock(side_effect=lambda row: row),
        get_awaiting_for_chat_user=AsyncMock(return_value=None),
        get_by_id=AsyncMock(return_value=None),
        complete=AsyncMock(),
        close=AsyncMock(),
    )
    workflow = PendingWorkflow(rig.config, repo, rig.storage, rig.telegram)
    rig.pending_repo = repo
    rig.workflow = workflow
    rig.processor = ReceiptProcessor(
        rig.config,
        rig.images,
        rig.vision,
        rig.vendors,
        rig.receipts,
        rig.storage,
        rig.telegram,
        workflow,
    )
    rig.vendors.find_by_normalized_name.return_value = None
    return rig


def create(r):
    asyncio.run(r.processor.process(r.event))
    return r.pending_repo.create_pending.call_args.args[0]


def reply(r, text=" Maintenance "):
    event = IncomingTextMessage(8, 123, 123, 10, datetime.now(UTC), text)
    return asyncio.run(r.workflow.reply(event))


def awaiting(r):
    row = create(r)
    r.pending_repo.get_awaiting_for_chat_user.return_value = row
    r.telegram.send_message.reset_mock()
    return row


def terminal(row, outcome, *, cleanup=True):
    state = "failed" if outcome == "duplicate" else outcome
    return PendingResult(
        outcome=outcome,
        pending=row.model_copy(update={"state": WorkflowState(state)}),
        cleanup_allowed=cleanup,
    )


def completed(row, category="Maintenance"):
    data = row.model_dump(exclude={"telegram_user_id", "state", "expires_at"})
    data["category"] = category
    receipt = Receipt(**data)
    return PendingResult(
        outcome="completed",
        receipt=receipt,
        pending=row.model_copy(update={"state": WorkflowState.COMPLETED, "category": category}),
    )


def test_pending_creation_contains_resume_data_and_orders_prompt(pending_rig):
    r = pending_rig
    seen = []

    async def insert(row):
        seen.append("insert")
        return row

    async def send(*args):
        assert seen == ["insert"]
        seen.append("prompt")

    r.pending_repo.create_pending.side_effect = insert
    r.telegram.send_message.side_effect = send
    row = create(r)
    assert row.state == "awaiting_category" and row.category is None
    assert row.total_amount == r.extraction.total_amount and row.receipt_date == r.extraction.date
    assert row.telegram_chat_id == 123 and row.telegram_user_id == 123
    assert row.image_storage_uri == r.stored.storage_uri
    assert row.expires_at - row.created_at == timedelta(minutes=30)
    assert r.storage.store.call_args.args[0] is r.vision.extract.call_args.args[0]
    r.storage.store.assert_awaited_once()
    r.receipts.create_receipt.assert_not_awaited()
    r.vendors.upsert_vendor.assert_not_awaited()
    assert seen == ["insert", "prompt"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_pending_insert_failure_compensation_original_preserved(pending_rig, cleanup_fails, caplog):
    r = pending_rig
    failure = PersistenceError()
    r.pending_repo.create_pending.side_effect = failure
    if cleanup_fails:
        r.storage.delete.side_effect = StorageDeleteError()
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        create(r)
    assert exc.value.failure is failure and exc.value.cleanup_failed == cleanup_fails
    r.storage.delete.assert_awaited_once_with(r.stored.object_key)
    assert r.stored.object_key not in caplog.text


def test_pending_prompt_failure_retains_work(pending_rig):
    r = pending_rig
    r.telegram.send_message.side_effect = TelegramTimeoutError()
    with pytest.raises(ReceiptNotificationFailure) as exc:
        create(r)
    assert exc.value.committed
    r.pending_repo.create_pending.assert_awaited_once()
    r.storage.delete.assert_not_awaited()


def test_pending_insert_ambiguous_retains_object(pending_rig):
    r = pending_rig
    r.pending_repo.create_pending.side_effect = PersistenceUnavailableError()
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        create(r)
    assert exc.value.uncertain
    r.storage.delete.assert_not_awaited()


def test_pending_insert_ambiguous_confirmed_can_prompt(pending_rig):
    r = pending_rig

    async def insert(row):
        r.pending_repo.get_by_id.return_value = row
        raise PersistenceUnavailableError()

    r.pending_repo.create_pending.side_effect = insert
    create(r)
    assert "Which expense category" in r.telegram.send_message.call_args.args[1]
    r.storage.delete.assert_not_awaited()


def test_pending_unique_race_cleans_losing_image(pending_rig):
    r = pending_rig
    r.pending_repo.create_pending.side_effect = PendingReceiptConflictError()
    create(r)
    r.storage.delete.assert_awaited_once_with(r.stored.object_key)
    r.telegram.send_message.assert_awaited_once_with(123, WAITING)


def test_active_pending_blocks_before_download_and_vision(pending_rig):
    r = pending_rig
    awaiting(r)
    r.telegram.get_file.reset_mock()
    r.images.process.reset_mock()
    r.vision.extract.reset_mock()
    updates = Updates()
    ingress = TelegramIngressService(r.config, updates, r.telegram, r.processor)
    from second_brain_receipts.schemas.telegram import TelegramUpdate

    assert asyncio.run(ingress.handle(TelegramUpdate.model_validate(payload()))) == "handled"
    r.telegram.get_file.assert_not_awaited()
    r.images.process.assert_not_called()
    r.vision.extract.assert_not_awaited()
    r.telegram.send_message.assert_awaited_once_with(123, WAITING)


@pytest.mark.parametrize("category", ["  Custom expense  ", "Cancellation Fees", "費用", "x" * 100])
def test_valid_freeform_category_completes_without_vision_or_upload(pending_rig, category):
    r = pending_rig
    row = awaiting(r)
    r.pending_repo.complete.return_value = completed(row, category.strip())
    reply(r, category)
    r.pending_repo.complete.assert_awaited_once_with(row.id, 123, 123, category.strip())
    assert r.vision.extract.await_count == 1 and r.storage.store.await_count == 1
    r.storage.delete.assert_not_awaited()
    text = r.telegram.send_message.call_args.args[1]
    assert "Receipt Processed" in text and "Saved category memory" in text and "10/09/2026" in text
    # The execution summary is the one message that opts into Telegram MarkdownV2.
    assert r.telegram.send_message.call_args.kwargs["parse_mode"] == "MarkdownV2"


@pytest.mark.parametrize(
    "category",
    [
        "",
        " ",
        "x" * 101,
        "Food\nSupplies",
        "\tFood",
        "x\x00",
        "x\x7f",
        "x\x85",
        "x\u202e",
        "x\u200b",
    ],
)
def test_invalid_category_no_completion(pending_rig, category):
    r = pending_rig
    awaiting(r)
    reply(r, category)
    r.pending_repo.complete.assert_not_awaited()
    r.telegram.send_message.assert_awaited_once_with(123, INVALID_CATEGORY)
    with pytest.raises(ValidationError):
        validate_category(category)


def test_no_pending_arbitrary_text(pending_rig):
    r = pending_rig
    reply(r)
    r.pending_repo.complete.assert_not_awaited()
    r.telegram.send_message.assert_awaited_once_with(123, NO_PENDING)


@pytest.mark.parametrize("text", ["cancel", " Cancel ", "CANCEL"])
def test_cancel_explicit_state_no_business_writes(pending_rig, text):
    r = pending_rig
    row = awaiting(r)
    r.pending_repo.close.return_value = terminal(row, "cancelled")
    reply(r, text)
    r.pending_repo.close.assert_awaited_once_with(row.id, 123, 123, "cancelled")
    r.pending_repo.complete.assert_not_awaited()
    r.storage.delete.assert_awaited_once_with(row.image_storage_key)
    r.telegram.send_message.assert_awaited_once_with(123, CANCELLED)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_expiry_closes_and_cleans_before_notification(pending_rig, cleanup_fails):
    r = pending_rig
    row = awaiting(r).model_copy(
        update={
            "created_at": datetime.now(UTC) - timedelta(hours=1),
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    r.pending_repo.get_awaiting_for_chat_user.return_value = row
    r.pending_repo.close.return_value = terminal(row, "expired")
    if cleanup_fails:
        r.storage.delete.side_effect = StorageDeleteError()
        with pytest.raises(ReceiptStorageFailure) as exc:
            reply(r)
        assert exc.value.cleanup_failed
    else:
        reply(r)
    r.pending_repo.complete.assert_not_awaited()
    r.storage.delete.assert_awaited_once_with(row.image_storage_key)
    r.telegram.send_message.assert_awaited_once_with(123, EXPIRED)


@pytest.mark.parametrize("cleanup", [True, False])
def test_duplicate_completion_only_deletes_owned_unreferenced_object(pending_rig, cleanup):
    r = pending_rig
    row = awaiting(r)
    r.pending_repo.complete.side_effect = DuplicateReceiptPersistenceError()
    r.pending_repo.close.return_value = terminal(row, "duplicate", cleanup=cleanup)
    reply(r)
    r.pending_repo.close.assert_awaited_once_with(row.id, 123, 123, "duplicate")
    assert r.storage.delete.await_count == int(cleanup)
    if cleanup:
        r.storage.delete.assert_awaited_once_with(row.image_storage_key)
    assert "already been recorded" in r.telegram.send_message.call_args.args[1]


def test_second_completion_inactive_never_claims_success(pending_rig):
    r = pending_rig
    awaiting(r)
    r.pending_repo.complete.return_value = PendingResult(outcome="inactive")
    reply(r)
    r.telegram.send_message.assert_awaited_once_with(123, NO_PENDING)
    r.storage.delete.assert_not_awaited()


def test_completion_confirmation_failure_keeps_business_state(pending_rig):
    r = pending_rig
    row = awaiting(r)
    r.pending_repo.complete.return_value = completed(row)
    r.telegram.send_message.side_effect = TelegramTimeoutError()
    with pytest.raises(ReceiptNotificationFailure) as exc:
        reply(r)
    assert exc.value.committed
    r.pending_repo.close.assert_not_awaited()
    r.storage.delete.assert_not_awaited()


def test_atomic_db_error_preserves_pending_image(pending_rig):
    r = pending_rig
    awaiting(r)
    original = PersistenceUnavailableError()
    r.pending_repo.complete.side_effect = original
    with pytest.raises(ReceiptPersistenceFailure) as exc:
        reply(r)
    assert exc.value.failure is original
    r.storage.delete.assert_not_awaited()


def test_low_confidence_unknown_no_pending_or_prompt(pending_rig):
    r = pending_rig
    r.vision.extract.return_value = r.extraction.model_copy(
        update={"confidence_score": Confidence.LOW}
    )
    asyncio.run(r.processor.process(r.event))
    r.pending_repo.create_pending.assert_not_awaited()
    r.storage.store.assert_not_awaited()


def test_existing_final_unknown_skips_pending_storage(pending_rig):
    r = pending_rig
    r.receipts.find_duplicate.return_value = object()
    asyncio.run(r.processor.process(r.event))
    r.pending_repo.create_pending.assert_not_awaited()
    r.storage.store.assert_not_awaited()


def test_contextual_memory_separate_app_instances_and_updates(pending_rig):
    r = pending_rig
    durable = {"pending": None, "vendor": None, "receipts": []}
    updates = Updates()

    async def active(*args):
        row = durable["pending"]
        return row if row and row.state == "awaiting_category" else None

    async def insert(row):
        durable["pending"] = row
        return row

    async def complete(pending_id, chat, user, category):
        result = completed(durable["pending"], category)
        durable["pending"] = result.pending
        durable["receipts"].append(result.receipt)
        durable["vendor"] = Vendor(
            id=uuid4(),
            normalized_name="abc hardware",
            display_name="ABC Hardware",
            category=category,
            created_at=datetime.now(UTC),
        )
        return result

    async def vendor(name):
        return durable["vendor"]

    r.pending_repo.get_awaiting_for_chat_user.side_effect = active
    r.pending_repo.create_pending.side_effect = insert
    r.pending_repo.complete.side_effect = complete
    r.vendors.find_by_normalized_name.side_effect = vendor

    async def deliver(body):
        # New workflow/processor/app for every webhook; only durable fake DB survives.
        workflow = PendingWorkflow(r.config, r.pending_repo, r.storage, r.telegram)
        processor = ReceiptProcessor(
            r.config, r.images, r.vision, r.vendors, r.receipts, r.storage, r.telegram, workflow
        )
        ingress = TelegramIngressService(r.config, updates, r.telegram, processor)
        app = create_app(r.config, ingress)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
        ) as client:
            return await client.post(
                "/webhooks/telegram",
                json=body,
                headers={"X-Telegram-Bot-Api-Secret-Token": "fake-webhook-secret"},
            )

    assert asyncio.run(deliver(payload())).json() == {"status": "handled"}
    text = payload()
    text["update_id"] = 8
    text["message"].pop("photo")
    text["message"]["text"] = " Maintenance "
    assert asyncio.run(deliver(text)).json() == {"status": "handled"}
    assert asyncio.run(deliver(text)).json() == {"status": "duplicate"}
    assert durable["pending"].state == "completed" and durable["vendor"].category == "Maintenance"
    r.vision.extract.return_value = r.extraction.model_copy(
        update={"date": r.extraction.date + timedelta(days=1)}
    )
    another = payload()
    another["update_id"] = 9
    assert asyncio.run(deliver(another)).json() == {"status": "handled"}
    assert r.receipts.create_receipt.call_args.args[0].category == "Maintenance"
    assert (
        r.pending_repo.create_pending.await_count == 1 and r.pending_repo.complete.await_count == 1
    )
    assert r.vision.extract.await_count == 2 and r.storage.store.await_count == 2
    prompts = [
        call.args[1]
        for call in r.telegram.send_message.call_args_list
        if "Which expense category" in call.args[1]
    ]
    assert len(prompts) == 1 and updates.rows == {7: "handled", 8: "handled", 9: "handled"}


@pytest.mark.parametrize("failure", ["prompt", "completion", "confirmation"])
def test_webhook_failure_lifecycle_preserves_pending_or_completed_work(pending_rig, failure):
    r = pending_rig
    body = payload()
    if failure == "prompt":
        r.telegram.send_message.side_effect = TelegramTimeoutError()
    else:
        row = awaiting(r)
        body["message"].pop("photo")
        body["message"]["text"] = "Maintenance"
        if failure == "completion":
            r.pending_repo.complete.side_effect = PersistenceError()
        else:
            r.pending_repo.complete.return_value = completed(row)
            r.telegram.send_message.side_effect = TelegramTimeoutError()
    updates = Updates()
    service = TelegramIngressService(r.config, updates, r.telegram, r.processor)
    from second_brain_receipts.schemas.telegram import TelegramUpdate

    event = TelegramUpdate.model_validate(body)
    assert asyncio.run(service.handle(event)) == "failed"
    assert updates.rows == {7: "failed"}
    assert asyncio.run(service.handle(event)) == "duplicate"
    r.storage.delete.assert_not_awaited()


def test_expired_photo_guard_closes_before_download(pending_rig):
    r = pending_rig
    row = awaiting(r).model_copy(
        update={
            "created_at": datetime.now(UTC) - timedelta(hours=1),
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    r.pending_repo.get_awaiting_for_chat_user.return_value = row
    r.pending_repo.close.return_value = terminal(row, "expired")
    assert asyncio.run(r.workflow.before_photo(123, 123, 8))
    r.telegram.get_file.assert_not_awaited()
    r.storage.delete.assert_awaited_once()
    r.telegram.send_message.assert_awaited_once_with(123, EXPIRED)


def test_repeated_cancel_after_closed_state_has_no_cleanup(pending_rig):
    r = pending_rig
    row = awaiting(r)
    r.pending_repo.close.return_value = terminal(row, "cancelled")
    reply(r, "cancel")
    r.pending_repo.get_awaiting_for_chat_user.return_value = None
    reply(r, "cancel")
    r.pending_repo.close.assert_awaited_once()
    r.storage.delete.assert_awaited_once()
    assert r.telegram.send_message.call_args.args[1] == NO_PENDING


def test_expiry_detected_inside_atomic_rpc(pending_rig):
    from second_brain_receipts.repositories.errors import PendingExpiredError

    r = pending_rig
    row = awaiting(r)
    r.pending_repo.complete.side_effect = PendingExpiredError()
    r.pending_repo.close.return_value = terminal(row, "expired")
    reply(r)
    r.pending_repo.close.assert_awaited_once_with(row.id, 123, 123, "expired")
    r.telegram.send_message.assert_awaited_once_with(123, EXPIRED)
