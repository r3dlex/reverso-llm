.PHONY: install sync proxy smoke test lint fmt

# ── Setup ──────────────────────────────────────────────────────────────────────

install:  ## Install dependencies and create venv via uv
	uv sync

# ── Run ───────────────────────────────────────────────────────────────────────

proxy:  ## Start the LiteLLM proxy in the foreground (dev mode)
	uv run reverso-proxy

# ── Tests ─────────────────────────────────────────────────────────────────────

smoke:  ## Run smoke tests against a running proxy (127.0.0.1:64946)
	bash scripts/smoke.sh

test:  ## Run unit tests
	uv run pytest tests/ -v

# ── Code quality ──────────────────────────────────────────────────────────────

lint:  ## Lint with ruff
	uv run ruff check src/ tests/

fmt:  ## Format with ruff
	uv run ruff format src/ tests/

# ── launchd ───────────────────────────────────────────────────────────────────

install-agents:  ## Install launchd LaunchAgents
	bash scripts/install-launchagents.sh

uninstall-agents:  ## Uninstall launchd LaunchAgents
	bash scripts/uninstall-launchagents.sh

# ── Help ──────────────────────────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	    awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
