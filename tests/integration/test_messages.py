import asyncio

from sqlalchemy import select

from agentbox.db.models import Event, Job


async def _inbox(client, org, username="agent"):
    return (await client.post("/v1/inboxes", headers=org.headers, json={"username": username})).json()


async def test_send_creates_queued_message_thread_and_job(client, org, runtime):
    inbox = await _inbox(client, org)
    r = await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                          json={"to": ["Sales@Supplier.ru", {"email": "x@y.ru", "name": "X"}],
                                "cc": ["sales@supplier.ru"], "subject": "Запрос КП", "text": "Привет",
                                "headers": {"X-Agent": "1"}})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["thread_id"].startswith("thr_")
    r = await client.get(f"/v1/messages/{body['id']}", headers=org.headers, params={"include": "headers"})
    m = r.json()
    assert m["from"]["email"] == "agent@agentbox.local"
    assert [a["email"] for a in m["to"]] == ["sales@supplier.ru", "x@y.ru"]
    assert m["cc"] == [] and m["internet_message_id"] == f"<{body['id']}@agentbox.local>"
    assert m["headers"] == [["X-Agent", "1"]]
    async with runtime.db.session() as s:
        assert await s.scalar(select(Job).where(Job.kind == "outbound_send")) is not None
        assert await s.scalar(select(Event).where(Event.type == "message.queued")) is not None
    t = (await client.get(f"/v1/threads/{body['thread_id']}", headers=org.headers)).json()
    assert t["message_count"] == 1 and t["participants"] == ["sales@supplier.ru", "x@y.ru"]
    assert [x["id"] for x in t["messages"]] == [body["id"]]


async def test_validation_errors(client, org):
    inbox = await _inbox(client, org)
    url = f"/v1/inboxes/{inbox['id']}/messages"
    assert (await client.post(url, headers=org.headers, json={"to": ["bad"], "text": "x"})).status_code == 422
    assert (await client.post(url, headers=org.headers, json={"to": ["a@b.ru"]})).status_code == 422
    r = await client.post(url, headers=org.headers, json={"to": ["a@b.ru"], "text": "x", "headers": {"Bcc": "z@z.ru"}})
    assert r.status_code == 422
    await client.post(f"/v1/inboxes/{inbox['id']}/disable", headers=org.headers)
    r = await client.post(url, headers=org.headers, json={"to": ["a@b.ru"], "text": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "inbox_disabled"


async def test_reply_and_forward_threading(client, org):
    inbox = await _inbox(client, org)
    first = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                               json={"to": ["sales@supplier.ru"], "subject": "Quote", "text": "hi"})).json()
    first_full = (await client.get(f"/v1/messages/{first['id']}", headers=org.headers)).json()
    rep = await client.post(f"/v1/messages/{first['id']}/reply", headers=org.headers, json={"text": "ping"})
    assert rep.status_code == 202 and rep.json()["thread_id"] == first["thread_id"]
    m = (await client.get(f"/v1/messages/{rep.json()['id']}", headers=org.headers)).json()
    assert m["subject"] == "Re: Quote" and m["in_reply_to"] == first_full["internet_message_id"]
    assert m["references"] == [first_full["internet_message_id"]]
    assert [a["email"] for a in m["to"]] == ["sales@supplier.ru"]
    fwd = await client.post(f"/v1/messages/{first['id']}/forward", headers=org.headers,
                            json={"to": ["acc@company.ru"], "text": "FYI"})
    assert fwd.status_code == 202 and fwd.json()["thread_id"] != first["thread_id"]
    fm = (await client.get(f"/v1/messages/{fwd.json()['id']}", headers=org.headers)).json()
    assert fm["subject"] == "Fwd: Quote" and "Forwarded message" in fm["text"]
    threads = (await client.get(f"/v1/inboxes/{inbox['id']}/threads", headers=org.headers)).json()["data"]
    assert len(threads) == 2


async def test_list_filters_and_long_poll(client, org):
    inbox = await _inbox(client, org)
    url = f"/v1/inboxes/{inbox['id']}/messages"
    r = await client.get(url, headers=org.headers, params={"direction": "inbound", "wait": 1})
    assert r.status_code == 200 and r.json()["data"] == []

    async def later():
        await asyncio.sleep(0.5)
        await client.post(url, headers=org.headers, json={"to": ["a@b.ru"], "subject": "s", "text": "x"})

    task = asyncio.create_task(later())
    r = await client.get(url, headers=org.headers, params={"direction": "outbound", "wait": 5})
    await task
    assert len(r.json()["data"]) == 1
    r = await client.get(url, headers=org.headers, params={"to": "a@b.ru"})
    assert len(r.json()["data"]) == 1
    r = await client.get(url, headers=org.headers, params={"from": "nobody@x.ru"})
    assert r.json()["data"] == []


async def test_tenant_isolation(client, org, make_org):
    inbox = await _inbox(client, org)
    m = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                           json={"to": ["a@b.ru"], "text": "x"})).json()
    other = await make_org("Other")
    assert (await client.get(f"/v1/messages/{m['id']}", headers=other.headers)).status_code == 404
    assert (await client.get(f"/v1/threads/{m['thread_id']}", headers=other.headers)).status_code == 404
    r = await client.post(f"/v1/messages/{m['id']}/reply", headers=other.headers, json={"text": "x"})
    assert r.status_code == 404
