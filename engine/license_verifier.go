package main

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

	"github.com/google/uuid" // Ensure this is in your go.mod
)

// ── Configuration ─────────────────────────────────────────────────────────────

const (
	licenseCachePath   = "/app/data/license_cache.json"
	licenseCheckURL    = "/v1/check"
	licenseActivateURL = "/v1/activate"

	checkInterval      = 12 * time.Hour
	httpTimeout        = 10 * time.Second
)

// ── Cache & ID Helpers ────────────────────────────────────────────────────────

type licenseCache struct {
	Token       string `json:"token"`
	TokenExp    int64  `json:"token_exp"`
	LicenseKey  string `json:"license_key"`
	Plan        string `json:"plan"`
	Customer    string `json:"customer"`
	FetchedAt   int64  `json:"fetched_at"`
}

// getPersistentID ensures the engine identity remains constant across reboots.
// It retrieves a UUID from the database or generates one if it doesn't exist.
func getPersistentID() string {
	// Re-uses the config table defined in engine/database.go
	id, err := GetConfigValue("system_instance_id")
	if err == nil && id != "" {
		return id
	}

	// Generate a permanent UUID for this installation
	newID := uuid.NewString()
	if err := SetConfigValue("system_instance_id", newID); err != nil {
		// Fallback to hostname if DB is failing, but log an error
		slog.Error("[LICENSE] Failed to save persistent ID to DB", "err", err)
		h, _ := os.Hostname()
		return h
	}
	
	slog.Info("[LICENSE] Generated new persistent instance ID", "id", newID)
	return newID
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
		return nil, fmt.Errorf("malformed token")
	}

	sigInput := parts[0] + "." + parts[1]
	mac      := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(sigInput))
	expectedSig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(expectedSig), []byte(parts[2])) {
		return nil, fmt.Errorf("token signature invalid")
	}

	payloadJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, err
	}
	var claims tokenClaims
	if err := json.Unmarshal(payloadJSON, &claims); err != nil {
		return nil, err
	}

	if time.Now().Unix() > claims.Exp {
		return nil, fmt.Errorf("cached license token expired")
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

func callServer(endpoint, licenseKey, instanceID string) (*serverResponse, int, error) {
	apiURL := os.Getenv("LICENSE_API_URL")
	if apiURL == "" {
		apiURL = "https://logistics-license.paminduvidusaka.workers.dev"
	}

	payload, _ := json.Marshal(map[string]string{
		"license_key": licenseKey,
		"hostname":    instanceID, // Identifying via persistent UUID
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
	bypass := os.Getenv("DEV_BYPASS_SECRET")
	if bypass != "" && bypass == os.Getenv("SKIP_LICENSE_CHECK") {
		slog.Warn("[LICENSE] DEV_BYPASS_SECRET matches SKIP_LICENSE_CHECK — bypassing license verification!")
		return nil
	}

	licenseKey := os.Getenv("LICENSE_KEY")
	tokenSecret := os.Getenv("TOKEN_SECRET")
	if licenseKey == "" {
		return fmt.Errorf("LICENSE_KEY is not set")
	}

	// Use persistent ID to maintain consistency across reboots
	instanceID := getPersistentID()

	cache, err := readCache()
	if err == nil && cache.LicenseKey == licenseKey {
		if tokenSecret != "" {
			claims, err := verifyToken(cache.Token, tokenSecret)
			if err == nil {
				slog.Info("[LICENSE] Offline validation OK", "customer", claims.Customer, "plan", claims.Plan)
				return nil
			}
		} else {
			// Without TOKEN_SECRET, we can't verify offline cryptographically.
			// If the token hasn't expired according to cache.TokenExp, we trust it temporarily
			// and let the background checker validate it online.
			if time.Now().Unix() < cache.TokenExp {
				slog.Info("[LICENSE] Offline validation (cache only) OK", "customer", cache.Customer, "plan", cache.Plan)
				return nil
			}
		}
	}

	slog.Info("[LICENSE] Contacting license server for activation...")
	sr, status, err := callServer(licenseActivateURL, licenseKey, instanceID)
	if err != nil {
		return fmt.Errorf("license server unreachable: %w", err)
	}

	if status == 200 && sr.Success {
		writeCache(&licenseCache{
			Token:      sr.Token,
			TokenExp:   sr.TokenExp,
			LicenseKey: licenseKey,
			Plan:       sr.Plan,
			Customer:   sr.Customer,
			FetchedAt:  time.Now().Unix(),
		})
		slog.Info("[LICENSE] Activated", "customer", sr.Customer, "plan", sr.Plan)
		return nil
	}

	return fmt.Errorf("activation failed (HTTP %d): %s", status, sr.Error)
}

// ── StartLicenseBackgroundChecker ─────────────────────────────────────────────

func StartLicenseBackgroundChecker(shutdown func()) {
	bypass := os.Getenv("DEV_BYPASS_SECRET")
	if bypass != "" && bypass == os.Getenv("SKIP_LICENSE_CHECK") {
		return
	}

	licenseKey := os.Getenv("LICENSE_KEY")
	tokenSecret := os.Getenv("TOKEN_SECRET")
	if licenseKey == "" {
		return
	}

	instanceID := getPersistentID()

	go func() {
		ticker := time.NewTicker(checkInterval)
		defer ticker.Stop()
		for range ticker.C {
			doCheck(licenseKey, tokenSecret, instanceID, shutdown)
		}
	}()

	slog.Info("[LICENSE] Background checker started", "interval", checkInterval)
}

func doCheck(licenseKey, tokenSecret, instanceID string, shutdown func()) {
	sr, status, err := callServer(licenseCheckURL, licenseKey, instanceID)
	if err != nil {
		cache, _ := readCache()
		if cache == nil || time.Now().Unix() > cache.TokenExp {
			slog.Error("[LICENSE] Background check failed and token expired")
			shutdown()
		}
		return
	}

	if status == 200 && sr.Success {
		writeCache(&licenseCache{
			Token:      sr.Token,
			TokenExp:   sr.TokenExp,
			LicenseKey: licenseKey,
			Plan:       sr.Plan,
			Customer:   sr.Customer,
			FetchedAt:  time.Now().Unix(),
		})
	} else if status == 402 || status == 403 {
		slog.Error("[LICENSE] License revoked or expired — shutting down")
		shutdown()
	}
}