# Telegram bot container image.
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

RUN useradd -m -u 1001 bot
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY bot/pyproject.toml bot/uv.lock /app/
RUN uv sync --locked --no-dev --no-install-project --no-managed-python

COPY --chown=bot:bot bot/src/ /app/
USER bot
CMD ["python", "-m", "main"]
