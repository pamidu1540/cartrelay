package main

// license_verifier.go — license validation

import (
	"log/slog"
	"os"
	"strings"
	"time"
)

// placeholderPatterns — any key containing these is not a real key.
var placeholderPatterns = []string{
	"PLACEHOLDER", "YOUR_KEY", "INSERT_KEY", "EXAMPLE", "TODO", "CHANGEME",
}

// VerifyLicense checks if a valid license key is configured.
func VerifyLicense() error {
	// Block dev secrets in production
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
	// Presence of a non-placeholder key is accepted for now.
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
