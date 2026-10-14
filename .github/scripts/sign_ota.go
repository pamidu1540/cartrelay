package main

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintf(os.Stderr, "Usage: OTA_PRIVATE_KEY=<private key> sign_ota <tarball_path>\n")
		os.Exit(1)
	}
	tarballPath := os.Args[1]
	privKeyHex := os.Getenv("OTA_PRIVATE_KEY")

	privKeyBytes, err := hex.DecodeString(privKeyHex)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to decode private key: %v\n", err)
		os.Exit(1)
	}
	if len(privKeyBytes) != ed25519.PrivateKeySize {
		fmt.Fprintf(os.Stderr, "Invalid private key size: %d\n", len(privKeyBytes))
		os.Exit(1)
	}

	f, err := os.Open(tarballPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to open tarball: %v\n", err)
		os.Exit(1)
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to hash tarball: %v\n", err)
		os.Exit(1)
	}
	digest := h.Sum(nil)

	sig := ed25519.Sign(ed25519.PrivateKey(privKeyBytes), digest)
	fmt.Print(hex.EncodeToString(sig))
}
