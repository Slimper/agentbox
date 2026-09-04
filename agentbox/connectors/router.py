"""REST API for mailbox connectors: give an agent an existing mailbox (Gmail, Yandex, M365, any IMAP) in one call."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session, get_settings_dep
from agentbox.api.idempotency import IdempotencyGuard, idempotency
from agentbox.config import Settings
from agentbox.connectors.service import (
    PRESETS,
    connect_mailbox,
    connection_to_dict,
    disconnect,
    get_connection,
    list_connections,
)
from agentbox.jobs.queue import enqueue
from agentbox.services.inboxes import inbox_to_dict

router = APIRouter(prefix="/v1/connections", tags=["connections"])


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(description="yandex360 | vkworkspace | m365 | gmail | imap")
    address: str = Field(description="The mailbox address; becomes the inbox address")
    password: str = Field(description="App password (or account password where the provider allows it)")
    username: str | None = Field(default=None, description="Login if it differs from the address")
    imap_host: str | None = None
    imap_port: int | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool | None = Field(default=None, description="Implicit TLS on the SMTP port (465); STARTTLS otherwise")
    display_name: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/presets")
async def presets(principal: Principal = Depends(require_scope("inboxes:read"))):
    return {"data": [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "oauth"}} for k, v in PRESETS.items()]}


@router.post("", status_code=201)
async def create(
    body: ConnectionCreate,
    principal: Principal = Depends(require_scope("inboxes:write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    inbox, conn = await connect_mailbox(
        session, settings, organization_id=principal.organization_id, provider=body.provider, address=body.address,
        username=body.username, password=body.password, imap_host=body.imap_host, imap_port=body.imap_port,
        smtp_host=body.smtp_host, smtp_port=body.smtp_port, smtp_ssl=body.smtp_ssl, display_name=body.display_name,
        metadata=body.metadata,
    )
    await session.commit()
    return await idem.commit(201, {**connection_to_dict(conn, settings), "inbox": inbox_to_dict(inbox)})


@router.get("")
async def list_all(principal: Principal = Depends(require_scope("inboxes:read")),
                   session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    rows = await list_connections(session, principal.organization_id)
    return {"data": [connection_to_dict(c, settings) for c in rows]}


@router.get("/{connection_id}")
async def get(connection_id: str, principal: Principal = Depends(require_scope("inboxes:read")),
              session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    return connection_to_dict(await get_connection(session, principal.organization_id, connection_id), settings)


@router.post("/{connection_id}/sync", status_code=202)
async def sync_now(connection_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                   session: AsyncSession = Depends(get_session)):
    conn = await get_connection(session, principal.organization_id, connection_id)
    await enqueue(session, "connector_sync", {"connection_id": conn.id})
    await session.commit()
    return {"queued": True, "connection_id": conn.id}


@router.post("/{connection_id}/pause")
async def pause(connection_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    conn = await get_connection(session, principal.organization_id, connection_id)
    conn.status = "paused"
    await session.commit()
    return connection_to_dict(conn, settings)


@router.post("/{connection_id}/resume")
async def resume(connection_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                 session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)):
    conn = await get_connection(session, principal.organization_id, connection_id)
    conn.status = "active"
    await enqueue(session, "connector_sync", {"connection_id": conn.id})
    await session.commit()
    return connection_to_dict(conn, settings)


@router.delete("/{connection_id}", status_code=204, response_class=Response)
async def delete(connection_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                 session: AsyncSession = Depends(get_session)):
    conn = await get_connection(session, principal.organization_id, connection_id)
    await disconnect(session, conn, actor=principal.api_key_id)
    await session.commit()
    return Response(status_code=204)
