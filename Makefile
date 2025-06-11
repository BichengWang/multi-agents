.PHONY: setup train serve install-dev update-deps help

.DEFAULT_GOAL := help

help:
	@echo "Available commands:"
	@echo "  make setup       - Initial project setup"
	@echo "  make train       - Train the model"
	@echo "  make serve       - Start the FastAPI server"
	@echo "  make install-dev - Install development dependencies"
	@echo "  make update-deps - Update dependencies"

bootstrap:
	@echo "Installing uv..."
	pip install uv
	@echo "Detecting OS and installing direnv..."
	@if [ "$$(uname)" = "Darwin" ]; then \
		echo "Detected macOS. Installing direnv with brew..."; \
		brew install direnv; \
	elif [ "$$(uname)" = "Linux" ]; then \
		echo "Detected Linux. Installing direnv with apt (requires sudo)..."; \
		sudo apt-get update && sudo apt-get install -y direnv; \
	elif [ "$$(uname -o 2>/dev/null)" = "Msys" ] || [ "$$(uname -o 2>/dev/null)" = "Cygwin" ] || [ "$$(uname -o 2>/dev/null)" = "Windows_NT" ]; then \
		echo "Detected Windows. Please install direnv manually from https://direnv.net/docs/installation.html"; \
	else \
		echo "Unknown OS. Please install direnv manually from https://direnv.net/docs/installation.html"; \
	fi
	@echo "Creating virtual environment..."
	python3.12 -m venv .venv
	@echo "Bootstrap complete! Activate the virtual environment with: source .venv/bin/activate"

reload:
	direnv allow
	direnv reload

install:
	@echo "Installing dependencies..."
	. .venv/bin/activate && uv pip install -e '.[train,serve]'
	@echo "Dependencies installed!"

setup: bootstrap reload install
	@echo "Setup complete! Activate the virtual environment with: source .venv/bin/activate"

train:
	cd trainer && python train.py

serve:
	cd server && python serve.py

install-dev:
	uv pip install -e .[dev]

update-deps:
	uv pip compile pyproject.toml -o requirements.txt
