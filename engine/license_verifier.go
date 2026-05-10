package main

// license_verifier.go
//
// How it works:
//
//   1. On first boot (or if cache is missing/invalid), the engine calls
//      POST /v1/activate on the licensing server. The server validates the
//      key and returns a signed HMAC-SHA256 token valid for 24 hours.
//
//   2. The engine writes the token to /app/data/license_cache.json and
//      verifies it locally on every subsequent boot — no network call needed
//      as long as the token is not expired.
//
//   3. A background goroutine re-checks every 12 hours (while online).
//      Each successful check refreshes the cached token, resetting the 24 h clock.
//
//   4. If the server is unreachable, the cached token stays valid until its
//      exp timestamp.  Once exp passes without a successful refresh, the engine
//      refuses to start and logs a clear error.
//
//   5. If the server returns 403 (revoked/suspended) or 402 (expired), the
//      engine shuts down gracefully on the next check.
//
// TOKEN_SECRET must be set in the environment.  It is the same secret baked
// into the Cloudflare Worker.  This is the only secret needed at runtime —
// no RSA keys, no certificate files.

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// ── Configuration ─────────────────────────────────────────────────────────────

const (
	licenseCachePath   = "/app/data/license_cache.json"
	licenseCheckURL    = "/v1/check"     // appended to LICENSE_API_URL
	licenseActivateURL = "/v1/activate"  // appended to LICENSE_API_URL

	checkInterval      = 12 * time.Hour  // background re-check period
	httpTimeout        = 10 * time.Second
)

// ── Cache file ────────────────────────────────────────────────────────────────

type licenseCache struct {
	Token       string `json:"token"`
	TokenExp    int64  `json:"token_exp"`
	LicenseKey  string `json:"license_key"`  // stored so background checker can re-use it
	Plan        string `json:"plan"`
	Customer    string `json:"customer"`
	FetchedAt   int64  `json:"fetched_at"`
}

func readCache() (*licenseCache, error) {
	data, err := os.ReadFile(licenseCachePath)
	if err != nil {
		return nil, err
	}
	var c licenseCache
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, err
	}
	return &c, nil
}

func writeCache(c *licenseCache) error {
	data, err := json.Marshal(c)
	if err != nil {
		return err
	}
	return os.WriteFile(licenseCachePath, data, 0o600)
}

// ── Token verification (offline) ─────────────────────────────────────────────

type tokenClaims struct {
	Sub      string `json:"sub"`
	Host     string `json:"host"`
	Plan     string `json:"plan"`
	Customer string `json:"customer"`
	Iat      int64  `json:"iat"`
	Exp      int64  `json:"exp"`
}

func verifyToken(tokenStr, secret string) (*tokenClaims, error) {
	parts := strings.Split(tokenStr, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("malformed token: expected 3 parts, got %d", len(parts))
	}

	// Verify HMAC-SHA256 signature
	sigInput := parts[0] + "." + parts[1]
	mac      := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(sigInput))
	expectedSig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(expectedSig), []byte(parts[2])) {
		return nil, fmt.Errorf("token signature invalid — possible tampering")
	}

	// Decode payload
	payloadJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("token payload decode error: %w", err)
	}
	var claims tokenClaims
	if err := json.Unmarshal(payloadJSON, &claims); err != nil {
		return nil, fmt.Errorf("token payload unmarshal error: %w", err)
	}

	// Check expiry
	if time.Now().Unix() > claims.Exp {
		return nil, fmt.Errorf("cached license token expired at %s — engine cannot start offline; please restore internet access",
			time.Unix(claims.Exp, 0).Format("2006-01-02 15:04:05"))
	}

	return &claims, nil
}

// ── Network calls ─────────────────────────────────────────────────────────────

type serverResponse struct {
	Success  bool   `json:"success"`
	Valid    bool   `json:"valid"`
	Token    string `json:"token"`
	TokenExp int64  `json:"token_exp"`
	Plan     string `json:"plan"`
	Customer string `json:"customer"`
	Reason   string `json:"reason"`
	Error    string `json:"error"`
	Expires  string `json:"expires"`
}

func callServer(endpoint, licenseKey, hostname string) (*serverResponse, int, error) {
	apiURL := os.Getenv("LICENSE_API_URL")
	if apiURL == "" {
		return nil, 0, fmt.Errorf("LICENSE_API_URL is not set")
	}

	payload, _ := json.Marshal(map[string]string{
		"license_key": licenseKey,
		"hostname":    hostname,
	})

	client := &http.Client{Timeout: httpTimeout}
	resp, err := client.Post(apiURL+endpoint, "application/json", bytes.NewReader(payload))
	if err != nil {
		return nil, 0, fmt.Errorf("network error: %w", err)
	}
	defer resp.Body.Close()

	var sr serverResponse
	if err := json.NewDecoder(resp.Body).Decode(&sr); err != nil {
		return nil, resp.StatusCode, fmt.Errorf("server response decode error: %w", err)
	}
	return &sr, resp.StatusCode, nil
}

// ── VerifyLicense — called once at startup ────────────────────────────────────

func VerifyLicense() error {
	// ── Dev bypass ────────────────────────────────────────────────────────────
	bypass := os.Getenv("SKIP_LICENSE_CHECK")
	devSec := os.Getenv("DEV_BYPASS_SECRET")
	if bypass != "" && devSec != "" && bypass == devSec {
		slog.Warn("[LICENSE] Dev bypass active — NOT for production")
		return nil
	}

	licenseKey := os.Getenv("LICENSE_KEY")
	if licenseKey == "" {
		return fmt.Errorf("LICENSE_KEY is not set — set it in your .env file")
	}

	tokenSecret := os.Getenv("TOKEN_SECRET")
	if tokenSecret == "" {
		return fmt.Errorf("TOKEN_SECRET is not set — set it in your .env file")
	}

	hostname, _ := os.Hostname()

	// ── Try cached token first ────────────────────────────────────────────────
	cache, err := readCache()
	if err == nil && cache.LicenseKey == licenseKey {
		claims, err := verifyToken(cache.Token, tokenSecret)
		if err == nil {
			slog.Info("[LICENSE] Offline validation OK",
				"customer", claims.Customer,
				"plan", claims.Plan,
				"token_exp", time.Unix(claims.Exp, 0).Format("2006-01-02 15:04"),
			)
			return nil
		}
		slog.Warn("[LICENSE] Cached token invalid, attempting online activation", "reason", err)
	}

	// ── No valid cache — must go online ───────────────────────────────────────
	slog.Info("[LICENSE] Contacting license server for activation...")

	sr, status, err := callServer(licenseActivateURL, licenseKey, hostname)
	if err != nil {
		return fmt.Errorf("license server unreachable and no valid cached token: %w\n"+
			"  → Restore internet access to activate, or the 24 h offline grace period has lapsed", err)
	}

	switch status {
	case 200:
		// OK — cache the token
		if err := writeCache(&licenseCache{
			Token:      sr.Token,
			TokenExp:   sr.TokenExp,
			LicenseKey: licenseKey,
			Plan:       sr.Plan,
			Customer:   sr.Customer,
			FetchedAt:  time.Now().Unix(),
		}); err != nil {
			slog.Warn("[LICENSE] Failed to write cache", "err", err)
		}
		slog.Info("[LICENSE] Activated",
			"customer", sr.Customer,
			"plan", sr.Plan,
			"expires", sr.Expires,
			"seats_used", sr.Success,
		)
		return nil

	case 401:
		return fmt.Errorf("invalid license key — check LICENSE_KEY in your .env file")
	case 402:
		return fmt.Errorf("license expired: %s", sr.Error)
	case 403:
		return fmt.Errorf("license %s: %s", sr.Reason, sr.Error)
	case 429:
		// Rate limited — if we have any cached token (even expired), log and try to continue
		if cache != nil {
			slog.Warn("[LICENSE] Rate limited by server; using stale cache temporarily")
			return nil
		}
		return fmt.Errorf("rate limited by license server and no cache available")
	default:
		return fmt.Errorf("unexpected license server response (HTTP %d): %s", status, sr.Error)
	}
}

// ── StartLicenseBackgroundChecker — call after VerifyLicense succeeds ─────────
//
// Runs a goroutine that re-checks every 12 h.
// On 403/402 it calls shutdown() to gracefully stop the gRPC server.

func StartLicenseBackgroundChecker(shutdown func()) {
	licenseKey := os.Getenv("LICENSE_KEY")
	tokenSecret := os.Getenv("TOKEN_SECRET")

	// bypass mode — no background checks needed
	bypass := os.Getenv("SKIP_LICENSE_CHECK")
	devSec := os.Getenv("DEV_BYPASS_SECRET")
	if bypass != "" && devSec != "" && bypass == devSec {
		return
	}
	if licenseKey == "" || tokenSecret == "" {
		return
	}

	hostname, _ := os.Hostname()

	go func() {
		ticker := time.NewTicker(checkInterval)
		defer ticker.Stop()

		for range ticker.C {
			doCheck(licenseKey, tokenSecret, hostname, shutdown)
		}
	}()

	slog.Info("[LICENSE] Background checker started", "interval", checkInterval)
}

func doCheck(licenseKey, tokenSecret, hostname string, shutdown func()) {
	sr, status, err := callServer(licenseCheckURL, licenseKey, hostname)
	if err != nil {
		// Network error — check if cached token is still valid
		cache, cerr := readCache()
		if cerr != nil {
			slog.Error("[LICENSE] Background check failed and no cache", "err", err)
			shutdown()
			return
		}
		if _, verr := verifyToken(cache.Token, tokenSecret); verr != nil {
			slog.Error("[LICENSE] Background check failed and cached token expired",
				"network_err", err, "token_err", verr)
			shutdown()
			return
		}
		slog.Warn("[LICENSE] Background check failed (network), running on cached token",
			"err", err,
			"cache_exp", time.Unix(cache.TokenExp, 0).Format("2006-01-02 15:04"),
		)
		return
	}

	switch status {
	case 200:
		if err := writeCache(&licenseCache{
			Token:      sr.Token,
			TokenExp:   sr.TokenExp,
			LicenseKey: licenseKey,
			Plan:       sr.Plan,
			Customer:   sr.Customer,
			FetchedAt:  time.Now().Unix(),
		}); err != nil {
			slog.Warn("[LICENSE] Failed to refresh cache", "err", err)
		} else {
			slog.Info("[LICENSE] Token refreshed",
				"plan", sr.Plan,
				"new_exp", time.Unix(sr.TokenExp, 0).Format("2006-01-02 15:04"),
			)
		}

	case 402:
		slog.Error("[LICENSE] License expired — shutting down", "detail", sr.Reason)
		shutdown()

	case 403:
		slog.Error("[LICENSE] License revoked/suspended — shutting down",
			"reason", sr.Reason, "detail", sr.Error)
		shutdown()

	case 429:
		slog.Warn("[LICENSE] Rate limited during background check — will retry next interval")

	default:
		slog.Warn("[LICENSE] Unexpected server response during background check",
			"status", status, "error", sr.Error)
	}
}
