package main

import (
	"os"
	"testing"
	"time"
)

func TestStartLicenseBackgroundChecker_UnlicensedMode(t *testing.T) {
	_ = os.Unsetenv("LICENSE_PUBLIC_KEY")
	shutdownCalled := false
	shutdown := func() { shutdownCalled = true }

	StartLicenseBackgroundChecker(shutdown)

	// Wait a brief moment to ensure goroutine wouldn't trigger anything.
	time.Sleep(10 * time.Millisecond)

	if shutdownCalled {
		t.Errorf("shutdown called in unlicensed mode")
	}
}

func TestStartLicenseBackgroundChecker_Success(t *testing.T) {
	// Use a very short interval for the test
	originalInterval := LicenseCheckInterval
	LicenseCheckInterval = 5 * time.Millisecond
	defer func() { LicenseCheckInterval = originalInterval }()

	_ = os.Setenv("LICENSE_PUBLIC_KEY", "ed25519-valid-key-12345")
	_ = os.Unsetenv("DEPLOY_ENV")
	defer func() { _ = os.Unsetenv("LICENSE_PUBLIC_KEY") }()

	shutdownCalled := false
	shutdown := func() { shutdownCalled = true }

	StartLicenseBackgroundChecker(shutdown)

	// Wait long enough for ticker to tick a few times.
	time.Sleep(20 * time.Millisecond)

	if shutdownCalled {
		t.Errorf("shutdown called unexpectedly when license is valid")
	}
}

func TestStartLicenseBackgroundChecker_FailureTriggerShutdown(t *testing.T) {
	// Use a very short interval for the test
	originalInterval := LicenseCheckInterval
	LicenseCheckInterval = 5 * time.Millisecond
	defer func() { LicenseCheckInterval = originalInterval }()

	_ = os.Setenv("LICENSE_PUBLIC_KEY", "TODO-invalid-key")
	_ = os.Unsetenv("DEPLOY_ENV")
	defer func() { _ = os.Unsetenv("LICENSE_PUBLIC_KEY") }()

	shutdownCalled := make(chan struct{}, 1)
	shutdown := func() {
		select {
		case shutdownCalled <- struct{}{}:
		default:
		}
	}

	StartLicenseBackgroundChecker(shutdown)

	select {
	case <-shutdownCalled:
		// success
	case <-time.After(50 * time.Millisecond):
		t.Errorf("shutdown was not called when license re-validation failed")
	}
}
