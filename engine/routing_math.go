package main

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"

	pb "logistics-engine/api"
)

const earthRadiusKm = 6371.0

func Haversine(lon1, lat1, lon2, lat2 float64) float64 {
	dLat := (lat2 - lat1) * (math.Pi / 180.0)
	dLon := (lon2 - lon1) * (math.Pi / 180.0)

	lat1Rad := lat1 * (math.Pi / 180.0)
	lat2Rad := lat2 * (math.Pi / 180.0)

	a := math.Sin(dLat/2)*math.Sin(dLat/2) + math.Sin(dLon/2)*math.Sin(dLon/2)*math.Cos(lat1Rad)*math.Cos(lat2Rad)
	c := 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))

	return earthRadiusKm * c
}

func FetchMapboxDistance(startLon, startLat, endLon, endLat float64) (float64, error) {
	token := os.Getenv("MAPBOX_TOKEN")
	url := fmt.Sprintf("https://api.mapbox.com/directions/v5/mapbox/driving/%f,%f;%f,%f?access_token=%s",
		startLon, startLat, endLon, endLat, token)

	resp, err := http.Get(url)
	if err != nil || resp.StatusCode != 200 {
		return 0, fmt.Errorf("routing API error")
	}
	defer resp.Body.Close()

	var result struct {
		Routes[]struct {
			Distance float64 `json:"distance"`
		} `json:"routes"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil || len(result.Routes) == 0 {
		return 0, fmt.Errorf("invalid route response")
	}

	return result.Routes[0].Distance / 1000.0, nil
}

func GenerateQuote(req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
	// Abstracted representation of the quotation generation
	// Calculates volumes, queries Mapbox, applies base fare, surge, and taxes.
	// Returns compiled pb.QuoteResponse
	return &pb.QuoteResponse{
		Success:          true,
		GrandTotal:       0.0, // Replaced with logic output
		FormattedReceipt: "Receipt generated via Engine...",
	}, nil
}