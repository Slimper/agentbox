from agentbox.config import Settings
from agentbox.domains.dns import check_domain, expected_records


class FakeResolver:
    def __init__(self, txt=None, mx=None):
        self._txt, self._mx = txt or {}, mx or {}

    async def txt(self, name):
        return self._txt.get(name, [])

    async def mx(self, name):
        return self._mx.get(name, [])


SETTINGS = Settings(_env_file=None, mx_hostnames="mx1.agentbox.local,mx2.agentbox.local",
                    spf_include="spf.agentbox.local")


def test_expected_records():
    recs = expected_records("agents.company.ru", "tok", SETTINGS)
    assert recs[0] == {"type": "TXT", "name": "_agentbox.agents.company.ru", "value": "agentbox-verification=tok",
                       "purpose": "ownership", "required": True}
    assert [(r["type"], r["value"], r["priority"]) for r in recs if r["type"] == "MX"] == [
        ("MX", "mx1.agentbox.local", 10), ("MX", "mx2.agentbox.local", 20)]
    assert any(r["purpose"] == "dmarc" for r in recs) and not any(r["purpose"] == "dkim" for r in recs)


async def test_check_domain_all_ok_and_missing():
    r = FakeResolver(
        txt={"_agentbox.d.ru": ["agentbox-verification=tok"], "d.ru": ["v=spf1 include:spf.agentbox.local ~all"],
             "_dmarc.d.ru": ["v=DMARC1; p=none"]},
        mx={"d.ru": [(10, "mx1.agentbox.local"), (20, "mx2.agentbox.local")]},
    )
    assert await check_domain(r, "d.ru", "tok", SETTINGS) == {
        "ownership": "ok", "mx": "ok", "spf": "ok", "dmarc": "ok", "dkim": "skipped"}
    r = FakeResolver(txt={"d.ru": ["v=spf1 include:other ~all"]}, mx={"d.ru": [(10, "mail.other.ru")]})
    assert await check_domain(r, "d.ru", "tok", SETTINGS) == {
        "ownership": "missing", "mx": "wrong", "spf": "wrong", "dmarc": "missing", "dkim": "skipped"}
    r = FakeResolver(txt={"_agentbox.d.ru": ["agentbox-verification=tok"]},
                     mx={"d.ru": [(10, "mx1.agentbox.local"), (20, "mx.legacy.ru")]})
    assert (await check_domain(r, "d.ru", "tok", SETTINGS))["mx"] == "partial"
