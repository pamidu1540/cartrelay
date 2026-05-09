# ── Stage 1: Generate proto + build binary ────────────────────────────────────
FROM golang:1.22-alpine AS builder

WORKDIR /app

# Install protoc and Go plugins so proto generation is reproducible in CI.
RUN apk add --no-cache protobuf protobuf-dev curl
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@latest \
 && go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest

COPY go.mod go.sum ./
RUN go mod download

COPY . .

# Regenerate the proto bindings from source so the build always matches the .proto.
RUN protoc \
    --go_out=. --go_opt=paths=source_relative \
    --go-grpc_out=. --go-grpc_opt=paths=source_relative \
    api/logistics.proto

# BUILD_VERSION is injected by docker buildx / the CI pipeline.
# Fallback to "unknown" so the binary still compiles in local builds.
ARG BUILD_VERSION=unknown
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-w -s -X main.Version=${BUILD_VERSION}" \
    -o engine_bin .

# ── Stage 2: Minimal runtime image ───────────────────────────────────────────
FROM alpine:3.20

# ca-certificates is needed for HTTPS calls to Mapbox.
RUN apk add --no-cache ca-certificates

WORKDIR /app

COPY --from=builder /app/engine_bin .
RUN mkdir -p /app/sockets /app/data && chmod +x ./engine_bin

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD test -S /app/sockets/logistics.sock || exit 1

CMD ["./engine_bin"]
