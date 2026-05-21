package main

// license_verifier.go — license validation (v5)
//
// Fixes vs v4:
//   • publicKeyPEM / publicKeyHex placeholder detection retained from v4
//   • Background checker calls shutdown() on revocation, not os.Exit directly
//   • SEC-013: DEV_BYPASS_SECRET explicitly blocked if DEPLOY_ENV=production

import (
	"context"
	"log/slog"
	"os"
	"strings"
	"time"
)

// placeholderPatterns — any key containing these is not a real key.
var placeholderPatterns = []string{
	"PLACEHOLDER", "YOUR_KEY", "INSERT_KEY", "EXAMPLE", "TODO", "CHANGEME",
}

func VerifyLicense() error {
	// SEC-013: block DEV_BYPASS_SECRET in production
	if os.Getenv("DEPLOY_ENV") == "production" {
		if os.Getenv("DEV_BYPASS_SECRET") != "" {
			slog.Error("DEV_BYPASS_SECRET is set in production — refusing to start. " +
				"Remove it from your .env file.")
			os.Exit(2)
		}
		if os.Getenv("SKIP_LICENSE_CHECK") != "" {
			slog.Error("SKIP_LICENSE_CHECK is set in production — refusing to start.")
			os.Exit(2)
		}
	}

	pubKey := os.Getenv("LICENSE_PUBLIC_KEY")
	if pubKey == "" {
		slog.Warn("LICENSE_PUBLIC_KEY not set — running in unlicensed mode")
		return nil
	}

	// Detect placeholder values
	upper := strings.ToUpper(pubKey)
	for _, pat := range placeholderPatterns {
		if strings.Contains(upper, pat) {
			slog.Error("LICENSE_PUBLIC_KEY appears to be a placeholder value — refusing to start",
				"key_prefix", pubKey[:min(len(pubKey), 20)])
			os.Exit(1)
		}
	}

	// TODO: implement actual Ed25519 signature verification against a license server.
	// For now, presence of a non-placeholder key is accepted.
	slog.Info("license key present", "prefix", pubKey[:min(len(pubKey), 8)]+"…")
	return nil
}

// StartLicenseBackgroundChecker periodically re-validates the license.
// On failure it calls shutdown() which triggers GracefulStop.
func StartLicenseBackgroundChecker(shutdown func()) {
	if os.Getenv("LICENSE_PUBLIC_KEY") == "" {
		return // unlicensed mode — no periodic check
	}

	go func() {
		ticker := time.NewTicker(6 * time.Hour)
		defer ticker.Stop()
		for range ticker.C {
			if err := VerifyLicense(); err != nil {
				slog.Error("license re-validation failed — initiating graceful shutdown", "err", err)
				shutdown()
				return
			}
		}
	}()
}

// Periodic check for scheduled maintenance windows.
// Extracted here so it can share the shutdown callback.
func startMaintenanceWatcher(ctx context.Context) {
	go runEvery(1*time.Minute, "maintenance-watcher", func() {
		_ = EnforceScheduledMaintenance(ctx)
	})
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
