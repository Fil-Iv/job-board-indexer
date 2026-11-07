COMPOSE ?= docker compose
RUN     := $(COMPOSE) run --rm app

.PHONY: help up down logs migrate test test-live lint format crawl coverage parser-fingerprint shell

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

up: .env  ## Build and start postgres + app (waits until healthy)
	$(COMPOSE) up -d --build --wait

down:  ## Stop containers (keeps the database volume)
	$(COMPOSE) down

logs:  ## Follow app logs
	$(COMPOSE) logs -f app

migrate:  ## Apply Alembic migrations to the main database
	$(RUN) alembic upgrade head

test:  ## Run the test suite (uses the jobindex_test database)
	$(RUN) pytest $(ARGS)

test-live:  ## Run live tests against real employer job boards (network)
	$(RUN) pytest -m live $(ARGS)

lint:  ## ruff check + ruff format --check + mypy
	$(RUN) sh -c "ruff check . && ruff format --check . && mypy"

format:  ## Auto-fix lint issues and format code
	$(RUN) sh -c "ruff check --fix . && ruff format ."

crawl:  ## Crawl one source (make crawl SOURCE=<id>) or all active sources (make crawl)
	$(RUN) python -m jobindex.cli crawl $(if $(SOURCE),$(SOURCE),--all)

coverage:  ## Live-crawl done platforms' examples and regenerate docs/COVERAGE.md
	$(RUN) python scripts/coverage_report.py $(ARGS)

parser-fingerprint:  ## Record normalize.py behaviour after bumping PARSER_VERSION
	$(RUN) python -m tests.parser_corpus --write

shell:  ## Open a shell in the app container
	$(RUN) bash
