package main

import (
	"context"
	"os"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

type Server struct {
	pb.UnimplementedLogisticsEngineServer
}

func AuthInterceptor(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
	md, ok := metadata.FromIncomingContext(ctx)
	expectedToken := os.Getenv("INTERNAL_TOKEN")
	
	if !ok || len(md["x-internal-token"]) == 0 || md["x-internal-token"][0] != expectedToken {
		return nil, status.Errorf(codes.Unauthenticated, "unauthorized socket access")
	}
	return handler(ctx, req)
}

func (s *Server) CheckHealth(ctx context.Context, req *pb.Empty) (*pb.HealthStatus, error) {
	return &pb.HealthStatus{
		IsHealthy:       true,
		MaintenanceMode: GetConfigBool("maintenance_mode"),
		Version:         "2.0.0",
	}, nil
}

func (s *Server) GetCatalog(ctx context.Context, req *pb.Empty) (*pb.CatalogResponse, error) {
	items, err := FetchCatalogItems()
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to fetch catalog")
	}
	return &pb.CatalogResponse{Items: items}, nil
}

func (s *Server) CalculateQuote(ctx context.Context, req *pb.QuoteRequest) (*pb.QuoteResponse, error) {
	return GenerateQuote(req)
}

func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderResponse, error) {
	orderId, err := InsertOrder(req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to process order")
	}
	return &pb.OrderResponse{
		Success: true,
		OrderId: orderId,
		Message: "Order registered successfully.",
	}, nil
}