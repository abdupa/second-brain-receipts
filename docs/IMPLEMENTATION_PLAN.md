# Implementation plan

V0.1-001 through V0.1-009 are complete. V0.1-010 has not started.
Each subsequent milestone remains a separate scope of work.

For every milestone: keep scope narrow, add tests for new behavior, run relevant
tests plus lint/type checks, update CURRENT.md with actual results and remaining
limitations, and revise requirements/architecture if decisions change. Documentation
only work uses structural validation rather than artificial behavior tests.

| Milestone | Deliverables | Verification gate |
| --- | --- | --- |
| V0.1-001 Foundation and documentation | Contributor map, requirements, architecture, plan/status, empty typed Python package, tooling config, ignore rules, blank environment template, minimal README. No application behavior. | Parse project config, check documentation links/files, compile empty package, check ignore rules and whitespace. Report unavailable tooling. |
| V0.1-002 Configuration, schemas, data model | Typed settings and environment validation; Pydantic extraction/domain/state models; deterministic normalization; decide currency/precision/category policy/limits; SQL migrations for tables, checks, indexes, private access and atomic completion contract. Add only necessary dependencies. | Settings missing/invalid values and secret masking; dates/Decimal/confidence/schema checks; normalization cases; apply migrations to disposable PostgreSQL and verify constraints, active pending uniqueness and duplicate uniqueness. |
| V0.1-003 Image validation and compression | Bounded Pillow pipeline, orientation, aspect-ratio resize, compression and metadata stripping; supported formats and limits. | Valid images, corruption, false MIME, oversized bytes/pixels, animation, orientation, ratios and output bounds; manual readability review of representative synthetic/redacted receipts. |
| V0.1-004 Structured extraction | OpenAI adapter with configurable vision model, structured response validation and confidence signal; unreadable/refusal errors. SDK retries disabled, bounded request timeout. | Mock valid/malformed/refusal/unreadable responses, timeout, rate limit and server errors; no raw response reaches repositories. |
| V0.1-005 Repositories and image storage | Async Supabase repositories and encrypted private AWS S3 upload/presign/delete; exact Decimal transport, stable references, typed conflicts and cleanup capability. Atomic completion and cleanup coordination deferred. | Repository contract tests and disposable database integration tests including uniqueness races; mocked S3 encryption/access arguments and failures; real cloud privacy verification deferred to deployment, not required for this milestone. |
| V0.1-006 Telegram ingress | FastAPI route, webhook secret and numeric private-user authorization, event dedupe/claims, bounded download, Telegram send adapter. | Auth failures, malformed/unsupported/oversized events, repeated and concurrent updates, download and send errors; no sensitive logs. |
| V0.1-007 Receipt orchestration | Connect ingestion/extraction, confidence checks, known vendor override, duplicate check and atomic final insert, plain-text success summary and compensating cleanup. Unknown vendors stay behind an explicit supported-scope response until 008. | Valid flow, recognized vendor, duplicate and race, rejected confidence, transaction/storage failures and Markdown escaping. |
| V0.1-008 Pending state and resumption | Persist unknown extraction and category question; implement atomic completion RPC/transaction; resume later reply, save memory, handle one active workflow, cancellation and TTL. | Separate webhook calls/process restart, invalid reply, expiry, repeat/concurrent replies, memory precedence race, duplicate at completion. |
| V0.1-009 Resilience, presentation and packaging | One bounded retry policy across all provider boundaries with per-operation retryability; reconciled Supabase/S3 writes; escaped MarkdownV2 execution-summary card; runtime container image. | Fault injection for all providers, bounded attempt counts, ambiguous-write reconciliation, escaping/injection and message-limit checks, container build and health smoke test. |
| V0.1-010 Demo readiness | Integration suite, redacted/synthetic demo fixtures, operations walkthrough and an authorized live smoke test. | Full automated suite, lint/types, authorized private-service smoke test and manual Telegram demo of success, duplicate, pending/resume and retry, with measured end-to-end latency. |

## Next milestone boundary

V0.1-010 only, when authorized: assemble an integration suite and redacted demo
fixtures, write the operations/setup walkthrough, and run an authorized live smoke
test against real Telegram, OpenAI, Supabase and S3 covering success, duplicate,
pending/resume and retry paths. Measure real end-to-end latency from image upload to
user response, and confirm deployed S3/Supabase access policies. No new business
behavior, frontend or generic workflow engine is implied.

V0.1-009 added one bounded retry policy across every provider boundary, decided
retryability per operation by idempotence, reconciled uncertain Supabase and S3
writes instead of replaying them, sent the execution summary as an escaped MarkdownV2
card, and packaged the service as a container image. It deliberately adds no
scheduled worker, orphan sweeper, notification-replay ledger or live cloud resource;
stale-claim recovery and retention policy remain open. One active question per
chat/user remains the POC policy.
