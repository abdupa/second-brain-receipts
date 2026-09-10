import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from second_brain_receipts.core.config import Settings
from second_brain_receipts.repositories.errors import (
    AmbiguousPersistenceError,
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PendingReceiptConflictError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.repositories.supabase import (
    SupabasePendingReceiptRepository,
    SupabaseReceiptRepository,
    SupabaseVendorRepository,
)
from second_brain_receipts.repositories.supabase_client import open_supabase_client
from second_brain_receipts.schemas.receipts import PendingReceipt, Receipt, WorkflowState
from second_brain_receipts.schemas.vendors import Vendor, VendorWrite

SECRET = "test-service-key-never-real"


@pytest.fixture
def row():
    return dict(
        id=str(uuid4()),
        telegram_chat_id=1,
        vendor_name="ABC Hardware",
        normalized_vendor_name="abc hardware",
        receipt_date="2026-09-10",
        total_amount="12.30",
        vat_amount="0.10",
        category="Maintenance",
        confidence_score="High",
        image_storage_key="receipts/opaque.jpg",
        image_storage_uri="s3://test-bucket/receipts/opaque.jpg",
        created_at=datetime.now(UTC).isoformat(),
    )


@pytest.fixture
def pending_row(row):
    return dict(
        **row,
        telegram_user_id=2,
        state="awaiting_category",
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )


@pytest.fixture
def vendor_row():
    return dict(
        id=str(uuid4()),
        normalized_name="abc hardware",
        display_name="ABC Hardware",
        category="Maintenance",
        created_at=datetime.now(UTC).isoformat(),
    )


def run_query(operation, body, *, status=200, failure=None):
    requests = []

    def handle(request):
        requests.append(request)
        if failure:
            raise failure
        return httpx.Response(status, json=body)

    async def run():
        settings = Settings(
            app_env="test",
            _env_file=None,
            supabase_url="https://example.invalid",
            supabase_service_role_key=SECRET,
        )
        async with open_supabase_client(settings, transport=httpx.MockTransport(handle)) as client:
            return await operation(client)

    return lambda: asyncio.run(run()), requests


def test_vendor_lookup_and_absent(vendor_row):
    async def find(client):
        return await SupabaseVendorRepository(client).find_by_normalized_name("abc hardware")

    run, requests = run_query(find, [vendor_row])
    assert run() == Vendor(**vendor_row)
    assert requests[0].url.params["normalized_name"] == "eq.abc hardware"
    run, _ = run_query(find, [])
    assert run() is None


def test_vendor_upsert_uses_normalized_identity(vendor_row):
    vendor = VendorWrite(
        normalized_name="abc hardware", display_name="ABC Hardware", category="Maintenance"
    )

    async def upsert(client):
        return await SupabaseVendorRepository(client).upsert_vendor(vendor)

    run, requests = run_query(upsert, [vendor_row])
    assert run().category == vendor.category
    assert requests[0].method == "POST"
    assert requests[0].url.params["on_conflict"] == "normalized_name"
    assert json.loads(requests[0].content) == vendor.model_dump(mode="json")
    assert "resolution=merge-duplicates" in requests[0].headers["prefer"]


@pytest.mark.parametrize("amount", ["0.10", "12.30", "999999.99", "9999999999.99"])
def test_receipt_exact_roundtrip_and_insert_not_upsert(row, amount):
    row.update(total_amount=amount, vat_amount=None)
    receipt = Receipt(**row)

    async def create(client):
        return await SupabaseReceiptRepository(client).create_receipt(receipt)

    run, requests = run_query(create, [row])
    result = run()
    assert result.total_amount == Decimal(amount)
    assert result.total_amount.as_tuple().exponent == -2
    sent = json.loads(requests[0].content)
    assert sent["total_amount"] == amount and sent["vat_amount"] is None
    assert "total_amount::text" in requests[0].url.params["select"]
    assert "vat_amount::text" in requests[0].url.params["select"]
    assert "on_conflict" not in requests[0].url.params
    assert "resolution" not in requests[0].headers["prefer"]


def test_duplicate_lookup_found_and_absent(row):
    async def find(client):
        return await SupabaseReceiptRepository(client).find_duplicate(
            "abc hardware", date(2026, 9, 10), Decimal("12.30")
        )

    run, requests = run_query(find, [row])
    assert run() == Receipt(**row)
    assert requests[0].url.params["total_amount"] == "eq.12.30"
    assert requests[0].url.params["receipt_date"] == "eq.2026-09-10"
    run, _ = run_query(find, [])
    assert run() is None


def test_pending_create_and_lookup(pending_row):
    pending = PendingReceipt(**pending_row)

    async def create(client):
        return await SupabasePendingReceiptRepository(client).create_pending(pending)

    run, requests = run_query(create, [pending_row])
    assert run() == pending
    assert json.loads(requests[0].content)["image_storage_uri"] == pending.image_storage_uri

    async def get(client):
        return await SupabasePendingReceiptRepository(client).get_awaiting_for_chat_user(1, 2)

    run, requests = run_query(get, [pending_row])
    assert run() == pending
    for key, value in [
        ("telegram_chat_id", "eq.1"),
        ("telegram_user_id", "eq.2"),
        ("state", "eq.awaiting_category"),
    ]:
        assert requests[0].url.params[key] == value
    run, _ = run_query(get, [])
    assert run() is None

    async def by_id(client):
        return await SupabasePendingReceiptRepository(client).get_by_id(pending.id)

    run, requests = run_query(by_id, [pending_row])
    assert run() == pending
    assert requests[0].url.params["id"] == f"eq.{pending.id}"


def test_pending_compare_and_set_update(pending_row):
    pending_row["state"] = "expired"
    pending = PendingReceipt(**pending_row)

    async def update(client):
        return await SupabasePendingReceiptRepository(client).update_state(
            pending.id, WorkflowState.AWAITING_CATEGORY, WorkflowState.EXPIRED
        )

    run, requests = run_query(update, [pending_row])
    assert run().state is WorkflowState.EXPIRED
    assert requests[0].method == "PATCH"
    assert requests[0].url.params["state"] == "eq.awaiting_category"
    assert json.loads(requests[0].content) == {"state": "expired"}
    run, _ = run_query(update, [])
    with pytest.raises(PendingReceiptConflictError):
        run()


@pytest.mark.parametrize(
    ("code", "message", "error"),
    [
        ("23505", 'constraint "receipts_vendor_date_total_key"', DuplicateReceiptPersistenceError),
        ("23505", 'constraint "pending_receipts_one_question_idx"', PendingReceiptConflictError),
        ("23505", 'constraint "receipts_pkey"', PersistenceError),
        ("23514", "check violated", PersistenceError),
        # Unavailability during an INSERT is uncertain (the write may have actually
        # committed), so the bounded retry/reconciliation path applies. When the mocked
        # service keeps failing every request, including the reconciliation read, the
        # write's true outcome can never be confirmed either way; that is surfaced as
        # AmbiguousPersistenceError, never a plain "unavailable, safe to just retry" claim.
        ("PGRST002", "unavailable", AmbiguousPersistenceError),
        ("08006", "connection failure", AmbiguousPersistenceError),
        ("40001", "serialization conflict", AmbiguousPersistenceError),
        (None, "unknown failure", PersistenceError),
    ],
)
def test_provider_error_mapping(row, code, message, error, caplog):
    async def create(client):
        return await SupabaseReceiptRepository(client).create_receipt(Receipt(**row))

    body = dict(code=code, message=message, details=SECRET, hint=None)
    run, requests = run_query(create, body, status=409)
    with pytest.raises(error) as exc:
        run()
    assert type(exc.value) is error
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert exc.value.__suppress_context__
    if error is AmbiguousPersistenceError:
        # The insert plus reconciliation reads all retry under the bounded policy
        # before giving up; more than the single deterministic-rejection request.
        assert len(requests) > 1
    else:
        assert len(requests) == 1


@pytest.mark.parametrize("body", [None, {}, [{}], [{"total_amount": "0"}], "not rows"])
def test_invalid_response(body):
    async def find(client):
        return await SupabaseVendorRepository(client).find_by_normalized_name("abc")

    run, _ = run_query(find, body)
    with pytest.raises(InvalidPersistenceResponseError):
        run()


def test_unexpected_multiple_rows_and_float_rejected(row):
    async def find(client):
        return await SupabaseReceiptRepository(client).find_duplicate(
            "abc hardware", date(2026, 9, 10), Decimal("12.30")
        )

    run, _ = run_query(find, [row, row])
    with pytest.raises(InvalidPersistenceResponseError):
        run()
    row["total_amount"] = 12.30
    run, _ = run_query(find, [row])
    with pytest.raises(InvalidPersistenceResponseError):
        run()


def test_network_failure_retries_bounded_then_unavailable():
    async def find(client):
        return await SupabaseVendorRepository(client).find_by_normalized_name("abc")

    # A read is idempotent and safe to retry, so the bounded policy applies (3 attempts)
    # before surfacing the safe, detail-free PersistenceUnavailableError.
    run, requests = run_query(find, None, failure=httpx.ReadTimeout(SECRET))
    with pytest.raises(PersistenceUnavailableError) as exc:
        run()
    assert SECRET not in str(exc.value) and len(requests) == 3
    run, requests = run_query(
        find, dict(code="503", message="unavailable", details=None, hint=None), status=503
    )
    with pytest.raises(PersistenceUnavailableError):
        run()
    assert len(requests) == 3


def test_stable_uri_validation(row):
    for uri in ["https://example.invalid/signed?token=secret", "s3://test-bucket/wrong.jpg"]:
        with pytest.raises(ValueError):
            Receipt(**{**row, "image_storage_uri": uri})


def test_factory_requires_credentials():
    async def missing():
        async with open_supabase_client(Settings(app_env="test", _env_file=None)):
            pytest.fail("missing credentials should prevent client creation")

    with pytest.raises(PersistenceError):
        asyncio.run(missing())


def test_pending_insert_collision(pending_row):
    async def create(client):
        return await SupabasePendingReceiptRepository(client).create_pending(
            PendingReceipt(**pending_row)
        )

    run, requests = run_query(
        create,
        dict(
            code="23505",
            hint=None,
            details=None,
            message='duplicate key violates unique constraint "pending_receipts_one_question_idx"',
        ),
        status=409,
    )
    with pytest.raises(PendingReceiptConflictError):
        run()
    assert len(requests) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 504])
def test_http_service_failures(status):
    async def find(client):
        return await SupabaseVendorRepository(client).find_by_normalized_name("abc")

    run, requests = run_query(find, None, status=status)
    with pytest.raises(PersistenceUnavailableError):
        run()
    # A read is idempotent and safe to retry under the bounded policy (3 attempts).
    assert len(requests) == 3


def test_supabase_context_closes_http_client():
    async def run():
        settings = Settings(
            app_env="test",
            _env_file=None,
            supabase_url="https://example.invalid",
            supabase_service_role_key=SECRET,
        )
        async with open_supabase_client(
            settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        ) as client:
            http = client.options.httpx_client
            assert not http.is_closed
            assert client.options.auto_refresh_token is False
            assert client.options.persist_session is False
        assert http.is_closed

    asyncio.run(run())
