package main

// grpc_server.go — gRPC server structure, lifecycle helpers, and error mapping

import (
	"context"
	"errors"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "logistics-engine/api"
)

func fireAndForget(action func(ctx context.Context)) {
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		action(ctx)
	}()
}

// Server implements pb.LogisticsEngineServer and handles all gRPC API requests.
type Server struct {
	pb.UnimplementedLogisticsEngineServer
}

// ── Internal Helpers & Error Translation ─────────────────────────────────────

func grpcErr(err error) error {
	switch {
	case errors.Is(err, ErrNotFound):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, ErrUnauthorized):
		return status.Error(codes.Unauthenticated, err.Error())
	case errors.Is(err, ErrInvalidTransition):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, ErrDuplicateOrder):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, ErrOutOfStock):
		return status.Error(codes.ResourceExhausted, err.Error())
	case errors.Is(err, ErrPriceMismatch):
		return status.Error(codes.InvalidArgument, err.Error())
	default:
		return status.Error(codes.Internal, err.Error())
	}
}
