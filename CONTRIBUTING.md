# Contributing to AgentBox

Thanks for helping build email infrastructure for AI agents.

## Layout and licensing

- This repository is the open-source core, Apache-2.0: API, SMTP edge, domains, policies, SDKs, MCP, console.
- AgentBox Cloud (sign-up, teams, billing, SSO, connectors) is a separate, closed package that plugs into the core
  through `agentbox/extensions.py`. Keep the core usable without it; new hook points are welcome when they are generic.
- A rule we keep: features that are already open never move behind a paywall.

## Development

    make up && uv sync --extra dev && uv run agentbox migrate
    make test              # unit
    make test-integration  # needs the compose infra
    make e2e

Run `uv run ruff check .` before committing. Every behaviour change needs a test: unit for pure logic, integration
for anything touching Postgres, MinIO, SMTP or the job queue.

## Pull requests

- One topic per PR, conventional commit title (`feat(domains): ...`, `fix(inbound): ...`).
- Migrations: `uv run alembic revision --autogenerate -m "..."`; NOT NULL columns need a `server_default`, unique
  constraints need names, and the downgrade must work (`alembic downgrade base` runs in CI).
- Public API changes: update `README.md` and the SDKs (`sdk/python`, `sdk/typescript`).

## Security

Report vulnerabilities privately to security@agentbox.ru. Do not open public issues for exploitable bugs.
