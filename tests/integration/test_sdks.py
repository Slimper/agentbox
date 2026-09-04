import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentbox.webhooks.signing import signature_header
from agentbox_sdk import AgentBox, AgentBoxError, verify_webhook_signature
from agentbox_sdk.mcp_server import build_server

ROOT = Path(__file__).resolve().parents[2]


async def test_python_sdk_end_to_end(live_api_url, org, runtime):
    mail = AgentBox(org.api_key, base_url=live_api_url)
    assert mail.me()["organization_id"] == org.id
    inbox = mail.inboxes.create("py-agent", display_name="Py", metadata={"lang": "py"})
    assert inbox["email"] == "py-agent@agentbox.local"
    assert [i["id"] for i in mail.inboxes.list(metadata={"lang": "py"})["data"]] == [inbox["id"]]

    sent = mail.messages.send(inbox["id"], to=["vendor@example.com"], subject="SDK hello", text="hi",
                              idempotency_key="py-idem-1")
    again = mail.messages.send(inbox["id"], to=["vendor@example.com"], subject="SDK hello", text="hi",
                               idempotency_key="py-idem-1")
    assert sent["id"] == again["id"]
    assert mail.messages.get(sent["id"], include_headers=True)["subject"] == "SDK hello"
    assert len(mail.threads.get(sent["thread_id"])["messages"]) == 1

    att = mail.attachments.upload("note.txt", b"hello attachment", "text/plain")
    rep = mail.messages.reply(sent["id"], text="follow-up", attachment_ids=[att])
    assert rep["thread_id"] == sent["thread_id"]
    assert mail.attachments.download(att) == b"hello attachment"
    assert mail.attachments.list(rep["id"])["data"][0]["filename"] == "note.txt"

    hook = mail.webhooks.create("https://example.com/h", event_types=["message.received"])
    header = signature_header(hook["secret"], b'{"id":"evt"}', 1_700_000_000)
    assert verify_webhook_signature(hook["secret"], header, b'{"id":"evt"}', now=1_700_000_010)
    assert not verify_webhook_signature("x", header, b'{"id":"evt"}', now=1_700_000_010)
    mail.webhooks.delete(hook["id"])

    assert mail.messages.wait_for(inbox["id"], timeout=1.5) is None
    assert mail.policy.set({"limits": {"emails_per_day": 10}})["effective"]["limits"]["emails_per_day"] == 10
    assert mail.suppressions.create("bad@example.com")["email"] == "bad@example.com"
    assert mail.analytics.delivery()["group_by"] == "provider"
    assert mail.events.list(type="inbox.created")["data"][0]["data"]["inbox_id"] == inbox["id"]
    with pytest.raises(AgentBoxError) as exc:
        mail.inboxes.get("ibx_nope")
    assert exc.value.code == "not_found" and exc.value.request_id.startswith("req_")
    mail.close()


async def test_mcp_tools(live_api_url, org):
    server = build_server(AgentBox(org.api_key, base_url=live_api_url))
    names = {t.name for t in await server.list_tools()}
    assert {"create_inbox", "send_email", "reply_email", "forward_email", "list_threads", "get_thread",
            "list_messages", "read_email", "list_attachments", "download_attachment", "wait_for_email",
            "upload_attachment", "list_pending_approvals"} <= names
    res = await server.call_tool("create_inbox", {"username": "mcp-agent"})
    inbox = _payload(res)
    assert inbox["email"] == "mcp-agent@agentbox.local"
    sent = _payload(await server.call_tool("send_email", {"inbox_id": inbox["id"], "to": ["v@example.com"],
                                                          "subject": "MCP", "text": "hello"}))
    assert sent["status"] == "queued"
    thread = _payload(await server.call_tool("get_thread", {"thread_id": sent["thread_id"]}))
    assert thread["messages"][0]["preview"] == "hello"
    msgs = _payload(await server.call_tool("list_messages", {"inbox_id": inbox["id"], "direction": "outbound"}))
    assert [m["id"] for m in msgs] == [sent["id"]]
    err = _payload(await server.call_tool("read_email", {"message_id": "msg_nope"}))
    assert err["error"]["code"] == "not_found"
    waited = _payload(await server.call_tool("wait_for_email", {"inbox_id": inbox["id"], "timeout_seconds": 1}))
    assert waited is None


def _payload(result):
    """Normalize FastMCP/MCPServer call_tool results across mcp versions into the tool's JSON value."""
    import json

    if isinstance(result, tuple):  # mcp 1.x: (content, structured)
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        result = content
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    blocks = getattr(result, "content", result)
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
async def test_typescript_sdk_smoke(live_api_url, org):
    ts = ROOT / "sdk" / "typescript"
    if not (ts / "dist" / "index.js").exists():
        subprocess.run(["npm", "run", "build"], cwd=ts, check=True, capture_output=True)
    env = {**os.environ, "AGENTBOX_API_URL": live_api_url, "AGENTBOX_API_KEY": org.api_key}
    r = subprocess.run(["node", "test/smoke.mjs"], cwd=ts, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ts sdk smoke ok" in r.stdout
