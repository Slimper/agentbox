---
name: agentbox
description: Give the agent a real email address and let it send, receive, reply and forward email. Use when a task needs the agent to email someone, wait for a reply, read attachments, or use an existing mailbox (Gmail, Yandex, Microsoft 365, any IMAP).
---

# AgentBox: email for this agent

AgentBox is an email API for agents. Every inbox has a real address; messages arrive as JSON with threads and
attachments; the agent can wait for a reply with one call. It runs self-hosted (open source, Apache-2.0) or as a
hosted service. Configuration comes from two environment variables:

- `AGENTBOX_API_URL` (default `http://localhost:8000`)
- `AGENTBOX_API_KEY` (`ab_live_...`; self-host: `agentbox org create "Me"` prints one)

If the `agentbox-mcp` MCP server is connected, prefer its tools (`create_inbox`, `connect_mailbox`, `send_email`,
`wait_for_email`, `reply_email`, `read_email`, `list_threads`, ...). Otherwise use the REST API below with curl.

## Get an address (pick one)

**A. Managed inbox** (address on the AgentBox domain, instant):

    curl -s $AGENTBOX_API_URL/v1/inboxes -H "Authorization: Bearer $AGENTBOX_API_KEY" \
      -H 'Content-Type: application/json' -d '{"username":"assistant"}'
    # → {"id":"ibx_...","email":"assistant@<domain>",...}

Add `"ttl":"24h"` for a temporary address.

**B. Use an existing mailbox** (the user's Gmail / Yandex / Microsoft 365 / any IMAP, with an app password):

    curl -s $AGENTBOX_API_URL/v1/connections -H "Authorization: Bearer $AGENTBOX_API_KEY" \
      -H 'Content-Type: application/json' \
      -d '{"provider":"gmail","address":"me@gmail.com","password":"<app password>"}'
    # → {"id":"mbc_...","inbox":{"id":"ibx_...","email":"me@gmail.com"},...}

Providers: `gmail`, `yandex360`, `vkworkspace`, `m365`, `imap` (then also pass `imap_host` and `smtp_host`).
Ask the user for an app password; never guess it. New mail is pulled every 2 minutes;
`POST /v1/connections/{id}/sync` pulls now.

## Send, wait, reply

    # send
    curl -s $AGENTBOX_API_URL/v1/inboxes/$INBOX/messages -H "Authorization: Bearer $AGENTBOX_API_KEY" \
      -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen)" \
      -d '{"to":["sales@supplier.com"],"subject":"Quote request","text":"Please send a quote for ..."}'

    # wait up to 60 s for the next inbound message (long-poll)
    curl -s "$AGENTBOX_API_URL/v1/inboxes/$INBOX/messages?direction=inbound&wait=60" \
      -H "Authorization: Bearer $AGENTBOX_API_KEY"

    # reply in the same thread
    curl -s $AGENTBOX_API_URL/v1/messages/$MESSAGE/reply -H "Authorization: Bearer $AGENTBOX_API_KEY" \
      -H 'Content-Type: application/json' -d '{"text":"Thanks, confirmed."}'

Other endpoints: `GET /v1/messages/{id}` (full body, `include_headers=true`), `GET /v1/messages/{id}/attachments`
and `GET /v1/attachments/{id}/download`, `POST /v1/messages/{id}/forward`, `GET /v1/inboxes/{id}/threads`.
Errors come as `{"error":{"code","message"}}`; `402 quota_exceeded` means the plan limit was hit,
`202` with `status: pending_approval` means a human must approve the message in the console first.

## Rules

- One idempotency key per send; retry with the same key, never a new one.
- Do not send to addresses the user did not mention. Do not send marketing or bulk mail.
- Quote the relevant part of the incoming message when replying; keep the thread (use `reply`, not a new send).
