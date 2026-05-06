FROM python:3.12-slim

# Copy the ultra-fast 'uv' package manager from the official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy the modern Python configuration file
COPY pyproject.toml .

# Use uv to install dependencies directly into the system Python.
# This replaces `pip install -r requirements.txt` and is up to 100x faster.
RUN uv pip install --system grpcio grpcio-tools python-telegram-bot[job-queue]

# Copy the source code directory
COPY src/ ./src/

WORKDIR /app/src

# Execute the Telegram interface
CMD ["python", "main.py"]