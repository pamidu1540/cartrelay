package main

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/emptypb"
)

func TestGRPCToggleMaintenance(t *testing.T) {
	tmpDir := t.TempDir()
	sockFile := filepath.Join(tmpDir, "test.sock")

	lis, err := net.Listen("unix", sockFile)
	if err != nil {
		t.Fatalf("Failed to listen: %v", err)
	}

	var authError bool

	srv := grpc.NewServer(grpc.UnknownServiceHandler(func(srv interface{}, stream grpc.ServerStream) error {
		method, ok := grpc.MethodFromServerStream(stream)
		if !ok || method != "/logistics.LogisticsEngine/ToggleMaintenance" {
			return fmt.Errorf("unexpected method: %v", method)
		}

		md, ok := metadata.FromIncomingContext(stream.Context())
		if !ok {
			return fmt.Errorf("missing metadata")
		}

		tokens := md.Get("x-auth-token")
		if len(tokens) == 0 || tokens[0] == "" {
			return fmt.Errorf("missing x-auth-token")
		}
		if authError {
			return fmt.Errorf("simulated auth error")
		}

		req := &emptypb.Empty{}
		if err := stream.RecvMsg(req); err != nil {
			return err
		}

		resp := &statusResponse{Success: true, Message: "Maintenance mode enabled"}
		return stream.SendMsg(resp)
	}))

	go srv.Serve(lis)
	defer srv.Stop()

	// Give server time to start
	time.Sleep(10 * time.Millisecond)

	origPath := logisticsSocketPath
	logisticsSocketPath = sockFile
	defer func() { logisticsSocketPath = origPath }()

	t.Run("Success", func(t *testing.T) {
		authError = false
		os.Setenv("INTERNAL_TOKEN", "test-token")
		defer os.Unsetenv("INTERNAL_TOKEN")

		success, err := GRPCToggleMaintenance()
		if err != nil {
			t.Errorf("Expected no error, got %v", err)
		}
		if !success {
			t.Errorf("Expected success to be true")
		}
	})

	t.Run("MissingToken", func(t *testing.T) {
		os.Unsetenv("INTERNAL_TOKEN")

		success, err := GRPCToggleMaintenance()
		if err == nil {
			t.Errorf("Expected error for missing token")
		}
		if success {
			t.Errorf("Expected success to be false")
		}
	})

	t.Run("RPCError", func(t *testing.T) {
		authError = true
		os.Setenv("INTERNAL_TOKEN", "test-token")
		defer os.Unsetenv("INTERNAL_TOKEN")

		success, err := GRPCToggleMaintenance()
		if err == nil {
			t.Errorf("Expected error from RPC")
		}
		if success {
			t.Errorf("Expected success to be false")
		}
	})
}
