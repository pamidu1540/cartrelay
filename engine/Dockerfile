# Logistics engine container image.
FROM golang:1.25-bookworm AS builder

WORKDIR /src
COPY engine/go.mod engine/go.sum ./
RUN go mod download
COPY engine/ ./

ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w -X main.Version=${VERSION}" -o /engine .

FROM alpine:3.19 AS runtime
RUN apk add --no-cache ca-certificates
COPY --from=builder /engine /engine

ENTRYPOINT ["/engine"]
