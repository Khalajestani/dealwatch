.PHONY: install dev test lint format backup assistant-token pi-ship pi-site pi-backups pi-status

install:
	uv sync

dev:
	uv run uvicorn dealwatch.web.app:create_app --factory --reload --port 8765

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

format:
	uv run ruff check --fix . && uv run ruff format .

backup:
	uv run dealwatch-backup

# Signs Claude Code in with your Claude plan in the browser and prints a one-year token for Dealwatch
# (docs/assistant.md). Uses the Claude Code that ships with the app.
assistant-token:
	@uv run python -c "import os, pathlib, claude_agent_sdk as sdk; c = str(pathlib.Path(sdk.__file__).parent / '_bundled' / 'claude'); os.execv(c, [c, 'setup-token'])"

# Raspberry Pi (docs/raspberry-pi.md). Reached over SSH as "pi-apps"; override with PI_HOST=.
pi-ship:
	deploy/ship.sh app

pi-site:
	deploy/ship.sh site

pi-backups:
	deploy/ship.sh backups

pi-status:
	deploy/ship.sh status
