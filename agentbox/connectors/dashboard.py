"""Console page for mailbox connectors."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError
from agentbox.connectors.service import (
    PRESETS,
    connect_mailbox,
    connection_to_dict,
    disconnect,
    get_connection,
    list_connections,
)
from agentbox.dashboard.router import _redirect, _render, _shell, dash_principal
from agentbox.jobs.queue import enqueue
from agentbox.runtime import Runtime

router = APIRouter(include_in_schema=False, tags=["connectors"])


@router.get("/dashboard/connectors", response_class=HTMLResponse)
async def connectors(request: Request, connect: str | None = None, principal=Depends(dash_principal),
                     session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    rows = await list_connections(session, principal.organization_id)
    ctx = await _shell(request, session, principal, "connectors")
    ctx.update({"connections": [connection_to_dict(c, runtime.settings) for c in rows], "presets": PRESETS,
                "connect": connect, "error": request.query_params.get("error")})
    return _render(request, "connectors.html", ctx)


@router.post("/dashboard/connectors")
async def connect(provider: str = Form(...), address: str = Form(...), username: str = Form(""), password: str = Form(...),
                  imap_host: str = Form(""), imap_port: str = Form(""), smtp_host: str = Form(""), smtp_port: str = Form(""),
                  display_name: str = Form(""), principal=Depends(dash_principal),
                  session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    if not principal.has("inboxes:write"):
        raise APIError(403, "forbidden", "API key lacks scope 'inboxes:write'.")
    inbox, conn = await connect_mailbox(
        session, runtime.settings, organization_id=principal.organization_id, provider=provider, address=address,
        username=username.strip() or None, password=password, imap_host=imap_host.strip() or None,
        imap_port=int(imap_port) if imap_port.strip().isdigit() else None, smtp_host=smtp_host.strip() or None,
        smtp_port=int(smtp_port) if smtp_port.strip().isdigit() else None, display_name=display_name.strip() or None)
    await session.commit()
    return _redirect(f"/dashboard/inboxes/{inbox.id}", f"Connected {conn.address}; first sync queued")


@router.post("/dashboard/connectors/{connection_id}/action")
async def connector_action(connection_id: str, action: str = Form(...), principal=Depends(dash_principal),
                           session: AsyncSession = Depends(get_session)):
    if not principal.has("inboxes:write"):
        raise APIError(403, "forbidden", "API key lacks scope 'inboxes:write'.")
    conn = await get_connection(session, principal.organization_id, connection_id)
    toast = None
    if action == "sync":
        await enqueue(session, "connector_sync", {"connection_id": conn.id})
        toast = "Sync queued"
    elif action == "toggle":
        conn.status = "paused" if conn.status == "active" else "active"
        toast = f"Connection {conn.status}"
    elif action == "disconnect":
        await disconnect(session, conn, actor=principal.api_key_id)
        toast = f"Disconnected {conn.address}"
    await session.commit()
    return _redirect("/dashboard/connectors", toast)
