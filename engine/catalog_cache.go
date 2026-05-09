package main

import (
	"sync"
	"sync/atomic"

	pb "logistics-engine/api"
)

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

	items, err := FetchCatalogItems()
	if err != nil {
		return nil, err
	}
	catalogCache = items
	catalogIsDirty.Store(false)
	return items, nil
}
