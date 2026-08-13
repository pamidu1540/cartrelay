package main

// license_verifier.go — license validation

import (
	"fmt"
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
			return fmt.Errorf("refusing to start in production with bypass secrets")
		}
		if os.Getenv("SKIP_LICENSE_CHECK") != "" {
			slog.Error("SKIP_LICENSE_CHECK is set in production — refusing to start.")
			return fmt.Errorf("refusing to start in production with bypass secrets")
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
			return fmt.Errorf("LICENSE_PUBLIC_KEY appears to be a placeholder value")
		}
	}

	// TODO: implement actual Ed25519 signature verification against a license server.
	// Presence of a non-placeholder key is accepted for now.
	slog.Info("license key present", "prefix", pubKey[:min(len(pubKey), 8)]+"…")
	return nil
}

// LicenseCheckInterval is the interval for the background license checker. It is a variable to allow mocking in tests.
var LicenseCheckInterval = 6 * time.Hour

// StartLicenseBackgroundChecker periodically re-validates the license.
// On failure it calls shutdown() which triggers GracefulStop.
func StartLicenseBackgroundChecker(shutdown func()) {
	if os.Getenv("LICENSE_PUBLIC_KEY") == "" {
		return // unlicensed mode — no periodic check
	}

	go func() {
		ticker := time.NewTicker(LicenseCheckInterval)
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
