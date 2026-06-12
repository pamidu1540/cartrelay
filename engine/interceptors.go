package main

// interceptors.go — gRPC server interceptors

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"runtime/debug"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// ── Context keys ──────────────────────────────────────────────────────────────

type contextKey string

const (
	ctxActorID contextKey = "actor_id"
	ctxRole    contextKey = "actor_role"
)

func actorFromCtx(ctx context.Context) int64 {
	if v, ok := ctx.Value(ctxActorID).(int64); ok {
		return v
	}
	return 0
}

// AuthInterceptor validates each request via HMAC-SHA256 over the method name +
// unix timestamp using the shared INTERNAL_TOKEN as the key.
// Tokens older than ±5 seconds are rejected for replay protection.
//
// Header format:
//   x-auth-token:    <hex(HMAC-SHA256(key=INTERNAL_TOKEN, msg="method:timestamp"))>
//   x-actor-id:      <telegram_user_id>   (set by bot; validated against admin_users)
//   x-actor-role:    <role>               (informational; enforced server-side)

func AuthInterceptor(
	ctx context.Context,
	req interface{},
	info *grpc.UnaryServerInfo,
	handler grpc.UnaryHandler,
) (interface{}, error) {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "missing metadata")
	}

	token := mdFirst(md, "x-auth-token")
	if token == "" {
		return nil, status.Error(codes.Unauthenticated, "missing x-auth-token")
	}

	if !validateHMACToken(token, info.FullMethod) {
		slog.Warn("auth rejected", "method", info.FullMethod)
		return nil, status.Error(codes.Unauthenticated, "invalid token")
	}

	// Inject actor context
	newCtx := ctx
	actorStr := mdFirst(md, "x-actor-id")
	if actorStr != "" {
		var actorID int64
		_, _ = fmt.Sscanf(actorStr, "%d", &actorID)
		newCtx = context.WithValue(newCtx, ctxActorID, actorID)

		// Validate admin role for admin-only RPCs
		if isAdminRPC(info.FullMethod) {
			isAdmin, role, err := DBIsAdmin(ctx, actorID)
			if err != nil || !isAdmin {
				return nil, status.Error(codes.PermissionDenied, "not authorised for this operation")
			}
			newCtx = context.WithValue(newCtx, ctxRole, role)

			// Role-based access control
			if err := enforceRBAC(info.FullMethod, role); err != nil {
				return nil, err
			}
		}
	}

	return handler(newCtx, req)
}

// validateHMACToken checks the HMAC for the current and adjacent seconds
// to tolerate clock skew (±5 seconds) between bot and engine.
func validateHMACToken(token, method string) bool {
	secret := []byte(os.Getenv("INTERNAL_TOKEN"))
	now := time.Now().Unix()
	tokenBytes := []byte(token)

	// Fast path: exact match on current second
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(fmt.Sprintf("%s:%d", method, now)))
	expected := hex.EncodeToString(mac.Sum(nil))
	if hmac.Equal(tokenBytes, []byte(expected)) {
		return true
	}

	for offset := int64(-5); offset <= 5; offset++ {
		if offset == 0 {
			continue
		}
		msg := fmt.Sprintf("%s:%d", method, now+offset)
		mac = hmac.New(sha256.New, secret)
		mac.Write([]byte(msg))
		expected = hex.EncodeToString(mac.Sum(nil))
		if hmac.Equal(tokenBytes, []byte(expected)) {
			return true
		}
	}
	return false
}

var adminMethods = map[string]bool{
	"/logistics.LogisticsEngine/AddItem":            true,
	"/logistics.LogisticsEngine/EditItem":           true,
	"/logistics.LogisticsEngine/EditItemPrice":      true,
	"/logistics.LogisticsEngine/EditItemStock":      true,
	"/logistics.LogisticsEngine/RemoveItem":         true,
	"/logistics.LogisticsEngine/BulkImportItems":    true,
	"/logistics.LogisticsEngine/GetAllOrders":       true,
	"/logistics.LogisticsEngine/UpdateOrderStatus":  true,
	"/logistics.LogisticsEngine/AssignDriver":       true,
	"/logistics.LogisticsEngine/AddOrderNote":       true,
	"/logistics.LogisticsEngine/ResolveOrderIssue":  true,
	"/logistics.LogisticsEngine/AddWarehouse":       true,
	"/logistics.LogisticsEngine/EditWarehouse":      true,
	"/logistics.LogisticsEngine/RemoveWarehouse":    true,
	"/logistics.LogisticsEngine/AddDriver":          true,
	"/logistics.LogisticsEngine/EditDriver":         true,
	"/logistics.LogisticsEngine/RemoveDriver":       true,
	"/logistics.LogisticsEngine/BroadcastDrivers":   true,
	"/logistics.LogisticsEngine/AddPromo":           true,
	"/logistics.LogisticsEngine/EditPromo":          true,
	"/logistics.LogisticsEngine/RemovePromo":        true,
	"/logistics.LogisticsEngine/AdjustLoyaltyPoints":true,
	"/logistics.LogisticsEngine/AddAdmin":           true,
	"/logistics.LogisticsEngine/RemoveAdmin":        true,
	"/logistics.LogisticsEngine/ListAdmins":         true,
	"/logistics.LogisticsEngine/GetAuditLog":        true,
	"/logistics.LogisticsEngine/SetSystemSettings":  true,
	"/logistics.LogisticsEngine/ToggleMaintenance":  true,
	"/logistics.LogisticsEngine/ScheduleMaintenance":true,
	"/logistics.LogisticsEngine/GetRevenueSummary":  true,
	"/logistics.LogisticsEngine/GetTopItems":        true,
	"/logistics.LogisticsEngine/GetOrderVolumeTrend":true,
}

// isAdminRPC returns true for RPCs that require admin privileges.
func isAdminRPC(method string) bool {
	return adminMethods[method]
}

var catalogMethods = map[string]bool{
	"/logistics.LogisticsEngine/AddItem":         true,
	"/logistics.LogisticsEngine/EditItem":        true,
	"/logistics.LogisticsEngine/EditItemPrice":   true,
	"/logistics.LogisticsEngine/EditItemStock":   true,
	"/logistics.LogisticsEngine/RemoveItem":      true,
	"/logistics.LogisticsEngine/BulkImportItems": true,
}

var dispatchMethods = map[string]bool{
	"/logistics.LogisticsEngine/GetAllOrders":      true,
	"/logistics.LogisticsEngine/UpdateOrderStatus": true,
	"/logistics.LogisticsEngine/AssignDriver":      true,
	"/logistics.LogisticsEngine/AddOrderNote":      true,
}

var supportMethods = map[string]bool{
	"/logistics.LogisticsEngine/GetAllOrders":      true,
	"/logistics.LogisticsEngine/ResolveOrderIssue": true,
	"/logistics.LogisticsEngine/AddOrderNote":      true,
}

// enforceRBAC restricts certain RPCs to specific roles.
func enforceRBAC(method, role string) error {
	if role == "super_admin" {
		return nil // super_admin can do everything
	}

	allowed := false
	switch role {
	case "catalog_manager":
		allowed = catalogMethods[method]
	case "dispatcher":
		allowed = dispatchMethods[method]
	case "support":
		allowed = supportMethods[method]
	}

	if !allowed {
		return status.Errorf(codes.PermissionDenied,
			"role %q is not permitted to call %s", role, method)
	}
	return nil
}

// ── Rate limit interceptor ────────────────────────────────────────────────────
//
// Token-bucket rate limiter: 30 requests/minute per actor_id.
// Engine-side backstop; the bot layer also rate-limits (see bot/src/middleware.py).

type tokenBucket struct {
	tokens   float64
	capacity float64
	rate     float64 // tokens per second
	lastFill time.Time
	mu       sync.Mutex
}

func (b *tokenBucket) allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := time.Now()
	elapsed := now.Sub(b.lastFill).Seconds()
	b.tokens = min64(b.capacity, b.tokens+elapsed*b.rate)
	b.lastFill = now
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

func min64(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

var (
	rateBuckets   = make(map[int64]*tokenBucket)
	rateBucketsMu sync.Mutex
)

func init() {
	go func() {
		for {
			time.Sleep(10 * time.Minute)
			rateBucketsMu.Lock()
			now := time.Now()
			for id, b := range rateBuckets {
				b.mu.Lock()
				lastFill := b.lastFill
				b.mu.Unlock()
				if now.Sub(lastFill) > 1*time.Hour {
					delete(rateBuckets, id)
				}
			}
			rateBucketsMu.Unlock()
		}
	}()
}

func getBucket(actorID int64) *tokenBucket {
	rateBucketsMu.Lock()
	defer rateBucketsMu.Unlock()
	if b, ok := rateBuckets[actorID]; ok {
		return b
	}
	b := &tokenBucket{
		tokens:   30,
		capacity: 30,
		rate:     0.5, // 30 per minute
		lastFill: time.Now(),
	}
	rateBuckets[actorID] = b
	return b
}

func RateLimitInterceptor(
	ctx context.Context,
	req interface{},
	info *grpc.UnaryServerInfo,
	handler grpc.UnaryHandler,
) (interface{}, error) {
	actorID := actorFromCtx(ctx)
	if actorID == 0 {
		return handler(ctx, req)
	}
	if !getBucket(actorID).allow() {
		slog.Warn("rate limit exceeded", "actor", actorID, "method", info.FullMethod)
		return nil, status.Errorf(codes.ResourceExhausted,
			"too many requests — please slow down")
	}
	return handler(ctx, req)
}

// ── Recovery interceptor ──────────────────────────────────────────────────────
//
// Converts panics into gRPC Internal errors so one bad request
// cannot crash the entire engine process.

func RecoveryInterceptor(
	ctx context.Context,
	req interface{},
	info *grpc.UnaryServerInfo,
	handler grpc.UnaryHandler,
) (resp interface{}, err error) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("panic in RPC handler",
				"method", info.FullMethod,
				"panic", fmt.Sprintf("%v", r),
				"stack", string(debug.Stack()),
			)
			err = status.Errorf(codes.Internal, "internal server error")
		}
	}()
	return handler(ctx, req)
}

// ── Metadata helpers ──────────────────────────────────────────────────────────

func mdFirst(md metadata.MD, key string) string {
	vals := md.Get(key)
	if len(vals) == 0 {
		return ""
	}
	return vals[0]
}
