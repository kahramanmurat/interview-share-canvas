SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
BACKEND_PORT ?= 8000
FRONTEND_PORT ?= 8081
PYTHON ?= python3

.PHONY: help setup install backend frontend run dev test check

help:
	@echo "Interview Share Canvas"
	@echo ""
	@echo "  make setup      Install backend dependencies with uv"
	@echo "  make run        Run backend and frontend together"
	@echo "  make backend    Run only the FastAPI backend"
	@echo "  make frontend   Run only the frontend static server"
	@echo "  make test       Run the backend tests"
	@echo "  make check      Verify the lockfile and run tests"
	@echo ""
	@echo "Defaults: backend http://$(HOST):$(BACKEND_PORT), frontend http://$(HOST):$(FRONTEND_PORT)"

setup:
	uv sync

install: setup

backend:
	uv run uvicorn backend.main:app --reload --host $(HOST) --port $(BACKEND_PORT)

frontend:
	cd frontend && $(PYTHON) -m http.server $(FRONTEND_PORT) --bind $(HOST)

run:
	@set -e; \
	(cd frontend && exec $(PYTHON) -m http.server $(FRONTEND_PORT) --bind $(HOST)) & frontend_pid=$$!; \
	cleanup() { \
		kill $$frontend_pid 2>/dev/null || true; \
		wait $$frontend_pid 2>/dev/null || true; \
	}; \
	trap cleanup INT TERM EXIT; \
	echo "Frontend: http://$(HOST):$(FRONTEND_PORT)/interview-platform.dc.html"; \
	echo "API docs: http://$(HOST):$(BACKEND_PORT)/docs"; \
	uv run uvicorn backend.main:app --reload --host $(HOST) --port $(BACKEND_PORT)

dev: run

test:
	uv run pytest

check:
	uv lock --check
	uv run pytest
