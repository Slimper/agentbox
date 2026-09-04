async def test_events_listing(client, org, make_org):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "e"})).json()
    await client.post(f"/v1/inboxes/{inbox['id']}/disable", headers=org.headers)
    r = await client.get("/v1/events", headers=org.headers)
    assert [e["type"] for e in r.json()["data"]] == ["inbox.disabled", "inbox.created"]
    r = await client.get("/v1/events", headers=org.headers, params={"type": "inbox.created"})
    assert len(r.json()["data"]) == 1 and r.json()["data"][0]["data"]["inbox_id"] == inbox["id"]
    other = await make_org("Other")
    assert (await client.get("/v1/events", headers=other.headers)).json()["data"] == []
