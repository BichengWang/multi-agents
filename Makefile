UV ?= uv
NODE ?= node
PYTEST_ARGS ?= -q
RUN = $(UV) run --locked --extra dev

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  setup        - Initial setup of the project"
	@echo "  install-uv   - Install uv package manager"
	@echo "  venv         - Create the project virtual environment"
	@echo "  install      - Install project dependencies"
	@echo "  install-dev  - Install development dependencies"
	@echo "  update-deps  - Update dependencies"
	@echo "  bootstrap    - Bootstrap the environment"
	@echo "  lock         - Refresh the dependency lockfile"
	@echo "  lint         - Run ruff on agent code and tests"
	@echo "  test         - Run lint and offline tests"
	@echo "  test-ci      - Run offline GitHub workflow and attribution tests (Node.js)"
	@echo "  check        - Run all offline quality checks"
	@echo "  test-workflows - Run offline multi-agent workflow tests (no API keys)"
	@echo "  clean        - Clean up generated files"
	@echo "Use FILE_NAME and TEST_NAME to select Python tests; PYTEST_ARGS defaults to -q."

.PHONY: setup
setup: install-uv
	$(MAKE) bootstrap

.PHONY: install-uv
install-uv:
	python3 -m pip install uv

.PHONY: venv
venv:
	$(UV) venv
	@echo "Virtual environment created. Please run: source .venv/bin/activate"

.PHONY: install install-dev bootstrap
install install-dev bootstrap:
	$(UV) sync --locked --extra dev

.PHONY: update-deps
update-deps:
	$(UV) lock --upgrade

.PHONY: lock
lock:
	$(UV) lock

.PHONY: compile
compile: lock

.PHONY: env
env: venv

.PHONY: lint
lint:
	$(RUN) ruff check agent tests

.PHONY: test
test: lint
	$(RUN) python -m pytest $(PYTEST_ARGS) "tests/$(FILE_NAME)" $(if $(TEST_NAME),-k "$(TEST_NAME)")

.PHONY: test-ci
test-ci:
	$(NODE) --test .github/scripts/*.test.cjs

.PHONY: check
check: test test-ci

.PHONY: test-workflows
test-workflows:
	$(RUN) python -m pytest tests/workflows $(PYTEST_ARGS)

.PHONY: all_test
all_test: test

.PHONY: clean
clean:
	@find agent tests -type d -name __pycache__ -prune -exec rm -rf {} +
	@rm -rf .pytest_cache .ruff_cache htmlcov coverage
	@rm -f .coverage .coverage.* coverage.xml junit.xml
