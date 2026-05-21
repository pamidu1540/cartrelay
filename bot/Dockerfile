# bot/Dockerfile — multi-stage build (v5)

# ── Stage 1: proto codegen ────────────────────────────────────────────────────
FROM python:3.12-slim AS proto-gen

RUN pip install --no-cache-dir grpcio-tools==1.69.0 protobuf==5.29.3

WORKDIR /proto
COPY engine/api/logistics.proto ./logistics.proto
RUN python -m grpc_tools.protoc \
      -I. \
      --python_out=/out \
      --grpc_python_out=/out \
      logistics.proto && \
    mkdir -p /out/api && \
    mv /out/logistics_pb2*.py /out/api/ && \
    touch /out/api/__init__.py

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN useradd -m -u 1001 bot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN pip install --no-cache-dir \
        "python-telegram-bot[job-queue]==21.10" \
        "grpcio==1.69.0" \
        "protobuf==5.29.3" \
        "anyio==4.7.0" \
        "structlog==24.4.0"

COPY --from=proto-gen /out/api /app/api
COPY bot/src/ /app/

RUN mkdir -p /app/sockets && chown -R bot:bot /app

USER bot

CMD ["python", "-m", "main"]
