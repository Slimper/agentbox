import asyncio


async def test_replay_same_key_same_body(client, org):
    h = {**org.headers, "Idempotency-Key": "k1"}
    r1 = await client.post("/v1/inboxes", headers=h, json={"username": "idem"})
    r2 = await client.post("/v1/inboxes", headers=h, json={"username": "idem"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers.get("Idempotent-Replayed") == "true"


async def test_conflict_on_different_body(client, org):
    h = {**org.headers, "Idempotency-Key": "k2"}
    assert (await client.post("/v1/inboxes", headers=h, json={"username": "one"})).status_code == 201
    r = await client.post("/v1/inboxes", headers=h, json={"username": "two"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"


async def test_failed_request_does_not_poison_key(client, org):
    h = {**org.headers, "Idempotency-Key": "k3"}
    r = await client.post("/v1/inboxes", headers=h, json={"username": "postmaster"})
    assert r.status_code == 422
    r = await client.post("/v1/inboxes", headers=h, json={"username": "postmaster"})
    assert r.status_code == 422  # re-executed, not replayed
    assert r.headers.get("Idempotent-Replayed") is None


async def test_concurrent_duplicates_serialize(client, org):
    h = {**org.headers, "Idempotency-Key": "k4"}
    calls = [client.post("/v1/inboxes", headers=h, json={"username": "race"}) for _ in range(5)]
    results = await asyncio.gather(*calls)
    codes = sorted(r.status_code for r in results)
    assert codes == [201] * 5
    assert len({r.json()["id"] for r in results}) == 1
