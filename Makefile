.DEFAULT_GOAL := help

.PHONY: help install test test-base lint format gate build clean tool version bump uat release status guard-clean

# Development keeps the [app] extra, which a bare `uv sync --locked` removes. Recipes stay
# literal: the crew gate reads `make test` and escalates any variable it cannot resolve.

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "%-10s %s\n", $$1, $$2}'

install: ## Sync the locked dev environment with the [app] extra
	uv sync --locked --extra app

test: ## Run the unittest suite
	uv run --locked --extra app python -m unittest discover -s tests -v

test-base: ## Run the suite in a throwaway env without the [app] extra
	uv run --locked --isolated python -m unittest discover -s tests

lint: ## Check lint and formatting without modifying files
	uv run --locked --extra app ruff check .
	uv run --locked --extra app ruff format --check .

format: ## Apply formatting and fix supported lint issues
	uv run --locked --extra app ruff check --fix .
	uv run --locked --extra app ruff format .

gate: ## Run the full pre-push gate (lint, format, tests)
	uv run --locked --extra app pre-commit run --all-files --hook-stage pre-push

build: ## Build sdist and wheel
	uv build

clean: ## Remove build artifacts and caches
	rm -rf dist build *.egg-info src/*.egg-info .ruff_cache .pytest_cache
	find . -type d -name '__pycache__' -exec rm -rf {} +

tool: ## Install captain as a uv tool from this checkout
	uv tool install --force .

version: ## Print the current package version
	@uv version --short

bump: ## Set package version, e.g. make bump VERSION=x.y.z (no commit)
	@if [ -z "$(VERSION)" ]; then echo "usage: make bump VERSION=x.y.z" >&2; exit 1; fi
	uv version $(VERSION)
	uv lock --offline

guard-clean:
	@git diff --quiet && git diff --cached --quiet || { echo "working tree is dirty" >&2; exit 1; }

uat: guard-clean ## Fast-forward push uat to the current branch
	git push origin HEAD:uat

release: guard-clean ## On main only: tag v<version> and push the tag
	@branch=$$(git rev-parse --abbrev-ref HEAD); \
	if [ "$$branch" != "main" ]; then \
		echo "release must run from main (on $$branch)" >&2; exit 1; \
	fi; \
	version=$$(uv version --short); \
	git tag "v$$version" && git push origin "v$$version"

status: ## Print latest published versions on PyPI and TestPyPI
	@echo "PyPI:     $$(curl -fsS https://pypi.org/pypi/captain-barbossa/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])')"
	@echo "TestPyPI: $$(curl -fsS https://test.pypi.org/pypi/captain-barbossa/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])')"
