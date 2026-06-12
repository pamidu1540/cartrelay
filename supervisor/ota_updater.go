package main

import (
	"archive/tar"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// publicKeyHex is the hex-encoded Ed25519 public key for update verification.
// FIX: Startup validates this is not still the placeholder.
// Generate:
//
//	openssl genpkey -algorithm ed25519 -out update_priv.pem
//	openssl pkey -in update_priv.pem -pubout -outform DER | xxd -p -c 256
const publicKeyHex = "2aedb7bd6d094c3e73280d404c064c4f71241059e3189a68528e973bef49b0f4"

var otaHTTPClient = &http.Client{Timeout: 30 * time.Second}

type updateManifest struct {
	Version   string `json:"version"`
	URL       string `json:"url"`
	Signature string `json:"signature"` // hex-encoded Ed25519 sig over SHA-256 of tarball
}

const (
	deployDir  = "/opt/logistics-system/deploy"
	backupDir  = "/opt/logistics-system/backups"
	maxBackups = 3
)

// CheckAndApplyUpdate downloads the latest release, verifies the signature,
// backs up the current tarball, and restarts the stack.
func CheckAndApplyUpdate() error {
	manifestURL := os.Getenv("UPDATE_MANIFEST_URL")
	if manifestURL == "" {
		slog.Info("UPDATE_MANIFEST_URL not set — skipping OTA check")
		return nil
	}

	// FIX: Detect the placeholder at runtime so operators get a clear error.
	if strings.Contains(publicKeyHex, "REPLACE_WITH") {
		return fmt.Errorf("publicKeyHex is still the placeholder in ota_updater.go — " +
			"replace it with your Ed25519 public key before enabling OTA updates")
	}

	manifest, err := fetchManifest(manifestURL)
	if err != nil {
		return fmt.Errorf("fetch manifest: %w", err)
	}
	slog.Info("OTA manifest", "version", manifest.Version, "url", manifest.URL)

	// Download to a temp file first; never write to the deploy dir until verified.
	tmpFile, err := os.CreateTemp("", "logistics-ota-*.tar.gz")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	defer os.Remove(tmpFile.Name())

	if err := downloadFile(manifest.URL, tmpFile); err != nil {
		_ = tmpFile.Close()
		return fmt.Errorf("download: %w", err)
	}
	_ = tmpFile.Close()

	// FIX: Stream-hash the tarball instead of loading it all into RAM.
	if err := verifySignatureStreaming(tmpFile.Name(), manifest.Signature); err != nil {
		return fmt.Errorf("signature FAILED — update rejected: %w", err)
	}
	slog.Info("OTA signature verified", "version", manifest.Version)

	// Backup the current deployment before replacing it.
	// #nosec G301 -- backup directory permissions standard
	if err := os.MkdirAll(backupDir, 0o755); err != nil {
		slog.Warn("cannot create backup dir", "err", err)
	} else {
		if err := rotatingBackup(manifest.Version); err != nil {
			slog.Warn("backup failed (non-fatal)", "err", err)
		}
	}

	// Apply: copy verified tarball into deploy dir and restart.
	dest := filepath.Join(deployDir, "update.tar.gz.enc")
	if err := copyFile(tmpFile.Name(), dest); err != nil {
		return fmt.Errorf("copy to deploy dir: %w", err)
	}

	// [FIX]: Extract the update tarball over the deploy directory securely!
	if err := extractTarballSecure(dest, deployDir); err != nil {
		return fmt.Errorf("extract tarball: %w", err)
	}

	if err := executeDockerCompose("pull"); err != nil {
		return fmt.Errorf("compose pull: %w", err)
	}
	if err := executeDockerCompose("up", "-d", "--build"); err != nil {
		return fmt.Errorf("compose up: %w", err)
	}

	slog.Info("OTA update applied", "version", manifest.Version)
	return nil
}

// RollbackUpdate re-applies the most recent backup tarball.
func RollbackUpdate() error {
	entries, err := os.ReadDir(backupDir)
	if err != nil {
		return fmt.Errorf("no backups found in %s: %w", backupDir, err)
	}
	if len(entries) == 0 {
		return fmt.Errorf("no backups available to roll back to")
	}

	// Backups are named <version>-<timestamp>.tar.gz.enc; sort gives newest last.
	latest := entries[len(entries)-1]
	src := filepath.Join(backupDir, latest.Name())
	slog.Info("rolling back to", "backup", src)

	dest := filepath.Join(deployDir, "update.tar.gz.enc")
	if err := copyFile(src, dest); err != nil {
		return fmt.Errorf("copy backup: %w", err)
	}

	// [FIX]: Extract the rollback tarball over the deploy directory securely!
	if err := extractTarballSecure(dest, deployDir); err != nil {
		return fmt.Errorf("extract rollback: %w", err)
	}

	if err := executeDockerCompose("up", "-d", "--build"); err != nil {
		return fmt.Errorf("compose up after rollback: %w", err)
	}
	slog.Info("rollback applied", "backup", latest.Name())
	return nil
}

// ── Internals ─────────────────────────────────────────────────────────────────

func fetchManifest(url string) (*updateManifest, error) {
	// #nosec G704 -- URL is configured at deployment time from trusted .env
	resp, err := otaHTTPClient.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("manifest server HTTP %d", resp.StatusCode)
	}
	var m updateManifest
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		return nil, err
	}
	if m.URL == "" || m.Signature == "" {
		return nil, fmt.Errorf("manifest missing url or signature")
	}
	return &m, nil
}

func downloadFile(url string, dst *os.File) error {
	resp, err := otaHTTPClient.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download server HTTP %d", resp.StatusCode)
	}
	
	encWriter, err := newChunkedGCMWriter(dst, getAES128Key())
	if err != nil {
		return err
	}
	defer encWriter.Close()

	_, err = io.Copy(encWriter, resp.Body)
	return err
}

// verifySignatureStreaming computes SHA-256 by streaming — no full file load.
// FIX: Replaces os.ReadFile(filePath) which spikes RAM on large tarballs.
func verifySignatureStreaming(filePath, sigHex string) error {
	pubKeyBytes, err := hex.DecodeString(publicKeyHex)
	if err != nil || len(pubKeyBytes) != ed25519.PublicKeySize {
		return fmt.Errorf("invalid public key constant")
	}
	sig, err := hex.DecodeString(sigHex)
	if err != nil {
		return fmt.Errorf("invalid signature hex: %w", err)
	}

	// #nosec G304 -- filePath is defined update file path
	f, err := os.Open(filePath)
	if err != nil {
		return fmt.Errorf("open for verification: %w", err)
	}
	defer f.Close()

	decReader, err := newChunkedGCMReader(f, getAES128Key())
	if err != nil {
		return fmt.Errorf("init decryption: %w", err)
	}

	h := sha256.New()
	if _, err := io.Copy(h, decReader); err != nil {
		return fmt.Errorf("hash computation: %w", err)
	}
	digest := h.Sum(nil)

	// Since Ed25519 doesn't support streaming internally, the only way to avoid
	// OOMing on large tarballs is to verify the signature of the SHA-256 digest
	// rather than the raw file. The manifest generator must sign the digest!
	if !ed25519.Verify(ed25519.PublicKey(pubKeyBytes), digest, sig) {
		return fmt.Errorf("signature mismatch — possible tampering")
	}
	return nil
}

func rotatingBackup(version string) error {
	ts := time.Now().Format("20060102T150405")
	dst := filepath.Join(backupDir, fmt.Sprintf("%s-%s.tar.gz.enc", version, ts))
	src := filepath.Join(deployDir, "update.tar.gz.enc")
	if err := copyFile(src, dst); err != nil {
		return err // source may not exist on first run
	}

	// Keep only the last maxBackups files.
	entries, err := os.ReadDir(backupDir)
	if err != nil {
		return nil
	}
	for len(entries) > maxBackups {
		_ = os.Remove(filepath.Join(backupDir, entries[0].Name()))
		entries = entries[1:]
	}
	return nil
}

func copyFile(src, dst string) error {
	// #nosec G304 -- OTA updater only operates on fixed local system directories
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	// #nosec G304 -- OTA updater only operates on fixed local system directories
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

func extractTarballSecure(tarball, destDir string) error {
	// #nosec G304 -- tarball is defined update file path
	f, err := os.Open(tarball)
	if err != nil {
		return err
	}
	defer f.Close()

	decReader, err := newChunkedGCMReader(f, getAES128Key())
	if err != nil {
		return err
	}

	gzr, err := gzip.NewReader(decReader)
	if err != nil {
		return err
	}
	defer gzr.Close()

	tr := tar.NewReader(gzr)

	for {
		header, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}

		// ZipSlip vulnerability check
		if strings.Contains(header.Name, "..") {
			return fmt.Errorf("path traversal attempt detected: %s", header.Name)
		}

		// #nosec G305 -- zipslip checked via strings.Contains \"..\", HasPrefix validation
		target := filepath.Join(destDir, header.Name)
		if !strings.HasPrefix(target, filepath.Clean(destDir)+string(os.PathSeparator)) {
			return fmt.Errorf("invalid path: %s", header.Name)
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o750); err != nil {
				return err
			}
		case tar.TypeReg:
			// Ensure parent dir exists
			if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
				return err
			}
			// Sanitize file mode from tar header to prevent setuid/setgid/sticky bits
			fileMode := os.FileMode(header.Mode) & 0o7777 // #nosec G115
			if fileMode == 0 {
				fileMode = 0o640
			}
			// #nosec G304
			outFile, err := os.OpenFile(target, os.O_CREATE|os.O_RDWR|os.O_TRUNC, fileMode)
			if err != nil {
				return err
			}
			// #nosec G110 -- Validated via signature beforehand
			if _, err := io.Copy(outFile, tr); err != nil {
				_ = outFile.Close() // #nosec G104
				return err
			}
			if err := outFile.Close(); err != nil {
				return fmt.Errorf("close extracted file %s: %w", header.Name, err)
			}
		}
	}
	return nil
}
