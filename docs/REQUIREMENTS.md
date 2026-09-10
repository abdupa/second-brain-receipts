# Requirements

## Scope

Build an autonomous Second Brain receipt sub-agent as a polished hiring-assessment
POC. Telegram is the selected channel. Use Python, FastAPI, Pydantic, Pillow,
OpenAI Vision, Supabase PostgreSQL and private AWS S3, pytest, and Docker.
V0.1-001 through V0.1-004 delivered the foundation, image and Vision boundaries.
V0.1-005 added Supabase database repositories and private AWS S3 storage.
V0.1-006 added authenticated Telegram ingress, durable transport deduplication and
bounded download/send adapters. V0.1-007 connects known-vendor receipt processing.
V0.1-008 adds persistent unknown-vendor category confirmation and atomic completion.
V0.1-009 adds bounded provider retries with reconciled writes, the escaped MarkdownV2
execution-summary card, the runtime container image and the CI pipeline.

Client assignment explicitly requires S3 or Google Drive for receipt images; AWS S3 was selected.
This corrects the earlier Supabase Storage assumption. Store only the validated,
metadata-stripped, compressed JPEG from V0.1-003, not raw uploads.

Do not add React, Next.js, LangChain, LangGraph, Temporal, Redis, Celery, Kafka,
Kubernetes, vector databases, or multiple autonomous agents unless proven necessary.

## Functional acceptance criteria

| ID | Requirement and acceptance |
| --- | --- |
| R01 | Authenticate Telegram webhook events using the configured secret before processing; download photo inputs through the Telegram Bot API. Documents are unsupported in V0.1-006. |
| R02 | A custom Pillow function checks actual format, byte size, dimensions, corruption and decompression hazards; applies orientation, preserves aspect ratio, downsizes large images, strips unnecessary metadata, and compresses while retaining readable text. Reject unsupported or unsafe input with a retry message. |
| R03 | Send only optimized images to OpenAI Vision. Validate structured extraction with Pydantic before any receipt insert: vendor_name, date, total_amount, optional vat_amount, category, and confidence_score (High, Medium, Low). Display dates as DD/MM/YYYY. |
| R04 | Deterministically normalize vendor names. Detect duplicates using normalized vendor, receipt date and total amount; enforce the same rule with database uniqueness to resolve races. |
| R05 | Recognized vendors reuse saved categories, overriding any model suggestion. Unknown vendors persist extracted data and an awaiting_category state, receive a category question, and resume from a later authenticated webhook. Save the vendor/category association for future use. |
| R06 | Never hold an HTTP request open while awaiting a category reply. State must survive process restart; handle expiry, repeated events and concurrent replies predictably. |
| R07 | Blurry, damaged, corrupted, unsupported, unreadable, invalid or low-confidence receipts never produce unreliable final receipt rows. Give a useful retry message without crashing or silently discarding the problem. |
| R08 | Handle OpenAI timeouts, rate limits and server errors, Telegram API errors, and database/storage failures with bounded retries where safe, explicit state, and safe user-facing errors. |
| R09 | Store validated receipts in Supabase PostgreSQL. Store processed JPEG images only in private AWS S3 with explicit SSE-S3 AES256 encryption; persist object keys and stable s3:// URIs. Generate short-lived presigned GET URLs only on authorized access; never persist them. |
| R10 | Send a clean Telegram summary after success; use plain text or correctly escaped formatting. Report duplicates clearly without inserting another receipt. |

## Security acceptance criteria

No hardcoded credentials; ignore `.env` and provide blank variable names in
`.env.example`. Keep privileged Supabase and provider credentials server-side.
Validate webhook bodies and files, restrict download/upload sizes and image pixel
counts, generate object names, and authenticate before expensive work. Restrict
chat access for this POC. Do not log secrets, token-bearing Telegram URLs, image
bytes, model payloads, or sensitive receipt content. Return safe messages without
stack traces. Keep AWS credentials server-side or use IAM roles. S3 Block Public
Access and private bucket/IAM policies are required; RLS remains enabled in
Supabase. Live cloud access-policy verification is a later deployment gate; local
mocked adapter and PostgreSQL checks do not establish actual cloud privacy.

## Test coverage required by later milestones

Use pytest and mock external providers at service boundaries. Cover:
1. Valid end-to-end receipt orchestration.
2. Image validation, aspect ratio, resizing, compression, metadata removal and limits.
3. Structured extraction validation, including dates, decimals and missing fields.
4. Recognized vendor category override.
5. Unknown vendor persistence and prompt.
6. Category reply resumption, including restart, expiry and concurrent delivery.
7. Duplicate receipt and database uniqueness races.
8. Corrupted and non-image input.
9. Unreadable and low-confidence extraction.
10. OpenAI timeout, rate limiting and server errors.
11. Database/storage failure, rollback and cleanup; Telegram delivery failure.
12. Webhook authentication, chat authorization, payload limits and secret-safe logs.

## Assumptions to confirm before their implementation

- Single trusted user/private Telegram chat for the POC. V0.1-006 enforces
  TELEGRAM_ALLOWED_USER_ID, a numeric sender ID, and matching private chat ID.
  Vendor memory and duplicate uniqueness are global as requested. Multi-user support
  requires owner-scoped uniqueness, memory, storage and access policies first.
- One configured currency (`DEFAULT_CURRENCY=PHP` by default); no conversion or
  cross-currency deduplication. Use Decimal/NUMERIC(12,2); do not infer currency.
  Changing currency on an existing database requires an explicit data migration.
- Low confidence is returned by the provider as a candidate and rejected by
  V0.1-007 before vendor lookup, storage or insertion. Medium confidence passes validation;
  model confidence alone does not establish correctness.
- One active category question per chat/user. Further images receive a useful
  finish-or-cancel message. Support explicit cancellation and expiry.
- Upload limit defaults to 10 MiB and is enforced by the image service. Pending TTL
  defaults to 30 minutes, enforced on direct access and inside atomic completion. Image defaults are 40 MP input,
  1600-pixel output long edge and JPEG quality 85; see ARCHITECTURE.md for bounds.
  Readability still requires representative receipts and visual/extraction checks;
  dimension validation alone cannot detect blur. Retry budgets are set in V0.1-009
  and bound individual provider operations, not aggregate webhook latency.

Confirmed categories are trimmed free-form strings of 1–100 characters, not an enum.
C0/C1 controls and selected bidi/zero-width formatting characters are rejected in
Python and SQL; AI category suggestions remain advisory and are not persisted as memory. Pending rows
store post-extraction data; the database permits only one awaiting_category row
per chat/user. Runtime settings require credentials in development/production;
test mode may omit them, but explicitly blank secrets are always invalid.

## V0.1-006 transport acceptance

POST /webhooks/telegram authenticates the secret header before reading bounded
JSON (64 KiB default), validates strict numeric update/sender IDs and authorizes
one private-chat user. Unauthorized updates receive HTTP 200 with ignored status
without claiming or calling providers, avoiding pointless Telegram retries.
Photo variants are ranked by pixel area, then declared bytes, among variants not
known to exceed MAX_UPLOAD_BYTES. Actual streamed bytes remain authoritative.
Text is preserved until application validation, including blank/control-containing
replies, so invalid categories receive a useful response. Commands, documents and other
unsupported messages receive a safe photo-required response without extraction.

Atomic PostgreSQL update-ID claims prevent repeated transport side effects, including
concurrent deliveries. Claim/status database errors receive 503, not duplicate success. Claimed
transport failures are marked failed and acknowledged with 200; duplicate received,
handled and failed claims are never replayed automatically. Crash/cancellation can
leave received rows. This is at-most-once transport attempting, not reliable receipt
completion. Stale-claim recovery is still open (V0.1-009 bounded provider retries but
did not add claim replay); resubmission with a new update ID is needed now.
V0.1-007 replaces the transport-only handler with the known-vendor processor.
The claim/recovery limitation still applies, including after partial success.

## V0.1-007 known-vendor acceptance

Only processed, validated JPEG bytes reach Vision and S3; the same ProcessedImage
instance is reused without recompression. High/Medium continue, while Low and
unreadable/invalid images receive a retry message without any upload or receipt.
Normalize vendor identity deterministically, read saved vendor memory and always
use its category instead of the AI suggestion. Do not mutate that memory.
V0.1-008 extends unknown vendors to the persistent pending workflow below.

Check normalized vendor/date/Decimal total duplicates before storage. A known nonduplicate receipt uploads to S3, then performs one final receipt INSERT.
An unknown nonduplicate stores a pending record and image until category confirmation. Existing
SQL uniqueness handles races: a collision produces the duplicate response and
best-effort deletion of this attempt's new object. Confirmed insert rejections also
trigger one cleanup attempt, preserving the original typed failure if cleanup fails.
Timeout/malformed insert responses have uncertain commits: one duplicate-key read
can positively identify our UUID and storage reference; otherwise retain the object
for reconciliation rather than delete possibly committed receipt data. No insert
retry, general recovery worker or cross-system atomicity is claimed.

Success sends plain text with DD/MM/YYYY, configured currency, Decimal amounts to
two places, optional VAT, saved category and confidence. Successful notification of
saved/duplicate/unreadable/unknown/unsupported outcomes marks the update handled.
Internal failures mark failed when the DB is available; a failed success send never
deletes the saved receipt/image. Claims are not replayed automatically.

Webhook processing remains synchronous through confirmation. Major stage and total
latencies are measured without financial contents. Provider limits bound individual
operations, not aggregate webhook latency. Live latency/quality/cloud privacy remain
later deployment checks; offline tests establish behavior, not production readiness.

## V0.1-008 persistent category acceptance

Before photo download, check for this chat/user's awaiting_category record. An
active question blocks further photos with a finish-or-cancel instruction. Expired
work discovered on access is closed and cleaned up best effort; ask for resubmission.
Concurrent creation is still protected by the partial unique index, never overwritten.

For accepted unknown vendors: check final duplicates before upload; store the exact
processed JPEG; persist all extraction values, identity, key/URI, null category and
created_at/expires_at in awaiting_category; then prompt and end the request. Prompt
failure retains the pending row/image and marks the update failed where possible.
A later text webhook loads persisted state and completes without image or Vision work.
Blank/invalid categories remain awaiting, arbitrary text without pending creates no
state, and exact trimmed case-insensitive cancel transitions to explicit cancelled.
Expiry transitions to expired, with neither vendor creation nor final receipt.

The backend-only completion RPC locks and verifies pending ownership/state/expiry,
uses authoritative stored receipt values, resolves vendor memory (existing category
wins), inserts the final receipt and marks pending completed in one transaction.
Receipt failure rolls everything back. Concurrent replies cannot complete twice.
A duplicate collision is resolved in a separate conditional transaction after rollback:
mark failed, permit cleanup only of an unreferenced pending object, and notify duplicate.

Successful completion retains the same S3 object and sends the summary plus accurate
vendor-category memory confirmation. No category prompt/completion notification failure
undoes already-persisted work. Cancellation/expiry/duplicate cleanup is best effort;
failed cleanup is surfaced and may leave an orphan. No scheduled worker, retry engine,
claim replay or notification recovery is implemented by this milestone.

## V0.1-009 resilience, presentation and packaging acceptance

One shared bounded retry policy covers every provider boundary: 3 attempts, 0.25s
initial delay, 1.0s maximum single wait, 1.5s total sleep. A provider `retry_after`
hint is honored only while it fits that budget, so an oversized hint ends the attempt
instead of holding a webhook open. Callers supply the retryable predicate; no adapter
decides on its own that an operation may be repeated. Retry logs carry attempt count
and total delay only.

Retryability follows idempotence, not error class alone. Reads retry transient
failures. A send is never retried after a timeout or network failure, because the
message may already have been delivered; that surfaces as TelegramAmbiguousSendError.
Only a definitive `ok=false` rate-limit rejection is retried for a send.

Writes are reconciled, never blindly replayed. An insert re-reads before retrying: a
matching row is success, a different row is ambiguous, a confirmed absence is retried.
An upload reuses one opaque key per attempt and HEADs the object after a transient
failure or `IfNoneMatch` collision: identical content is idempotent success, different
content is ambiguous, a missing object is retried. Unresolved write uncertainty is
reported as ambiguous with the object retained, never as a confident claim that
nothing happened. A received-but-invalid response (malformed, mismatched, or failing
an ownership/safety check) fails immediately rather than being retried or masked as
ambiguous, since it indicates a contract violation retrying cannot fix.

The success confirmation is a MarkdownV2 card: a header over bold field labels with
DD/MM/YYYY, configured currency, Decimal amounts, optional VAT, category and
confidence. Every interpolated value is escaped across MarkdownV2's full reserved set,
after the existing control-character collapse. Untrusted receipt text therefore renders
literally, a forged newline cannot fabricate a field line, and the message cannot be
rejected by Telegram for malformed escaping. Every other message stays plain text with
no parse_mode. Escaped worst-case values must still fit the 4096-character send limit.

The service builds as a two-stage container image on a slim Python 3.12 base with only
runtime dependencies, an unprivileged user, and a health check that probes no provider.
No credential appears in any image layer or build argument; configuration is read from
the environment at start-up.

Out of scope for this milestone and still open: stale ingress-claim recovery,
notification replay, scheduled expiry/orphan repair, retention policy, and any live
cloud verification.
