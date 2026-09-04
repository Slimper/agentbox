async def test_missing_and_invalid_key(client):
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.headers["AgentBox-Request-Id"].startswith("req_")
    assert r.json()["error"]["request_id"] == r.headers["AgentBox-Request-Id"]
    r = await client.get("/v1/me", headers={"Authorization": "Bearer ab_live_nope"})
    assert r.status_code == 401


async def test_valid_key(client, org):
    r = await client.get("/v1/me", headers=org.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["organization_id"] == org.id and body["scopes"] == ["admin"]


async def test_health(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
