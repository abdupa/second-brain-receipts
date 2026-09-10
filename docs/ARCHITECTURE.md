# Architecture

## Boundaries

A single Python service exposes a FastAPI Telegram webhook. Pydantic models validate
configuration, events and extraction. Thin adapters handle Telegram, OpenAI,
Supabase PostgreSQL and private AWS S3. Pillow handles custom image preprocessing.
An explicit orchestration service owns business rules; repositories own persistence.
There is no generic workflow engine and no in-memory-only pending conversation.

Logical flow: authenticated Telegram event → bounded file download → Pillow
validation/compression → OpenAI structured extraction → Pydantic validation →
deterministic confidence/duplicate/vendor checks → private storage and persistence
→ Telegram response. Check duplicates before storing an image where practical.

The LLM reads receipts, extracts fields, estimates confidence and may propose a
category. It does not control database queries, state changes, authorization,
duplicate decisions or category memory. Receipt text is untrusted input.

## Implemented image boundary (V0.1-003)

`ImageService(settings).process(raw_bytes)` returns immutable `ProcessedImage`:
`data`, `mime_type`, `width`, `height`, `original_byte_size`, computed
`processed_byte_size`, `resized`, and `recompressed`. Bytes are excluded from repr.
Recompressed is always true because every successful input is re-encoded to JPEG;
resized compares dimensions after orientation correction, not rotation alone.
No Telegram, OpenAI, storage or receipt logic is involved.

Input bytes must be nonempty and at most MAX_UPLOAD_BYTES (default 10 MiB).
Only content-decoded JPEG, PNG and WEBP are allowed; other decoders are excluded.
Animations/multiple frames are rejected. Inspect dimensions before verification or
full decoding, call verify(), reopen, and load() the full image. Both checks are
needed: for example, JPEG verify alone can accept a truncated file. Unrecognized
input receives unsupported_image; recognized damaged input receives corrupt_image.
Pillow-detectable corruption is rejected; this is not proof of visual correctness.

| Limit/setting | Default / allowed values |
| --- | --- |
| IMAGE_MAX_PIXELS | 40,000,000; configurable 16,384–80,000,000 |
| Maximum raw edge | 20,000 pixels, fixed safety ceiling |
| Minimum dimensions | Each edge at least 64 pixels and area at least 16,384 pixels |
| IMAGE_MAX_LONG_EDGE | 1600; configurable 128–4096 |
| IMAGE_JPEG_QUALITY | 85; configurable 75–95 |

The 40 MP default allows ordinary 12–24 MP smartphone photos while bounding decoded
memory; higher-resolution camera modes may need export at a lower resolution.
Pillow bomb protections remain enabled. Bomb warnings/errors become typed dimension
errors; malformed metadata warnings become corrupt-image errors. Warning filters
are scoped to processing; no global Pillow thresholds or truncated-loading settings
are modified. Do not enable LOAD_TRUNCATED_IMAGES elsewhere in this application.
Future concurrent hosting must account for Pillow/Python warning-filter behavior
and decoded memory (several full-frame buffers may exist); pixel limits are not a
process memory quota or timeout.

Apply EXIF transpose before sizing. Convert common RGB, grayscale, CMYK and palette
modes to RGB; composite alpha/transparency onto white. Use LANCZOS thumbnail with
reducing_gap=3.0, preserving aspect ratio without cropping/upscaling. Check minimum
dimensions again after resizing so extreme aspect ratios do not return unusable
slivers. Encode once at configured quality, optimize=True, subsampling=0 (4:4:4)
to retain colored text detail. There is no iterative quality search or guaranteed
smaller output for already-optimized inputs. Byte reduction reduces transmission
size; model-specific token/latency improvements are not measured in this milestone.

Clear inherited image info and encode without EXIF/ICC. GPS/device/timestamps,
comments and PNG text are not copied. This is metadata privacy protection, not
image encryption, redaction of visible content or removal of pixel steganography.
No disk persistence occurs during processing. A separate test-only visual review
writes a synthetic output under /tmp. Debug service logs contain only sizes,
dimensions, source format and resize status; no pixels, metadata or receipt text.

ImageValidationError exposes ImageErrorCode values: empty_image, unsupported_image,
corrupt_image, image_too_large, unsafe_image_dimensions, image_too_small. Messages
contain only the code, with decoder exception chaining suppressed. Later orchestration
maps these codes to retry messages. Operational failures such as MemoryError remain
operational failures rather than being mislabeled as bad user images.

The minimum-size check is coarse; it does not identify blur, faint print, or receipt
content. The synthetic visual review is encouraging, not a guarantee across real
receipts. Vision confidence/readability handling remains in later milestones.
Implementation follows Pillow's [verification and decoding API](https://pillow.readthedocs.io/en/stable/reference/Image.html)
and [metadata security guidance](https://pillow.readthedocs.io/en/stable/handbook/security.html).

## Implemented Vision boundary (V0.1-004)

`ReceiptVisionProvider` exposes `async extract(image: ProcessedImage) ->
ReceiptExtraction`. `OpenAIReceiptVisionProvider` implements it with the official
OpenAI Python SDK 3.11.0 and `AsyncOpenAI.responses.create`. It accepts the existing
processed JPEG, encodes its exact bytes as an in-memory data URL, and sends one
high-detail image with strict JSON-schema Structured Outputs. It never reopens or
recompresses the source, writes files, uploads to S3, or queries/persists data.

The configured OPENAI_MODEL remains `gpt-4o` (no fixed snapshot). Temperature is 0;
this encourages consistency but does not guarantee deterministic model output.
The prompt treats receipt text as untrusted data, forbids fabrication, distinguishes
final total from subtotal, requests only explicit VAT, and uses ISO transaction
dates. Categories are suggestions; confidence is exactly High/Medium/Low.

The wire schema carries one field the domain model does not: `date_text`, the
transaction date copied exactly as printed. A numeric date such as `08/09/2026` is
genuinely ambiguous, and live runs showed gpt-4o resolving it month-first at High
confidence even on a receipt with clear non-US regional markers, so the reading is
settled by `domain/receipt_date.py` under the configured `RECEIPT_DATE_ORDER` rather
than by the model. Resolution happens before validation and only breaks a genuine
tie: an unambiguous, unrecognized, absent or impossible value leaves the model's own
reading untouched. `date_text` is consumed during parsing and never reaches
`ReceiptExtraction`, storage or any message.

The provider-specific envelope is `{ "receipt": <receipt object or null> }`.
The object uses the seven domain fields plus date_text; every schema property is
required, VAT/category/date_text permit null, and extra properties are forbidden.
Money uses JSON numbers. This small equivalent wire schema avoids exposing the domain
Decimal number/string union to the model. A test pins the wire fields to exactly the
domain fields plus date_text, so neither set can drift unnoticed.
Receipt=null means the required vendor/date/total cannot be read without invention.
SDK refusal content and null receipt raise ReceiptExtractionError. There are no
fallback vendors, zero totals, inferred VAT or current-date placeholders.

Response status must be completed, with no response error/incomplete details and
exactly one output text block. The SDK response structure is revalidated, then its
JSON text is parsed with parse_float=Decimal; binary floats never enter the monetary
validation path. Non-finite tokens, duplicate keys, oversized text (>32,768
characters), malformed JSON and invalid envelopes fail safely. The existing
ReceiptExtraction model enforces vendor/date/amount/VAT/confidence/category rules.
Optional VAT/category omitted by a malformed-but-otherwise-valid response normalize
to None; the requested strict wire schema still requires explicit nulls.
Structurally valid Low confidence is returned unchanged; V0.1-007 orchestration
rejects it before storage/insertion. Semantic hallucinations cannot be excluded by
schema validation alone, so confidence and downstream policy remain necessary.

```text
AI category suggestion
        ↓
later vendor-memory lookup
        ↓
known vendor?
    yes → stored category wins
    no  → interactive category workflow
```

`create_openai_client(settings)` builds a reusable caller-owned async client.
Use `async with` at application lifetime scope, and inject it into the provider.
The provider's options clone shares the HTTP transport. Tests can inject a fake
client or an official client with a mocked transport; no global SDK patch is needed.
Factory and provider both disable SDK retries (`max_retries=0`); retry policy is
owned by the application, not the SDK, so it stays visible, bounded and testable.
OPENAI_TIMEOUT_SECONDS defaults to 30 (positive, maximum 120), applies to SDK I/O
and an asyncio request deadline. Maximum output is 1024 tokens. Cancellation
propagates. V0.1-009 wraps `extract` in the shared bounded retry (see
[Bounded provider retries](#bounded-provider-retries-v01-009)): transient failures
(rate limit, timeout, unavailable) are re-attempted, and non-transient ones
(authentication, invalid response, unreadable receipt) fail on the first attempt.

| Failure | Application signal | Potentially transient |
| --- | --- | --- |
| Authentication / permissions (401/403) | VisionAuthenticationError | No |
| HTTP 429 or response rate_limit_exceeded | VisionRateLimitError | Yes |
| SDK timeout or request deadline | VisionTimeoutError | Yes |
| Connection failure, HTTP 408/409/5xx or response server_error | VisionUnavailableError | Yes |
| Other request status, malformed/incomplete output, failed validation | VisionResponseError | No |
| Refusal or null receipt | ReceiptExtractionError | No |
| Other SDK API errors | VisionProviderError | No |

Potentially transient does not mean automatic retry: quota-related 429s may require
operator action. Errors expose only a safe code and transient flag, suppressing
provider exception chaining. Logs contain duration, processed-byte size, provider,
result class and confidence only. No receipt text, keys, model payloads or base64
are logged. Request IDs/token usage are not collected yet. `store=False` disables
Responses application-state storage; it does not promise zero provider retention.
Production logging must avoid request/response tracing and exception-local capture.

API choices were checked against official OpenAI documentation:
[GPT-4o capabilities](https://developers.openai.com/api/docs/models/gpt-4o),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
and [image inputs](https://developers.openai.com/api/docs/guides/images-vision).
All verification used synthetic responses and mock transports; actual account/model
access, wire-schema acceptance and extraction quality require a later authorized
live smoke test. No live call is needed or made for this milestone.

## Implemented persistence and S3 boundaries (V0.1-005)

Client assignment explicitly requires S3 or Google Drive for receipt images; AWS S3 was selected.
This replaces the earlier Supabase Storage choice, not the Supabase database.
SUPABASE_STORAGE_BUCKET is removed. Supabase's SDK includes storage3 transitively,
but no image storage implementation uses it.

```text
ProcessedImage ──→ OpenAI Vision ──→ ReceiptExtraction
       │                                  │
       └──→ private AWS S3                └──→ validated Supabase repositories
                  │                                      ↑
                  └── object key + stable s3:// URI ──────┘
```

Store the sanitized, validated, compressed JPEG from ImageService. This is the
receipt image retained by the POC, with known MIME, less transmission/storage cost
and no original device/GPS metadata. The unvalidated original is not stored.

The async repository protocols cover vendor lookup/upsert; receipt duplicate lookup
and insert; pending create, ID lookup, awaiting chat/user lookup and expected-state
update. Callers supply validated models with stable UUIDs/timestamps for inserts.
Responses become Vendor, Receipt or PendingReceipt, never business-layer dictionaries.
VendorWrite separates upsert input from generated vendor IDs/creation timestamps.
Upsert uses normalized_name identity and updates explicitly supplied fields; it
does not decide whether AI or stored memory wins. Receipt creation uses INSERT,
never UPSERT. Repeating an insert is not silently treated as success.

One `open_supabase_client(settings)` async context owns an HTTP client shared by
injected repositories. Auth token refresh/session persistence are disabled; service
credentials stay server-side. The HTTP timeout is 20 seconds. Query retries are
explicitly disabled. No repositories call auth, realtime, Storage or workflow RPCs.
The returned client must not be used for interactive user sign-in.

Decimal writes serialize as two-place JSON strings via Pydantic, accepted by
PostgreSQL NUMERIC(12,2). All receipt/pending read and write-return projections
request total_amount::text and vat_amount::text from PostgREST, so its JSON decoder
never creates floats for money. Filters use exact two-place strings too. Tests
cover 0.10, 12.30, 999999.99 and the upper bound; float responses fail validation.
This uses supported [PostgREST column casting](https://docs.postgrest.org/en/v14/references/api/tables_views.html).

Database unique violation 23505 is mapped to DuplicateReceiptPersistenceError only
when it names receipts_vendor_date_total_key, and PendingReceiptConflictError when
it names pending_receipts_one_question_idx. Unrelated primary-key conflicts remain
PersistenceError. Empty/malformed/unexpected-count rows produce
InvalidPersistenceResponseError; absent lookup rows return None. Connection,
HTTP 408/429/5xx and selected database availability/serialization errors produce
PersistenceUnavailableError. No raw provider message, connection details or
credential appears in application exceptions. Empty successful HTTP bodies are
rejected, rather than misinterpreted as “not found.”

`update_state` is a single conditional PATCH filtered by ID and expected state;
zero matched rows raise PendingReceiptConflictError. It is not a state machine or
multi-table completion transaction. Awaiting lookup returns expiry as data, without
expiring anything. Vendor memory, final receipt insertion and pending completion
still require the V0.1-008 atomic RPC; never compose independent repository calls
and describe them as atomic completion.

### S3 capabilities and security

ReceiptImageStorage exposes async store(ProcessedImage), delete(object_key), and
create_download_url(object_key). StoredReceiptImage contains object_key, stable
storage_uri, bucket, content_type and size_bytes. The reusable boto3 client is
created/closed by `open_s3_storage`; client creation and operations use
asyncio.to_thread to keep blocking I/O off the event loop. Connect/read timeouts
are 5/20 seconds; total_max_attempts=1 prevents hidden retries.

Store generates receipts/YYYY/MM/<uuid4>.jpg using UTC; keys contain no vendor,
Telegram identity or amount. PutObject receives binary JPEG bytes, content length,
ContentType=image/jpeg, ServerSideEncryption=AES256 and IfNoneMatch=* (no accidental
overwrite). It sets no ACL and no custom metadata. SSE-S3 is AWS-managed encryption
at rest, not application-level client-side encryption. See the
[AWS PutObject API](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html).

Deployment requirements (not provisioned or cloud-verified here): private bucket,
all S3 Block Public Access controls enabled, no public bucket policy, preferably
Bucket owner enforced object ownership, TLS, and least-privilege backend IAM access
to GetObject/PutObject/DeleteObject in the receipts/ prefix. Keep Supabase RLS and
server service-role access unchanged; no browser or Telegram credential exposure.
AWS keys are optional as a pair; with none supplied, boto3 uses its standard
credential chain, including roles/profiles. Explicit session tokens require an
explicit key pair. AWS_REGION and S3_BUCKET_NAME are required outside test mode.

`image_storage_uri` is a required additional field on both receipt/pending models
and tables, matching image_storage_key. The new additive migration preserves the
original migration. It intentionally refuses populated tables instead of inventing
bucket mappings: an existing installation requires a reviewed mapping/backfill
migration before adopting this constraint. This repository has no live data.
The URI is stable identity, not a public/authorized download URL.

Presigning returns an HTTPS GET URL for one validated generated key in the configured
bucket, with S3_PRESIGNED_URL_TTL_SECONDS=300 (allowed 1–3600). It neither checks
object existence nor authorizes the caller: later orchestration must authorize
access before calling. URLs are never stored or logged and can expire earlier with
session credentials. Bucket changes require an explicit migration: key-only methods
operate on the configured bucket, not arbitrary URI-provided buckets.

Delete is idempotent for missing keys; missing buckets/access failures remain errors.
On a versioned bucket, deletion creates a marker rather than erasing old versions;
deploy lifecycle cleanup or use an unversioned POC bucket as appropriate. The
adapter does not alter bucket settings. Storage errors distinguish authentication,
unavailability, upload, delete and presign failure. Failed uploads retain their
opaque key on the typed exception for later reconciliation of ambiguous outcomes.
No raw boto errors or URLs are exposed. Cancellation of to_thread cannot stop an
in-flight S3 request; later recovery must account for uncertain upload outcomes.

Adapters emit no content logs. Keep SDK request tracing off and third-party
httpx/httpcore/boto3/botocore loggers at WARNING or above in deployment: debug
tracing can expose payloads/signatures, and HTTP URL logs may include SQL filters.
Do not log returned presigned URLs, settings, client objects or exception locals.

### Compensation contract

After validation/extraction, store S3 image, then create the final or pending row.
On confirmed database failure, attempt best-effort S3 deletion, then surface the
original persistence failure. V0.1-007 attempts deletion once on confirmed rejection/collision. If deletion
fails, its typed error preserves the original failure and cleanup metadata. A durable
cleanup ledger/recovery coordinator is still deferred to V0.1-009. On
ambiguous database timeouts, reconcile the stable row/object IDs before deleting
an image that might already be referenced by a committed row. Keep images after
successful writes. S3 and PostgreSQL do not share an ordinary transaction.

## Implemented Telegram transport (V0.1-006)

`main.create_app()` explicitly loads settings; module import performs no I/O.
Uvicorn uses the factory. Lifespan owns one Supabase HTTP client and one direct
Telegram HTTP adapter. V0.1-007 also owns an OpenAI client and S3 storage client
for the connected processor. No webhook is registered or message sent at startup.
Tests inject a TelegramIngressService with fake repository/API/handler boundaries.
GET /health returns only {"status":"ok"}; it does not probe any provider.

POST /webhooks/telegram uses hmac.compare_digest on the configured secret header
before reading the body. Missing/wrong secrets return 401. Content-Length and
actual request.stream() bytes are bounded by TELEGRAM_WEBHOOK_MAX_BYTES (65536;
allowed 1024–1048576). Bad JSON/schema returns 400, oversize 413 and a slow body
408. No FastAPI validation response echoes raw payloads or inputs.

TelegramUpdate models ignore unused provider fields and strictly validate numeric
IDs. Sender ID must match TELEGRAM_ALLOWED_USER_ID (positive signed BIGINT), chat
type must be private and chat ID must match sender ID. Usernames/display names are
never authorization inputs. Other senders/chats receive 200 ignored, without a DB
claim, avoiding retries of permanently unauthorized input. Updates with no supported
message envelope return 200 unsupported without a claim or effects.

The service claims authorized messages through SupabaseUpdateRepository. Its single
PostgREST insert uses resolution=ignore-duplicates with on_conflict=update_id:
INSERT ... ON CONFLICT(update_id) DO NOTHING, returning the inserted row. An empty
result means duplicate; database/protocol failures remain distinct typed persistence
errors. Existing rows are never overwritten/reset. Migration 202609100003 adds
telegram_updates(update_id BIGINT PRIMARY KEY CHECK >=0, received_at TIMESTAMPTZ
NOT NULL DEFAULT now(), status received/handled/failed). RLS is enabled, no client
policies exist, and only backend service_role receives DML privileges. Raw messages,
chat/user IDs, images and file IDs are not stored in this table.

After a claim, the highest pixel-area photo variant that is not declared oversized
wins; declared size breaks ties. Unknown size is allowed because the actual download
is bounded. No suitable variant becomes failed. getFile resolves the file path and
download_file streams raw bytes into memory; captions/filenames/MIME are not proof
of type. The download boundary itself calls no Pillow, Vision, S3 or receipt repository.
IncomingPhotoMessage contains IDs, received_at, a file ID and raw bytes excluded
from repr. IncomingTextMessage has common metadata and text excluded from repr. Commands, stickers, audio and documents become UnsupportedMessage. Blank/text
payloads reach IncomingTextMessage with original whitespace for category validation;
the transport performs no category interpretation.

The injected IngressHandler accepts only these domain values, not raw Telegram
JSON. V0.1-007 wires ReceiptProcessor into the default application lifespan,
replacing TransportOnlyHandler. The HTTP response exposes only an ingress status,
never event contents. Receipt business outcomes are sent through the Telegram adapter.

### Failure and acknowledgement contract

Claims permit one attempt, not automatic recovery or exactly-once completion.
Duplicate IDs in ANY status get 200 duplicate without downloads or handler calls.
Telegram download error/deadline attempts one safe error notification, marks failed
and returns 200 failed, intentionally ending Telegram retries for that ID. Typed
receipt-processing failures also return 200 failed after recording failed state. Unexpected handler errors mark failed and return
a sanitized 500. Persistence errors (claim or completion) return 503. A timeout can
leave the commit outcome uncertain; a following delivery checks the durable claim.
Crashes/cancellation or failed terminal writes leave received; they are also skipped
on redelivery. Handled means the applicable user-facing outcome completed. Only the saved business
outcome establishes a new receipt; duplicates/unreadable/unknown/unsupported are
also handled after successful notification. Failed may coexist with a committed
receipt when confirmation or the terminal status write fails.

There is no stored raw payload, lease, replay command, recovery worker, durable
notification ledger or retry loop. Today the user/operator must resubmit a new message/update ID after a
failure; failed/received rows must not be blindly deleted because a prior effect
might already have happened. V0.1-009 must define durable recovery before this is
considered reliable receipt ingestion. Later recovery must revise this contract with persisted work and reconciliation,
rather than assume HTTP retries replay claims.

### Telegram HTTP and messaging boundary

TelegramClient exposes get_file, download_file and send_message; HTTPTelegramClient
uses HTTPX directly. Endpoints are fixed HTTPS api.telegram.org URLs; returned paths
must be relative safe segments, with no traversal, query, fragment, percent escapes
or redirects. No externally supplied hostname is fetched. The factory disables
environment proxies. HTTP I/O has finite timeout values and each operation has an
asyncio deadline. TELEGRAM_TIMEOUT_SECONDS defaults to 10 (positive, at most 30);
photo getFile plus download share that deadline. Request body reading has the same
separate deadline; DB operations retain their existing 20-second I/O timeout.
There is no promised aggregate webhook latency or background processing.

File downloads check Content-Length and cumulative streamed bytes against
MAX_UPLOAD_BYTES, reject empty results, and close streams on overflow/error.
getFile declared size is also checked. JSON API responses have a separate 64 KiB
cap. Accept-Encoding is identity and compressed responses are rejected before
iteration to prevent decompression expansion. All bytes remain in memory, never
written to temporary files. ReceiptProcessor now passes the downloaded bytes to V0.1-003 image validation.

send_message accepts a numeric chat ID and nonblank text (maximum 4096 characters).
It omits parse_mode and entities by default, so external vendor/category text stays
literal, and forwards a caller-supplied `parse_mode` only when one is passed.
Exactly one message opts in: the execution-summary card (see
[Presentation, errors and lifecycle](#presentation-errors-and-lifecycle)).
receipt_messages.py constructs business summaries and V0.1-008 category prompts;
the Telegram adapter remains presentation-policy independent and never infers,
adds or rewrites formatting itself.
Authentication errors (401/403), rate limits (429), timeout/deadline, unavailable
(408/5xx/network), download rejection and malformed responses have separate safe
Telegram exception classes. 429 exposes a positive integer retry_after when valid.
Error strings contain only static codes, with sensitive provider exception chaining
suppressed. Retry behavior differs by idempotence: getFile/download retry transient
failures under the bounded policy, while a send is never retried after a timeout or
network failure (it may already have been delivered) and instead raises
TelegramAmbiguousSendError. Only a definitive rate-limit rejection, which proves
nothing was sent, is retried for send_message.

Service logs contain update ID and outcome only. HTTPX token-bearing request logs
are suppressed, and HTTPcore tracing is disabled via idempotent logger filters
process-wide when the adapter is constructed. No file IDs, text, bytes, secrets or
provider bodies are logged. Keep other SDK tracing and exception-local capture off;
custom logging/HTTP instrumentation must uphold the same boundary.

API references: [Telegram Bot API](https://core.telegram.org/bots/api#getfile)
for getFile/sendMessage, rate-limit metadata and webhook secret headers. No live
Telegram call or registration was performed. Local PostgreSQL checks validate
constraints and competing claims, not a deployed PostgREST/Supabase installation.

## Implemented known-vendor orchestration (V0.1-007)

ReceiptProcessor implements IngressHandler through accept(event), with a separately
testable process(IncomingPhotoMessage) -> ProcessingOutcome. Dependencies are the
image processor, ReceiptVisionProvider, VendorRepository, ReceiptRepository,
ReceiptImageStorage and TelegramClient protocols. Routes and adapters contain no
receipt rules. Startup injects real implementations with shared, lifespan-owned
clients; tests inject fakes and HTTP mock transports. No new runtime dependency or
SQL migration was needed.

```text
claimed photo → bounded download → image validation/compression → Vision
  → confidence → normalized vendor lookup → stored category → duplicate lookup
  → exact processed JPEG to S3 → final receipt INSERT → plain-text confirmation
```

ImageService runs in a worker thread to avoid blocking the HTTP event loop. A
processor-local lock serializes its image work, protecting Pillow warning contexts
in the single application instance. Cancellation waits for that image worker before
releasing the lock; it does not continue Vision/database processing. This is awaited
work, not durable background processing. The same immutable ProcessedImage instance
is passed to Vision and S3. No raw-image persistence or recompression occurs.

The Vision provider returns a validated ReceiptExtraction. The processor never
parses raw model JSON. High/Medium continue; Low stops before vendor lookup/storage.
ImageValidationError and ReceiptExtractionError produce a concise clearer-photo
request, with no receipt writes. Provider/system errors are separate typed failures.

normalize_vendor_name performs the existing exact deterministic Unicode/whitespace
normalization. Vendor lookup uses that value; no fuzzy/AI matching or aliases are
introduced. Known vendor.category always wins, including when AI category is absent.
The known-vendor path never upserts vendor memory. V0.1-008 replaces the temporary
unknown-vendor branch: after duplicate lookup it delegates to PendingWorkflow, which
persists the same processed image/extraction and prompts for a category.

Duplicate lookup uses normalized vendor, date and exact Decimal total. A match
skips upload/INSERT and sends the duplicate notice. Otherwise upload occurs only
after all earlier gates pass. A Receipt is constructed with an opaque UUID, UTC
creation time, chat ID, extraction fields, stored category, key and stable s3:// URI.
The final INSERT is one database statement. No presigned URL is generated or stored.
The database's existing unique constraint is the final defense against races.

### Compensation and uncertain commits

A duplicate INSERT collision triggers one best-effort delete of this attempt's new
object, then the same duplicate message. A confirmed generic PersistenceError also
triggers deletion before surfacing ReceiptPersistenceFailure, preserving the original
typed error. Invalid final model construction is handled before any INSERT and also
cleans up the new upload. Successful insertion never triggers deletion, even if the
later Telegram confirmation fails.

Cleanup failure is logged with update ID only. ReceiptPersistenceFailure retains
failure, cleanup_failed, receipt_id and object_key for future repair logic; its
public message is a static code. On collision plus cleanup failure, send the duplicate
notice but raise the typed cleanup-bearing failure and mark the update failed. If
notification also fails, preserve the original insert/collision reason and mark
notification_failed; do not send a second fallback message.

PersistenceUnavailableError or InvalidPersistenceResponseError from INSERT do not
prove rollback. One existing duplicate-key lookup checks for our exact UUID and
storage key/URI. A positive identity match confirms commit and proceeds to success
notification. Otherwise keep the object, raise an uncertain ReceiptPersistenceFailure
and send a generic service-failure message. A negative read cannot exclude a still-
in-flight transaction committing later. This narrow check protects committed data; it
is not a general replay/recovery subsystem. V0.1-009 generalizes the same idea in the
repository layer: an insert is retried only after a reconciliation read establishes
that the earlier attempt did not commit (see
[Bounded provider retries](#bounded-provider-retries-v01-009)).

Upload errors can similarly leave uncertain objects; the storage error's opaque
key is retained on ReceiptStorageFailure. Cancellation during upload/INSERT may
leave partial work. V0.1-009 added reconciliation for uncertain inserts and uploads,
but there is still no durable cleanup ledger and no orphan sweeper, so an unresolved
ambiguous outcome retains its object for later manual repair. S3 plus PostgreSQL is a
compensating transaction pattern, never an atomic transaction.

### Presentation, errors and lifecycle

receipt_messages.py owns presentation. Success includes vendor, DD/MM/YYYY,
DEFAULT_CURRENCY and Decimal amounts with commas/two decimals, optional VAT, saved
category and confidence. VAT=None omits the line; zero remains visible. No float
conversion, inferred currency or storage URL is used. Line/control characters are
collapsed and display values capped at 300 characters so messages fit the send limit.
Stored domain values retain their full precision and content.

The execution-summary card (`success_summary`, and `remembered_summary` after a
category reply) is the one message sent with `parse_mode=MarkdownV2`: bold field
labels over a `✅ Receipt Processed` header, with every value rendered as a code span.
Two distinct defences apply, because Telegram interprets message text in two distinct
ways.

Label text and any value outside a code span passes through `escape_markdown_v2`,
which backslash-escapes the full reserved set ``_*[]()~`>#+-=|{}.!\`` documented for
[MarkdownV2](https://core.telegram.org/bots/api#markdownv2-style). That is needed for
two reasons: receipt text is untrusted model output, so an injected `_[Vendor]*` must
render literally rather than as formatting; and Telegram rejects an incorrectly escaped
MarkdownV2 message outright, so a missed escape would be a delivery failure, not a
cosmetic bug. Escaping applies after the existing control-character collapse, so a
forged newline cannot fabricate an extra field line either.

Escaping does not stop Telegram's own entity detection, which runs regardless of
parse_mode and turns URLs, hashtags, mentions and emails into clickable links. A
crafted receipt image could otherwise place a live attacker-supplied link inside a
message the bot vouches for. Text inside a code entity is exempt, so every extracted
value is wrapped in a code span, where only the backtick and backslash need escaping.
Both behaviors were confirmed against the live Bot API by inspecting the entities
Telegram returns.

Every other message (errors, prompts, duplicate and cancellation notices) remains
literal plain text with no parse_mode, which needs no escaping at all.

Text delegates to PendingWorkflow.reply; unsupported messages receive a photo-required
response. Category interpretation remains deterministic application validation. User-correctable outcomes (unreadable/Low confidence),
unknown scope, duplicate and saved outcomes return handled after successful sends.
Internal Vision/vendor/duplicate-lookup/storage/persistence failures retain separate
orchestration exception types, attempt one generic user-safe failure message, and
mark the update failed. An error-message send failure preserves the original reason.
A success-send failure exposes ReceiptNotificationFailure(committed=True) and keeps
the receipt/image. No send retries or notification recovery are added.

Claim/status DB errors still return HTTP 503. Failed terminal writes can leave
received after a receipt and message already succeeded. Repeated update IDs never
repeat work; received/failed claims require later recovery or a new user submission.
Unexpected handler bugs are sanitized by ingress and return 500. Cancellation
propagates and can leave received. These are limitations, not exactly-once guarantees.

### Timing and logging

Processing remains synchronous through the final send; clients wait for the image,
Vision, database, storage and messaging stages. Network work has existing adapter
limits; there is no promised aggregate latency budget or queue durability. Telegram
may retry before completion; the durable claim prevents overlapping repeats, but
there is no automatic recovery if the owning request/process stops.

Safe logs measure telegram_download, image_processing (including lock wait), vision,
vendor_lookup, duplicate_lookup, s3_upload, database_insert, optional s3_cleanup and
insert_reconciliation, plus processor and whole-ingress total_processing_ms.
Stage/duration/update ID and safe outcome/error flags are emitted as record fields
and useful console text. No amounts, vendor/category text, user/chat IDs, image bytes,
credentials or signed URLs are logged by orchestration. Application startup sets
provider HTTP/SDK tracing to WARNING, including HTTPX INFO URLs that could expose
PostgREST financial filters; Telegram's token-URL filters remain active. Keep custom
instrumentation and exception-local capture disabled.

Offline fake-provider timings verify instrumentation, not live response-time or
extraction quality. Real cloud privacy, provider contract and operational latency
remain later deployment/demo checks.

## Implemented pending workflow (V0.1-008)

PendingWorkflow owns unknown-vendor creation, photo admission and later text replies.
ReceiptProcessor depends on its narrow PendingHandler interface. The application
factory injects SupabasePendingWorkflowRepository and the existing S3/Telegram
clients. Ingress remains responsible for authentication, authorization and durable
update claims. No route contains category rules. Import/startup never registers a
webhook or sends a message.

After an authorized update is claimed, before_photo checks awaiting_category by
chat AND user before downloading/processing the next photo. Active work produces a
finish-or-cancel response and ends the request. Expired work is closed/cleaned on
access, then the user is asked to resend. Admission is an optimization; it is not a
reservation. The partial unique index remains the final concurrent creation defense.
A second check after extraction avoids avoidable uploads if another request won.

For an unknown vendor, Low confidence is still rejected first. Final receipt duplicate
lookup runs BEFORE upload or pending creation. Then store the exact ProcessedImage,
construct PendingReceipt with all extraction data, chat/user identity, key/stable URI,
null category, awaiting_category, UTC creation time and configured TTL (30 minutes
by default), and INSERT it. Do not create a final receipt or vendor memory yet.
Prompt only after persistence: ask for a free-form category, give suggestions and
explain exact cancel. The request then ends; no coroutine, HTTP request or process
memory waits for the human. A fresh application instance can resume from the row.

Confirmed pending INSERT failure attempts one S3 delete and preserves the original
typed failure if cleanup fails. A partial-index collision deletes only this attempt's
new object and sends the already-waiting message. Ambiguous INSERT responses use one
get_by_id check against the generated row; unresolved commits retain the image and
surface uncertainty rather than delete possibly persisted work. Prompt delivery
failure retains the pending row AND its image. There is no notification retry.

### Category replies and lifecycle

IncomingTextMessage reaches PendingWorkflow.reply under the same authorization and
update-ID claim boundary. It loads only awaiting_category for the exact chat/user.
No active work produces a no-pending/photo-first response without writes. Text is
preserved by ingress until validation so blank/control input is not silently treated
as an unsupported attachment. Commands beginning with slash remain unsupported.

Confirmed Category is a shared Python type: trim Unicode whitespace, accept 1–100
Unicode code points, reject C0/C1 controls and the explicit bidi/zero-width formatting
set in domain/category.py. Do not collapse internal spaces, casefold categories or
use an LLM/enum. Suggestions do not restrict allowed categories. SQL enforces the
same length, trimmed form and forbidden characters on vendor/final/pending categories.
AI extraction.category remains advisory/unbounded by this confirmed-category rule.

Exact trimmed case-insensitive cancel closes active work as cancelled. Cancellation
Fees remains a valid category. Expiry is checked on direct access and authoritatively
inside completion with clock_timestamp(), including after waits on pending/vendor/
receipt locks. Late expiry detected after a vendor or receipt mutation raises P0002,
rolling the attempt back; Python then closes expired work in a separate transaction.
No automatic extraction reruns. App/database clocks should be synchronized; SQL is
the authority that prevents completing expired rows.

```text
awaiting_category → completed  (atomic vendor + receipt + pending transaction)
awaiting_category → cancelled  (exact cancel, conditional close)
awaiting_category → expired    (expiry found during direct access)
awaiting_category → failed     (resolved final-receipt duplicate after rollback)
```

Legacy processing/failed/completed/expired states remain valid. No transient across-
request processing transition releases the awaiting slot during completion. The
new cancelled value is additive; no old migration is rewritten. Proactive expiry
and orphan cleanup, retention and failed-update recovery remain V0.1-009 work.

### Atomic PostgreSQL completion and security

Migration 202609100004 adds valid_receipt_category, complete_pending_receipt and
close_pending_receipt plus category constraints and cancelled state. Existing
category rows longer than 100, untrimmed or containing forbidden controls cause the
migration to fail: review/backfill them explicitly, never silently truncate memory.
No new table, queue or distributed workflow framework was added.

complete_pending_receipt accepts only pending UUID, chat ID, user ID and validated
confirmed category. It SELECTs the owned row FOR UPDATE, requires awaiting_category,
checks expiry and rejects Low-confidence persisted candidates. All receipt values
come from that row, not caller-supplied extraction fields. Existing category memory
wins: INSERT vendor ON CONFLICT(normalized_name) performs an identity-only update,
locking the vendor and returning its saved category without replacing it. Then
INSERT the receipt and UPDATE pending to completed with the resolved category.
The final receipt ID equals the pending UUID, and image key/URI are reused unchanged.
All three mutations commit together or roll back together on any failure.

No Python sequence of vendor upsert + receipt insert + state update is used.
The second completion waits for the pending lock then returns inactive. Concurrent
workflows for one vendor serialize on vendor identity and use the same stored
category. A final receipt uniqueness error is propagated and mapped to the existing
DuplicateReceiptPersistenceError; the entire completion attempt rolls back, including
any vendor insert/identity update and pending transition.

After that rollback, close_pending_receipt locks the same owned awaiting row and
requires the final duplicate to exist before marking pending failed. It handles
cancel/expiry with equivalent row locking and expected-state checks. Already-terminal
or missing/wrong-owner rows return inactive without cleanup permission. It returns
cleanup_allowed only if no final receipt references this pending storage URI. This
protects a shared object and prevents cancellation racing completion from deleting
the winning receipt image. Cleanup always uses the returned owned pending key, never
the winning receipt's key. The trusted backend must not invent/reuse object references
outside these flows; normal uploads use new opaque keys.

All functions use SECURITY INVOKER and SET search_path=pg_catalog with public objects
schema-qualified. PUBLIC, anon and authenticated cannot execute them; service_role
gets explicit execution. RLS and existing backend-only table grants remain intact.
No SECURITY DEFINER privilege escalation is used. Role checks were verified on real
PostgreSQL with stand-in Supabase roles, not a live cloud project.

RPC envelopes become PendingResult with typed PendingReceipt/Receipt, ownership and
outcome validation. SQL renders nested monetary amounts as text before JSON encoding,
preventing float conversion. Malformed/empty/mismatched envelopes fail safely. The
SDK RPC adapter disables retries and maps named duplicate, late-expiry and availability
failures into typed exceptions. PostgreSQL transaction/error behavior follows its
[PL/pgSQL documentation](https://www.postgresql.org/docs/17/plpgsql-control-structures.html),
and function permissions follow [CREATE FUNCTION](https://www.postgresql.org/docs/17/sql-createfunction.html).

### Cleanup, notification and contextual memory

Completion retains the original S3 object without copying/re-uploading and formats
the existing receipt summary plus an accurate saved-category-memory statement. A
later distinct receipt from that normalized vendor follows the known-vendor path,
with memory overriding AI category and no second prompt.

Cancellation/expiry/resolved duplicate transitions commit before best-effort S3
cleanup. A failed delete logs only update ID, surfaces a typed failure with cleanup
metadata and still attempts the appropriate user response. The terminal pending row
retains its key for later repair; no second cleanup automatically occurs on another
reply. This is not a cross-system atomic transaction.

Prompt sent after pending creation: handled. Category completion and confirmation:
handled. Invalid category/no pending/cancel/expiry/duplicate notices: handled when
required operations succeed. DB, cleanup or notification failures: failed where
possible. A failed prompt keeps awaiting state; a failed completion confirmation
keeps completed pending/vendor/receipt/image state. No compensating deletion follows
a mere send failure. Claim/status failures still return 503, and repeated update IDs
are never replayed. Remaining ambiguous commits and notification recovery are
explicit V0.1-009 limitations.

Verification includes an offline multi-webhook memory-cycle test using separate
application instances with shared fake persisted rows, and real PostgreSQL tests
for atomicity/rollback, concurrent replies/cancel/vendor identity, expiry after lock
waits, ownership, exact money, shared-image cleanup prevention and function privileges.
No live cloud integration or scheduled worker is required or implemented.

## Bounded provider retries (V0.1-009)

`core/retry.py` holds the single retry primitive every provider boundary uses.
It is deliberately small and explicit: the caller passes the operation plus a
`retryable` predicate, so no adapter decides on its own that an operation is safe to
repeat. `RetryPolicy` defaults to 3 attempts, 0.25s initial delay, exponential
growth capped at 1.0s per wait and 1.5s of total sleep. Both ceilings matter inside a
webhook: a provider's own `retry_after` hint is honored only while it fits the
budget, so a Telegram "retry after 12 seconds" gives up immediately instead of
holding the request open. Retry logging records attempt count and total delay only,
never exceptions, payloads, URLs or operation arguments.

Retryability is decided per operation, by idempotence rather than by error class
alone:

| Boundary | Retried | Never retried |
| --- | --- | --- |
| OpenAI Vision | rate limit, timeout, unavailable | authentication, invalid response, unreadable receipt |
| Telegram reads (getFile, download) | transient Telegram errors | authentication, download rejection, malformed response |
| Telegram send | definitive rate-limit rejection (`ok=false`, so nothing was sent) | timeout/network failure, which may already have delivered the message and raises TelegramAmbiguousSendError |
| Supabase reads | unavailable/serialization failures | duplicate key, check violation, malformed response |
| Supabase writes | unavailable, then reconciled (below) | duplicate key, check violation, malformed response |
| S3 | unavailable, then reconciled (below) | authentication, invalid key |

Writes cannot simply be replayed, because an unavailability error does not prove the
write did not commit. Two reconciliation paths handle that. `_insert_reconciled`
retries an insert only after re-reading the row: an existing row matching the
expected value is returned as success, a different row is ambiguous, and a confirmed
absence is retried. `S3ReceiptImageStorage.store` reuses one opaque key for every
attempt and, on a transient failure or an `IfNoneMatch` collision, issues a HEAD:
identical content is treated as already stored (idempotent success), different
content is ambiguous, and a missing object is retried.

Uncertainty is never downgraded into a confident answer. Once a write enters the
uncertain path and reconciliation cannot resolve it, the failure surfaces as
`AmbiguousPersistenceError` or `StorageAmbiguousError`, never as a plain "unavailable,
nothing happened" claim, and the uploaded object is retained for later repair rather
than deleted. The converse also holds: a response that was actually received but is
malformed, mismatched or fails an ownership/safety check is an
`InvalidPersistenceResponseError` and fails immediately. It is a deterministic
contract violation, so retrying cannot help, and masking it as ambiguous would hide a
backend fault (for example an RPC returning another user's pending row).

## Implemented schema and models (V0.1-002)

The initial migration defines vendors, receipts and pending_receipts only.
Use UUID primary keys, timezone-aware timestamps, SQL DATE/Python date and
NUMERIC(12,2)/Decimal. Money is positive for totals, nonnegative for VAT, and VAT
cannot exceed total. Python rejects floats, booleans, non-finite values, excess
fractional precision and values above 9,999,999,999.99. Accepted amounts are
quantized to two places without rounding meaningful digits. SQL NUMERIC rounds
excess fractional digits before constraints; all future writes must first use the
Pydantic models. Future extraction adapters must preserve decimal tokens or request
decimal strings; passing JSON float amounts directly is intentionally rejected.

Categories are trimmed nonblank free-form strings, nullable on extraction/pending,
and mandatory on final receipts and completed pending rows. Confidence is exactly
High/Medium/Low. All three can be represented by schemas; V0.1-007 rejects Low confidence
before final insertion and formats DD/MM/YYYY only at the presentation boundary. Models reject unknown fields and are immutable.

Vendor normalization uses NFKC, casefold, NFKC again, and whitespace collapse.
Punctuation remains significant. Internal models verify normalized identity against
the display name. SQL uniqueness compares the normalized values provided by trusted
server code; SQL does not independently reproduce Python Unicode normalization.

| Table | Implemented constraints and indexes |
| --- | --- |
| vendors | UUID id, normalized_name unique/nonblank, display_name and category nonblank, created_at. Unique constraint supplies vendor lookup index; no speculative updated_at field. |
| receipts | UUID id, BIGINT telegram_chat_id, required nonblank vendor_name/normalized_vendor_name/category/image_storage_key, DATE receipt_date, NUMERIC amounts, confidence check, created_at. UNIQUE(normalized_vendor_name, receipt_date, total_amount), plus chat/creation index. |
| pending_receipts | UUID id, BIGINT chat/user IDs, validated extraction fields and private image key, nullable category, six-state check including cancelled (migration 004), created_at and expires_at with expiry after creation. Completed requires category. Unique partial index on chat/user WHERE state = 'awaiting_category'; active lookup and awaiting-expiry indexes. |

PendingReceipt represents post-extraction data, so extraction fields are required
in every state. Raw ingress event records, attempt counters, claims, notification
status and receipt linkage are deferred to recovery. Migration 202609100003 adds
the separate telegram_updates transport table; the initial migration stays unchanged.

Only one category question per chat/user is allowed. The partial unique index
covers awaiting_category only, not processing. In V0.1-008, lock the pending row
and keep the awaiting state until finalization commits, or perform any intermediate
transition in that same transaction. This avoids releasing the slot while a user
interaction is still active. The earlier proposed across-request claim design is
superseded by this transaction contract. Expired rows must be explicitly transitioned;
a wall-clock expression does not belong in the partial-index predicate.

## Completion contract

The V0.1-008 RPC implementation above fulfills the vendor/receipt/pending atomic
completion contract. Object storage and Telegram stay outside the database
transaction. Global retries, ambiguous-write repair, notification replay and orphan
maintenance are future resilience work.

## Database security

RLS is enabled on all four tables (including telegram_updates), with no browser/client policies. PUBLIC and,
when present, anon/authenticated receive no table privileges. Only the server
service_role gets explicit DML grants; Supabase supplies its BYPASSRLS attribute.
Plain PostgreSQL migration owners can administer tables. Local verification uses
stand-in roles, not actual Supabase credentials. No bucket is created yet; future
images use private AWS S3. Global vendor and receipt uniqueness assumes one
trusted user; multi-user access requires a new owner-scoped design.

Reference behavior: [PostgreSQL numeric types](https://www.postgresql.org/docs/15/datatype-numeric.html)
explain scale rounding; [partial indexes](https://www.postgresql.org/docs/16/indexes-partial.html)
provide uniqueness over a selected subset of rows.

## Storage, security and failure consistency

Generate opaque UUID-based image object keys; do not trust vendor names or supplied
filenames. Use a private bucket with restricted access policies. Keep object keys and stable s3:// URIs in
rows, with authorized short-lived S3 presigned GET URLs when access is required.
Privileged Supabase credentials remain on the server and bypass policies only in
narrow repository operations; never expose them to Telegram or clients.

Object storage, SQL and Telegram cannot share one transaction. Persist enough state
to reuse an uploaded object after retries; compensate failed inserts with deletion
or a recorded cleanup obligation. Expiry and orphan cleanup use an explicit
maintenance command initially. Record Telegram notification status and retry failed
sends without replaying receipt insertion. Exactly-once Telegram message delivery
is not promised: a network timeout after acceptance may cause a repeated message.

Use explicit timeouts and bounded exponential backoff with jitter for transient
provider errors, honoring retry hints. Retry only operations safe to repeat; use
stable update/workflow/object identifiers. Permanent failures transition to failed
and send a helpful retry message where Telegram is reachable. If Telegram itself
is unavailable, record the delivery failure and emit a sanitized operational log.
Database outages cannot be hidden behind a successful acknowledgement.

Logs contain correlation IDs, safe error codes, timings and state transitions;
exclude credentials, image data and sensitive payloads. Restrict request/download
size while streaming, inspect actual image formats, limit decoded pixels, handle
Pillow decompression warnings/errors, apply EXIF orientation before stripping
metadata, and reject unsafe animation/multipage input unless explicitly supported.
