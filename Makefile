PY ?= python3.11
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help venv install lint format typecheck security test check run secrets \
        docker-build up down logs backup pwa-build tma-build

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "%-14s %s\n", $$1, $$2}'

venv: ## Create virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install dev dependencies
	$(BIN)/pip install -r requirements-dev.txt
	$(BIN)/pre-commit install

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-format
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck: ## mypy strict
	$(BIN)/mypy .

security: ## secret scan + bandit + dependency audit
	./scripts/secret_scan.sh
	$(BIN)/bandit -r . -c pyproject.toml -q
	$(BIN)/pip-audit -r requirements.txt

test: ## pytest
	$(BIN)/pytest -q

check: lint typecheck security test ## Everything CI runs

run: ## Run locally with .env
	$(BIN)/python main.py

secrets: ## Generate ./secrets/*.txt (prompts for external tokens)
	./scripts/init_secrets.sh

docker-build: ## Build images
	docker compose build

up: ## Start stack
	docker compose up -d

down: ## Stop stack
	docker compose down

logs: ## Follow bot logs
	docker compose logs -f bot

backup: ## Encrypted SQLite backup
	./scripts/backup.sh

pwa-build: ## Build PWA
	cd pwa && npm ci && npm run build

tma-build: ## Build Telegram Mini App
	cd tma && npm ci && npm run build
