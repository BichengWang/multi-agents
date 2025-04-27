project := python-notebook
pytest_args := -rs --tb short --junitxml junit.xml --suppress-no-test-exit-code
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

.DEFAULT_GOAL := test


.PHONY: bootstrap
bootstrap:
	pip3 install -U pip>=25.0.1 setuptools>=75.8.0 wheel pip-tools>=44.0.0
	pip3 install --no-deps -r requirements.txt --no-cache-dir
	pip3 install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu123

.PHONY: compile
compile:
	pip-compile requirements.in

.PHONY: env
env:
	python3 -m venv venv

.PHONY: test
test: clean lint
	SIM_TEST_MODE=true $(pytest) -k "not personal_transport_e2e" tests/$(file_name) $(pytest_extra_args)

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
