.PHONY: help install dev lint format test test-fast study serve build docker clean

PY ?= python3
VENV ?= .venv

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the package (core only, zero runtime deps)
	$(PY) -m pip install .

dev: ## Editable install with API + test tooling
	$(PY) -m pip install -e ".[api,dev]"

lint: ## Static checks (ruff check + format check)
	ruff check .
	ruff format --check .

format: ## Auto-fix and format
	ruff check --fix .
	ruff format .

test: ## Full test suite (~20 s)
	$(PY) -m pytest

test-fast: ## Stop at first failure, show slowest tests
	$(PY) -m pytest -x --durations=10

study: ## Regenerate the full experiment ledger
	$(PY) -m seatlab.cli study --days 40 --sweep-days 16 --out examples/results.json

serve: ## Run the HTTP API on :8000
	$(PY) -m seatlab.cli serve --host 0.0.0.0 --port 8000

build: ## Build sdist + wheel
	$(PY) -m build

docker: ## Build the container image
	docker build -t seatlab:1.4.0 .

clean: ## Remove caches and build output
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
