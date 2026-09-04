# AgentBox Core Service — Design (Sub-project 1)

Date: 2026-09-01
Status: approved (revised after design review; deviations from the product spec listed in §1.3)
Source: `agentbox_product_technical_spec.md` (product spec, sections 1–102)

## 1. Scope

Sub-project 1 of five. It delivers Stage 1 of the roadmap ("managed inbox, send, receive, reply, threads, attachments, webhooks") as a service that runs fully locally with docker-compose and is testable end to end without DNS or external providers.

Decomposition of the full product spec (build order):

1. **Core service** — this document.
2. Custom domains + DNS verification worker.
3. Governance and delivery: policies, rate limits, suppressions, provider routing rules API, HTTP provider adapters (SendGrid, Unisender Go) with provider event normalization endpoints.
4. Python SDK, TypeScript SDK, MCP server.
5. Minimal dashboard, usage accounting, billing counters.

### 1.1 In scope

- Organizations, API keys (hashed, scoped), bootstrap CLI.
- Managed inboxes on an AgentBox-managed domain (configured, not verified). Ephemeral inboxes via `ttl`.
- Send, reply, forward; list/get messages with optional long-poll (`wait`); list/get threads.
- Native SMTP receiving edge (aiosmtpd) → durable raw MIME → inbound job.
- Thread resolution per product spec §15.
- Attachments: pre-signed upload (Flow B), list, download via signed URL; stored in S3-compatible storage.
- Append-only events; signed webhooks with at-least-once delivery on the product-spec retry schedule; delivery attempts listable and retryable.
- Outbound provider abstraction with one adapter: SMTP relay (covers Unisender Go, SendGrid, Mailgun, corporate relays — all expose SMTP). Per-organization relay override via `provider_accounts` ("customer SMTP").
- Provider-independent bounce handling: VERP return-path `bounce+<id>@<domain>` received by our own SMTP edge, DSN parsed into `bounced` / `deferred`.
- Postgres-backed job queue (transactional enqueue, `FOR UPDATE SKIP LOCKED`), used for outbound send, inbound processing, webhook delivery, inbox expiry.
- Idempotency (Postgres-backed), error envelope, request ids, structured logs, message size limits.

### 1.2 Out of scope (later sub-projects)

Custom domain onboarding/DNS checks, DKIM signing, policies, rate limits, suppressions, wildcard addressing, mailbox connectors, HTTP provider adapters and provider event webhooks, SDKs, MCP, dashboard, billing/usage counters, RBAC beyond API-key scopes, HTML sanitization, attachment malware scanning (`scan_status` stored as `unknown`), Projects.

### 1.3 Deliberate deviations from the product spec

| Product spec | This design | Why |
|---|---|---|
| Redis Streams queue, publish after DB commit | Postgres `jobs` table written in the same transaction as the message/event | Removes the dual-write gap (commit ok, publish lost). Retries are a `run_at` column. One fewer stateful service for on-prem. |
| Two outbound providers + provider webhooks in v1 | One SMTP relay adapter; bounces via VERP into our own MX | SMTP already gives provider independence; DSN parsing works identically for every relay and on-prem. HTTP adapters move to sub-project 3. |
| Ephemeral inboxes in v1.1 | In v1 | One scheduled job per inbox once the job table exists. Needed by browser/registration agents. |
| Events API "optional later" | Long-poll `wait` on message listing in v1 | MCP agents cannot receive webhooks; "wait for the reply" is the magic moment. |
| Projects entity | Dropped from v1 | Empty entity that would thread through every table. Separate organizations give isolation today; migration later. |
| Full body in `message.received` webhook | `text`/`html` capped at 64 KB each with `truncated: true` | Keeps webhook payloads bounded; full body via `GET /v1/messages/{id}`. |

## 2. Stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.12, managed with `uv` |
| API | FastAPI + Pydantic v2, uvicorn |
| DB + queue | PostgreSQL 16, SQLAlchemy 2.x async (asyncpg), Alembic migrations; `jobs` table as queue |
| Object storage | S3-compatible (MinIO locally), via `aioboto3` |
| SMTP receive | `aiosmtpd` (protocol class hosted in our own event loop) |
| SMTP send | `aiosmtplib` |
| MIME | stdlib `email` (parser + `EmailMessage` builder) |
| IDs | prefixed ULIDs (`python-ulid`): `org_ key_ dom_ ibx_ thr_ msg_ att_ evt_ whk_ wdl_ dat_ pa_ ing_` |
| Secrets at rest | Fernet (`cryptography`) with `AGENTBOX_APP_SECRET_KEY` |
| Tests | pytest, pytest-asyncio, httpx `AsyncClient` (ASGI transport); integration tests against the compose stack |
| Local "internet" | Mailpit (SMTP sink on 1025, HTTP API on 8025) |

## 3. Repository layout

```
/agentbox/                 Python package (modular monolith)
  api/                     FastAPI app, routers, auth, errors, pagination, idempotency, schemas
    routers/{inboxes,messages,threads,attachments,webhooks,events}.py
  db/                      models.py, session.py (Database), seed.py
  domain/                  pure logic: ids, addresses, subject, threading, ttl
  jobs/                    queue.py (enqueue, claim, complete/retry), worker.py (loop), handlers registry
  mime/                    parse.py (raw → ParsedMessage), build.py (OutboundMessage → bytes), dsn.py
  providers/               base.py (OutboundProvider), smtp_relay.py, router.py
  inbound/                 smtp_server.py (edge), processor.py (job handler)
  outbound/                sender.py (job handler)
  webhooks/                signing.py, delivery.py (job handler + fan-out)
  lifecycle/               expire.py (job handler)
  storage/                 s3.py
  services/                organizations, inboxes, threads, messages, attachments, events
  security/                crypto.py
  config.py, logging.py, runtime.py, cli.py
/alembic/                  migrations
/tests/{unit,integration,e2e}/
/deploy/docker/            docker-compose.yml, Dockerfile, initdb/
/sdk/, /apps/mcp/, /apps/dashboard/, /docs/   reserved for later sub-projects
pyproject.toml, Makefile, README.md, .env.example
```

Processes (same image):

- `agentbox api` — HTTP API.
- `agentbox smtp` — inbound SMTP edge.
- `agentbox worker` — job loop (all job kinds; `--kinds` to restrict).

## 4. Data model

All tables except `organizations` and `jobs` carry `organization_id`; every query on tenant data filters by it. No lookup relies on an opaque id alone.

- **organizations**: id, name, slug (unique), status (`active|suspended`), plan, created_at, updated_at.
- **api_keys**: id, organization_id, name, key_prefix (first 12 chars), key_hash (sha256 hex), scopes (JSONB array), environment (`live|test`), last_used_at, revoked_at, created_at.
- **domains**: id, organization_id nullable (null = shared managed domain), domain (unique), type (`agentbox_managed|customer_custom`), status (`active|verification_pending|failed`), inbound_status, outbound_status, spf_status, dkim_status, dmarc_status, mx_status (all text, `unknown` until sub-project 2), verification_token, verified_at, created_at, updated_at.
- **inboxes**: id, organization_id, address (partial unique index where `deleted_at IS NULL`), username, domain_id, display_name, status (`active|suspended|expired|deleted`), provider_mode (`managed`), metadata JSONB, expires_at nullable, deleted_at nullable, created_at, updated_at.
- **threads**: id, organization_id, inbox_id, subject, subject_normalized, participants JSONB (sorted lowercase addresses, excluding the inbox address), last_message_at, message_count, metadata JSONB, created_at, updated_at.
- **messages**: id, organization_id, inbox_id, thread_id, direction (`inbound|outbound`), status, from_address JSONB `{email,name}`, to_addresses/cc_addresses/bcc_addresses/reply_to_addresses JSONB arrays of `{email,name}`, subject, text_body, html_body, internet_message_id, in_reply_to, references JSONB, provider, provider_message_id, headers JSONB (list of `[name, value]`), raw_storage_key nullable, size_bytes, error_code, error_message, metadata JSONB, sent_at, received_at, created_at, updated_at. Unique `(organization_id, inbox_id, internet_message_id)`.
- **attachments**: id, organization_id, message_id nullable (null while pending upload), filename, content_type, size_bytes, storage_key, sha256 nullable, disposition (`attachment|inline`), content_id, scan_status (`unknown`), status (`pending|ready`), expires_at nullable, created_at.
- **events**: id, organization_id, resource_type, resource_id, type, payload JSONB, created_at. Append-only.
- **webhooks**: id, organization_id, inbox_id nullable, url, secret_encrypted, description, status (`active|disabled`), event_types JSONB (`["*"]` = all), deleted_at, created_at, updated_at.
- **webhook_deliveries**: id, organization_id, webhook_id, event_id, attempt_number, status (`pending|succeeded|failed|exhausted`), response_status, response_excerpt, error, scheduled_at, started_at, finished_at. Unique `(webhook_id, event_id, attempt_number)`.
- **delivery_attempts**: id, organization_id, message_id, provider, provider_account_id, provider_message_id, attempt_number, status (`started|accepted|temporary_failure|permanent_failure`), error_code, error_message, started_at, finished_at.
- **provider_accounts**: id, organization_id nullable (null = shared default), provider (`smtp_relay`), name, config_encrypted (Fernet JSON: host, port, username, password, starttls), status (`active|disabled`), created_at, updated_at.
- **inbound_ingests**: id, organization_id nullable (null for bounces to unknown orgs is rejected, so always set), kind (`message|bounce`), inbox_id nullable, bounce_message_id nullable, storage_key, mail_from, rcpt_to, size_bytes, status (`received|stored|duplicate|failed`), message_id nullable, error, created_at, processed_at.
- **idempotency_keys**: organization_id, key, endpoint, request_hash, response_status nullable, response_body JSONB nullable, created_at, expires_at. PK `(organization_id, key, endpoint)`.
- **jobs**: id bigserial, kind (`outbound_send|inbound_process|webhook_deliver|inbox_expire`), payload JSONB, run_at, status (`pending|running|done|dead`), attempts, max_attempts, locked_at, locked_by, last_error, created_at, updated_at. Index `(status, run_at)`.

Indexes: product spec §68, plus `webhook_deliveries(webhook_id, created_at desc)`, `attachments(status, expires_at)`, `inboxes(expires_at) where status='active'`.

Message statuses follow product spec §8.6 (outbound `queued provider_accepted delivered deferred bounced rejected complained failed`; inbound `received parsed stored quarantined rejected`). With the SMTP relay adapter a message reaches `provider_accepted` on 2xx after DATA; `bounced`/`deferred` come from DSNs; `delivered` is only set by providers that report it (sub-project 3).

## 5. API

Base path `/v1`. Auth: `Authorization: Bearer ab_live_<secret>` (or `ab_test_`). Hash the secret, load key + organization, check scope. Every response carries `AgentBox-Request-Id`. Errors use the product-spec §70 envelope with codes `unauthorized forbidden not_found validation_error conflict inbox_disabled message_too_large attachment_blocked idempotency_conflict internal_error`.

Scopes: `inboxes:read inboxes:write messages:read messages:send attachments:read attachments:write webhooks:read webhooks:write events:read admin` (`admin` implies all).

List endpoints return `{"data": [...], "next_cursor": str|null}`; `limit` ≤ 100, default 20; cursor = last item's id, ordering id desc (ULIDs are time-ordered).

- `POST /v1/inboxes` [inboxes:write] — `{username?, domain?, display_name?, metadata?, ttl?}`. `domain` must be active and visible to the org (shared managed domain or the org's own). Missing `username` → generated `<word>-<4 hex>`. Reserved local parts rejected. `ttl` like `30m`, `24h`, `7d` (max 30d) → `expires_at` + `inbox_expire` job. Idempotent.
- `GET /v1/inboxes` [inboxes:read] — filters `status`, `domain`, `metadata.<k>=<v>`.
- `GET /v1/inboxes/{id}`, `POST /v1/inboxes/{id}/disable`, `POST /v1/inboxes/{id}/enable` (not allowed for `expired`), `DELETE /v1/inboxes/{id}` (soft delete, address reusable).
- `POST /v1/inboxes/{id}/messages` [messages:send] — `to` (required; `{email,name?}` objects or bare strings), `cc`, `bcc`, `reply_to`, `subject`, `text`, `html` (≥ one of text/html), `attachment_ids`, `headers` (only names starting `X-`), `metadata`. Returns 202 `{id, thread_id, status:"queued", created_at}`. Idempotent.
- `GET /v1/inboxes/{id}/messages` [messages:read] — filters `direction, status, thread_id, from, to, since, until`; `wait=<1..60 seconds>` long-polls: if the filtered result is empty, re-query every second until non-empty or timeout, then return (possibly empty) list.
- `GET /v1/messages/{id}` — full message with `attachments[]` summaries; `?include=headers` adds `headers`.
- `POST /v1/messages/{id}/reply` [messages:send] — `{text?, html?, reply_all?=false, to?, cc?, bcc?, attachment_ids?}`. Recipients: explicit `to` if given, else original `Reply-To` if present else original `From`; `reply_all` adds original To+Cc minus the inbox address and duplicates. Subject `Re: <stripped subject>`. `In-Reply-To` = original Message-ID; `References` = original References + original Message-ID. Same thread. Idempotent.
- `POST /v1/messages/{id}/forward` [messages:send] — `{to, cc?, bcc?, text?, html?, include_attachments?=true}`. Subject `Fwd: <stripped subject>`. Body = given text/html followed by a quoted block (`---------- Forwarded message ----------`, From/Date/Subject/To, original body). Original attachments re-attached by creating new attachment rows pointing to the same storage keys. New thread. Idempotent.
- `GET /v1/inboxes/{id}/threads` [messages:read]; `GET /v1/threads/{id}` — includes `messages[]` ordered by created_at asc.
- `POST /v1/attachments/uploads` [attachments:write] — `{filename, content_type, size_bytes}` (≤ 20 MB) → `{attachment_id, upload_url, expires_at}` (presigned PUT, 15 min). Pending rows expire 24 h after creation. On send, each attachment must belong to the org, be `pending` or `ready`, not yet attached; the API HEADs the object (must exist, size must match) and marks it `ready`.
- `GET /v1/messages/{id}/attachments`, `GET /v1/attachments/{id}`, `GET /v1/attachments/{id}/download` [attachments:read] → `{url, expires_at}` (presigned GET, 10 min); `?redirect=true` → 302.
- `POST /v1/webhooks` [webhooks:write] `{url (https or http), event_types?=["*"], inbox_id?, description?}` → returns `secret` once (`whsec_...`). `GET /v1/webhooks`, `GET /v1/webhooks/{id}`, `PATCH /v1/webhooks/{id}` (url, event_types, status, description), `DELETE`. `GET /v1/webhooks/{id}/deliveries`, `POST /v1/webhooks/{id}/deliveries/{delivery_id}/retry` (enqueues a new attempt now).
- `GET /v1/events` [events:read] — filters `type, resource_id, since, until`.
- `GET /healthz` (process up), `GET /readyz` (DB reachable).

Idempotency: `Idempotency-Key` header on all mutating endpoints above. Row key = `(organization_id, key, route path template)`. Flow: insert a placeholder row in its own transaction; on unique violation load the row: if it has a response and the same request hash → replay with `Idempotent-Replayed: true`; different hash → 409 `idempotency_conflict`; no response yet → poll every 100 ms up to 5 s, then 409 `conflict` ("request in flight"). After the handler succeeds, store status + body on the row. Expired rows (24 h) are ignored and overwritten.

## 6. Job queue

`jobs` is the only queue. `enqueue(session, kind, payload, run_at=None, max_attempts=...)` adds a row in the caller's transaction. Worker loop (per process, N concurrent claimers, default 4):

```sql
UPDATE jobs SET status='running', locked_at=now(), locked_by=:worker, attempts=attempts+1
WHERE id = (SELECT id FROM jobs WHERE status='pending' AND run_at <= now()
            ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

The handler runs inside one transaction with the job's completion (`status='done'`), so DB effects and job completion are atomic. Handlers may raise `RetryLater(delay_seconds, error)` to reschedule explicitly (`status='pending'`, `run_at=now()+delay`) or any other exception, which reschedules with the kind's default backoff until `attempts >= max_attempts`, then `status='dead'` with `last_error`. A sweeper resets `running` jobs whose `locked_at` is older than 10 minutes to `pending`. Poll interval 500 ms when idle. Done jobs older than 7 days are deleted by the sweeper.

Default backoff per kind: `outbound_send` `[30, 120, 600, 3600, 14400]` (max 5 attempts); `inbound_process` `[10, 60, 300, 1800, 7200]`; `webhook_deliver` uses the explicit schedule below; `inbox_expire` `[60, 600]`.

## 7. Flows

### 7.1 Send / reply / forward (API side)

1. Auth + scope; load inbox (must be `active`, else 409 `inbox_disabled`).
2. Validate recipients (strict addr-spec regex), at least one of text/html, custom headers `X-*` only, attachments (see §5), total size (bodies + attachment sizes) ≤ 25 MB else 413 `message_too_large`.
3. Thread: reply → original's thread; send/forward → new thread.
4. `internet_message_id = <msg_id@inbox_domain>`.
5. One transaction: insert message `queued`, update thread counters, bind attachment rows, event `message.queued`, jobs `outbound_send {message_id}` and `webhook_deliver {event_id}`.
6. Return 202.

### 7.2 Outbound send job

1. Load message; skip (done) if status not in `queued`.
2. Build `OutboundMessage` (addresses, bodies, headers, attachments streamed from S3) and MIME bytes.
3. Router: active `provider_accounts` row for the org, else shared default (seeded from settings). Build `SMTPRelayProvider` from decrypted config.
4. Envelope: `mail_from = bounce+<msg ulid>@<inbox domain>`, `rcpt_to` = to+cc+bcc emails.
5. Insert `delivery_attempts` row `started` in its own committed transaction, then `provider.send(envelope, raw)`.
6. `SendResult(accepted=True)` → attempt `accepted`, message `provider_accepted`, `sent_at`, event `message.provider_accepted` + webhook job. `TemporaryError` → attempt `temporary_failure`, raise `RetryLater(backoff)`; after the last attempt → message `failed`, event `message.failed`. `PermanentError` → attempt `permanent_failure`, message `rejected`, event `message.rejected`.

Provider interface:

```python
class OutboundProvider(Protocol):
    name: str
    async def send(self, envelope: Envelope, raw: bytes) -> SendResult: ...
    async def health(self) -> bool: ...
```

`Envelope(mail_from, rcpt_to: list[str], message_id)`; `SendResult(accepted: bool, provider_message_id: str | None, response: str)`. SMTP relay: 2xx after DATA → accepted; 4xx or connection errors → `TemporaryError`; 5xx → `PermanentError`; per-recipient refusal for all recipients → `PermanentError`, partial → accepted with the refused recipients listed in `response` and recorded on the attempt.

### 7.3 Inbound SMTP edge

- Listens on `smtp_bind_host:smtp_bind_port`; announces SIZE = 30 MB; STARTTLS offered when a cert is configured.
- `RCPT TO` (lowercased): (a) `bounce+<ulid>@<known domain>` → message `msg_<ulid>` must exist and be outbound → accept as bounce; (b) address matches an active inbox → accept; expired/suspended inbox → `550 5.2.1 mailbox disabled`; otherwise `550 5.1.1 no such user`. Recipients are validated against the DB per RCPT.
- `DATA`: for each accepted recipient: write raw bytes to S3 `org/{org}/raw/{ing_id}.eml`, insert `inbound_ingests` + job `inbound_process {ingest_id}` in one transaction. Reply `250 2.0.0 queued as <ing_id>`. Any failure → `451 4.3.0 try again later`.

### 7.4 Inbound process job

Kind `message`:
1. Load ingest (skip if not `received`); fetch raw from S3; `parse_mime`.
2. Duplicate check on `(org, inbox, internet_message_id)` → mark ingest `duplicate`, done.
3. Store attachments to `org/{org}/messages/{msg}/attachments/{att}` with sha256; parts over 20 MB dropped with event `attachment.blocked`.
4. Thread resolution (§8) → existing or new thread.
5. Insert message `stored` (direction inbound, all headers, `raw_storage_key`), update thread, event `message.received` (payload §7.5), ingest `stored`, webhook job. One transaction.

Kind `bounce`:
1. Parse DSN (`multipart/report; report-type=delivery-status`): per-recipient `Action` and `Status`. `failed`/`5.x.x` → message `bounced`; `delayed`/`4.x.x` → `deferred`; no parsable status → `bounced` with reason `unparsed_bounce`. Only forward transitions (`provider_accepted → deferred → bounced`; never back).
2. Event `message.bounced` / `message.deferred` with `{recipient, status_code, diagnostic}`; ingest `stored`; webhook job. Bounce mail is not surfaced as an inbox message.

### 7.5 Events and webhooks

- `emit(session, org_id, resource_type, resource_id, type, payload) -> Event` inserts the event and a `webhook_deliver {event_id}` job in the caller's transaction.
- Webhook job: load event; select active, non-deleted webhooks of the org matching `event_types` (`*` or exact) and `inbox_id` (null or equal to the event's inbox); for each, create `webhook_deliveries` attempt 1 and POST. Payload `{id, type, created_at, data}`; headers `Content-Type: application/json`, `AgentBox-Event-Id`, `AgentBox-Signature: t=<unix>,v1=<hex hmac-sha256(secret, f"{t}.{raw_body}")>`, `User-Agent: AgentBox-Webhooks/1.0`. Timeout 10 s; 2xx = `succeeded`.
- Failure → attempt `failed`; next attempt row `pending` with `scheduled_at` on the schedule `[10, 60, 300, 1800, 7200, 28800, 86400]` seconds and a `webhook_deliver {delivery_id}` job at that time. After the last → `exhausted` and an event `webhook.failed` (which itself is never fanned out). The `webhook_deliver` job payload is therefore either `{event_id}` (fan out) or `{delivery_id}` (single attempt).
- Manual retry creates a new attempt row and a job now.
- Message bodies inside `message.received` payload: `text` and `html` truncated to 64 KB each with `truncated: true` when cut.

### 7.6 Inbox expiry

`inbox_expire {inbox_id}` at `expires_at`: if still `active` and `expires_at <= now` → status `expired`, event `inbox.expired`. The SMTP edge and send endpoint reject expired inboxes.

## 8. Thread resolution details

Subject normalization: repeatedly strip leading prefixes matching `(re|fw|fwd|aw|wg|sv|vs|ответ|пересылка)\s*(\[\d+\])?\s*:` case-insensitively, collapse whitespace, lowercase. Participants = all From/To/Cc addresses lowercased, excluding the inbox's own address. Resolution order: In-Reply-To, References (newest first) against `messages.internet_message_id` within the same inbox; then normalized subject equality + overlapping participant within 30 days; else new thread. Original headers always retained on the message.

## 9. Configuration

`pydantic-settings`, prefix `AGENTBOX_`: `DATABASE_URL, S3_ENDPOINT, S3_PUBLIC_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, MANAGED_DOMAIN, API_BASE_URL, SMTP_BIND_HOST, SMTP_BIND_PORT, SMTP_HOSTNAME, SMTP_TLS_CERT, SMTP_TLS_KEY, MAX_INBOUND_BYTES, MAX_OUTBOUND_BYTES, MAX_ATTACHMENT_BYTES, OUTBOUND_SMTP_HOST, OUTBOUND_SMTP_PORT, OUTBOUND_SMTP_USERNAME, OUTBOUND_SMTP_PASSWORD, OUTBOUND_SMTP_STARTTLS, APP_SECRET_KEY, IDEMPOTENCY_TTL_SECONDS, WORKER_CONCURRENCY, LOG_LEVEL`.

## 10. Local environment

`deploy/docker/docker-compose.yml`: postgres:16 (creates `agentbox` and `agentbox_test`), minio (+ bucket bootstrap), mailpit; profile `app` adds api, worker, smtp built from `deploy/docker/Dockerfile`. `.env.example` points outbound SMTP at Mailpit. `make up`, `make migrate`, `make test`, `make test-integration`, `make e2e`.

## 11. Error handling summary

- API errors → §70 envelope; unexpected exceptions → 500 `internal_error` with request id, logged with stack trace.
- Jobs: handler exception → retry with backoff, then `dead` with `last_error`; never silently dropped. Stale `running` jobs are reclaimed.
- SMTP edge: 4xx for internal failures (sender retries), 5xx only for policy decisions.
- Outbound: temporary relay errors back off; a crash between `started` attempt and completion leads to a re-send on retry (at-least-once), recorded as a new attempt.

## 12. Testing strategy

- **Unit** (no services): subject normalizer, thread resolver, ttl parser, address rules, MIME parse fixtures (plain, alternative, mixed+PDF, inline image, Cyrillic headers, KOI8-R body), MIME build round-trip, DSN parser, webhook signing, idempotency hashing, crypto, job backoff.
- **Integration** (compose infra, marker `integration`): migrations apply; auth and scopes; tenancy isolation; inbox CRUD, uniqueness, ttl + expiry job; send → rows and jobs; idempotency replay/conflict/in-flight; attachments upload/download against MinIO; webhook fan-out, signature, retry scheduling, exhaustion; job queue claim/retry/dead/sweeper; SMTP edge accept/reject; inbound processing from raw `.eml`; bounce DSN processing; outbound send job against Mailpit.
- **E2E** (`tests/e2e/test_acceptance.py`): in-process API + worker + SMTP edge against compose infra: create inbox → send → Mailpit shows it with correct headers → simulated vendor reply with PDF into the SMTP edge with `In-Reply-To` → webhook listener receives signed `message.received` → thread shows both → attachment download matches sha256 → reply → Mailpit receives it with `Re:` and correct `In-Reply-To`/`References` → long-poll returns within the wait window → DSN bounce moves an outbound message to `bounced`.

## 13. Invariants (product spec §98)

1, 2, 3, 4, 5, 6, 7, 8, 10 are enforced: partial unique index on active addresses; message FKs to one inbox/thread; unique `(org, inbox, internet_message_id)`; retries reuse event ids; idempotency table; org scoping everywhere; provider config never serialized; disabled/expired inbox rejected on send and on RCPT; SMTP 250 only after S3 write + DB insert + job insert commit.
