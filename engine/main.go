package main

// main.go — logistics engine entrypoint

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

	pb "logistics-engine/api"
)

// Version represents the current build version of the engine.
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
	bgCtx, bgCancel := context.WithCancel(context.Background())

	go runEvery(bgCtx, 6*time.Hour, "cart-expiry", func() {
		if err := DBExpireCarts(context.Background(), 48); err != nil {
			slog.Warn("cart expiry failed", "err", err)
		}
	})
	go runEvery(bgCtx, 24*time.Hour, "birthday-rewards", func() {
		if err := IssueBirthdayRewards(context.Background()); err != nil {
			slog.Warn("birthday rewards failed", "err", err)
		}
	})
	go runEvery(bgCtx, 1*time.Hour, "low-stock-alerts", func() {
		if err := CheckLowStockAndAlert(context.Background()); err != nil {
			slog.Warn("low-stock alert check failed", "err", err)
		}
	})
	go runEvery(bgCtx, 15*time.Minute, "scheduled-maintenance", func() {
		if err := EnforceScheduledMaintenance(context.Background()); err != nil {
			slog.Warn("scheduled maintenance check failed", "err", err)
		}
	})

	// ── gRPC listener ────────────────────────────────────────────────────────
	// #nosec G301 -- socket directory must be traversable by other container users
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
	// #nosec G302 -- socket file must be read-writeable by other container users
	if err := os.Chmod(socketPath, 0o666); err != nil {
		slog.Error("chmod socket failed", "err", err)
		os.Exit(1)
	}

	lim := 512
	if v := os.Getenv("GRPC_MAX_CONNECTIONS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			lim = n
		}
	}
	listener := newLoggedLimitListener(netutil.LimitListener(raw, lim), lim)

	// ── gRPC server options ──────────────────────────────────────────────────
	chain := []grpc.UnaryServerInterceptor{
		RecoveryInterceptor,
		AuthInterceptor,
		RateLimitInterceptor,
	}

	srvOpts := []grpc.ServerOption{
		grpc.ChainUnaryInterceptor(chain...),
		grpc.MaxConcurrentStreams(256),
	}

	srv := grpc.NewServer(srvOpts...)
	pb.RegisterLogisticsEngineServer(srv, &Server{})

	// ── Graceful shutdown ────────────────────────────────────────────────────
	shutdown := func() {
		slog.Info("graceful shutdown initiated")
		bgCancel()
		srv.GracefulStop()
	}

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
	if len(t) <= 4 {
		return strings.Repeat("*", len(t))
	}
	// SEC: only show last 4 characters to avoid leaking secret material in logs
	return "…" + t[len(t)-4:]
}

func logLevel() slog.Level {
	if os.Getenv("LOG_LEVEL") == "debug" {
		return slog.LevelDebug
	}
	return slog.LevelInfo
}

func runEvery(ctx context.Context, d time.Duration, name string, fn func()) {
	ticker := time.NewTicker(d)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			slog.Debug("background task stopped", "task", name)
			return
		case <-ticker.C:
			slog.Debug("background task running", "task", name)
			fn()
		}
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
