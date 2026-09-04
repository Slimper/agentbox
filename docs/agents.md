# Email for your agent in two minutes

Works with any agent that speaks MCP (OpenClaw, Hermes Agent, Claude Desktop, Cursor, Codex) or can run curl.

## 1. Get an AgentBox

Self-host (Docker, no account):

    git clone https://github.com/Slimper/agentbox && cd agentbox
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox migrate
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox org create "Me"   # prints ab_live_...
    docker compose -f deploy/docker/docker-compose.yml --profile app up -d                                  # http://localhost:8000

Or use a hosted AgentBox and take an API key from its console.

## 2. Connect the MCP server

No install step: `uvx` fetches it from this repository.

    {
      "mcpServers": {
        "agentbox": {
          "command": "uvx",
          "args": ["--from", "agentbox-sdk[mcp] @ git+https://github.com/Slimper/agentbox#subdirectory=sdk/python", "agentbox-mcp"],
          "env": { "AGENTBOX_API_URL": "http://localhost:8000", "AGENTBOX_API_KEY": "ab_live_..." }
        }
      }
    }

- **Claude Desktop / Cursor / Codex**: paste into their MCP config (`claude_desktop_config.json`, `.cursor/mcp.json`, `~/.codex/config.toml`).
- **OpenClaw**: register the same stdio server in OpenClaw's MCP configuration (or via `mcporter`), and copy
  `skills/agentbox/` into `~/.openclaw/skills/agentbox/` so the agent knows when to use it.
- **Hermes Agent**: add the server under `mcp_servers` in `~/.hermes/config.yaml` (see the `native-mcp` skill) and copy
  `skills/agentbox/` into `~/.hermes/skills/agentbox/`.
- **No MCP?** The skill file alone is enough: it documents the REST calls with curl.

Tools the agent gets: `create_inbox`, `connect_mailbox`, `list_inboxes`, `send_email`, `wait_for_email`, `read_email`,
`reply_email`, `forward_email`, `list_threads`, `get_thread`, `list_attachments`, `download_attachment`,
`upload_attachment`, `list_pending_approvals`, `sync_mailbox`.

## 3. Give the agent an address

Either a managed address (`create_inbox`, instant, on the AgentBox domain) or the user's own mailbox
(`connect_mailbox` with an app password: Gmail, Yandex 360, VK WorkSpace, Microsoft 365 or any IMAP server).
With a connected mailbox nothing else is needed: no domain, no DNS, no port 25. New mail is pulled every two minutes
and replies go out through the mailbox's own SMTP from its own address.

Try it: "Connect my Gmail me@gmail.com with this app password, then email sales@supplier.com asking for a quote and
tell me when they answer."

## Guardrails

Policies (allow/block lists, rate limits, attachment rules) and approval gates are set per inbox in the console or via
`PUT /v1/inboxes/{id}/policy`; a gated message waits in `/dashboard/approvals` until a human approves it.
