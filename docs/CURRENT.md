# Current status

- Current milestone: V0.1-009 — Bounded provider retries, write reconciliation,
  the formatted execution-summary card and container packaging.
- Status: complete. V0.1-010 has not started.
- Completed: one shared bounded retry policy across all four provider boundaries,
  per-operation retryability decided by idempotence, reconciled writes for Supabase
  inserts and S3 uploads, an escaped MarkdownV2 summary card, and a runtime Dockerfile.
- Active work: none. No milestone blocker remains.
- Next action: V0.1-010 only when authorized; exact scope below.

## Files and dependencies

Created:
- src/second_brain_receipts/core/retry.py: the single bounded retry primitive.
- tests/unit/test_receipt_messages.py: direct coverage of MarkdownV2 escaping,
  card formatting, injection resistance and the message-length ceiling.
- Dockerfile and .dockerignore: two-stage runtime image, unprivileged user,
  health check, no credentials in any layer.

Changed: the OpenAI, Telegram, Supabase and S3 adapters now call `retry`;
`_insert_reconciled` and `S3ReceiptImageStorage.store` reconcile uncertain writes;
`TelegramClient.send_message` accepts an optional `parse_mode`; `receipt_messages.py`
gained `escape_markdown_v2` and the summary card; the two success call sites in
`receipt_processor.py` and `pending_workflow.py` opt into MarkdownV2. No new runtime
dependency, configuration variable, table or migration was added.

## Bounded retries and reconciliation

`RetryPolicy` defaults to 3 attempts, 0.25s initial delay, 1.0s maximum single wait
and 1.5s total sleep. A provider `retry_after` hint is honored only while it fits
that budget, so a "retry after 12 seconds" gives up immediately instead of holding a
webhook open. Retry logs record attempt count and total delay only.

Retryability is per operation, decided by idempotence rather than error class alone.
Reads (Vision extraction, getFile, download, Supabase selects) retry transient
failures. A Telegram send is never retried after a timeout or network failure,
because the message may already have been delivered; that surfaces as
TelegramAmbiguousSendError. Only a definitive `ok=false` rate-limit rejection, which
proves nothing was sent, is retried for a send.

Writes are never blindly replayed. `_insert_reconciled` re-reads before retrying: a
matching row is success, a different row is ambiguous, a confirmed absence is
retried. `store` reuses one opaque key per attempt and, on a transient failure or
`IfNoneMatch` collision, HEADs the object: identical content is idempotent success,
different content is ambiguous, a missing object is retried. Unresolved write
uncertainty is reported as AmbiguousPersistenceError or StorageAmbiguousError and the
object is retained for repair, never downgraded to a confident "nothing happened".

A response that was received but is malformed, mismatched, or fails an
ownership/safety check remains InvalidPersistenceResponseError and fails on the first
attempt. It is a deterministic contract violation, so retrying cannot help, and
treating it as ambiguous would mask a backend fault such as an RPC returning another
user's pending row.

## Execution summary card

`success_summary` and `remembered_summary` are the only messages sent with
`parse_mode=MarkdownV2`: a `✅ Receipt Processed` header over bold field labels with
DD/MM/YYYY, configured currency, Decimal amounts, optional VAT, category and
confidence. Every interpolated value passes through `escape_markdown_v2`, covering
the full reserved set ``_*[]()~`>#+-=|{}.!\``.

Escaping is required twice over: receipt text is untrusted model output, so an
injected `_[Vendor]*` must render literally rather than as formatting; and Telegram
rejects an incorrectly escaped MarkdownV2 message outright, so a missed escape is a
delivery failure, not a cosmetic one. Escaping runs after the existing
control-character collapse, so a forged newline cannot fabricate an extra field line.
Every other message stays literal plain text with no parse_mode.

## Container packaging

A two-stage `python:3.12-slim` build installs only the exactly pinned runtime
dependencies from pyproject.toml, drops to an unprivileged uid, and declares a
HEALTHCHECK against `/health`, which probes no provider. `.dockerignore` keeps
`.env`, tests, migrations and local state out of the build context. Configuration is
read from the environment at start-up; no credential enters an image layer or build
argument.

## Verification

Python 3.12.13, pytest 9.1.1, Ruff 0.16.6, mypy 1.20.2:

```text
.venv/bin/python -m pytest
528 passed in 35.76s

.venv/bin/python -m ruff check .
All checks passed!

.venv/bin/python -m ruff format --check .
61 files already formatted

.venv/bin/python -m mypy
Success: no issues found in 39 source files

.venv/bin/python -m pip check
No broken requirements found.
```

The suite was brought back to green from 39 pre-existing failures, which had accrued
while the retry work landed ahead of its tests. Each failure was resolved by deciding
whether the assertion or the implementation was right, not by relaxing checks:

- Retry-count assertions written against the earlier no-retry behavior now assert the
  bounded attempt counts, and separate transient from non-transient cases.
- Telegram send tests were split from idempotent-read tests, since a send that times
  out is deliberately ambiguous rather than retried.
- S3 mapping tests gained a realistic `head_object` stub, plus new cases for the
  collision-reconciliation outcomes (identical content succeeds, different content is
  ambiguous).
- Write-path error mapping now expects AmbiguousPersistenceError where sustained
  unavailability leaves the outcome genuinely unknown.
- One real defect was fixed rather than accommodated: pending completion treated
  InvalidPersistenceResponseError like unavailability, so a malformed or wrong-owner
  RPC response was retried and then masked as ambiguous. Received-but-invalid
  responses now propagate immediately.

Docker verification used a real build and run: the image built, the container came up
as the unprivileged user, `GET /health` returned 200, an unauthenticated webhook was
rejected with 401, the declared HEALTHCHECK reported healthy, and no `.env` was
present in the image. The container and image were then removed.

PostgreSQL 17 (disposable container, `--network none`, no published ports, tmpfs
data): all four migrations applied from scratch with ON_ERROR_STOP=1, unchanged by
this milestone, and every existing SQL check still passes.

```text
PASS: database constraints, indexes, lifecycle checks and role privileges
PASS: atomic pending completion, exact money, memory precedence, rollback, ownership, expiry, cancellation, duplicate cleanup and RPC privileges
PASS: concurrent receipts inserts enforce receipts_vendor_date_total_key
PASS: concurrent pending_receipts inserts enforce pending_receipts_one_question_idx
PASS: concurrent telegram_updates inserts enforce telegram_updates_pkey (one durable owner, competing claim ignored)
PASS: pending RPC race operation=complete same_vendor=False; row/vendor locks preserve one completion and category memory
PASS: pending RPC race operation=cancel same_vendor=False; row/vendor locks preserve one completion and category memory
PASS: pending RPC race operation=complete same_vendor=True; row/vendor locks preserve one completion and category memory
PASS: completion rechecks wall-clock expiry after waiting on a row lock
```

Test rows were rolled back or deleted and the disposable container removed.

Default tests retained the socket-blocking guard and made zero external network
calls. Fixtures were synthetic or official SDK MockTransports. No real Telegram,
OpenAI, Supabase or AWS resource was contacted or modified; no webhook was
registered, and no live extraction or Telegram message occurred.

## Remaining assumptions and limitations

- One private user, one bot/database, one configured currency, global vendor/receipt
  identities and one active category question remain the POC assumptions.
- Retries bound individual provider operations, not aggregate webhook latency, and
  they add real wall-clock delay to a failing request within the 1.5s sleep budget.
- Reconciliation narrows write uncertainty but cannot eliminate it: a negative
  existence check cannot exclude a transaction committing moments later, so ambiguous
  outcomes still require human or future automated repair.
- Cancellation/expiry/duplicate cleanup failure may still leave an orphaned object.
  There is no scheduled expiry, orphan sweeper, durable cleanup ledger or
  notification-replay worker.
- Claims remain at-most-once attempts; received/failed updates do not auto-replay.
- MarkdownV2 escaping is verified against Telegram's documented reserved set in unit
  tests, not against the live Bot API. A live send remains a deployment check.
- Stand-in PostgreSQL roles and mocked HTTP do not prove live Supabase/PostgREST, S3
  privacy, Telegram delivery or extraction accuracy/latency. The container smoke test
  used fake credentials and therefore exercised start-up and routing only.

## Exact next milestone: V0.1-010 (not started)

Demo readiness: an integration suite, redacted/synthetic demo fixtures, an
operations/setup walkthrough, and an authorized live smoke test covering success,
duplicate, pending/resume and retry paths against real Telegram, OpenAI, Supabase and
S3. Measure real end-to-end latency from image upload to user response. No new
business behavior is implied.
