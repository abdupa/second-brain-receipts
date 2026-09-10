import asyncio
import base64
import json
import logging
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from openai import APIError, APIResponseValidationError, AsyncOpenAI
from PIL import Image

from second_brain_receipts.core.config import Settings
from second_brain_receipts.providers.openai_vision import (
    OpenAIReceiptVisionProvider,
    create_openai_client,
    extraction_json_schema,
)
from second_brain_receipts.providers.vision import (
    ReceiptExtractionError,
    VisionAuthenticationError,
    VisionProviderError,
    VisionRateLimitError,
    VisionResponseError,
    VisionTimeoutError,
    VisionUnavailableError,
)
from second_brain_receipts.schemas.receipts import Confidence, ReceiptExtraction
from second_brain_receipts.services.image_service import ImageService

FAKE_KEY = "test-only-secret-never-real"
PRIVATE = "PRIVATE-RECEIPT-CONTENT"


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings():
    return Settings(app_env="test", _env_file=None)


@pytest.fixture
def image(settings):
    output = BytesIO()
    with Image.new("RGB", (320, 240), "white") as source:
        source.save(output, "PNG")
    return ImageService(settings).process(output.getvalue())


@pytest.fixture
def receipt():
    return {
        "vendor_name": "  ABC Hardware  ",
        "date": "2026-09-10",
        "total_amount": 100.50,
        "vat_amount": 10.05,
        "category": "  Maintenance  ",
        "confidence_score": "High",
    }


def response_body(text):
    return {
        "id": "resp_test",
        "created_at": 0,
        "model": "gpt-4o",
        "object": "response",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def run_provider(image, settings, body, *, status=200, failure=None):
    requests = []

    def handler(request):
        requests.append(request)
        if failure:
            raise failure(request)
        return httpx2.Response(status, json=body)

    async def extract():
        async with AsyncOpenAI(
            api_key=FAKE_KEY,
            base_url="https://example.invalid/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        ) as client:
            provider = OpenAIReceiptVisionProvider(client, settings)
            return await provider.extract(image)

    return extract, requests


@pytest.mark.parametrize("confidence", list(Confidence))
def test_success(image, settings, receipt, confidence):
    receipt["confidence_score"] = confidence.value
    extract, requests = run_provider(
        image, settings, response_body(json.dumps({"receipt": receipt}))
    )
    result = asyncio.run(extract())
    assert isinstance(result, ReceiptExtraction)
    assert result.vendor_name == "ABC Hardware"
    assert result.date == date(2026, 9, 10)
    assert result.total_amount == Decimal("100.50")
    assert result.vat_amount == Decimal("10.05")
    assert isinstance(result.total_amount, Decimal)
    assert result.category == "Maintenance"
    assert result.confidence_score is confidence
    assert len(requests) == 1


@pytest.mark.parametrize("missing", [True, False])
def test_optional_vat_and_category(image, settings, receipt, missing):
    for field in ("vat_amount", "category"):
        if missing:
            receipt.pop(field)
        else:
            receipt[field] = None
    extract, _ = run_provider(image, settings, response_body(json.dumps({"receipt": receipt})))
    result = asyncio.run(extract())
    assert result.vat_amount is None and result.category is None


def test_exact_numeric_token_parsing(image, settings, receipt):
    text = json.dumps({"receipt": receipt}).replace("100.5", "9999999999.99")
    extract, _ = run_provider(image, settings, response_body(text))
    assert asyncio.run(extract()).total_amount == Decimal("9999999999.99")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor_name", "  "),
        ("vendor_name", None),
        ("date", "09/10/2026"),
        ("date", "2026-02-30"),
        ("date", 0),
        ("total_amount", 0),
        ("total_amount", -1),
        ("total_amount", None),
        ("total_amount", "100.50"),
        ("total_amount", True),
        ("total_amount", 1.001),
        ("total_amount", 10000000000),
        ("vat_amount", -1),
        ("vat_amount", 101),
        ("vat_amount", "10.05"),
        ("confidence_score", "Maybe"),
        ("category", " "),
        ("extra", "unexpected"),
    ],
)
def test_business_validation(image, settings, receipt, field, value):
    receipt[field] = value
    extract, _ = run_provider(image, settings, response_body(json.dumps({"receipt": receipt})))
    with pytest.raises(VisionResponseError):
        asyncio.run(extract())


@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        "{}",
        '{"receipt": {}}',
        '{"receipt": null, "extra": 1}',
        '{"receipt":null,"receipt":null}',
        '{"receipt": {"total_amount": NaN}}',
        '{"receipt": {"total_amount": Infinity}}',
        '{"receipt": {"total_amount": -Infinity}}',
        '{"receipt": {"total_amount": 1e999999999999999999999}}',
        "x" * 32769,
    ],
)
def test_malformed_structured_output(image, settings, text):
    extract, _ = run_provider(image, settings, response_body(text))
    with pytest.raises(VisionResponseError):
        asyncio.run(extract())


def test_unreadable_does_not_invent_fields(image, settings):
    extract, _ = run_provider(image, settings, response_body('{"receipt":null}'))
    with pytest.raises(ReceiptExtractionError) as exc:
        asyncio.run(extract())
    assert str(exc.value) == "receipt_unextractable"
    assert not exc.value.transient


def test_refusal_is_safe_extraction_error(image, settings):
    body = response_body("")
    body["output"][0]["content"] = [{"type": "refusal", "refusal": PRIVATE}]
    extract, _ = run_provider(image, settings, body)
    with pytest.raises(ReceiptExtractionError) as exc:
        asyncio.run(extract())
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", "queued", "cancelled"])
def test_non_completed_responses(image, settings, receipt, status):
    body = response_body(json.dumps({"receipt": receipt}))
    body["status"] = status
    extract, _ = run_provider(image, settings, body)
    with pytest.raises(VisionResponseError):
        asyncio.run(extract())


@pytest.mark.parametrize(
    "variation",
    [
        "empty",
        "no_output",
        "wrong_output",
        "double_text",
        "message_incomplete",
        "incomplete_details",
    ],
)
def test_malformed_response_envelope(image, settings, receipt, variation):
    body = response_body(json.dumps({"receipt": receipt}))
    if variation == "empty":
        body["output"] = []
    elif variation == "no_output":
        del body["output"]
    elif variation == "wrong_output":
        body["output"] = PRIVATE
    elif variation == "double_text":
        body["output"][0]["content"] *= 2
    elif variation == "message_incomplete":
        body["output"][0]["status"] = "incomplete"
    else:
        body["incomplete_details"] = {"reason": "max_output_tokens"}
    extract, _ = run_provider(image, settings, body)
    with pytest.raises(VisionResponseError):
        asyncio.run(extract())


@pytest.mark.parametrize(
    ("status", "error", "transient"),
    [
        (401, VisionAuthenticationError, False),
        (403, VisionAuthenticationError, False),
        (429, VisionRateLimitError, True),
        (500, VisionUnavailableError, True),
        (503, VisionUnavailableError, True),
        (408, VisionUnavailableError, True),
        (409, VisionUnavailableError, True),
        (400, VisionResponseError, False),
        (404, VisionResponseError, False),
        (422, VisionResponseError, False),
    ],
)
def test_sdk_http_errors_and_no_retries(image, settings, status, error, transient, caplog):
    caplog.set_level(logging.DEBUG)
    encoded = base64.b64encode(image.data).decode()
    body = {"error": {"message": f"{FAKE_KEY} {PRIVATE} {encoded}", "type": "test_error"}}
    extract, requests = run_provider(image, settings, body, status=status)
    with pytest.raises(error) as exc:
        asyncio.run(extract())
    assert exc.value.transient is transient
    # Non-transient failures (auth/bad request) never retry; transient failures (rate
    # limit/unavailable) use the bounded provider retry policy (3 attempts).
    assert len(requests) == (3 if transient else 1)
    for secret in (FAKE_KEY, PRIVATE, encoded):
        assert secret not in str(exc.value)
        assert secret not in caplog.text
    assert exc.value.__suppress_context__


@pytest.mark.parametrize(
    ("http_error", "error"),
    [
        (httpx2.ReadTimeout, VisionTimeoutError),
        (httpx2.ConnectError, VisionUnavailableError),
    ],
)
def test_sdk_transport_failures(image, settings, http_error, error):
    extract, requests = run_provider(
        image, settings, None, failure=lambda request: http_error(PRIVATE, request=request)
    )
    with pytest.raises(error) as exc:
        asyncio.run(extract())
    assert exc.value.transient
    assert PRIVATE not in str(exc.value)
    # Both mapped errors are transient, so the bounded provider retry policy applies.
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("server_error", VisionUnavailableError),
        ("rate_limit_exceeded", VisionRateLimitError),
        ("invalid_image", VisionResponseError),
    ],
)
def test_response_level_errors(image, settings, code, error):
    body = response_body("")
    body["status"] = "failed"
    body["error"] = {"code": code, "message": PRIVATE}
    extract, _ = run_provider(image, settings, body)
    with pytest.raises(error):
        asyncio.run(extract())


def test_image_transport_model_and_strict_schema(image, settings, receipt, caplog):
    caplog.set_level(logging.DEBUG)
    settings = Settings(app_env="test", _env_file=None, openai_model="gpt-4o-2024-11-20")
    extract, requests = run_provider(
        image, settings, response_body(json.dumps({"receipt": receipt}))
    )
    asyncio.run(extract())
    request = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert request["model"] == "gpt-4o-2024-11-20"
    item = request["input"][0]["content"][0]
    header, encoded = item["image_url"].split(",", 1)
    assert header == "data:image/jpeg;base64"
    assert base64.b64decode(encoded) == image.data
    assert item["type"] == "input_image" and item["detail"] == "high"
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["store"] is False
    assert request["temperature"] == 0
    schema = request["text"]["format"]["schema"]
    fields = schema["properties"]["receipt"]["anyOf"][0]
    assert set(fields["properties"]) == set(ReceiptExtraction.model_fields)
    assert set(fields["required"]) == set(ReceiptExtraction.model_fields)
    assert fields["additionalProperties"] is False
    assert fields["properties"]["total_amount"]["type"] == "number"
    for value in (encoded, FAKE_KEY, "ABC Hardware", "100.5"):
        assert value not in caplog.text
    log = next(r for r in caplog.records if r.name.endswith("openai_vision"))
    assert log.duration_ms >= 0 and log.outcome == "success"
    assert log.confidence == "High"


def test_default_model(settings):
    assert settings.openai_model == "gpt-4o"
    assert extraction_json_schema()["required"] == ["receipt"]


def test_client_factory_requires_key_and_has_explicit_lifetime(settings):
    with pytest.raises(VisionAuthenticationError):
        create_openai_client(settings)

    async def construct():
        settings = Settings(app_env="test", _env_file=None, openai_api_key=FAKE_KEY)
        async with create_openai_client(settings) as client:
            assert client.max_retries == 0
            assert client.timeout == 30
        assert client.is_closed()

    asyncio.run(construct())


def fake_client():
    client = Mock(spec=AsyncOpenAI)
    client.with_options.return_value = client
    client.responses = Mock()
    client.responses.create = AsyncMock()
    return client


def test_deadline_and_reusable_client(image):
    client = fake_client()
    calls = 0

    async def delayed(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)

    client.responses.create.side_effect = delayed
    settings = Settings(app_env="test", _env_file=None, openai_timeout_seconds=0.01)
    provider = OpenAIReceiptVisionProvider(client, settings)
    for _ in range(2):
        with pytest.raises(VisionTimeoutError):
            asyncio.run(provider.extract(image))
    # Each extract() call retries the transient timeout up to the bounded policy (3
    # attempts); with_options is still called once at construction, not per attempt.
    assert calls == 6
    client.with_options.assert_called_once_with(max_retries=0, timeout=0.01)


def test_task_cancellation_propagates(image, settings):
    client = fake_client()
    client.responses.create.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(OpenAIReceiptVisionProvider(client, settings).extract(image))


@pytest.mark.parametrize("kind", ["api_validation", "api_error", "bad_sdk_result"])
def test_other_sdk_failures_are_safe(image, settings, kind):
    client = fake_client()
    request = httpx2.Request("POST", "https://example.invalid")
    if kind == "api_validation":
        client.responses.create.side_effect = APIResponseValidationError(
            httpx2.Response(200, request=request), body={"secret": PRIVATE}
        )
        error = VisionResponseError
    elif kind == "api_error":
        client.responses.create.side_effect = APIError(PRIVATE, request=request, body=None)
        error = VisionProviderError
    else:
        client.responses.create.return_value = PRIVATE
        error = VisionResponseError
    with pytest.raises(error) as exc:
        asyncio.run(OpenAIReceiptVisionProvider(client, settings).extract(image))
    assert PRIVATE not in str(exc.value)
