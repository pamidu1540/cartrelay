# ==========================================
# Stage 1: Build the Go Binary
# ==========================================
FROM golang:1.22-alpine AS builder

WORKDIR /app

# Copy dependency manifests first to leverage Docker caching
COPY go.mod go.sum ./
RUN go mod download

# Copy the remaining source code
COPY . .

# Compile the binary
# CGO_ENABLED=0 ensures a standalone static binary without C dependencies
# -ldflags="-w -s" strips debugging symbols, heavily obfuscating your IP
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o engine_bin .

# ==========================================
# Stage 2: Minimal Production Container
# ==========================================
FROM alpine:latest

WORKDIR /app

# Copy ONLY the compiled binary from the builder stage
COPY --from=builder /app/engine_bin .

# Create the isolated directories for the SQLite DB and Unix Socket
RUN mkdir -p /app/sockets /app/data

# Ensure the binary is executable
RUN chmod +x ./engine_bin

# Execute the binary
CMD ["./engine_bin"]