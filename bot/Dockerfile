# Telegram bot container image.
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /uvx /bin/

RUN useradd -m -u 1001 bot
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
WORKDIR /app

RUN uv pip install --system --no-cache-dir \
    "python-telegram-bot[job-queue]>=22.7.0" \
    "grpcio>=1.80.0" \
    "protobuf>=6.31.1,<7.0.0"

COPY --chown=bot:bot bot/src/ /app/
USER bot
CMD ["python", "-m", "main"]
