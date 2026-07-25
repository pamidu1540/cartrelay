package main

import (
	"os"
	"testing"
)

func TestVerifyLicense_NoPublicKey(t *testing.T) {
	_ = os.Unsetenv("LICENSE_PUBLIC_KEY")
	_ = os.Unsetenv("DEPLOY_ENV")

	err := VerifyLicense()
	if err != nil {
		t.Errorf("expected no error for unlicensed mode, got %v", err)
	}
}

func TestVerifyLicense_ValidKey(t *testing.T) {
	_ = os.Setenv("LICENSE_PUBLIC_KEY", "ed25519-valid-key-abcdef1234567890")
	_ = os.Unsetenv("DEPLOY_ENV")
	defer func() { _ = os.Unsetenv("LICENSE_PUBLIC_KEY") }()

	err := VerifyLicense()
	if err != nil {
		t.Errorf("expected no error for valid key, got %v", err)
	}
}
