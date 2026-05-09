package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// FIX: All Mapbox calls use this client with a 5 s timeout.
// The default http.Client has no timeout and can block a gRPC handler forever.
var mapboxClient = &http.Client{Timeout: 5 * time.Second}

// distanceCache stores quantized-key → km values.
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
// Two locations within the same ~1 km cell share a cached distance, which is
// accurate enough for delivery pricing.
func quantize(c float64) float64 { return math.Round(c*100) / 100 }

type depot struct {
	Name string  `json:"name"`
	Lat  float64 `json:"lat"`
	Lon  float64 `json:"lon"`
}

// loadDepots reads depot config. Supports two formats:
//  1. New (preferred): depots_json = [{"name":"HQ","lat":6.9,"lon":79.8},...]
//  2. Legacy: depot_lat + depot_lon + depot_name keys.
func loadDepots() ([]depot, error) {
	raw, err := GetConfigValue("depots_json")
	if err != nil {
		return nil, err
	}
	if raw != "" {
		var ds []depot
		if err := json.Unmarshal([]byte(raw), &ds); err != nil {
			return nil, fmt.Errorf("depots_json parse error: %w", err)
		}
		return ds, nil
	}

	// Legacy single-depot fallback.
	// FIX: strconv.ParseFloat replaces fmt.Sscanf — errors are no longer silently swallowed.
	latStr, _ := GetConfigValue("depot_lat")
	lonStr, _ := GetConfigValue("depot_lon")
	nameStr, _ := GetConfigValue("depot_name")

	if latStr == "" || lonStr == "" {
		// Absolute fallback — Colombo, Sri Lanka.
		slog.Warn("depot coordinates not configured, using built-in fallback")
		return []depot{{Name: "Main Warehouse", Lat: 6.9271, Lon: 79.8612}}, nil
	}

	lat, err := strconv.ParseFloat(latStr, 64)
	if err != nil {
		return nil, fmt.Errorf("depot_lat is not a valid float (%q): %w", latStr, err)
	}
	lon, err := strconv.ParseFloat(lonStr, 64)
	if err != nil {
		return nil, fmt.Errorf("depot_lon is not a valid float (%q): %w", lonStr, err)
	}
	if nameStr == "" {
		nameStr = "Main Warehouse"
	}
	return []depot{{Name: nameStr, Lat: lat, Lon: lon}}, nil
}

// GetDistance returns the driving distance (km) and the name of the nearest
// depot to the user's coordinates. Results are cached by quantized grid cell.
// Mapbox failures fall back to Haversine × 1.3 (road-distance approximation).
func GetDistance(userLat, userLon float64) (float64, string, error) {
	depots, err := loadDepots()
	if err != nil || len(depots) == 0 {
		return 0, "", fmt.Errorf("no depots available: %w", err)
	}

	// Pick the nearest depot by straight-line distance.
	best := depots[0]
	bestH := math.MaxFloat64
	for _, d := range depots {
		if h := Haversine(d.Lon, d.Lat, userLon, userLat); h < bestH {
			bestH = h
			best = d
		}
	}

	cacheKey := fmt.Sprintf("%.2f,%.2f→%.2f,%.2f",
		quantize(best.Lat), quantize(best.Lon),
		quantize(userLat), quantize(userLon))

	if v, ok := distCache.Load(cacheKey); ok {
		return v.(float64), best.Name, nil
	}

	dist, err := fetchMapboxDistance(best.Lon, best.Lat, userLon, userLat)
	if err != nil {
		slog.Warn("Mapbox unavailable — Haversine fallback", "depot", best.Name, "err", err)
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
	return result.Routes[0].Distance / 1000.0, nil // metres → km
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
