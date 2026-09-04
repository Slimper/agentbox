# AgentBox — Sub-projects 2–5 Design (as built)

Date: 2026-09-01. Companion to `2026-09-01-agentbox-core-service-design.md`. Records the decisions made while
building domains, governance/delivery, SDKs/MCP and the dashboard on top of the core service.

## 2. Custom domains + DNS verification

- `POST /v1/domains` registers a `customer_custom` domain in `verification_pending` with a random ownership token and
  returns the records to publish: TXT `_agentbox.<domain>=agentbox-verification=<token>` (required), MX for each
  host in `AGENTBOX_MX_HOSTNAMES` (required), SPF `include:<AGENTBOX_SPF_INCLUDE>`, DMARC, and DKIM only when
  `AGENTBOX_DKIM_SELECTOR`/`DKIM_PUBLIC_KEY` are configured (DKIM signing itself is done by the relay provider).
- Job `domain_verify` (dnspython, resolver injected via `Runtime.dns` so tests use a fake) computes
  `check_results` (`ownership`, `mx`, `spf`, `dmarc`, `dkim` ∈ ok/partial/wrong/missing/skipped). Minimum for
  `active` = ownership ok + MX ok/partial. `active → degraded` when the minimum stops holding (event
  `domain.degraded`), never deleted automatically. Re-checks every 10 min while pending, 6 h when active; only one
  scheduled check per domain is kept.
- Inboxes can be created on a domain only while it is `active`; deleting a domain requires no live inboxes.

## 3. Governance and delivery

- **Policies** live at organization and inbox level (`policies` table, partial unique indexes) and are deep-merged
  over defaults (`DEFAULT_POLICY`). Validation is strict (unknown keys rejected). Enforced in
  `create_outbound_message` before any write: send_enabled, suppressions, blocked/allowed domains, per-minute/hour/day
  counts, `per_thread_per_hour` loop protection, executable extensions, attachment size. A block emits
  `policy.blocked` in its own committed transaction, then raises the API error. `receive_enabled=false` is enforced at
  SMTP `RCPT TO`.
- **Approval gates**: `approval.external_domain` (recipient domain outside the org's domains) and
  `approval.new_recipient` (never successfully sent to before). The message is stored as `pending_approval` with
  no send job; `POST /v1/messages/{id}/approve|reject`, `GET /v1/approvals`.
- **Suppressions**: unique per org+email, manual via API/dashboard, automatic on hard bounce (`5.x.x` DSN or
  provider bounce) and complaints through the shared `services/delivery.apply_delivery_status`, which is also the
  single place for forward-only status transitions and canonical `message.<status>` events.
- **Providers**: interface `send(envelope, message, raw)`. Adapters: SMTP relay, SendGrid (v3 JSON, custom arg
  `agentbox_message_id`, our own `Message-ID` header kept), Unisender Go (`email/send.json`, `global_metadata`).
  Provider accounts are per organization with encrypted config and a `webhook_token`; delivery events are pushed to
  `/v1/providers/{provider}/events/{token}` (SendGrid ECDSA signature verified when `event_public_key` is configured).
- **Routing**: `routing_rules` (priority, `recipient_domain_suffix`, `inbox_id`) → the org's own first active
  account → shared default. Recipient domain is taken from the first `to` address.
- **API rate limit**: fixed one-minute window per API key, per process (`AGENTBOX_API_RATE_LIMIT_PER_MINUTE`).
- **Analytics**: `GET /v1/analytics/delivery?group_by=provider|recipient_domain|inbox|day` aggregates current message
  statuses (not a history), enough for the dashboard and for later routing decisions.

## 4. SDKs and MCP

- Python SDK `agentbox-sdk` (`sdk/python`, import `agentbox_sdk`; the server package already owns the `agentbox`
  name). Sync httpx client, plain dicts, retries on 429/5xx for GET and idempotent calls, helpers `attachments.upload`
  (presigned PUT), `messages.wait_for` (long-poll loop), `verify_webhook_signature`.
- MCP server `agentbox-mcp` (stdio) built on the same SDK; works with `mcp` 1.x (`FastMCP`) and 2.x (`MCPServer`).
  Errors are returned as `{"error": {...}}` so the model can react. The product spec's `npx @agentbox/mcp` was
  replaced by a Python entry point to keep one stack; the TypeScript SDK can host an MCP server later.
- TypeScript SDK `@agentbox/sdk` (`sdk/typescript`, fetch-based, ESM, zero runtime deps) with the same resources and
  `verifyWebhookSignature` on WebCrypto. Verified by a Node smoke test against a live server.

## 5. Dashboard and usage

- Dashboard at `/dashboard`: login with an API key (stored encrypted in an HttpOnly cookie, acts with that key's
  scopes). Pages: overview, inboxes (+ create / disable / send test / inbox policy), threads and messages (HTML
  sanitized with `nh3`), approvals, domains (DNS records + check now), webhooks (deliveries + retry), API keys,
  usage, deliverability, policies + suppressions, audit log, API console with curl/Python/TS/MCP snippets.
- Usage: `usage_daily` rows per org/day (active inboxes, ephemeral created, sent, received, attachment bytes,
  webhook attempts, custom domains) recomputed hourly by the `usage_rollup` job (scheduled by the worker's sweeper),
  plus `GET /v1/usage/current` for live counters. Billing plans/prices are not implemented (counters only).
- API keys: `GET/POST/DELETE /v1/api-keys` with scope checks; a key cannot revoke itself.

## Known limits / next steps

- Rate limiting and idempotency in-flight waits are per process; put a shared store behind them for multi-instance.
- Unisender Go webhook `auth` MD5 signature is not verified (token URL only). SendGrid signature verification is
  opt-in per account.
- DKIM signing for the native SMTP path, SSO/RBAC beyond scopes, and billing are out of scope.
