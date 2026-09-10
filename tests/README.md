# Tests

`python -m pytest` runs credential-free unit tests for settings, extraction/domain
validation, vendor normalization and image safety/compression. Image fixtures are
generated in memory; no internet, real receipts or provider credentials are needed.
The suite also covers the OpenAI adapter with synthetic Responses payloads and
mocked official SDK transports; tests/conftest.py blocks socket connections. Only synthetic values are used.
`python -m mypy` checks all application source with strict typing.

SQL checks are separate from pytest and require Docker/PostgreSQL. This disposable
container exposes no ports and has no network. Never point this fixture at a real
database; its stand-in Supabase roles are for local verification only.

```sh
docker run --detach --rm --name second-brain-receipts-v006-db --network none \
  --tmpfs /var/lib/postgresql/data -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
docker exec second-brain-receipts-v006-db pg_isready -U postgres
# Run the following once pg_isready reports accepting connections.
docker exec second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;'
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < supabase/migrations/202609100001_initial_receipt_schema.sql
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < supabase/migrations/202609100002_s3_storage_references.sql
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < supabase/migrations/202609100003_telegram_updates.sql
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < supabase/migrations/202609100004_pending_completion.sql
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < tests/database/constraints.sql
docker exec -i second-brain-receipts-v006-db psql -U postgres -v ON_ERROR_STOP=1 \
  < tests/database/pending_workflow.sql
python tests/database/check_races.py
python tests/database/check_pending_races.py
docker stop second-brain-receipts-v006-db
```

The SQL checks roll back test data and exercise database uniqueness, monetary and
state constraints, pending-slot reuse, RLS flags and client/server grants.
The standalone race check verifies competing inserts actually wait on a lock,
then one succeeds and the other fails with the correct unique constraint.
Actual Supabase access and atomic completion RPC tests remain deferred.

Image tests cover JPEG/PNG/WEBP and common modes, white alpha compositing, byte limits
before decode, dimension/bomb protection, corruption after valid headers, animation
rejection, resize invariants, EXIF/GPS/comment/profile removal, safe logging and no
processing disk I/O. `synthetic_receipt()` in `unit/test_image_service.py` generates
a deterministic camera-sized fixture for compression and visual review. Visual
review used that fixture encoded at JPEG quality 98 as the source, then processed
with defaults. It is a coarse synthetic readability check, not an OCR benchmark.

Vision tests verify strict schema requests, exact processed JPEG transport, Decimal
numbers, all confidence values, optional VAT/category, malformed/unreadable/refusal
responses, incomplete status, SDK failures, safe logs/errors, timeouts and one
attempt per request. Async tests use asyncio.run without another pytest plugin.
The OpenAI SDK transitively installs httpx2, whose MockTransport is used in tests.
No live API smoke test is present or run. In this development environment, sandboxed
asyncio thread wake-up/shutdown stalled; the complete offline suite was verified
outside that sandbox with the connection guard still enabled.

Repository tests use official async Supabase clients with httpx.MockTransport and
synthetic rows. They assert read/write money casts, exact decimal strings, named
constraint mapping, empty/malformed response handling and disabled query retries.
S3 tests use injected clients and botocore.Stubber; exact upload arguments include
AES256 and exclude ACL/custom metadata. Presigning uses fake credentials offline.
Factory lifetime and off-event-loop execution are tested. No cloud resources are
created and no live Supabase/AWS integration tests run in the ordinary suite.

Telegram tests use HTTPX ASGITransport/MockTransport and injected ingress boundaries.
They cover secret/numeric authorization, bounded/chunked HTTP requests, normalized
photo/text/unsupported events, claim duplicates, failures and cancellation, streamed
download limits, literal sends, rate-limit hints, sanitized errors/logs and lifespan
cleanup. No Telegram registration, sending or cloud calls occur. The database race
check additionally verifies concurrent ON CONFLICT DO NOTHING claims: one committed
owner, the competing transaction waits and returns no row. RLS and service-role
access are checked for all four tables. Default pytest still blocks socket connects.

V0.1-007 orchestration tests use synthetic PNGs, the real image service, validated
extraction models and injected async fakes. They verify exact processed-image reuse,
category override, confidence gates, duplicate races, one-shot compensation, failed
cleanup, ambiguous insert reconciliation, notification failure after commit, timing
and no secret/content logs. ASGI integration covers authenticated photo download to
receipt insertion/summary, failed/handled claims and duplicate deliveries. A full
factory test verifies default wiring; another uses the real Telegram HTTP adapter
with MockTransport. No live Vision, S3, Telegram or Supabase calls are required.
The same three SQL migrations/constraint/race checks remain applicable; V0.1-007
introduces no schema changes. The disposable container retains its v006 name so the
existing verification commands remain unchanged.

V0.1-008 adds pending creation/compensation, active-question admission, category
validation, cancellation, expiry, RPC adapter validation, notification failures and
a contextual-memory cycle across separate app instances. SQL checks apply migration
004 and test actual completion atomicity, rollback on duplicate and forced insert
failure, category memory precedence, owned row access, shared-image cleanup guards,
exact money serialization and RPC privileges. Concurrent RPC tests cover two replies,
completion versus cancel, two workflows for one vendor, and expiry after a lock wait.
The application-memory cycle uses fakes; transactional/concurrent guarantees are
separately verified by these real PostgreSQL checks. No live services are contacted.

V0.1-009 adds `tests/unit/test_receipt_messages.py`, covering MarkdownV2 escaping
against Telegram's documented reserved set, the summary card's bold-label layout,
injection resistance (formatting characters and forged field lines in untrusted
vendor/category text), and the worst-case escaped message length against the
4096-character send limit. Retry behavior is verified in place at each provider
boundary: bounded attempt counts, non-transient failures that must not retry, sends
that must never retry after an ambiguous timeout, and the reconciliation outcomes for
uncertain inserts and uploads. These are all offline; the retry policy's real sleeps
are short but do add a little wall-clock time to the suite. No schema change was
introduced, so the SQL commands above are unchanged.
