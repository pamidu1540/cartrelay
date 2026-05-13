package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"sync"
	"time"

	pb "logistics-engine/api"
)

// mapboxClient uses a 5s timeout to prevent gRPC handlers from blocking forever.
// The default http.Client has no timeout and can block indefinitely.
var mapboxClient = &http.Client{Timeout: 5 * time.Second}

// distCache stores quantized-key → km values.
// Evicted every hour to prevent unbounded growth.
var distCache sync.Map

func init() {
	go func() {
		for range time.Tick(time.Hour) {
			distCache.Range(func(k, _ any) bool {
				distCache.Delete(k)
				return true
			})
		}
	}()
}

// quantize rounds a coordinate to ~1.1 km precision for cache bucketing.
// Two locations within the same ~1 km cell share a cached distance.
func quantize(c float64) float64 { return math.Round(c*100) / 100 }

// GetDistance evaluates all active warehouses and calculates the driving
// distance to the nearest valid depot.
//
// [FIX]: Accepts cartItems to allow for future stock-aware filtering (A-003).
func GetDistance(userLat, userLon float64, cartItems []*pb.CartItem) (float64, string, error) {
	// Dynamically load warehouses from the database (Phase 1)
	warehouses, err := DBGetWarehouses()
	
	// [FIX]: Handle the empty origin state meaningfully.
	// If the database has no warehouses, we cannot calculate a distance.
	if err != nil || len(warehouses) == 0 {
		slog.Error("GetDistance failed: no warehouses configured in database")
		return 0, "", fmt.Errorf("delivery unavailable: no warehouses configured by admin")
	}

	// Step 1: The Haversine Race to find the closest warehouse origin.
	// In the future, this loop will filter out warehouses that lack stock for cartItems.
	best := warehouses[0]
	bestH := math.MaxFloat64
	for _, w := range warehouses {
		if h := Haversine(w.Lon, w.Lat, userLon, userLat); h < bestH {
			bestH = h
			best = w
		}
	}

	// Step 2: Check the Cache using quantized grid cell keys.
	cacheKey := fmt.Sprintf("%.2f,%.2f→%.2f,%.2f",
		quantize(best.Lat), quantize(best.Lon),
		quantize(userLat), quantize(userLon))

	if v, ok := distCache.Load(cacheKey); ok {
		return v.(float64), best.Name, nil
	}

	// Step 3: Fetch real road distance from Mapbox.
	dist, err := fetchMapboxDistance(best.Lon, best.Lat, userLon, userLat)
	if err != nil {
		// [FIX]: Mapbox failures fall back to Haversine × 1.3 (road approximation).
		slog.Warn("Mapbox unavailable — Haversine fallback", "warehouse", best.Name, "err", err)
		dist = bestH * 1.3
	}

	distCache.Store(cacheKey, dist)
	return dist, best.Name, nil
}

func fetchMapboxDistance(fromLon, fromLat, toLon, toLat float64) (float64, error) {
	token := mapboxToken
	if token == "" {
		return 0, fmt.Errorf("MAPBOX_TOKEN not set")
	}
	url := fmt.Sprintf(
		"https://api.mapbox.com/directions/v5/mapbox/driving/%.6f,%.6f;%.6f,%.6f?access_token=%s",
		fromLon, fromLat, toLon, toLat, token,
	)
	resp, err := mapboxClient.Get(url)
	if err != nil {
		return 0, fmt.Errorf("mapbox request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("mapbox HTTP %d", resp.StatusCode)
	}

	var result struct {
		Routes []struct {
			Distance float64 `json:"distance"`
		} `json:"routes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return 0, fmt.Errorf("mapbox decode: %w", err)
	}
	if len(result.Routes) == 0 {
		return 0, fmt.Errorf("mapbox returned no routes")
	}
	
	// Convert metres → km
	return result.Routes[0].Distance / 1000.0, nil 
}

// Haversine returns the great-circle distance in km between two lon/lat points.
func Haversine(lon1, lat1, lon2, lat2 float64) float64 {
	const R = 6371.0
	dLat := (lat2 - lat1) * math.Pi / 180
	dLon := (lon2 - lon1) * math.Pi / 180
	a := math.Sin(dLat/2)*math.Sin(dLat/2) +
		math.Cos(lat1*math.Pi/180)*math.Cos(lat2*math.Pi/180)*
			math.Sin(dLon/2)*math.Sin(dLon/2)
	return R * 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
}