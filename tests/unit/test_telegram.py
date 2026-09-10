import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from second_brain_receipts.core.config import Settings
from second_brain_receipts.domain.ingress import (
    IncomingPhotoMessage,
    IncomingTextMessage,
    UnsupportedMessage,
)
from second_brain_receipts.main import create_app
from second_brain_receipts.repositories.errors import (
    InvalidPersistenceResponseError,
    PersistenceError,
    PersistenceUnavailableError,
)
from second_brain_receipts.repositories.supabase_client import open_supabase_client
from second_brain_receipts.repositories.telegram_updates import SupabaseUpdateRepository
from second_brain_receipts.schemas.telegram import TelegramUpdate
from second_brain_receipts.services.telegram_service import TelegramIngressService
from second_brain_receipts.telegram.client import HTTPTelegramClient
from second_brain_receipts.telegram.errors import (
    TelegramAmbiguousSendError,
    TelegramAuthenticationError,
    TelegramDownloadError,
    TelegramRateLimitError,
    TelegramResponseError,
    TelegramTimeoutError,
    TelegramUnavailableError,
)

TOKEN = "123456:fake-token-only"
SECRET = "test-webhook-secret"


def settings(**kwargs):
    return Settings(
        app_env="test",
        _env_file=None,
        telegram_bot_token=TOKEN,
        telegram_webhook_secret=SECRET,
        telegram_allowed_user_id=123,
        **kwargs,
    )


def update(**message):
    return {
        "update_id": 7,
        "message": {
            "message_id": 9,
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "text": " Food ",
            **message,
        },
    }


class Updates:
    def __init__(self):
        self.rows = {}
        self.failure = None

    async def claim_update(self, update_id):
        if self.failure:
            raise self.failure
        if update_id in self.rows:
            return False
        self.rows[update_id] = "received"
        await asyncio.sleep(0)
        return True

    async def finish_update(self, update_id, status):
        self.rows[update_id] = status


class API:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def send_message(self, chat_id, text):
        pass

    async def get_file(self, file_id):
        self.calls.append(file_id)
        if self.failure:
            raise self.failure
        return "photos/file.jpg"

    async def download_file(self, path):
        self.calls.append(path)
        return b"raw-unvalidated-bytes"


class Handler:
    async def before_photo(self, *args):
        return False

    def __init__(self):
        self.events = []

    async def accept(self, event):
        self.events.append(event)


def boundary(**overrides):
    config = settings(**overrides)
    repo, api, handler = Updates(), API(), Handler()
    return config, repo, api, handler, TelegramIngressService(config, repo, api, handler)


def request(
    payload=None,
    *,
    headers=None,
    content=None,
    path="/webhooks/telegram",
    method="POST",
    parts=None,
):
    config, repo, api, handler, service = parts or boundary()

    async def run():
        app = create_app(config, service)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
            ) as client,
        ):
            return await client.request(
                method,
                path,
                json=payload,
                content=content,
                headers=headers
                if headers is not None
                else {"X-Telegram-Bot-Api-Secret-Token": SECRET},
            )

    return asyncio.run(run()), repo, api, handler


@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}])
def test_auth_rejected_before_processing(headers, caplog):
    response, repo, api, handler = request(update(), headers=headers)
    assert response.status_code == 401
    assert not repo.rows and not api.calls and not handler.events
    assert SECRET not in response.text + caplog.text


def test_valid_auth_trimmed_text_and_safe_logs(caplog):
    caplog.set_level(logging.DEBUG)
    response, repo, _, handler = request(update(text=" confidential Food "))
    assert response.json() == {"status": "handled"}
    assert repo.rows == {7: "handled"}
    event = handler.events[0]
    assert isinstance(event, IncomingTextMessage)
    assert event.text == " confidential Food " and event.received_at.tzinfo is not None
    assert "confidential" not in repr(event) + caplog.text
    assert SECRET not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        {"from": {"id": 999, "username": "trusted"}},
        {"from": None},
        {"chat": {"id": -123, "type": "group"}},
        {"chat": {"id": 999, "type": "private"}},
    ],
)
def test_unauthorized_ignored_without_claim(message):
    response, repo, api, handler = request(update(**message))
    assert response.json() == {"status": "ignored"}
    assert not repo.rows and not api.calls and not handler.events


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"update_id": True},
        {"update_id": "7"},
        {"update_id": -1},
        update(**{"from": {"username": "trusted"}}),
        update(**{"from": {"id": "123"}}),
        update(photo=[{"file_id": "x"}]),
    ],
)
def test_malformed_updates_safe(payload):
    response, repo, _, _ = request(payload)
    assert response.status_code == 400 and response.json() == {"detail": "invalid update"}
    assert not repo.rows


@pytest.mark.parametrize(
    "message",
    [
        {"text": "/start"},
        {"text": "  /anything "},
        {"text": None, "sticker": {"file_id": "x"}},
        {"text": None, "audio": {"file_id": "x"}},
        {"text": None, "document": {"file_id": "x", "mime_type": "image/jpeg"}},
    ],
)
def test_unsupported_message_is_normalized(message):
    response, repo, api, handler = request(update(**message))
    assert response.json() == {"status": "unsupported"}
    assert repo.rows == {7: "handled"} and not api.calls
    assert isinstance(handler.events[0], UnsupportedMessage)


def test_other_update_safe():
    response, repo, _, handler = request({"update_id": 9, "callback_query": {"data": "private"}})
    assert response.json() == {"status": "unsupported"}
    assert not repo.rows and not handler.events


def photos():
    return [
        {"file_id": "small", "width": 100, "height": 100, "file_size": 50},
        {"file_id": "best", "width": 1000, "height": 1000, "file_size": 90},
        {"file_id": "over-limit", "width": 2000, "height": 2000, "file_size": 101},
        {"file_id": "tie-smaller", "width": 1000, "height": 1000, "file_size": 80},
    ]


def test_largest_suitable_photo():
    response, _, api, handler = request(
        update(photo=photos(), caption="not trusted"), parts=boundary(max_upload_bytes=100)
    )
    assert response.status_code == 200 and api.calls == ["best", "photos/file.jpg"]
    event = handler.events[0]
    assert isinstance(event, IncomingPhotoMessage)
    assert event.image_bytes == b"raw-unvalidated-bytes" and event.telegram_file_id == "best"
    assert "raw-unvalidated" not in repr(event)


def test_no_suitable_photo_records_failure():
    response, repo, api, handler = request(
        update(photo=photos()), parts=boundary(max_upload_bytes=1)
    )
    assert response.json() == {"status": "failed"}
    assert repo.rows == {7: "failed"} and not api.calls and not handler.events


def test_duplicate_http_has_no_repeated_effect():
    parts = boundary()
    assert request(update(), parts=parts)[0].json() == {"status": "handled"}
    assert request(update(), parts=parts)[0].json() == {"status": "duplicate"}
    assert len(parts[3].events) == 1


def test_overlapping_deliveries_have_one_handler():
    _, repo, _, handler, service = boundary()

    async def run():
        return await asyncio.gather(
            *(service.handle(TelegramUpdate.model_validate(update())) for _ in range(10))
        )

    outcomes = asyncio.run(run())
    assert outcomes.count("handled") == 1 and outcomes.count("duplicate") == 9
    assert len(handler.events) == 1 and repo.rows == {7: "handled"}


def test_failed_claimed_download_not_replayed(caplog):
    parts = boundary()
    parts[2].failure = TelegramTimeoutError()
    payload = update(photo=photos())
    assert request(payload, parts=parts)[0].json() == {"status": "failed"}
    assert request(payload, parts=parts)[0].json() == {"status": "duplicate"}
    assert len(parts[2].calls) == 1 and parts[1].rows == {7: "failed"}
    assert TOKEN not in caplog.text


def test_db_outage_is_503_not_duplicate():
    parts = boundary()
    parts[1].failure = PersistenceUnavailableError()
    response = request(update(), parts=parts)[0]
    assert response.status_code == 503 and not parts[3].events


@pytest.mark.parametrize("content", [b"{bad", b"null", b'"private-text"'])
def test_invalid_json_returns_safe_error(content):
    response = request(content=content)[0]
    assert response.status_code == 400 and "private-text" not in response.text


def test_oversize_content_length():
    response, repo, _, _ = request(content=b"x" * 65537)
    assert response.status_code == 413 and not repo.rows


@pytest.mark.parametrize("declared", [None, "1"])
def test_chunked_oversize_not_dependent_on_length(declared):
    async def body():
        yield b"x" * 40000
        yield b"x" * 30000

    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    if declared:
        headers["content-length"] = declared
    assert request(content=body(), headers=headers)[0].status_code == 413


def test_health_safe_without_provider_work():
    response, repo, api, _ = request(method="GET", path="/health")
    assert response.json() == {"status": "ok"}
    assert not repo.rows and not api.calls


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def call_adapter(operation, response=None, *, failure=None, limit=10):
    requests = []

    def handle(req):
        requests.append(req)
        if failure:
            raise failure
        return response

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await operation(HTTPTelegramClient(client, settings(max_upload_bytes=limit)))

    return lambda: asyncio.run(run()), requests


def test_get_file_success_safe_url_logging(caplog):
    caplog.set_level(logging.DEBUG)
    run, requests = call_adapter(
        lambda c: c.get_file("photo-id"),
        httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/file.jpg"}}),
    )
    assert run() == "photos/file.jpg"
    assert json.loads(requests[0].content) == {"file_id": "photo-id"}
    assert requests[0].url.scheme == "https"
    assert all(value > 0 for value in requests[0].extensions["timeout"].values())
    assert TOKEN not in caplog.text and "photo-id" not in caplog.text


@pytest.mark.parametrize("headers", [{}, {"content-length": "10"}])
def test_download_exact_limit(headers):
    stream = Chunks([b"12345", b"67890"])
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"),
        httpx.Response(200, headers=headers, stream=stream),
    )
    assert run() == b"1234567890" and stream.closed


def test_declared_oversize_aborts_before_read():
    stream = Chunks([b"unused"])
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"),
        httpx.Response(200, headers={"content-length": "11"}, stream=stream),
    )
    with pytest.raises(TelegramDownloadError):
        run()
    assert stream.closed and stream.reads == 0


@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
def test_stream_overflow_aborts(headers):
    stream = Chunks([b"123456", b"789012", b"must-not-read"])
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"),
        httpx.Response(200, headers=headers, stream=stream),
    )
    with pytest.raises(TelegramDownloadError):
        run()
    assert stream.closed and stream.reads == 2


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/absolute",
        "https://evil.invalid/file",
        "photos/a?token=x",
        "photos/%2e%2e",
        "photos//x",
    ],
)
def test_invalid_paths_never_requested(path):
    run, requests = call_adapter(lambda c: c.download_file(path))
    with pytest.raises(TelegramResponseError):
        run()
    assert not requests


@pytest.mark.parametrize(
    "status,error",
    [
        (401, TelegramAuthenticationError),
        (403, TelegramAuthenticationError),
        (429, TelegramRateLimitError),
        (500, TelegramUnavailableError),
        (408, TelegramUnavailableError),
        (400, TelegramResponseError),
        (302, TelegramResponseError),
    ],
)
def test_http_errors_safe(status, error, caplog):
    run, requests = call_adapter(
        lambda c: c.get_file("id"),
        httpx.Response(status, json={"description": TOKEN, "parameters": {"retry_after": 12}}),
    )
    with pytest.raises(error) as exc:
        run()
    assert TOKEN not in str(exc.value) + repr(exc.value) + caplog.text
    if status == 429:
        # retry_after=12 exceeds the bounded retry policy's max delay, so the policy
        # gives up immediately rather than actually sleeping 12 seconds in a webhook.
        assert exc.value.retry_after == 12
        assert len(requests) == 1
    else:
        # Non-transient (401/403/400/302) never retries; transient (500/408) uses the
        # bounded provider retry policy (3 attempts) since no oversized hint applies.
        assert len(requests) == (3 if getattr(error, "transient", False) else 1)


@pytest.mark.parametrize(
    "operation",
    [
        lambda c: c.get_file("id"),
        lambda c: c.download_file("photos/file.jpg"),
    ],
)
@pytest.mark.parametrize(
    "failure,error",
    [
        (httpx.ReadTimeout(TOKEN), TelegramTimeoutError),
        (httpx.ConnectError(TOKEN), TelegramUnavailableError),
    ],
)
def test_network_errors_idempotent_methods_retry(operation, failure, error):
    run, requests = call_adapter(operation, failure=failure)
    with pytest.raises(error) as exc:
        run()
    assert TOKEN not in str(exc.value) and exc.value.__suppress_context__
    # get_file/download_file are read-only, so the bounded retry policy applies (3 attempts).
    assert len(requests) == 3


@pytest.mark.parametrize("failure", [httpx.ReadTimeout(TOKEN), httpx.ConnectError(TOKEN)])
def test_network_errors_send_message_never_blindly_retried(failure):
    run, requests = call_adapter(lambda c: c.send_message(123, "text"), failure=failure)
    with pytest.raises(TelegramAmbiguousSendError) as exc:
        run()
    assert TOKEN not in str(exc.value) and exc.value.__suppress_context__
    # Sending is not idempotent: a network failure mid-send must never be retried
    # transparently, since the message might already have reached the user.
    assert len(requests) == 1


def test_send_defaults_to_plain_text_with_no_markdown_interpretation(caplog):
    # Without an explicit parse_mode, no formatting directive is sent at all, so
    # Telegram treats vendor/category text literally and cannot be injected.
    text = "_[vendor](https://example.invalid) *category*"
    run, requests = call_adapter(
        lambda c: c.send_message(123, text),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
    )
    assert run() is None
    assert json.loads(requests[0].content) == {"chat_id": 123, "text": text}
    assert text not in caplog.text


def test_send_forwards_explicit_parse_mode():
    # The execution-summary card opts in explicitly; escaping is the caller's job
    # (services.receipt_messages), and the adapter forwards the mode unchanged.
    text = "✅ *Receipt Processed*\n\n*Total*: PHP 1,280\\.00"
    run, requests = call_adapter(
        lambda c: c.send_message(123, text, parse_mode="MarkdownV2"),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
    )
    assert run() is None
    assert json.loads(requests[0].content) == {
        "chat_id": 123,
        "text": text,
        "parse_mode": "MarkdownV2",
    }


def test_send_rate_limit_large_hint_never_sleeps():
    # A real retry_after (12s) always exceeds the bounded policy's max delay, so the
    # policy gives up immediately rather than blocking a webhook response for 12 seconds.
    run, requests = call_adapter(
        lambda c: c.send_message(123, "text"),
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}}),
    )
    with pytest.raises(TelegramRateLimitError) as exc:
        run()
    assert exc.value.retry_after == 3 and len(requests) == 1


@pytest.mark.parametrize("hint", [None, "3", True, -1])
def test_send_rate_limit_without_usable_hint_retries_bounded(hint):
    # No safe/explicit retry_after: the bounded policy applies its own short backoff.
    # Unlike a network timeout, a definitive ok=false rejection confirms nothing was
    # sent, so retrying a rate-limited send is safe here (never double-sends).
    run, requests = call_adapter(
        lambda c: c.send_message(123, "text"),
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": hint}}),
    )
    with pytest.raises(TelegramRateLimitError) as exc:
        run()
    assert exc.value.retry_after is None and len(requests) == 3


@pytest.mark.parametrize(
    "body,error",
    [
        ([], TelegramResponseError),
        ({"ok": True, "result": {}}, TelegramResponseError),
        ({"ok": False, "error_code": 401}, TelegramAuthenticationError),
        ({"ok": False, "error_code": 429}, TelegramRateLimitError),
        ({"ok": False, "error_code": 503}, TelegramUnavailableError),
        ({"ok": "true", "result": {}}, TelegramResponseError),
    ],
)
def test_invalid_or_error_envelopes(body, error):
    run, _ = call_adapter(lambda c: c.get_file("id"), httpx.Response(200, json=body))
    with pytest.raises(error):
        run()


def repo_call(body, *, status=200, finish=False):
    requests = []

    def respond(req):
        requests.append(req)
        return httpx.Response(status, json=body)

    async def run():
        config = settings(supabase_url="https://example.invalid", supabase_service_role_key="fake")
        async with open_supabase_client(config, transport=httpx.MockTransport(respond)) as client:
            repo = SupabaseUpdateRepository(client)
            if finish:
                return await repo.finish_update(7, "handled")
            return await repo.claim_update(7)

    return lambda: asyncio.run(run()), requests


def row(status="received"):
    return {"update_id": 7, "received_at": datetime.now(UTC).isoformat(), "status": status}


@pytest.mark.parametrize("body,expected", [([row()], True), ([], False)])
def test_atomic_repository_claim(body, expected):
    run, requests = repo_call(body)
    assert run() is expected
    assert len(requests) == 1 and requests[0].method == "POST"
    assert "resolution=ignore-duplicates" in requests[0].headers["prefer"]
    assert requests[0].url.params["on_conflict"] == "update_id"
    assert json.loads(requests[0].content) == {"update_id": 7}


@pytest.mark.parametrize(
    "body,status,error",
    [
        ({"code": "503", "message": "private"}, 503, PersistenceUnavailableError),
        ({"code": "23505", "message": "unrelated constraint"}, 409, PersistenceError),
        ([row("handled")], 200, InvalidPersistenceResponseError),
        ([row(), row()], 200, InvalidPersistenceResponseError),
        ([{}], 200, InvalidPersistenceResponseError),
    ],
)
def test_repository_errors_not_duplicates(body, status, error):
    run, _ = repo_call(body, status=status)
    with pytest.raises(error):
        run()


def test_finish_conditional_status():
    run, requests = repo_call([row("handled")], finish=True)
    assert run() is None
    assert requests[0].method == "PATCH"
    assert requests[0].url.params["status"] == "eq.received"
    assert requests[0].url.params["update_id"] == "eq.7"


def test_finish_absent_is_error():
    run, _ = repo_call([], finish=True)
    with pytest.raises(InvalidPersistenceResponseError):
        run()


@pytest.mark.parametrize(
    "field,value",
    [
        ("telegram_allowed_user_id", 0),
        ("telegram_allowed_user_id", -1),
        ("telegram_allowed_user_id", 2**63),
        ("telegram_timeout_seconds", 0),
        ("telegram_timeout_seconds", 31),
        ("telegram_timeout_seconds", "NaN"),
        ("telegram_webhook_max_bytes", 0),
        ("telegram_webhook_max_bytes", 1048577),
    ],
)
def test_invalid_transport_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(app_env="test", _env_file=None, **{field: value})


def test_user_setting_parses_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    assert Settings(app_env="test", _env_file=None).telegram_allowed_user_id == 123


def test_factory_requires_auth_even_in_test_mode():
    with pytest.raises(ValueError):
        create_app(Settings(app_env="test", _env_file=None))
    with pytest.raises(ValueError):
        create_app(Settings(app_env="test", _env_file=None, telegram_allowed_user_id=123))


@pytest.mark.parametrize(
    "headers,error",
    [
        ({"content-length": "nope"}, TelegramResponseError),
        ({"content-length": "9" * 5000}, TelegramDownloadError),
        ({"content-encoding": "gzip"}, TelegramResponseError),
    ],
)
def test_unsafe_download_headers(headers, error):
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"),
        httpx.Response(200, headers=headers, stream=Chunks([b"x"])),
    )
    with pytest.raises(error):
        run()


def test_empty_download_rejected():
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"), httpx.Response(200, content=b"")
    )
    with pytest.raises(TelegramDownloadError):
        run()


def test_getfile_declared_size_rejected():
    run, _ = call_adapter(
        lambda c: c.get_file("id"),
        httpx.Response(
            200, json={"ok": True, "result": {"file_path": "photos/file.jpg", "file_size": 11}}
        ),
    )
    with pytest.raises(TelegramDownloadError):
        run()


@pytest.mark.parametrize("content", [b"invalid-private-json", b"x" * 65537])
def test_invalid_or_oversized_api_response(content):
    run, _ = call_adapter(lambda c: c.get_file("id"), httpx.Response(200, content=content))
    with pytest.raises(TelegramResponseError):
        run()


def test_download_non_success():
    run, _ = call_adapter(
        lambda c: c.download_file("photos/file.jpg"), httpx.Response(404, content=TOKEN.encode())
    )
    with pytest.raises(TelegramDownloadError):
        run()


@pytest.mark.parametrize("text", ["", " ", "x" * 4097])
def test_invalid_outbound_text_no_request(text):
    run, requests = call_adapter(lambda c: c.send_message(123, text))
    with pytest.raises(TelegramResponseError):
        run()
    assert not requests


def test_final_status_failure_503_preserves_claim():
    parts = boundary()

    async def fail(*args):
        raise PersistenceUnavailableError()

    parts[1].finish_update = fail
    assert request(update(), parts=parts)[0].status_code == 503
    assert parts[1].rows == {7: "received"}
    assert request(update(), parts=parts)[0].json() == {"status": "duplicate"}
    assert len(parts[3].events) == 1


def test_handler_error_is_sanitized_and_failed(caplog):
    parts = boundary()

    async def fail(event):
        raise ValueError(TOKEN + SECRET)

    parts[3].accept = fail
    response = request(update(), parts=parts)[0]
    assert response.status_code == 500 and parts[1].rows == {7: "failed"}
    assert TOKEN not in response.text + caplog.text and SECRET not in response.text + caplog.text


def test_photo_deadline_and_cancellation():
    parts = boundary(telegram_timeout_seconds=0.01)

    async def slow(file_id):
        await asyncio.sleep(1)

    parts[2].get_file = slow
    assert request(update(photo=photos()), parts=parts)[0].json() == {"status": "failed"}

    async def cancel(file_id):
        raise asyncio.CancelledError()

    parts = boundary()
    parts[2].get_file = cancel
    with pytest.raises(asyncio.CancelledError):
        request(update(photo=photos()), parts=parts)
    assert parts[1].rows == {7: "received"}


def test_slow_webhook_body_has_deadline():
    async def slow():
        yield b"{"
        await asyncio.sleep(1)

    assert (
        request(content=slow(), parts=boundary(telegram_timeout_seconds=0.01))[0].status_code == 408
    )


def test_http_deadline_even_if_transport_never_finishes():
    async def respond(req):
        await asyncio.sleep(1)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = HTTPTelegramClient(client, settings(telegram_timeout_seconds=0.01))
            await api.send_message(123, "text")

    # The deadline still fires (the call does not hang), but a send that times out
    # without a response is ambiguous, not a plain timeout: it must never be retried
    # blindly since the message might already have reached the user.
    with pytest.raises(TelegramAmbiguousSendError):
        asyncio.run(run())


def test_factory_lifespan_owns_all_clients(monkeypatch):
    from contextlib import asynccontextmanager

    import second_brain_receipts.main as main

    events = []

    @asynccontextmanager
    async def database(config):
        events.append("database-open")
        try:
            yield object()
        finally:
            events.append("database-close")

    @asynccontextmanager
    async def telegram(config):
        events.append("telegram-open")
        try:
            yield API()
        finally:
            events.append("telegram-close")

    @asynccontextmanager
    async def vision(config):
        events.append("vision-open")
        try:
            yield object()
        finally:
            events.append("vision-close")

    @asynccontextmanager
    async def storage(config):
        events.append("storage-open")
        try:
            yield object()
        finally:
            events.append("storage-close")

    monkeypatch.setattr(main, "create_openai_client", vision)
    monkeypatch.setattr(main, "OpenAIReceiptVisionProvider", lambda client, config: object())
    monkeypatch.setattr(main, "open_s3_storage", storage)
    monkeypatch.setattr(main, "open_supabase_client", database)
    monkeypatch.setattr(main, "open_telegram_client", telegram)
    app = main.create_app(settings())
    assert events == []

    async def run():
        async with app.router.lifespan_context(app):
            assert events == ["database-open", "telegram-open", "vision-open", "storage-open"]

    asyncio.run(run())
    assert events == [
        "database-open",
        "telegram-open",
        "vision-open",
        "storage-open",
        "storage-close",
        "vision-close",
        "telegram-close",
        "database-close",
    ]


def test_low_level_logging_filter(caplog):
    caplog.set_level(logging.DEBUG)
    run, _ = call_adapter(
        lambda c: c.send_message(123, "text"),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
    )
    run()
    logging.getLogger("httpcore.http11").debug("untrusted tracing %s", TOKEN)
    logging.getLogger("httpx").info("HTTP Request https://api.telegram.org/bot%s/getFile", TOKEN)
    assert TOKEN not in caplog.text
