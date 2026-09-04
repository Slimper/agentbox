# Self-hosting AgentBox

One VM (2 vCPU / 4 GB is enough to start) with Docker, a public IPv4 and a domain. Below `DOMAIN=example.com`.

## 1. DNS

| Record | Name | Value | Purpose |
|---|---|---|---|
| A | `mail.example.com` | VM IP | console + API |
| A | `mx.example.com` | VM IP | inbound SMTP edge |
| MX | `example.com` | `10 mx.example.com` | mail for managed inboxes (`agent@example.com`) |
| TXT | `example.com` | `v=spf1 include:<your relay's SPF> ~all` | outbound authentication |
| TXT | `_dmarc.example.com` | `v=DMARC1; p=none; rua=mailto:dmarc@example.com` | reporting |
| PTR | VM IP | `mx.example.com` | reverse DNS (ask the hosting provider) |

DKIM is signed by the outbound relay (Unisender Go / SendGrid / Mailgun / your own Postfix); publish the record they
give you. Custom domains of your users point their MX at `mx.example.com`; the console shows the exact records.
Inbound port 25 must be open on the VM; with a relay you do not need outbound 25.

## 2. Run

    cp .env.example .env            # database, MinIO, relay credentials, AGENTBOX_MANAGED_DOMAIN=example.com
    uv run agentbox keygen          # → AGENTBOX_APP_SECRET_KEY
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox migrate
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox org create "My Org"
    docker compose -f deploy/docker/docker-compose.yml --profile app up -d

Put a TLS terminator (Caddy, nginx, Traefik) in front of port 8000 for `mail.example.com`; the SMTP edge listens on
2525 (map host port 25 to it). Set `AGENTBOX_PUBLIC_BASE_URL=https://mail.example.com` and
`AGENTBOX_S3_PUBLIC_ENDPOINT` to the URL your users can reach for attachment downloads.

## 3. Operate

- `agentbox org list`, `agentbox org create`, `agentbox keygen`, `agentbox migrate`, `agentbox worker`, `agentbox smtp`.
- Backups: dump Postgres daily (`pg_dump`), snapshot or sync the MinIO volume.
- Logs are JSON (structlog); `/healthz` and `/readyz` for probes.
- Upgrades: `git pull && docker compose ... build && run --rm api agentbox migrate && up -d`.
