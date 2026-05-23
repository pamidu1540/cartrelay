# engine/Dockerfile — multi-stage build (v5)
# Stage 1: generate protobuf Go bindings
# Stage 2: build the engine binary
# Stage 3: minimal runtime image

# ── Stage 1: proto codegen ────────────────────────────────────────────────────
FROM golang:1.25-bookworm AS proto-gen

RUN apt-get update && apt-get install -y --no-install-recommends \
        protobuf-compiler=3.21.* \
    && rm -rf /var/lib/apt/lists/*

RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.3 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1

WORKDIR /src
COPY engine/go.mod engine/go.sum ./
RUN go mod download

COPY engine/ .
RUN mkdir -p api/generated && \
    protoc \
      --go_out=api/generated --go_opt=paths=source_relative \
      --go-grpc_out=api/generated --go-grpc_opt=paths=source_relative \
      -I api \
      api/logistics.proto

# ── Stage 2: build ────────────────────────────────────────────────────────────
FROM golang:1.25-bookworm AS builder

WORKDIR /src
COPY --from=proto-gen /src/ .

ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
      -ldflags="-s -w -X main.Version=${VERSION}" \
      -o /engine \
      .

# ── Stage 3: runtime ──────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:latest AS runtime

# TLS certificates from host (mounted at runtime via volume or secret)
COPY --from=builder /engine /engine

EXPOSE 50051

ENTRYPOINT ["/engine"]
