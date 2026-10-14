# syntax=docker/dockerfile:1
# Built on the Mac for the Raspberry Pi (deploy/ship.sh): both are arm64, so no emulation is needed.

# Build stage: uv installs the locked dependencies and the app into /app/.venv.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first so code changes don't invalidate this layer. The Agent SDK brings Claude Code for arm64.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
# No cache here: uv keys its build cache for local projects on pyproject.toml, so it would reuse stale code.
RUN uv sync --frozen --no-dev --no-editable --no-cache


# Run stage: the same Python without the build tools.
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv

# Run as an ordinary user. Only the folders that hold data are writable; uid 1000 matches the first user on
# Raspberry Pi OS, so mounted folders need no special ownership. The chown comes after the copy, which resets it.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p data backups \
    && chown app:app data backups
USER app

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
VOLUME ["/app/data", "/app/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# One worker: SQLite has a single writer, and one process holds the schedule.
CMD ["uvicorn", "dealwatch.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
