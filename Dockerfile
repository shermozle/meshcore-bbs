FROM python:3.12-slim AS base

# tini gives us PID-1 signal forwarding for clean SIGTERM handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user. GID 20 is the standard dialout GID on most Debian
# images; the container should be run with --group-add for serial access.
RUN useradd -m -u 1000 bbs \
    && usermod -aG dialout bbs

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install runtime dependencies from the lockfile (no dev extras).
# Two-step: deps first for layer caching, then install the project.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev


# ── test stage ────────────────────────────────────────────────────────────────
FROM base AS test

COPY tests/ ./tests/
RUN uv sync --frozen --extra dev
RUN uv run pytest


# ── runtime stage ─────────────────────────────────────────────────────────────
FROM base AS runtime

USER bbs

# /data holds the SQLite DB, config.yaml, and logs.
VOLUME ["/data"]

# Health: 8080, optional metrics: 9090.
EXPOSE 8080 9090

ENV PYTHONUNBUFFERED=1 \
    BBS_CONFIG=/data/config.yaml \
    BBS_DB=/data/bbs.db \
    PATH="/app/.venv/bin:$PATH"

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bbs"]
