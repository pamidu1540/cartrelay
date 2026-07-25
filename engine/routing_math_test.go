package main

import (
	"math"
	"testing"

	pb "logistics-engine/api"
)

func TestHaversineKM_IdenticalPoints(t *testing.T) {
	dist := haversineKM(6.9271, 79.8612, 6.9271, 79.8612)
	if dist != 0 {
		t.Errorf("expected 0 distance for identical points, got %f", dist)
	}
}

func TestHaversineKM_KnownDistance(t *testing.T) {
	// Colombo (6.9271, 79.8612) to Kandy (7.2906, 80.6337) is approx 95 km
	colomboLat, colomboLon := 6.9271, 79.8612
	kandyLat, kandyLon := 7.2906, 80.6337

	dist := haversineKM(colomboLat, colomboLon, kandyLat, kandyLon)
	if math.Abs(dist-95.0) > 10.0 {
		t.Errorf("expected distance ~95km between Colombo and Kandy, got %f km", dist)
	}
}

func TestNearestWarehouse(t *testing.T) {
	wh1 := &pb.Warehouse{Id: "wh-colombo", Name: "Colombo", Lat: 6.9271, Lon: 79.8612, IsActive: true}
	wh2 := &pb.Warehouse{Id: "wh-kandy", Name: "Kandy", Lat: 7.2906, Lon: 80.6337, IsActive: true}
	wh3 := &pb.Warehouse{Id: "wh-galle", Name: "Galle (Inactive)", Lat: 6.0535, Lon: 80.2210, IsActive: false}

	warehouses := []*pb.Warehouse{wh1, wh2, wh3}

	// Point near Colombo should select Colombo
	closest, dist := nearestWarehouse(warehouses, 6.9300, 79.8600)
	if closest == nil || closest.Id != "wh-colombo" {
		t.Errorf("expected wh-colombo, got %v", closest)
	}
	if dist <= 0 {
		t.Errorf("expected positive distance, got %f", dist)
	}

	// Point near Galle (inactive) should ignore Galle and select Colombo
	closestGalle, _ := nearestWarehouse(warehouses, 6.0500, 80.2200)
	if closestGalle == nil || closestGalle.Id == "wh-galle" {
		t.Errorf("expected non-inactive warehouse, got %v", closestGalle)
	}
}

func TestNearestWarehouse_EmptyOrAllInactive(t *testing.T) {
	whInactive := &pb.Warehouse{Id: "wh-1", IsActive: false}
	closest, dist := nearestWarehouse([]*pb.Warehouse{whInactive}, 6.9271, 79.8612)
	if closest != nil {
		t.Errorf("expected nil for all-inactive list, got %v", closest)
	}
	if dist != math.MaxFloat64 {
		t.Errorf("expected MaxFloat64 distance, got %f", dist)
	}
}

func TestFetchMapboxDistance_FallbackToHaversine(t *testing.T) {
	// Without MAPBOX_TOKEN set, it should transparently fall back to Haversine
	dist, err := FetchMapboxDistance(6.9271, 79.8612, 7.2906, 80.6337)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if dist <= 0 {
		t.Errorf("expected positive fallback distance, got %f", dist)
	}
}
