// Smoke test against a live API: AGENTBOX_API_URL and AGENTBOX_API_KEY must be set.
import assert from "node:assert/strict";
import { AgentBox, AgentBoxError, verifyWebhookSignature } from "../dist/index.js";

const mail = new AgentBox({ apiKey: process.env.AGENTBOX_API_KEY, baseUrl: process.env.AGENTBOX_API_URL });
const me = await mail.me();
assert.ok(me.organization_id.startsWith("org_"));

const inbox = await mail.inboxes.create({ username: "ts-agent", metadata: { lang: "ts" } });
assert.equal(inbox.email, "ts-agent@agentbox.local");
const listed = await mail.inboxes.list({ metadata: { lang: "ts" } });
assert.equal(listed.data.length, 1);

const sent = await mail.messages.send(inbox.id, { to: ["vendor@example.com"], subject: "TS hello", text: "hi" }, "ts-idem-1");
const again = await mail.messages.send(inbox.id, { to: ["vendor@example.com"], subject: "TS hello", text: "hi" }, "ts-idem-1");
assert.equal(sent.id, again.id);
const full = await mail.messages.get(sent.id, true);
assert.equal(full.subject, "TS hello");
assert.ok(Array.isArray(full.headers));
const thread = await mail.threads.get(sent.thread_id);
assert.equal(thread.messages.length, 1);

const att = await mail.attachments.upload("note.txt", new TextEncoder().encode("hello attachment"), "text/plain");
const rep = await mail.messages.reply(sent.id, { text: "follow-up", attachment_ids: [att] });
assert.equal(rep.thread_id, sent.thread_id);
const bytes = await mail.attachments.download(att);
assert.equal(new TextDecoder().decode(bytes), "hello attachment");

const hook = await mail.webhooks.create({ url: "https://example.com/h", event_types: ["message.received"] });
assert.ok(hook.secret.startsWith("whsec_"));
const body = '{"id":"evt_1"}';
const ts = Math.floor(Date.now() / 1000);
// build a valid header with the same algorithm via the Python side convention: t.body HMAC
const enc = new TextEncoder();
const key = await crypto.subtle.importKey("raw", enc.encode(hook.secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(`${ts}.${body}`)));
const hex = Array.from(sig).map((b) => b.toString(16).padStart(2, "0")).join("");
assert.equal(await verifyWebhookSignature(hook.secret, `t=${ts},v1=${hex}`, body), true);
assert.equal(await verifyWebhookSignature("other", `t=${ts},v1=${hex}`, body), false);
await mail.webhooks.delete(hook.id);

const empty = await mail.messages.waitFor(inbox.id, { timeoutMs: 1500 });
assert.equal(empty, null);

try {
  await mail.inboxes.get("ibx_nope");
  assert.fail("expected error");
} catch (e) {
  assert.ok(e instanceof AgentBoxError);
  assert.equal(e.code, "not_found");
  assert.ok(e.requestId.startsWith("req_"));
}
console.log("ts sdk smoke ok");
