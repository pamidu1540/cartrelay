package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestValidateHMACToken_ValidToken(t *testing.T) {
	secret := "test-secret-12345"
	_ = os.Setenv("INTERNAL_TOKEN", secret)
	defer func() { _ = os.Unsetenv("INTERNAL_TOKEN") }()

	method := "/logistics.LogisticsEngine/GetCatalog"
	now := time.Now().Unix()

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(fmt.Sprintf("%s:%d", method, now)))
	token := hex.EncodeToString(mac.Sum(nil))

	if !validateHMACToken(token, method) {
		t.Error("expected valid token to pass HMAC verification")
	}
}

func TestValidateHMACToken_ClockSkew(t *testing.T) {
	secret := "test-secret-12345"
	_ = os.Setenv("INTERNAL_TOKEN", secret)
	defer func() { _ = os.Unsetenv("INTERNAL_TOKEN") }()

	method := "/logistics.LogisticsEngine/GetCatalog"
	skewedTime := time.Now().Unix() - 2 // 2 seconds ago (within ±2s tolerance)

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(fmt.Sprintf("%s:%d", method, skewedTime)))
	token := hex.EncodeToString(mac.Sum(nil))

	if !validateHMACToken(token, method) {
		t.Error("expected skewed token within tolerance to pass verification")
	}
}

func TestValidateHMACToken_Invalid(t *testing.T) {
	secret := "test-secret-12345"
	_ = os.Setenv("INTERNAL_TOKEN", secret)
	defer func() { _ = os.Unsetenv("INTERNAL_TOKEN") }()

	if validateHMACToken("invalid-hex-token", "/logistics.LogisticsEngine/GetCatalog") {
		t.Error("expected invalid token to be rejected")
	}
}

func TestActorFromCtx(t *testing.T) {
	ctx := context.Background()
	if actorFromCtx(ctx) != 0 {
		t.Errorf("expected 0 for empty context")
	}

	ctxWithActor := context.WithValue(ctx, ctxActorID, int64(123456))
	if actorFromCtx(ctxWithActor) != int64(123456) {
		t.Errorf("expected 123456, got %d", actorFromCtx(ctxWithActor))
	}
}

func TestRecoveryInterceptor_CatchesPanic(t *testing.T) {
	panickingHandler := func(_ context.Context, _ interface{}) (interface{}, error) {
		panic("simulated RPC handler crash")
	}

	info := &grpc.UnaryServerInfo{FullMethod: "/logistics.LogisticsEngine/TestPanic"}

	resp, err := RecoveryInterceptor(context.Background(), nil, info, panickingHandler)
	if resp != nil {
		t.Errorf("expected nil response on panic, got %v", resp)
	}
	if err == nil {
		t.Fatal("expected error from RecoveryInterceptor after panic, got nil")
	}

	st, ok := status.FromError(err)
	if !ok || st.Code() != codes.Internal {
		t.Errorf("expected status code Internal, got %v", st.Code())
	}
}
