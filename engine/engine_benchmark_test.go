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
)

// BenchmarkHaversineKM benchmarks high-speed trigonometric distance calculation.
func BenchmarkHaversineKM(b *testing.B) {
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = haversineKM(6.9271, 79.8612, 7.2906, 80.6337)
	}
}

// BenchmarkMapboxCacheKey benchmarks spatial quantization and cache key formatting.
func BenchmarkMapboxCacheKey(b *testing.B) {
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = mapboxCacheKey(6.927189, 79.861234, 7.290612, 80.633789)
	}
}

// BenchmarkMapboxCache_SetAndGet benchmarks single-thread distance LRU read/write operations.
func BenchmarkMapboxCache_SetAndGet(b *testing.B) {
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		setMapboxCached(6.927, 79.861, 7.290, 80.633, 94.5)
		_, _ = getMapboxCached(6.927, 79.861, 7.290, 80.633)
	}
}

// BenchmarkMapboxCache_HighContention benchmarks distance cache under parallel worker thread stress.
func BenchmarkMapboxCache_HighContention(b *testing.B) {
	b.ReportAllocs()
	b.RunParallel(func(pb *testing.PB) {
		workerID := float64(time.Now().UnixNano() % 1000)
		for pb.Next() {
			setMapboxCached(6.927+workerID*0.001, 79.861, 7.290, 80.633, 94.5)
			_, _ = getMapboxCached(6.927+workerID*0.001, 79.861, 7.290, 80.633)
		}
	})
}

// BenchmarkValidateHMACToken benchmarks HMAC-SHA256 calculation overhead per RPC request.
func BenchmarkValidateHMACToken(b *testing.B) {
	secret := "benchmark-secret-key-12345"
	_ = os.Setenv("INTERNAL_TOKEN", secret)
	defer func() { _ = os.Unsetenv("INTERNAL_TOKEN") }()

	method := "/logistics.LogisticsEngine/GetCatalog"
	now := time.Now().Unix()

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(fmt.Sprintf("%s:%d", method, now)))
	token := hex.EncodeToString(mac.Sum(nil))

	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = validateHMACToken(token, method)
	}
}

// BenchmarkRecoveryInterceptor benchmarks panic-recovery interceptor wrapper overhead on normal RPC calls.
func BenchmarkRecoveryInterceptor(b *testing.B) {
	ctx := context.Background()
	info := &grpc.UnaryServerInfo{FullMethod: "/logistics.LogisticsEngine/GetCatalog"}
	noopHandler := func(_ context.Context, _ interface{}) (interface{}, error) {
		return "ok", nil
	}

	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_, _ = RecoveryInterceptor(ctx, nil, info, noopHandler)
	}
}
