# second-brain-receipts

A hiring-assessment POC for a Telegram receipt-processing Second Brain sub-agent.
**V0.1-009 is complete:** receipt photos pass from authenticated
Telegram ingress through image validation, Vision extraction, saved-category lookup,
duplicate protection, private S3 storage, receipt insertion and a formatted
confirmation card. Unknown vendors persist a category question and resume on a later
reply; vendor memory, final receipt and pending completion commit atomically in
PostgreSQL. Every provider call runs under one bounded retry policy, with writes
reconciled rather than blindly replayed. The service ships as a container image.

Start with [requirements](docs/REQUIREMENTS.md), [architecture](docs/ARCHITECTURE.md),
[implementation plan](docs/IMPLEMENTATION_PLAN.md), [current status](docs/CURRENT.md)
and [contributor instructions](AGENTS.md).

Use Python 3.12 or newer (verified on 3.12.13). Install the pinned development
snapshot, including transitive dependencies, then the editable package:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

Direct runtime/dev dependencies are defined in pyproject.toml. The lock snapshot
was resolved on Linux/Python 3.12; other environments require verification.
Runtime dependencies are Pydantic, pydantic-settings, Pillow 12.3.0 and the official
OpenAI SDK 3.11.0, Supabase 2.31.0 and boto3 1.43.91. Matching boto3 S3 type stubs
are development-only. Telegram ingress adds FastAPI 0.141.1, Uvicorn 0.52.4 and
direct HTTPX 0.28.1. Direct and transitive versions are pinned in the lock snapshot.

Settings are instantiated explicitly with `Settings()` from
`second_brain_receipts.core.config`; import does not read credentials.
Environment variables override `.env`. Copy `.env.example` if needed, supply
credentials and remove blank lines for settings whose defaults you want to use.
Blank values do not mean “use the default.” Never commit `.env`.

| Setting | Default / requirement |
| --- | --- |
| APP_ENV | development; accepts development, test, production |
| LOG_LEVEL | INFO; DEBUG, INFO, WARNING, ERROR, CRITICAL |
| OPENAI_MODEL | gpt-4o; configurable model supporting images and strict Structured Outputs |
| OPENAI_TIMEOUT_SECONDS | 30; positive, maximum 120; SDK I/O timeout and request deadline |
| DEFAULT_CURRENCY | PHP; uppercase three-letter application currency code |
| PENDING_RECEIPT_TTL_MINUTES | 30; positive integer |
| MAX_UPLOAD_BYTES | 10485760 (10 MiB); positive integer |
| IMAGE_MAX_LONG_EDGE | 1600; 128–4096 pixels |
| IMAGE_JPEG_QUALITY | 85; 75–95 |
| IMAGE_MAX_PIXELS | 40000000; 16384–80000000 |
| AWS_REGION, S3_BUCKET_NAME | Required outside test mode; no deployment values defaulted |
| AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY | Optional pair; otherwise standard boto3 role/profile credential chain |
| AWS_SESSION_TOKEN | Optional with an explicit key pair |
| S3_PRESIGNED_URL_TTL_SECONDS | 300; allowed 1–3600 seconds |
| OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, SUPABASE_SERVICE_ROLE_KEY | Required outside test mode; SecretStr, never blank |
| TELEGRAM_ALLOWED_USER_ID | Required outside test mode; positive numeric sender ID, private chat only |
| TELEGRAM_WEBHOOK_MAX_BYTES | 65536; JSON body limit, allowed 1024–1048576 |
| TELEGRAM_TIMEOUT_SECONDS | 10; positive, maximum 30; finite I/O and operation deadline |
| SUPABASE_URL | Required outside test mode; HTTP(S) URL |

Test mode permits omitted credentials; unit tests use isolated environments and
fake values. SecretStr masks ordinary representation/JSON. Do not log settings
or raw validation error dictionaries: Pydantic's `hide_input_in_errors` protects
formatted error text, not all structured error output. See
[Pydantic configuration](https://pydantic.dev/docs/validation/latest/api/pydantic/config/).
Telegram authorization requires TELEGRAM_ALLOWED_USER_ID; the application factory
requires a user ID and webhook secret even when test-mode credential checks are relaxed.

Run:

```sh
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

The migration is [initial receipt schema](supabase/migrations/202609100001_initial_receipt_schema.sql).
Apply it and [S3 references migration](supabase/migrations/202609100002_s3_storage_references.sql)
and [Telegram claims migration](supabase/migrations/202609100003_telegram_updates.sql)
and [pending completion migration](supabase/migrations/202609100004_pending_completion.sql)
in order to the intended empty database through Supabase migration tooling or
psql with ON_ERROR_STOP. Local SQL verification instructions are in
[tests/README.md](tests/README.md). No real Supabase project was modified.

The image service is provider-independent:

```python
from second_brain_receipts.core.config import Settings
from second_brain_receipts.services.image_service import ImageService

# Test-mode example requires no provider credentials.
image_service = ImageService(Settings(app_env="test", _env_file=None))
# processed = image_service.process(raw_bytes)
# Later extraction consumes processed.data and processed.mime_type.
```

It accepts static JPEG/PNG/WEBP, checks upload/pixel/dimension limits and corruption,
applies orientation, resizes proportionally, composites transparency on white and
returns metadata-stripped JPEG. It does not write images to disk, perform OCR, or
call Vision. Validation failures expose safe ImageValidationError codes. See the
[architecture](docs/ARCHITECTURE.md) for exact limits and readability limitations.

The Vision adapter consumes `ProcessedImage` and returns validated
`ReceiptExtraction`. Reuse a caller-owned async client:

```python
from second_brain_receipts.providers.openai_vision import (
    OpenAIReceiptVisionProvider,
    create_openai_client,
)


async def extract_receipt(processed, settings):
    # In the future service, keep this client open for the application lifespan.
    async with create_openai_client(settings) as client:
        provider = OpenAIReceiptVisionProvider(client, settings)
        return await provider.extract(processed)
```

This example would make a paid network request with configured credentials; the
unit suite never does. `python -m pytest` blocks socket connections and uses mocked
SDK transports. No live smoke test is included or required. The adapter uses
Responses strict JSON-schema output, exact Decimal parsing, safe typed errors and
zero SDK retries. Unreadable/refusal results raise ReceiptExtractionError; valid
Low confidence is returned for later review/retry policy. AI categories are advisory.
ReceiptProcessor connects these boundaries for known vendors; unknown-vendor
categories resume through PendingWorkflow and the atomic completion RPC. See
[architecture](docs/ARCHITECTURE.md) for the contract and error mapping.

Client assignment explicitly requires S3 or Google Drive for receipt images; AWS S3 was selected.
SUPABASE_STORAGE_BUCKET was removed. Supabase owns structured data; private S3
stores only the processed JPEG, with explicit AES256 server-side encryption. Rows
retain image_storage_key and stable s3://bucket/key references, never expiring URLs.
Use on-demand presigned GET URLs after authorization (default five minutes).

`open_supabase_client(settings)` and `open_s3_storage(settings)` are async context
managers intended for application-lifetime reuse. Inject the Supabase client into
SupabaseVendorRepository, SupabaseReceiptRepository and SupabasePendingReceiptRepository.
The S3 context yields S3ReceiptImageStorage. ReceiptProcessor owns known-vendor
orchestration; PendingWorkflow owns persisted category questions and atomic completion. See [architecture](docs/ARCHITECTURE.md) for operations, exceptions,
exact Decimal handling, compensation and credential/logging requirements.

Provision S3 privately with Block Public Access and backend-only IAM permissions;
this milestone does not create buckets or deploy resources. Remove blank optional
AWS credential lines from .env when using role/profile credentials. Existing data
requires a reviewed S3-reference backfill before the new required-column migration.
No real Supabase or AWS account is required for the default suite.

## Run the receipt POC

Configure environment credentials and numeric TELEGRAM_ALLOWED_USER_ID, apply all
four migrations, then start the application factory:

```sh
python -m uvicorn second_brain_receipts.main:create_app --factory --host 127.0.0.1 --port 8000
```

GET /health returns {"status":"ok"}. POST /webhooks/telegram requires the exact
X-Telegram-Bot-Api-Secret-Token header. The public deployment needs HTTPS; webhook
registration is an explicit deployment action using Telegram setWebhook with the
public `/webhooks/telegram` URL and secret_token from TELEGRAM_WEBHOOK_SECRET.
Read bot credentials from configuration; never paste token-bearing URLs into logs
or shell history. Nothing registers or sends a Telegram message during startup.
No live setup helper is included.

To run the same service in a container, build the image and pass configuration as
environment variables. No credential is ever baked into an image layer:

```sh
docker build -t second-brain-receipts .
docker run --rm -p 8000:8000 --env-file .env second-brain-receipts
```

The image is a two-stage build on `python:3.12-slim`, installs only runtime
dependencies, runs as an unprivileged user and declares a HEALTHCHECK against
`/health`, which probes no provider. Build context exclusions live in
`.dockerignore`, which keeps `.env`, tests and local state out of the image.

Only the configured numeric user in their own private chat is accepted; usernames
cannot grant access. Unauthorized updates get 200 ignored to avoid repeated delivery.
Photo variants are selected by resolution within declared upload limits, then actual
bytes are streamed with the upload cap. Text is trimmed; commands, documents, audio,
stickers and other messages are unsupported. JSON and image limits are separate.
Operational messages are sent as literal plain text with no parse_mode. The one
exception is the execution-summary card described below.

The application lifespan owns Telegram, Supabase, OpenAI and S3 clients. Known vendors
use saved categories. For an unknown vendor, the bot validates/extracts, rejects Low
confidence, checks duplicates, stores the processed image and pending extraction,
then asks for a category. That request ends immediately after the prompt; it does
not wait for you to reply. A later authenticated text webhook resumes from PostgreSQL
without another image download, Vision call or S3 upload.

Reply with a free-form category of 1–100 characters. Leading/trailing whitespace is
trimmed; control/bidi/zero-width formatting abuse is rejected. Suggestions are not an
enum. Reply exactly `cancel` (case-insensitive) to discard pending work; `Cancellation
Fees` is a valid category. One active question per private chat/user is supported.
Finish or cancel it before another photo; the bot checks this before downloading.
Pending TTL defaults to 30 minutes. Expiry discovered on access closes the pending
row, attempts image cleanup and asks you to resend; there is no scheduled sweeper.

The completion RPC locks the pending row and commits vendor memory, receipt insert
and completed state together, using the stored extraction. Existing vendor memory
wins a concurrent category conflict. The final receipt keeps the pending image.
A later receipt from that vendor automatically uses the saved category.
Presigned URLs are never generated during normal insertion.

Confirmation is a formatted summary card sent with Telegram MarkdownV2: a
`✅ Receipt Processed` header over bold field labels, with DD/MM/YYYY, the configured
currency, Decimal amounts and optional VAT.

```text
✅ *Receipt Processed*

*Vendor*: ABC Hardware
*Date*: 10/09/2026
*Total*: PHP 1,280\.00
*VAT*: PHP 137\.14
*Category*: Maintenance
*Confidence*: High
```

Every interpolated value is escaped for MarkdownV2 first. Receipt text is untrusted
model output, so a vendor name like `_[Vendor]*` renders literally instead of as
formatting, and a correctly escaped message is also what keeps Telegram from
rejecting the send outright. Every other message stays plain text.

Apply migration 004 before running this version. It restricts confirmed categories
in the database: existing oversized, untrimmed or control-containing categories need
a reviewed backfill first. Earlier migrations are preserved. Completion/close RPCs
use SECURITY INVOKER, fixed safe search_path and backend-only execution privileges.

Duplicate prechecks skip upload. Confirmed receipt/pending insert rejections trigger
one best-effort cleanup; unique pending-slot races leave the winning work untouched.
Finalization duplicate collisions roll back all attempted vendor/receipt/pending
changes, then a separate locked close resolves the loser. Cleanup deletes only the
pending-owned object when no final receipt references it. Ambiguous insertion results
retain images unless the exact committed identity can be established. S3 and SQL
are not a distributed atomic transaction; cleanup failure can leave orphans.

A failed prompt keeps pending state/image. A failed completion confirmation keeps
vendor memory, receipt, completed state and image. The ingress update becomes failed
where possible; no notification retry occurs. Duplicate update IDs never repeat
work. Interrupted/failed claims need future recovery or a fresh submission/reply;
do not blindly delete claims.

Every provider call is wrapped in one bounded retry policy (3 attempts, exponential
backoff capped at 1.0s per wait and 1.5s in total, honoring a provider `retry_after`
hint only while it fits that budget). What may be retried is decided per operation,
not per error type: reads retry freely, a Telegram send is never retried after a
timeout because it may already have been delivered, and writes are retried only
behind a reconciliation read that first establishes whether the earlier attempt
committed. When reconciliation cannot resolve a write, the result is reported as
ambiguous and the uploaded image is retained rather than deleted. See
[architecture](docs/ARCHITECTURE.md) for the per-boundary table. Scheduled orphan
repair and retention policy remain out of scope.

Each webhook processes synchronously through its immediate response, with existing
provider limits and safe stage/total timing logs. No HTTP request remains open waiting
for category input, and no queue/background durability is claimed. Keep SDK payload
and exception-local tracing off. Tests use synthetic data and blocked sockets;
real provider latency, extraction quality and deployed access policies remain later
checks. See [architecture](docs/ARCHITECTURE.md) for exact state/security contracts.
