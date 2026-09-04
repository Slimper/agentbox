"""AgentBox MCP server — lets an agent operate email natively.

Run:  AGENTBOX_API_KEY=ab_live_... AGENTBOX_API_URL=http://localhost:8000 agentbox-mcp
(stdio transport; add it to your MCP client config).
"""

from __future__ import annotations

import base64
import functools
import os

from agentbox_sdk.client import AgentBox, AgentBoxError

try:  # mcp >= 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit("Install the MCP extra: pip install 'agentbox-sdk[mcp]'") from e


def build_server(client: AgentBox | None = None) -> FastMCP:
    mcp = FastMCP("AgentBox", instructions=(
        "AgentBox gives you real email inboxes. Create an inbox, send email, then use wait_for_email or "
        "list_messages to read replies. Replies keep the thread; use reply_email on the message you are answering."
    ))
    state: dict[str, AgentBox | None] = {"client": client}

    def c() -> AgentBox:
        if state["client"] is None:
            key = os.environ.get("AGENTBOX_API_KEY")
            if not key:
                raise RuntimeError("AGENTBOX_API_KEY is not set")
            state["client"] = AgentBox(key, base_url=os.environ.get("AGENTBOX_API_URL", "http://localhost:8000"))
        return state["client"]

    def guarded(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except AgentBoxError as e:
                return {"error": {"code": e.code, "message": e.message, "details": e.details}}

        return wrapper

    def summary(m: dict) -> dict:
        return {k: m.get(k) for k in ("id", "thread_id", "direction", "status", "from", "to", "cc", "subject",
                                      "received_at", "sent_at", "created_at")} | {
            "attachments": [{"id": a["id"], "filename": a["filename"], "content_type": a["content_type"],
                             "size_bytes": a["size_bytes"]} for a in m.get("attachments", [])],
            "preview": (m.get("text") or "")[:300],
        }

    @mcp.tool()
    @guarded
    def create_inbox(username: str | None = None, display_name: str | None = None, domain: str | None = None,
                     ttl: str | None = None) -> dict:
        """Create an email inbox (identity) for the agent. Omit username for a random address.
        ttl like "24h" makes it temporary."""
        return c().inboxes.create(username, display_name=display_name, domain=domain, ttl=ttl)

    @mcp.tool()
    @guarded
    def get_inbox(inbox_id: str) -> dict:
        """Get an inbox by id."""
        return c().inboxes.get(inbox_id)

    @mcp.tool()
    @guarded
    def list_inboxes(status: str | None = None, limit: int = 20) -> list[dict]:
        """List the organization's inboxes."""
        return c().inboxes.list(status=status, limit=limit)["data"]

    @mcp.tool()
    @guarded
    def connect_mailbox(provider: str, address: str, password: str, username: str | None = None,
                        imap_host: str | None = None, smtp_host: str | None = None, display_name: str | None = None) -> dict:
        """Use an existing mailbox as an inbox: provider is gmail | yandex360 | vkworkspace | m365 | imap, password is an
        app password. New mail is pulled every 2 minutes; replies go out through the mailbox's own SMTP. Returns the
        connection with its inbox (use inbox["id"] with the other tools)."""
        return c().connections.create(provider, address, password, username=username, imap_host=imap_host,
                                      smtp_host=smtp_host, display_name=display_name)

    @mcp.tool()
    @guarded
    def list_connections() -> list[dict]:
        """List connected mailboxes (status, last sync, last error)."""
        return c().connections.list()

    @mcp.tool()
    @guarded
    def sync_mailbox(connection_id: str) -> dict:
        """Pull new mail from a connected mailbox right now instead of waiting for the periodic sync."""
        return c().connections.sync(connection_id)

    @mcp.tool()
    @guarded
    def send_email(inbox_id: str, to: list[str], subject: str, text: str, html: str | None = None,
                   cc: list[str] | None = None, attachment_ids: list[str] | None = None) -> dict:
        """Send an email from an inbox. Returns {id, thread_id, status}. status may be pending_approval
        when a policy requires a human to approve."""
        return c().messages.send(inbox_id, to=to, cc=cc, subject=subject, text=text, html=html,
                                 attachment_ids=attachment_ids)

    @mcp.tool()
    @guarded
    def reply_email(message_id: str, text: str, reply_all: bool = False, html: str | None = None,
                    attachment_ids: list[str] | None = None) -> dict:
        """Reply to a message; threading headers and recipients are handled automatically."""
        return c().messages.reply(message_id, text=text, html=html, reply_all=reply_all, attachment_ids=attachment_ids)

    @mcp.tool()
    @guarded
    def forward_email(message_id: str, to: list[str], text: str | None = None,
                      include_attachments: bool = True) -> dict:
        """Forward a message to other recipients."""
        return c().messages.forward(message_id, to=to, text=text, include_attachments=include_attachments)

    @mcp.tool()
    @guarded
    def list_threads(inbox_id: str, limit: int = 20) -> list[dict]:
        """List conversation threads of an inbox, newest first."""
        return c().threads.list(inbox_id, limit=limit)["data"]

    @mcp.tool()
    @guarded
    def get_thread(thread_id: str) -> dict:
        """Get a thread with all of its messages in order."""
        t = c().threads.get(thread_id)
        t["messages"] = [summary(m) | {"text": m.get("text")} for m in t.get("messages", [])]
        return t

    @mcp.tool()
    @guarded
    def list_messages(inbox_id: str, direction: str | None = "inbound", thread_id: str | None = None,
                      since: str | None = None, limit: int = 20) -> list[dict]:
        """List messages of an inbox (summaries). direction: inbound | outbound | null for both."""
        page = c().messages.list(inbox_id, direction=direction, thread_id=thread_id, since=since, limit=limit)
        return [summary(m) for m in page["data"]]

    @mcp.tool()
    @guarded
    def wait_for_email(inbox_id: str, timeout_seconds: int = 120, since: str | None = None,
                       thread_id: str | None = None) -> dict | None:
        """Block until a new inbound email arrives (up to timeout_seconds). Returns the message or null."""
        m = c().messages.wait_for(inbox_id, since=since, thread_id=thread_id, timeout=timeout_seconds)
        return (summary(m) | {"text": m.get("text")}) if m else None

    @mcp.tool()
    @guarded
    def read_email(message_id: str) -> dict:
        """Read a full message including text and html bodies and attachment list."""
        return c().messages.get(message_id)

    @mcp.tool()
    @guarded
    def list_attachments(message_id: str) -> list[dict]:
        """List attachments of a message."""
        return c().attachments.list(message_id)["data"]

    @mcp.tool()
    @guarded
    def download_attachment(attachment_id: str, save_to: str | None = None, max_inline_bytes: int = 200_000) -> dict:
        """Download an attachment. If save_to is a path, the file is written there; otherwise small files are
        returned inline as base64 (larger ones only as a signed download URL)."""
        meta = c().attachments.get(attachment_id)
        if save_to:
            data = c().attachments.download(attachment_id)
            with open(save_to, "wb") as f:
                f.write(data)
            return {**meta, "saved_to": save_to}
        if meta["size_bytes"] <= max_inline_bytes:
            data = c().attachments.download(attachment_id)
            return {**meta, "content_base64": base64.b64encode(data).decode("ascii")}
        return {**meta, "download": c().attachments.download_url(attachment_id)}

    @mcp.tool()
    @guarded
    def upload_attachment(filename: str, content_base64: str, content_type: str = "application/octet-stream") -> dict:
        """Upload a file (base64) to attach to a later send_email/reply_email call."""
        aid = c().attachments.upload(filename, base64.b64decode(content_base64), content_type)
        return {"attachment_id": aid}

    @mcp.tool()
    @guarded
    def list_pending_approvals() -> list[dict]:
        """List outbound messages waiting for human approval."""
        return [summary(m) for m in c().messages.pending_approvals()["data"]]

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
