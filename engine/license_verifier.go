package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// publicKeyPEM is the RSA public key used to verify license tokens.
// FIX: Startup now validates this is not the placeholder before accepting
// connections — prevents a silent crash on first license request.
//
// Replace with your actual key:
//
//	openssl genrsa -out license.pem 2048
//	openssl rsa -in license.pem -pubout > license_pub.pem
const publicKeyPEM = `-----BEGIN PUBLIC KEY-----
YOUR_PUBLIC_KEY_HERE
-----END PUBLIC KEY-----`

// VerifyLicense validates the cryptographic license token at /app/data/license.key.
//
// Dev bypass: set DEV_BYPASS_SECRET to a random hex value in your local .env,
// then pass the same value as SKIP_LICENSE_CHECK.  The plain SKIP_LICENSE_CHECK=true
// legacy path still works but prints a loud warning.
func VerifyLicense() error {
	bypass := os.Getenv("SKIP_LICENSE_CHECK")
	devSecret := os.Getenv("DEV_BYPASS_SECRET")

	if bypass != "" && devSecret != "" && bypass == devSecret {
		fmt.Fprintln(os.Stderr,
			"[WARNING] License check bypassed via DEV_BYPASS_SECRET — NOT for production.")
		return nil
	}
	if bypass == "true" {
		fmt.Fprintln(os.Stderr,
			"[WARNING] SKIP_LICENSE_CHECK=true is insecure. Use DEV_BYPASS_SECRET instead.")
		return nil
	}

	// FIX: Detect the placeholder before trying to parse it, giving operators
	// a clear actionable error message instead of a cryptic parse failure.
	if strings.Contains(publicKeyPEM, "YOUR_PUBLIC_KEY_HERE") {
		return fmt.Errorf(
			"publicKeyPEM is still the placeholder value in license_verifier.go. " +
				"Replace it with your RSA public key before deploying, or set " +
				"DEV_BYPASS_SECRET=<token> for local development.")
	}

	keyData, err := os.ReadFile("/app/data/license.key")
	if err != nil {
		return fmt.Errorf("license.key not found at /app/data/license.key: %w — "+
			"place a valid license file there or set DEV_BYPASS_SECRET for dev use", err)
	}

	pubKey, err := jwt.ParseRSAPublicKeyFromPEM([]byte(publicKeyPEM))
	if err != nil {
		return fmt.Errorf("failed to parse publicKeyPEM: %w", err)
	}

	token, err := jwt.Parse(string(keyData), func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return pubKey, nil
	})
	if err != nil || !token.Valid {
		return fmt.Errorf("invalid or expired license key: %w", err)
	}

	if claims, ok := token.Claims.(jwt.MapClaims); ok {
		if exp, ok := claims["exp"].(float64); ok && time.Now().Unix() > int64(exp) {
			return fmt.Errorf("license key expired on %s",
				time.Unix(int64(exp), 0).Format("2006-01-02"))
		}
	}
	return nil
}
