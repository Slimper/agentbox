import hashlib

import httpx
import pytest

from agentbox.api.errors import APIError
from agentbox.services.attachments import bind_attachments_for_send


async def test_upload_and_download_roundtrip(client, org, runtime):
    data = b"%PDF-1.4 fake pdf " * 100
    r = await client.post("/v1/attachments/uploads", headers=org.headers,
                          json={"filename": "offer.pdf", "content_type": "application/pdf", "size_bytes": len(data)})
    assert r.status_code == 201, r.text
    up = r.json()
    async with httpx.AsyncClient() as external:
        put = await external.put(up["upload_url"], content=data, headers={"Content-Type": "application/pdf"})
        assert put.status_code in (200, 204), put.text
    async with runtime.db.session() as s:
        [att] = await bind_attachments_for_send(s, runtime.storage, organization_id=org.id,
                                                attachment_ids=[up["attachment_id"]])
        assert att.status == "ready"
        await s.commit()
    r = await client.get(f"/v1/attachments/{up['attachment_id']}/download", headers=org.headers)
    assert r.status_code == 200
    async with httpx.AsyncClient() as external:
        got = await external.get(r.json()["url"])
        assert got.status_code == 200 and hashlib.sha256(got.content).digest() == hashlib.sha256(data).digest()
        assert 'filename="offer.pdf"' in got.headers.get("content-disposition", "")


async def test_bind_rejects_missing_upload_and_size_mismatch(client, org, runtime):
    r = await client.post("/v1/attachments/uploads", headers=org.headers,
                          json={"filename": "x.bin", "size_bytes": 10})
    aid = r.json()["attachment_id"]
    async with runtime.db.session() as s:
        with pytest.raises(APIError) as exc:
            await bind_attachments_for_send(s, runtime.storage, organization_id=org.id, attachment_ids=[aid])
        assert exc.value.code == "attachment_blocked"
    async with httpx.AsyncClient() as external:
        hdrs = {"Content-Type": "application/octet-stream"}
        await external.put(r.json()["upload_url"], content=b"12345", headers=hdrs)
    async with runtime.db.session() as s:
        with pytest.raises(APIError) as exc:
            await bind_attachments_for_send(s, runtime.storage, organization_id=org.id, attachment_ids=[aid])
        assert "size" in str(exc.value)


async def test_tenant_isolation(client, org, make_org):
    r = await client.post("/v1/attachments/uploads", headers=org.headers, json={"filename": "a", "size_bytes": 1})
    other = await make_org("Other")
    assert (await client.get(f"/v1/attachments/{r.json()['attachment_id']}", headers=other.headers)).status_code == 404
    assert (await client.post("/v1/attachments/uploads", headers=org.headers,
                              json={"filename": "big", "size_bytes": 21 * 1024 * 1024})).status_code == 413
