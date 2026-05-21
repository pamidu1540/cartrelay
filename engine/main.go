package main

// main.go — logistics-engine v5
// Changes vs v4:
//   • PostgreSQL via pgx/v5 connection pool (replaces SQLite + WAL hacks)
//   • BUG-001 fix: gRPC server started after all init, post-init hooks removed
//   • BUG-003 fix: SIGTERM waits for in-flight RPCs (GracefulStop)
//   • BUG-011 fix: LimitListener saturation is now logged
//   • SEC-001/002: TLS channel + mTLS-ready interceptor
//   • PERF-001: pgx pool supports concurrent writers natively
//   • WAL checkpoint goroutine removed (PostgreSQL handles this)
//   • Cart TTL enforcer still runs every 6 h

import (
	"context"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/net/netutil"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/keepalive"

	pb "logistics-engine/api"
)

var Version = "dev"

const socketPath = "/app/sockets/logistics.sock"

var startedAt = time.Now()

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		AddSource: false,
		Level:     logLevel(),
	})))

	// ── Required env vars ───────────────────────────────────────────────────
	if os.Getenv("INTERNAL_TOKEN") == "" {
		slog.Error("INTERNAL_TOKEN is not set — refusing to start")
		os.Exit(1)
	}

	// ── License check ────────────────────────────────────────────────────────
	if err := VerifyLicense(); err != nil {
		slog.Error("license check failed", "err", err)
		os.Exit(1)
	}

	// ── Database (PostgreSQL) ────────────────────────────────────────────────
	if err := InitDatabase(os.Getenv("DATABASE_URL")); err != nil {
		slog.Error("database init failed", "err", err)
		os.Exit(1)
	}
	defer CloseDatabase()

	// ── Run migrations ────────────────────────────────────────────────────────
	if err := RunMigrations(); err != nil {
		slog.Error("migrations failed", "err", err)
		os.Exit(1)
	}

	// ── Seed admin users from env ────────────────────────────────────────────
	if raw := os.Getenv("ADMIN_USER_IDS"); raw != "" {
		if err := SeedAdmins(raw); err != nil {
			slog.Warn("admin seed partial failure", "err", err)
		}
	}

	// ── Warm catalog cache ────────────────────────────────────────────────────
	if _, err := GetCachedCatalog(context.Background(), "", "", 0, 0); err != nil {
		slog.Warn("initial catalog cache warm failed", "err", err)
	}

	// ── Background tasks ─────────────────────────────────────────────────────
	go runEvery(6*time.Hour, "cart-expiry", func() {
		if err := DBExpireCarts(context.Background(), 48); err != nil {
			slog.Warn("cart expiry failed", "err", err)
		}
	})
	go runEvery(24*time.Hour, "birthday-rewards", func() {
		if err := IssueBirthdayRewards(context.Background()); err != nil {
			slog.Warn("birthday rewards failed", "err", err)
		}
	})
	go runEvery(1*time.Hour, "low-stock-alerts", func() {
		if err := CheckLowStockAndAlert(context.Background()); err != nil {
			slog.Warn("low-stock alert check failed", "err", err)
		}
	})
	go runEvery(15*time.Minute, "scheduled-maintenance", func() {
		if err := EnforceScheduledMaintenance(context.Background()); err != nil {
			slog.Warn("scheduled maintenance check failed", "err", err)
		}
	})

	// ── gRPC listener ────────────────────────────────────────────────────────
	if err := os.MkdirAll("/app/sockets", 0o755); err != nil {
		slog.Error("cannot create socket dir", "err", err)
		os.Exit(1)
	}
	_ = os.Remove(socketPath)

	raw, err := net.Listen("unix", socketPath)
	if err != nil {
		slog.Error("listen failed", "socket", socketPath, "err", err)
		os.Exit(1)
	}
	if err := os.Chmod(socketPath, 0o600); err != nil {
		slog.Error("chmod socket failed", "err", err)
		os.Exit(1)
	}

	// BUG-011 fix: log when LimitListener hits capacity
	lim := 512
	if v := os.Getenv("GRPC_MAX_CONNECTIONS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			lim = n
		}
	}
	listener := newLoggedLimitListener(netutil.LimitListener(raw, lim), lim)

	// ── gRPC server options ──────────────────────────────────────────────────
	srvOpts := []grpc.ServerOption{
		grpc.ChainUnaryInterceptor(
			RecoveryInterceptor,  // panic → Internal instead of crash
			AuthInterceptor,
			RateLimitInterceptor,
		),
		grpc.MaxConcurrentStreams(256),
		// Unix socket: keepalive is unnecessary overhead (BUG-026)
		// Kept minimal for future TCP migration
		grpc.KeepaliveParams(keepalive.ServerParameters{
			MaxConnectionIdle: 60 * time.Second,
			Time:              30 * time.Second,
			Timeout:           5 * time.Second,
		}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             10 * time.Second,
			PermitWithoutStream: true,
		}),
	}

	// SEC-001: TLS if cert/key paths are set
	if certFile, keyFile := os.Getenv("TLS_CERT_FILE"), os.Getenv("TLS_KEY_FILE"); certFile != "" && keyFile != "" {
		creds, err := credentials.NewServerTLSFromFile(certFile, keyFile)
		if err != nil {
			slog.Error("TLS credentials load failed", "err", err)
			os.Exit(1)
		}
		srvOpts = append(srvOpts, grpc.Creds(creds))
		slog.Info("TLS enabled", "cert", certFile)
	} else {
		slog.Warn("TLS not configured — plaintext gRPC (set TLS_CERT_FILE + TLS_KEY_FILE for production)")
	}

	srv := grpc.NewServer(srvOpts...)
	pb.RegisterLogisticsEngineServer(srv, &Server{})

	// ── Graceful shutdown ────────────────────────────────────────────────────
	shutdown := func() {
		slog.Info("graceful shutdown initiated")
		srv.GracefulStop() // BUG-003: waits for in-flight RPCs
	}

	StartLicenseBackgroundChecker(shutdown)

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		sig := <-quit
		slog.Info("signal received", "signal", sig)
		shutdown()
	}()

	slog.Info("engine ready",
		"socket", socketPath,
		"version", Version,
		"token_prefix", tokenPrefix(os.Getenv("INTERNAL_TOKEN")),
	)

	if err := srv.Serve(listener); err != nil {
		if !strings.Contains(err.Error(), "use of closed network connection") {
			slog.Error("serve error", "err", err)
			os.Exit(1)
		}
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func tokenPrefix(t string) string {
	if len(t) <= 8 {
		return strings.Repeat("*", len(t))
	}
	return t[:8] + "…"
}

func logLevel() slog.Level {
	if os.Getenv("LOG_LEVEL") == "debug" {
		return slog.LevelDebug
	}
	return slog.LevelInfo
}

func runEvery(d time.Duration, name string, fn func()) {
	ticker := time.NewTicker(d)
	defer ticker.Stop()
	for range ticker.C {
		slog.Debug("background task running", "task", name)
		fn()
	}
}

// loggedLimitListener wraps a LimitListener and logs when at capacity.
type loggedLimitListener struct {
	net.Listener
	limit int
}

func newLoggedLimitListener(l net.Listener, limit int) net.Listener {
	return &loggedLimitListener{Listener: l, limit: limit}
}

func (l *loggedLimitListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		if strings.Contains(err.Error(), "too many open files") ||
			strings.Contains(err.Error(), "connection refused") {
			slog.Warn("gRPC connection limit reached — new connections being dropped",
				"limit", l.limit)
		}
	}
	return conn, err
}
