VENV := venv
PYTHON := python3
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install run dry-run dashboard clean

help:
	@echo "Available commands:"
	@echo "  make venv        Create virtual environment"
	@echo "  make install     Install dependencies"
	@echo "  make run         Run main.py"
	@echo "  make dry-run     Run main.py in dry-run mode"
	@echo "  make dashboard   Run dashboard UI on port 8080"
	@echo "  make clean       Remove virtual environment"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run: install
	$(PY) main.py

dry-run: install
	$(PY) main.py --dry-run

dashboard: install
	$(PY) dashboard/server.py --port 8080

clean:
	rm -rf $(VENV)