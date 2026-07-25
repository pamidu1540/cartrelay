package main

// routing_math.go — distance calculation, Mapbox client, and nearest-warehouse selection

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/url"
	"os"
	"time"

	"golang.org/x/sync/singleflight"

	pb "logistics-engine/api"
)

const (
	mapboxBaseURL     = "https://api.mapbox.com/directions/v5/mapbox/driving"
	mapboxHTTPTimeout = 5 * time.Second
	earthRadiusKM     = 6371.0
)

var (
	httpClient = &http.Client{
		Timeout: mapboxHTTPTimeout,
	}
	mapboxSF singleflight.Group
)

// FetchMapboxDistance returns driving distance in km between two coordinates.
// Results are cached in the LRU cache (catalog_cache.go).
func FetchMapboxDistance(originLat, originLon, destLat, destLon float64) (float64, error) {
	// Cache hit
	if dist, ok := getMapboxCached(originLat, originLon, destLat, destLon); ok {
		return dist, nil
	}

	token := os.Getenv("MAPBOX_TOKEN")
	if token == "" {
		// Fall back to Haversine if no token configured
		return haversineKM(originLat, originLon, destLat, destLon), nil
	}

	key := mapboxCacheKey(originLat, originLon, destLat, destLon)
	v, err, _ := mapboxSF.Do(key, func() (interface{}, error) {
		// check cache again inside singleflight in case another flight finished
		if dist, ok := getMapboxCached(originLat, originLon, destLat, destLon); ok {
			return dist, nil
		}

		coords := fmt.Sprintf("%.6f,%.6f;%.6f,%.6f",
			originLon, originLat, destLon, destLat)
		reqURL := fmt.Sprintf("%s/%s?access_token=%s&overview=false",
			mapboxBaseURL, coords, url.QueryEscape(token))

		// #nosec G704 -- reqURL is built from trusted constant, float64 coordinates, and token from env
		resp, err := httpClient.Get(reqURL)
		if err != nil {
			slog.Warn("mapbox request failed, falling back to haversine", "err", err)
			return haversineKM(originLat, originLon, destLat, destLon), nil
		}
		defer func() { _ = resp.Body.Close() }()

		if resp.StatusCode != http.StatusOK {
			slog.Warn("mapbox non-200 response, falling back to haversine",
				"status", resp.StatusCode)
			return haversineKM(originLat, originLon, destLat, destLon), nil
		}

		body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
		if err != nil {
			return haversineKM(originLat, originLon, destLat, destLon), nil
		}

		var result struct {
			Routes []struct {
				Distance float64 `json:"distance"` // metres
			} `json:"routes"`
			Code string `json:"code"`
		}
		if err := json.Unmarshal(body, &result); err != nil || result.Code != "Ok" || len(result.Routes) == 0 {
			slog.Warn("mapbox parse failed, falling back to haversine",
				"code", result.Code, "err", err)
			return haversineKM(originLat, originLon, destLat, destLon), nil
		}

		distKM := result.Routes[0].Distance / 1000.0
		setMapboxCached(originLat, originLon, destLat, destLon, distKM)
		return distKM, nil
	})

	if err != nil {
		return haversineKM(originLat, originLon, destLat, destLon), nil
	}
	return v.(float64), nil
}

// nearestWarehouse returns the active warehouse closest to the given
// coordinates using Haversine distance. Returns nil if no active warehouses exist.
func nearestWarehouse(whs []*pb.Warehouse, lat, lon float64) (*pb.Warehouse, float64) {
	var best *pb.Warehouse
	bestDist := math.MaxFloat64

	for _, w := range whs {
		if !w.IsActive {
			continue
		}
		d := haversineKM(lat, lon, w.Lat, w.Lon)
		if d < bestDist {
			bestDist = d
			best = w
		}
	}
	return best, bestDist
}

// haversineKM computes the great-circle distance in km between two WGS-84 points.
func haversineKM(lat1, lon1, lat2, lon2 float64) float64 {
	dLat := toRad(lat2 - lat1)
	dLon := toRad(lon2 - lon1)
	a := math.Sin(dLat/2)*math.Sin(dLat/2) +
		math.Cos(toRad(lat1))*math.Cos(toRad(lat2))*
			math.Sin(dLon/2)*math.Sin(dLon/2)
	c := 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))
	return earthRadiusKM * c
}

func toRad(deg float64) float64 {
	return deg * math.Pi / 180
}

// MapboxHealthCheck pings the Mapbox API to verify the token is valid.
// Called once at startup; logs a warning if the token is misconfigured.
func MapboxHealthCheck(_ context.Context) {
	token := os.Getenv("MAPBOX_TOKEN")
	if token == "" {
		slog.Info("MAPBOX_TOKEN not set — using Haversine distance only")
		return
	}
	// Simple validation: try fetching a known short route (Colombo area)
	_, err := FetchMapboxDistance(6.9271, 79.8612, 6.9350, 79.8500)
	if err != nil {
		slog.Warn("Mapbox health check failed", "err", err)
	} else {
		slog.Info("Mapbox token validated")
	}
}
