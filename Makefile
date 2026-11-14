PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: help venv install dev test lint doctor mock serve console tools discover clean

help:
	@echo "make venv      create .venv"
	@echo "make install   editable install"
	@echo "make dev       install with test/lint tools"
	@echo "make test      run the offline test suite"
	@echo "make lint      ruff check"
	@echo "make doctor    check the MT5 link (waits for the EA)"
	@echo "make mock      doctor against synthetic data (no MT5)"
	@echo "make serve     run the MCP server on stdio"
	@echo "make discover  find the terminal's MQL5/Files folder (file backend)"
	@echo "make console   local web console on http://127.0.0.1:8790"
	@echo "make tools     list exposed MCP tools"

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip

install: venv
	$(PIP) install -e .

dev: venv
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	.venv/bin/ruff check src tests

doctor:
	.venv/bin/trade-mcp doctor --wait 30

mock:
	.venv/bin/trade-mcp --mock doctor

serve:
	.venv/bin/trade-mcp serve

console:
	.venv/bin/trade-mcp console

discover:
	.venv/bin/trade-mcp discover

tools:
	.venv/bin/trade-mcp tools

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ build dist *.egg-info
