FROM python:3.12-slim

WORKDIR /app

# Install protoc for proto generation at build time.
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy proto source and generate Python bindings so they're always in sync.
COPY ../engine/api/logistics.proto ./api/logistics.proto
RUN python -m grpc_tools.protoc \
    -I./api \
    --python_out=./api \
    --grpc_python_out=./api \
    ./api/logistics.proto \
 && touch ./api/__init__.py

COPY src/ ./src/
# Make the generated proto package importable from src/.
RUN ln -s /app/api /app/src/api

WORKDIR /app/src

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import grpc; ch=grpc.insecure_channel('unix:///app/sockets/logistics.sock'); ch.close()" || exit 1

CMD ["python", "main.py"]
