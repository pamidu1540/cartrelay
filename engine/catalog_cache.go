package main

import (
	"log/slog"
	"math"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	pb "logistics-engine/api"
)

// ── Catalog Cache ─────────────────────────────────────────────────────────────

var (
	catalogCache   map[string]*pb.Item
	catalogMu      sync.RWMutex
	catalogIsDirty atomic.Bool
)

func init() {
	// Force a DB read on first GetCachedCatalog call.
	catalogIsDirty.Store(true)
}

// InvalidateCatalog marks the cache stale. Called by any handler that mutates
// the items table (AddItem, EditItemPrice, EditItemStock, RemoveItem).
func InvalidateCatalog() {
	catalogIsDirty.Store(true)
}

// GetCachedCatalog returns the in-memory catalog, rebuilding from DB only when
// a write has occurred since the last read. Under steady-state load (no admin
// mutations) this never touches the DB.
func GetCachedCatalog() (map[string]*pb.Item, error) {
	// Fast path: cache is clean and populated.
	if !catalogIsDirty.Load() {
		catalogMu.RLock()
		c := catalogCache
		catalogMu.RUnlock()
		if c != nil {
			return c, nil
		}
	}

	// Slow path: rebuild from DB.
	catalogMu.Lock()
	defer catalogMu.Unlock()

	// Double-checked locking: another goroutine may have rebuilt while we waited.
	if !catalogIsDirty.Load() && catalogCache != nil {
		return catalogCache, nil
	}

	// Clear the dirty flag BEFORE fetching to prevent a race condition.
	// If an invalidation occurs during FetchCatalogItems, it will set the flag to true,
	// and we will leave it as true so the next call rebuilds the cache.
	catalogIsDirty.Store(false)

	items, err := FetchCatalogItems()
	if err != nil {
		// Revert the dirty flag on error
		catalogIsDirty.Store(true)
		return nil, err
	}
	catalogCache = items
	return items, nil
}

// ── Mapbox Distance Cache ─────────────────────────────────────────────────────
//
// LRU with per-entry TTL to prevent unbounded virtual/physical memory growth.
// Entries are not evicted all-at-once; each entry has its own expiry.
// When the cache exceeds mapboxMaxEntries, the oldest half is dropped.

const (
	mapboxCacheTTL          = 1 * time.Hour
	defaultMapboxMaxEntries = 2000 // Hard cap on cache size to avoid leaks/memory inflation
	quantizePrecision       = 2    // ~1.1 km precision for grid bucket coordinates
)

type mapboxEntry struct {
	distKM     float64
	expiresAt  time.Time
	insertedAt time.Time
}

var (
	mapboxCache      map[string]*mapboxEntry
	mapboxCacheMu    sync.Mutex
	mapboxMaxEntries int
)

func init() {
	mapboxMaxEntries = defaultMapboxMaxEntries
	if v := os.Getenv("MAPBOX_CACHE_MAX_ENTRIES"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			mapboxMaxEntries = n
		}
	}
	mapboxCache = make(map[string]*mapboxEntry)
}

func mapboxCacheKey(originLat, originLon, destLat, destLon float64) string {
	return strconv.FormatFloat(quantize(originLat), 'f', quantizePrecision, 64) + "," +
		strconv.FormatFloat(quantize(originLon), 'f', quantizePrecision, 64) + "->" +
		strconv.FormatFloat(quantize(destLat), 'f', quantizePrecision, 64) + "," +
		strconv.FormatFloat(quantize(destLon), 'f', quantizePrecision, 64)
}

func quantize(v float64) float64 {
	factor := math.Pow10(quantizePrecision)
	return math.Round(v*factor) / factor
}

func getMapboxCached(originLat, originLon, destLat, destLon float64) (float64, bool) {
	key := mapboxCacheKey(originLat, originLon, destLat, destLon)
	mapboxCacheMu.Lock()
	defer mapboxCacheMu.Unlock()

	e, ok := mapboxCache[key]
	if !ok {
		return 0, false
	}
	if time.Now().After(e.expiresAt) {
		delete(mapboxCache, key)
		return 0, false
	}
	return e.distKM, true
}

func setMapboxCached(originLat, originLon, destLat, destLon, distKM float64) {
	key := mapboxCacheKey(originLat, originLon, destLat, destLon)
	mapboxCacheMu.Lock()
	defer mapboxCacheMu.Unlock()

	// LRU Eviction: drop oldest half when at capacity
	if len(mapboxCache) >= mapboxMaxEntries {
		evictOldestMapbox(mapboxMaxEntries / 2)
	}

	mapboxCache[key] = &mapboxEntry{
		distKM:     distKM,
		expiresAt:  time.Now().Add(mapboxCacheTTL),
		insertedAt: time.Now(),
	}
}

// evictOldestMapbox removes the n oldest entries by insertedAt.
// Called with mapboxCacheMu held.
func evictOldestMapbox(n int) {
	if n <= 0 || len(mapboxCache) == 0 {
		return
	}
	type kv struct {
		key string
		ts  time.Time
	}
	entries := make([]kv, 0, len(mapboxCache))
	for k, v := range mapboxCache {
		entries = append(entries, kv{k, v.insertedAt})
	}

	// Partial sort: find the oldest half
	evicted := 0
	var oldest time.Time
	for _, e := range entries {
		if oldest.IsZero() || e.ts.Before(oldest) {
			oldest = e.ts
		}
	}
	threshold := oldest.Add(mapboxCacheTTL / 2)
	for _, e := range entries {
		if evicted >= n {
			break
		}
		if e.ts.Before(threshold) {
			delete(mapboxCache, e.key)
			evicted++
		}
	}
	slog.Debug("mapbox cache eviction", "evicted", evicted, "remaining", len(mapboxCache))
}
