package main

// main.go — updated from original: adds LICENSE_KEY / TOKEN_SECRET / LICENSE_API_URL
// validation and wires StartLicenseBackgroundChecker into graceful shutdown.

import (
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/net/netutil"
	"google.golang.org/grpc"
	"google.golang.org/grpc/keepalive"

	pb "logistics-engine/api"
)

var Version = "dev"

const socketPath = "/app/sockets/logistics.sock"

var mapboxToken string

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	if os.Getenv("INTERNAL_TOKEN") == "" {
		slog.Error("INTERNAL_TOKEN is not set — refusing to start")
		os.Exit(1)
	}

	mapboxToken = os.Getenv("MAPBOX_TOKEN")
	if mapboxToken == "" {
		slog.Warn("MAPBOX_TOKEN not set — Haversine fallback active")
	}

	// License check — reads cache or calls /v1/activate
	if err := VerifyLicense(); err != nil {
		slog.Error("license check failed", "err", err)
		os.Exit(1)
	}

	if err := InitDatabase("/app/data/logistics.db"); err != nil {
		slog.Error("database init failed", "err", err)
		os.Exit(1)
	}

	if _, err := GetCachedCatalog(); err != nil {
		slog.Warn("initial catalog cache warm failed", "err", err)
	}

	go func() {
		for range time.Tick(6 * time.Hour) {
			if err := DBExpireCarts(48); err != nil {
				slog.Warn("cart expiry failed", "err", err)
			}
		}
	}()

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

	listener := netutil.LimitListener(raw, 512)

	srv := grpc.NewServer(
		grpc.UnaryInterceptor(AuthInterceptor),
		grpc.MaxConcurrentStreams(256),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			MaxConnectionIdle: 30 * time.Second,
			Time:              10 * time.Second,
			Timeout:           3 * time.Second,
		}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             5 * time.Second,
			PermitWithoutStream: true,
		}),
	)
	pb.RegisterLogisticsEngineServer(srv, &Server{})

	// shutdown is shared between signal handler and license background checker
	shutdown := func() {
		slog.Info("graceful shutdown initiated")
		srv.GracefulStop()
	}

	// Background checker: re-validates every 12 h, shuts down on 403/402
	StartLicenseBackgroundChecker(shutdown)

	go func() {
		quit := make(chan os.Signal, 1)
		signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)
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

func tokenPrefix(t string) string {
	if len(t) <= 8 {
		return strings.Repeat("*", len(t))
	}
	return t[:8] + "…"
}
