#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Google Jules VM Setup Script for Logistics Engine Core
# ==============================================================================
# This script prepares the short-lived Linux VM environment used by Google Jules
# to execute automated software development tasks, run tests, and perform static analysis.
# ==============================================================================

echo "=== [Jules Setup] Starting environment initialization ==="

# 1. Install system packages
echo "=== [Jules Setup] Installing system dependencies (protobuf-compiler, curl) ==="
if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends protobuf-compiler ca-certificates curl git
fi

# 2. Setup Go tools & Protobuf plugins
if command -v go &> /dev/null; then
    echo "=== [Jules Setup] Installing Go protobuf plugins and tooling ==="
    export GOPATH="${GOPATH:-$HOME/go}"
    export PATH="$GOPATH/bin:$PATH"

    go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
    go install golang.org/x/vuln/cmd/govulncheck@latest

    # go mod tidy for engine and supervisor
    echo "=== [Jules Setup] Running go mod tidy for Engine and Supervisor ==="
    (cd engine && go mod tidy)
    (cd supervisor && go mod tidy)

    # Generate Go gRPC bindings
    echo "=== [Jules Setup] Generating Go protobuf bindings ==="
    protoc --proto_path=engine/api \
        --go_out=engine/api --go_opt=paths=source_relative \
        --go-grpc_out=engine/api --go-grpc_opt=paths=source_relative \
        engine/api/logistics.proto
fi

# 3. Setup Python dependencies using uv
echo "=== [Jules Setup] Installing Astral uv & Python dependencies ==="
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if command -v uv &> /dev/null; then
    # Generate Python gRPC bindings
    echo "=== [Jules Setup] Generating Python protobuf bindings ==="
    mkdir -p bot/src/api
    uv pip install --system --quiet grpcio-tools==1.80.0 || true
    
    python3 -m grpc_tools.protoc \
        -Iengine/api \
        --python_out=bot/src/api \
        --grpc_python_out=bot/src/api \
        engine/api/logistics.proto || true

    touch bot/src/api/__init__.py
    if [ -f bot/src/api/logistics_pb2_grpc.py ]; then
        sed -i 's/^import logistics_pb2/from . import logistics_pb2/' bot/src/api/logistics_pb2_grpc.py
    fi

    # Install Python dev dependencies for bot
    echo "=== [Jules Setup] Syncing Python dependencies for Bot ==="
    (cd bot && uv pip install --system --quiet -e ".[dev]")
fi

echo "=== [Jules Setup] Environment setup complete! ==="
