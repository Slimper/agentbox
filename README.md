# AgentBox — email infrastructure for AI agents

Give a software agent a real email address with one API call, then send, receive, reply and forward with threads,
attachments and signed webhooks. Governed by policies, approval gates and suppressions; delivered through pluggable
providers; operated from a console; driven from Python, TypeScript or MCP. Apache-2.0, self-hostable, no caps.

| Area | Highlights |
|---|---|
| Core | inboxes (incl. ephemeral `ttl`), send / receive / reply / forward, thread resolution, attachments, signed webhooks with retries, idempotency, long-poll `wait=`, Postgres-backed job queue, own SMTP edge, VERP bounce handling |
| Domains | custom domain onboarding, DNS records to publish, background verification (ownership, MX, SPF, DKIM, DMARC) |
| Governance | org + inbox policies (allow/block lists, rate limits, loop protection, attachment rules), approval gates, suppressions, per-key API rate limit, audit events with NDJSON export |
| Delivery | provider accounts (SMTP relay, SendGrid, Unisender Go), routing rules, provider event webhooks, delivery analytics |
| SDKs | Python (`sdk/python`), TypeScript (`sdk/typescript`), MCP server (`agentbox-mcp`) |
| Console | `/dashboard`: overview, thread viewer, domains, webhooks, keys, usage, policies, audit, API console |

A hosted edition with sign-up, teams, billing, SSO/SCIM and mailbox connectors is built on top of this core through
`agentbox/extensions.py`; the core never depends on it.

## Run locally

    cp .env.example .env
    make up                      # postgres (:5434), minio (:9010), mailpit (:1025 / :8025)
    uv sync --extra dev
    uv run agentbox migrate
    uv run agentbox org create "My Org"   # prints an admin API key (shown once)
    uv run agentbox api          # http://localhost:8000  (docs at /docs, console at /dashboard)
    uv run agentbox worker       # in another shell
    uv run agentbox smtp         # in another shell, inbound SMTP on :2525

Everything in Docker:

    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox migrate
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox org create "My Org"
    docker compose -f deploy/docker/docker-compose.yml --profile app up --build -d
    # http://localhost:8000/dashboard  → log in with the API key

Production self-hosting (DNS records, TLS, relay): `docs/self-host.md`.

## Hello world

    export KEY=ab_live_...
    curl -s localhost:8000/v1/inboxes -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -d '{"username":"demo-agent"}'
    curl -s localhost:8000/v1/inboxes/$INBOX/messages -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' -H 'Idempotency-Key: first-hello' \
      -d '{"to":["you@example.com"],"subject":"Hello","text":"Sent by an agent"}'
    curl -s "localhost:8000/v1/inboxes/$INBOX/messages?direction=inbound&wait=30" -H "Authorization: Bearer $KEY"

Python:

    pip install ./sdk/python        # agentbox-sdk
    from agentbox_sdk import AgentBox
    mail = AgentBox("ab_live_...", base_url="http://localhost:8000")
    inbox = mail.inboxes.create("procurement-agent")
    mail.messages.send(inbox["id"], to=["sales@supplier.ru"], subject="Запрос КП", text="Пришлите КП.")
    reply = mail.messages.wait_for(inbox["id"], timeout=300)

TypeScript: `cd sdk/typescript && npm install && npm run build`, then `new AgentBox({ apiKey, baseUrl })`.
MCP: `pip install './sdk/python[mcp]'` and run `agentbox-mcp` with `AGENTBOX_API_KEY` / `AGENTBOX_API_URL`.

## API surface

| Area | Endpoints |
|---|---|
| Inboxes | `POST/GET /v1/inboxes`, `GET /v1/inboxes/{id}`, `POST .../disable`, `POST .../enable`, `DELETE`, `GET/PUT/DELETE .../policy` |
| Messages | `POST /v1/inboxes/{id}/messages`, `GET /v1/inboxes/{id}/messages` (filters + `wait=`), `GET /v1/messages/{id}`, `POST .../reply`, `POST .../forward`, `POST .../approve`, `POST .../reject`, `GET /v1/approvals` |
| Threads | `GET /v1/inboxes/{id}/threads`, `GET /v1/threads/{id}` |
| Attachments | `POST /v1/attachments/uploads`, `GET /v1/attachments/{id}`, `GET .../download`, `GET /v1/messages/{id}/attachments` |
| Webhooks | `POST/GET/PATCH/DELETE /v1/webhooks`, `GET /v1/webhooks/{id}/deliveries`, `POST .../retry` |
| Domains | `POST/GET /v1/domains`, `GET /v1/domains/{id}`, `POST .../verify`, `DELETE` |
| Governance | `GET/PUT/DELETE /v1/policy`, `GET/POST/DELETE /v1/suppressions` |
| Delivery | `/v1/provider-accounts`, `/v1/routing-rules`, `POST /v1/providers/{provider}/events/{token}`, `GET /v1/analytics/delivery` |
| Account | `GET /v1/me`, `/v1/api-keys`, `GET /v1/usage`, `GET /v1/events`, `GET /v1/events/export` (NDJSON) |

Auth: `Authorization: Bearer ab_live_...` with scopes, `Idempotency-Key` on mutations, error envelope
`{"error":{"code","message","request_id","details"}}`, `AgentBox-Request-Id` on every response. Webhook signature
`AgentBox-Signature: t=<unix>,v1=<hmac-sha256>`.

## Extending

`agentbox/extensions.py` documents the entry point (`agentbox.extensions`) and hooks an edition or plugin can use:
routers, templates, job kinds, CLI commands, migrations, models, a `Settings` subclass, console login/shell hooks,
pre-create/pre-send checks and an outbound provider override.

## Tests

    make test              # unit
    make test-integration  # needs `make up` (API, jobs, SMTP edge, SDKs, MCP, console)
    make e2e               # acceptance walk-through

## Docs

`docs/self-host.md` (production), `docs/superpowers/specs/` (design notes), `CONTRIBUTING.md`.

## License

Apache-2.0. Copyright 2026 Mikhail Baklanov and AgentBox contributors.
