COMPOSE ?= docker compose
RUN     := $(COMPOSE) run --rm app
PROD    := docker compose -f docker-compose.prod.yml --env-file .env.prod

.PHONY: help up down logs migrate test test-live lint format seed-demo crawl dedup dedup-report fill-rates coverage parser-fingerprint shell \
        prod-up prod-down prod-logs prod-backup

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

seed-demo:  ## Register the example boards of every done platform (coverage/platforms.yaml)
	$(RUN) python -m jobindex.cli seed-demo $(if $(PLATFORM),--platform $(PLATFORM),)

crawl:  ## Crawl one source (make crawl SOURCE=<id>) or all active sources (make crawl)
	$(RUN) python -m jobindex.cli crawl $(if $(SOURCE),$(SOURCE),--all)

dedup:  ## Recompute cross-source duplicates
	$(RUN) python -m jobindex.cli dedup

dedup-report:  ## Merged openings + near misses / false-merge checks (Markdown)
	$(RUN) python scripts/dedup_report.py $(ARGS)

fill-rates:  ## Field fill rates of open postings per platform (ARGS=--markdown)
	$(RUN) python -m jobindex.cli fill-rates $(ARGS)

coverage:  ## Live-crawl done platforms' examples and regenerate docs/COVERAGE.md
	$(RUN) python scripts/coverage_report.py $(ARGS)

parser-fingerprint:  ## Record normalize.py behaviour after bumping PARSER_VERSION
	$(RUN) python -m tests.parser_corpus --write

shell:  ## Open a shell in the app container
	$(RUN) bash

# --- production (docker-compose.prod.yml, see docs/DEPLOY.md) -----------------

prod-up:  ## Build and start the production stack (needs .env.prod)
	$(PROD) up -d --build --wait

prod-down:  ## Stop the production stack (keeps volumes and backups)
	$(PROD) down

prod-logs:  ## Follow production app + worker logs
	$(PROD) logs -f --tail=100 app worker

prod-backup:  ## Make a database backup now (into BACKUP_DIR)
	$(PROD) exec backup /scripts/backup.sh now
