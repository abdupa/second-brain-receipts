import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from second_brain_receipts.core.config import Settings
from second_brain_receipts.repositories.errors import (
    AmbiguousPersistenceError,
    DuplicateReceiptPersistenceError,
    InvalidPersistenceResponseError,
    PendingExpiredError,
)
from second_brain_receipts.repositories.supabase import SupabasePendingWorkflowRepository
from second_brain_receipts.repositories.supabase_client import open_supabase_client


@pytest.fixture
def result():
    now = datetime.now(UTC)
    pending = dict(
        id=str(uuid4()),
        telegram_chat_id=123,
        telegram_user_id=123,
        vendor_name="ABC",
        normalized_vendor_name="abc",
        receipt_date="2026-09-10",
        total_amount="12.30",
        vat_amount="0.10",
        category="Maintenance",
        confidence_score="High",
        image_storage_key="a.jpg",
        image_storage_uri="s3://test-bucket/a.jpg",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=30)).isoformat(),
        state="completed",
    )
    receipt = {
        key: value
        for key, value in pending.items()
        if key not in ("telegram_user_id", "state", "expires_at")
    }
    return dict(outcome="completed", pending=pending, receipt=receipt, cleanup_allowed=False)


def run_query(body, pending_id, *, status=200, close=False):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    async def run():
        config = Settings(
            app_env="test",
            _env_file=None,
            supabase_url="https://example.invalid",
            supabase_service_role_key="fake",
        )
        async with open_supabase_client(config, transport=httpx.MockTransport(handler)) as client:
            repo = SupabasePendingWorkflowRepository(client)
            if close:
                return await repo.close(pending_id, 123, 123, "cancelled")
            return await repo.complete(pending_id, 123, 123, " Maintenance ")

    return lambda: asyncio.run(run()), requests


def test_completion_rpc_exact_decimal_typed_result_and_narrow_inputs(result):
    from uuid import UUID

    run, requests = run_query([result], UUID(result["pending"]["id"]))
    output = run()
    assert str(output.receipt.total_amount) == "12.30" and str(output.receipt.vat_amount) == "0.10"
    assert requests[0].url.path == "/rest/v1/rpc/complete_pending_receipt"
    assert json.loads(requests[0].content) == dict(
        p_pending_id=result["pending"]["id"], p_chat_id=123, p_user_id=123, p_category="Maintenance"
    )
    assert len(requests) == 1


def test_close_rpc_inactive():
    run, requests = run_query(
        [dict(outcome="inactive", pending=None, receipt=None, cleanup_allowed=False)],
        uuid4(),
        close=True,
    )
    assert run().outcome == "inactive" and requests[0].url.path.endswith("close_pending_receipt")
    assert json.loads(requests[0].content)["p_reason"] == "cancelled"


@pytest.mark.parametrize(
    "code,error",
    [
        ("23505", DuplicateReceiptPersistenceError),
        ("P0002", PendingExpiredError),
        # 503 is uncertain, not definitive: completion is a vendor+receipt+pending write,
        # so the bounded retry/reconciliation path applies. When the mocked service (and
        # its reconciliation reads) keep failing, the true outcome can never be confirmed
        # either way, so it surfaces as AmbiguousPersistenceError, never a confident
        # "unavailable, nothing happened" claim.
        ("503", AmbiguousPersistenceError),
    ],
)
def test_rpc_error_mapping(code, error):
    body = {
        "code": code,
        "message": 'duplicate constraint "receipts_vendor_date_total_key"',
        "details": "private",
        "hint": None,
    }
    run, requests = run_query(body, uuid4(), status=503 if code == "503" else 409)
    with pytest.raises(error) as exc:
        run()
    assert type(exc.value) is error
    if code == "503":
        assert len(requests) > 1
    else:
        assert len(requests) == 1


@pytest.mark.parametrize(
    "mutation",
    ["money_float", "wrong_owner", "wrong_id", "missing_receipt", "unsafe_cleanup", "empty"],
)
def test_invalid_rpc_result_rejected(result, mutation):
    from uuid import UUID

    pending_id = UUID(result["pending"]["id"])
    if mutation == "money_float":
        result["receipt"]["total_amount"] = 12.3
    elif mutation == "wrong_owner":
        result["pending"]["telegram_user_id"] = 999
    elif mutation == "wrong_id":
        result["receipt"]["id"] = str(uuid4())
    elif mutation == "missing_receipt":
        result["receipt"] = None
    elif mutation == "unsafe_cleanup":
        result["cleanup_allowed"] = True
    run, _ = run_query([] if mutation == "empty" else [result], pending_id)
    with pytest.raises(InvalidPersistenceResponseError):
        run()
