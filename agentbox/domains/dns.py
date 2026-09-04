from __future__ import annotations

import re
from typing import Protocol

from agentbox.config import Settings


class Resolver(Protocol):
    async def txt(self, name: str) -> list[str]: ...

    async def mx(self, name: str) -> list[tuple[int, str]]: ...


class DnsPythonResolver:
    def __init__(self, settings: Settings) -> None:
        import dns.asyncresolver

        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = settings.dns_timeout
        if settings.dns_nameservers:
            self._resolver.nameservers = [n.strip() for n in settings.dns_nameservers.split(",") if n.strip()]

    async def _query(self, name: str, rtype: str):
        import dns.exception
        import dns.resolver

        try:
            return await self._resolver.resolve(name, rtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
            return []

    async def txt(self, name: str) -> list[str]:
        answer = await self._query(name, "TXT")
        return ["".join(s.decode("utf-8", "replace") for s in r.strings) for r in answer]

    async def mx(self, name: str) -> list[tuple[int, str]]:
        answer = await self._query(name, "MX")
        return [(r.preference, str(r.exchange).rstrip(".").lower()) for r in answer]


def expected_records(domain: str, token: str, settings: Settings) -> list[dict]:
    mx_hosts = [h.strip() for h in settings.mx_hostnames.split(",") if h.strip()]
    records = [{"type": "TXT", "name": f"_agentbox.{domain}", "value": f"agentbox-verification={token}",
                "purpose": "ownership", "required": True}]
    for i, host in enumerate(mx_hosts):
        records.append({"type": "MX", "name": domain, "value": host, "priority": 10 * (i + 1),
                        "purpose": "inbound", "required": True})
    records.append({"type": "TXT", "name": domain, "value": f"v=spf1 include:{settings.spf_include} ~all",
                    "purpose": "spf", "required": False})
    if settings.dkim_selector and settings.dkim_public_key:
        records.append({"type": "TXT", "name": f"{settings.dkim_selector}._domainkey.{domain}",
                        "value": f"v=DKIM1; k=rsa; p={settings.dkim_public_key}", "purpose": "dkim",
                        "required": False})
    rua = f"; rua=mailto:{settings.dmarc_rua}" if settings.dmarc_rua else ""
    records.append({"type": "TXT", "name": f"_dmarc.{domain}", "value": f"v=DMARC1; p=none{rua}",
                    "purpose": "dmarc", "required": False})
    return records


_SPF = re.compile(r"^v=spf1\b", re.IGNORECASE)
_DMARC = re.compile(r"^v=DMARC1\b", re.IGNORECASE)
_DKIM = re.compile(r"^v=DKIM1\b", re.IGNORECASE)


async def check_domain(resolver: Resolver, domain: str, token: str, settings: Settings) -> dict:
    """Return per-record check results: ok | missing | wrong | skipped."""
    results: dict[str, str] = {}
    txts = await resolver.txt(f"_agentbox.{domain}")
    results["ownership"] = "ok" if f"agentbox-verification={token}" in txts else "missing"

    expected_mx = {h.strip().lower() for h in settings.mx_hostnames.split(",") if h.strip()}
    found_mx = {host for _, host in await resolver.mx(domain)}
    if not found_mx:
        results["mx"] = "missing"
    elif found_mx & expected_mx:
        results["mx"] = "ok" if found_mx <= expected_mx else "partial"
    else:
        results["mx"] = "wrong"

    root_txts = await resolver.txt(domain)
    spf = [t for t in root_txts if _SPF.match(t)]
    if not spf:
        results["spf"] = "missing"
    else:
        results["spf"] = "ok" if any(f"include:{settings.spf_include}" in t for t in spf) else "wrong"

    dmarc = await resolver.txt(f"_dmarc.{domain}")
    results["dmarc"] = "ok" if any(_DMARC.match(t) for t in dmarc) else "missing"

    if settings.dkim_selector and settings.dkim_public_key:
        dkim = await resolver.txt(f"{settings.dkim_selector}._domainkey.{domain}")
        results["dkim"] = "ok" if any(_DKIM.match(t) for t in dkim) else "missing"
    else:
        results["dkim"] = "skipped"
    return results
