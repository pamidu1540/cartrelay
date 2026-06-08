package main

// catalog_cache.go — in-memory catalog and Mapbox distance caches

import (
	"container/list"
	"context"
	"log/slog"
	"math"
	"os"
	"strconv"
	"sync"
	"time"

	pb "logistics-engine/api"
)

// ── Catalog cache ─────────────────────────────────────────────────────────────

const (
	defaultCatalogMaxItems = 2000
	catalogTTL             = 5 * time.Minute
)

type catalogCacheEntry struct {
	resp      *pb.CatalogResponse
	expiresAt time.Time
}

var (
	catalogCache   map[string]*catalogCacheEntry // key = "cat:search:page:size"
	catalogCacheMu sync.RWMutex
	catalogMaxItems int
)

func init() {
	catalogMaxItems = defaultCatalogMaxItems
	if v := os.Getenv("CATALOG_CACHE_MAX_ITEMS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			catalogMaxItems = n
		}
	}
	catalogCache = make(map[string]*catalogCacheEntry)
}

// GetCachedCatalog returns a cached CatalogResponse or fetches and caches it.
func GetCachedCatalog(ctx context.Context, cat, search string, page, pageSize int32) (*pb.CatalogResponse, error) {
	key := catalogKey(cat, search, page, pageSize)

	catalogCacheMu.RLock()
	entry, ok := catalogCache[key]
	catalogCacheMu.RUnlock()

	if ok && time.Now().Before(entry.expiresAt) {
		return entry.resp, nil
	}

	// Cache miss — fetch from DB
	items, total, err := FetchCatalogItems(ctx, cat, search, page, pageSize)
	if err != nil {
		return nil, err
	}
	resp := &pb.CatalogResponse{Items: items, TotalCount: total}

	catalogCacheMu.Lock()
	// Evict if over cap (simple: clear all — LRU would be overkill for catalog)
	if len(catalogCache) >= catalogMaxItems {
		slog.Info("catalog cache eviction", "entries", len(catalogCache))
		catalogCache = make(map[string]*catalogCacheEntry)
	}
	catalogCache[key] = &catalogCacheEntry{
		resp:      resp,
		expiresAt: time.Now().Add(catalogTTL),
	}
	catalogCacheMu.Unlock()

	return resp, nil
}

// InvalidateCatalogCache clears the entire catalog cache.
// Called on any admin mutation to items.
func InvalidateCatalogCache() {
	catalogCacheMu.Lock()
	catalogCache = make(map[string]*catalogCacheEntry)
	catalogCacheMu.Unlock()
	slog.Debug("catalog cache invalidated")
}

func catalogKey(cat, search string, page, pageSize int32) string {
	return cat + "|" + search + "|" + strconv.Itoa(int(page)) + "|" + strconv.Itoa(int(pageSize))
}

// ── Mapbox distance cache ─────────────────────────────────────────────────────
//
// LRU with per-entry TTL. Each entry has its own expiry timestamp;
// when the cache exceeds maxEntries, the oldest half is dropped.

const (
	mapboxCacheTTL          = 1 * time.Hour
	defaultMapboxMaxEntries = 2000
	quantizePrecision       = 3 // ~111 m resolution — same location → same cache key
)

type mapboxEntry struct {
	key       string
	distKM    float64
	expiresAt time.Time
}

var (
	mapboxCache      map[string]*list.Element
	mapboxList       *list.List
	mapboxCacheMu    sync.RWMutex
	mapboxMaxEntries int
)

func init() {
	mapboxMaxEntries = defaultMapboxMaxEntries
	if v := os.Getenv("MAPBOX_CACHE_MAX_ENTRIES"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			mapboxMaxEntries = n
		}
	}
	mapboxCache = make(map[string]*list.Element)
	mapboxList = list.New()
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
	
	mapboxCacheMu.RLock()
	ele, ok := mapboxCache[key]
	mapboxCacheMu.RUnlock()

	if !ok {
		return 0, false
	}

	e := ele.Value.(*mapboxEntry)
	if time.Now().After(e.expiresAt) {
		mapboxCacheMu.Lock()
		if ele2, ok := mapboxCache[key]; ok && ele2 == ele {
			mapboxList.Remove(ele)
			delete(mapboxCache, key)
		}
		mapboxCacheMu.Unlock()
		return 0, false
	}

	mapboxCacheMu.Lock()
	if ele3, ok := mapboxCache[key]; ok && ele3 == ele {
		mapboxList.MoveToFront(ele)
	}
	mapboxCacheMu.Unlock()

	return e.distKM, true
}

func setMapboxCached(originLat, originLon, destLat, destLon, distKM float64) {
	key := mapboxCacheKey(originLat, originLon, destLat, destLon)
	mapboxCacheMu.Lock()
	defer mapboxCacheMu.Unlock()

	if ele, ok := mapboxCache[key]; ok {
		mapboxList.MoveToFront(ele)
		ele.Value.(*mapboxEntry).distKM = distKM
		ele.Value.(*mapboxEntry).expiresAt = time.Now().Add(mapboxCacheTTL)
		return
	}

	ent := &mapboxEntry{
		key:       key,
		distKM:    distKM,
		expiresAt: time.Now().Add(mapboxCacheTTL),
	}
	ele := mapboxList.PushFront(ent)
	mapboxCache[key] = ele

	if mapboxList.Len() > mapboxMaxEntries {
		evictOldestMapbox(mapboxMaxEntries / 2)
	}
}

// evictOldestMapbox removes the n oldest entries using LRU list.
// Called with mapboxCacheMu held.
func evictOldestMapbox(n int) {
	evicted := 0
	for i := 0; i < n; i++ {
		ele := mapboxList.Back()
		if ele != nil {
			mapboxList.Remove(ele)
			ent := ele.Value.(*mapboxEntry)
			delete(mapboxCache, ent.key)
			evicted++
		}
	}
	slog.Debug("mapbox cache eviction", "evicted", evicted, "remaining", mapboxList.Len())
}

