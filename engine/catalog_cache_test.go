package main

import (
	"sync"
	"testing"
	"time"
)

func TestQuantize(t *testing.T) {
	// Quantization to 3 decimal places
	val := quantize(6.927189)
	expected := 6.927
	if val != expected {
		t.Errorf("expected %f, got %f", expected, val)
	}
}

func TestMapboxCache_SetAndGet(t *testing.T) {
	originLat, originLon := 6.927, 79.861
	destLat, destLon := 7.290, 80.633
	expectedDist := 94.5

	setMapboxCached(originLat, originLon, destLat, destLon, expectedDist)

	dist, found := getMapboxCached(originLat, originLon, destLat, destLon)
	if !found {
		t.Fatal("expected cached entry to be found")
	}
	if dist != expectedDist {
		t.Errorf("expected distance %f, got %f", expectedDist, dist)
	}
}

func TestMapboxCache_QuantizationMatch(t *testing.T) {
	// Slight variation within ~100m should hit the same quantized cache key
	lat1, lon1 := 6.92711, 79.86111
	lat2, lon2 := 6.92714, 79.86114 // rounds to same 3 decimals (6.927, 79.861)

	setMapboxCached(lat1, lon1, 7.0, 80.0, 42.0)

	dist, found := getMapboxCached(lat2, lon2, 7.0, 80.0)
	if !found || dist != 42.0 {
		t.Errorf("expected quantized cache hit with dist 42.0, got found=%v, dist=%f", found, dist)
	}
}

func TestMapboxCache_ConcurrentAccess(t *testing.T) {
	t.Helper()
	var wg sync.WaitGroup
	workers := 20
	iterations := 50

	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				lat := float64(workerID) + float64(j)*0.001
				setMapboxCached(lat, 79.8, 7.2, 80.6, float64(j))
				getMapboxCached(lat, 79.8, 7.2, 80.6)
			}
		}(i)
	}
	wg.Wait()
}

func TestInvalidateCatalogCache(t *testing.T) {
	catalogCacheMu.Lock()
	catalogCache["cat|test|0|10"] = &catalogCacheEntry{
		resp:      nil,
		expiresAt: time.Now().Add(time.Hour),
	}
	catalogCacheMu.Unlock()

	InvalidateCatalogCache()

	catalogCacheMu.RLock()
	count := len(catalogCache)
	catalogCacheMu.RUnlock()

	if count != 0 {
		t.Errorf("expected 0 entries after invalidation, got %d", count)
	}
}
