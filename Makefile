project := python-notebook
pytest_args := -rs --tb short --junitxml junit.xml
pytest := py.test $(pytest_args)
file_name := ''
ifdef FILE_NAME
	file_name := $(FILE_NAME)
endif
ifdef TEST_NAME
	pytest_extra_args := -k "$(TEST_NAME)"
endif
server := sim-dev.dev
ifdef SERVER
	server := $(SERVER).dev
endif
ifdef VERSION
    version := $(VERSION)
endif
ifdef LOG_FILE
    log_file := $(LOG_FILE)
endif

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  setup        - Initial setup of the project"
	@echo "  install-uv   - Install uv package manager"
	@echo "  venv         - Create and activate virtual environment"
	@echo "  install      - Install project dependencies"
	@echo "  install-dev  - Install development dependencies"
	@echo "  train        - Run model training"
	@echo "  serve        - Start the FastAPI server"
	@echo "  update-deps  - Update dependencies"
	@echo "  bootstrap    - Bootstrap the environment"
	@echo "  lint         - Run ruff on agent code and tests"
	@echo "  test         - Run lint and offline tests"
	@echo "  test-workflows - Run offline multi-agent workflow tests (no API keys)"
	@echo "  clean        - Clean up generated files"

.PHONY: setup
setup: install-uv venv install

.PHONY: install-uv
install-uv:
	pip install uv

.PHONY: venv
venv:
	uv venv
	@echo "Virtual environment created. Please run: source .venv/bin/activate"

.PHONY: install
install:
	uv pip install -e '.[dev]'

.PHONY: install-dev
install-dev:
	uv pip install -e '.[dev]'

.PHONY: train
train:
	cd ml && uv run --extra train python trainer/train.py

.PHONY: serve
serve:
	cd ml && uv run --extra serve python server/serve.py

.PHONY: update-deps
update-deps:
	uv lock --upgrade

.PHONY: bootstrap
bootstrap:
	uv sync --extra dev

.PHONY: compile
compile:
	uv lock

.PHONY: env
env:
	python3 -m venv venv

.PHONY: lint
lint:
	ruff check agent tests

.PHONY: test
test: clean lint
	$(pytest) tests/$(file_name) $(pytest_extra_args)

.PHONY: test-workflows
test-workflows:
	python -m pytest tests/workflows -q

.PHONY: all_test
all_test: test

.PHONY: open_tunnels
open_tunnels:
	echo "Opening tunnels to $(server)"
# 	ssh -fN -L port:localhost:port $(server)

.PHONY: close_tunnels
close_tunnels:
	echo "Closing tunnels"
	kill $$(lsof -ti:port) 2> /dev/null &

.PHONY: clean
clean:
	@find . "(" -name "*.pyc" -o -name "coverage.xml" -o -name "junit.xml" ")" -delete
	@rm -rf coverage
