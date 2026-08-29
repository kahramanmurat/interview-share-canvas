SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
PORT ?= 8091

.PHONY: help setup install backend frontend run dev test e2e check

help:
	@echo "Interview Share Canvas"
	@echo ""
	@echo "  make setup      Install backend dependencies with uv"
	@echo "  make run        Run the API and frontend together"
	@echo "  make backend    Alias for make run"
	@echo "  make frontend   Alias for make run"
	@echo "  make test       Run the backend tests"
	@echo "  make e2e        Run the Playwright collaboration test"
	@echo "  make check      Verify the lockfile and run tests"
	@echo ""
	@echo "App and API docs: http://$(HOST):$(PORT)"

setup:
	uv sync

install: setup

backend:
	uv run uvicorn backend.main:app --reload --host $(HOST) --port $(PORT)

frontend: backend

run: backend

dev: run

test:
	uv run pytest

e2e:
	npm --prefix e2e ci
	npm --prefix e2e run install:browsers
	npm --prefix e2e test

check:
	uv lock --check
	uv run pytest
