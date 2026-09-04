COMPOSE=docker compose -f deploy/docker/docker-compose.yml

.PHONY: up down logs migrate test test-integration e2e lint

up:
	$(COMPOSE) up -d postgres minio minio-init mailpit

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f

migrate:
	uv run agentbox migrate

lint:
	uv run ruff check .

test:
	uv run pytest tests/unit -q

test-integration:
	uv run pytest -m integration tests/integration -q

e2e:
	uv run pytest -m e2e tests/e2e -q
