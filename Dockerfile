# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies (cached unless lock file changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install the project itself
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---------------------------------------------------------------------------
FROM builder AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --dev

COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["/app/.venv/bin/pytest"]

# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

WORKDIR /app

# Copy the virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source and alembic
COPY --from=builder /app/src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
