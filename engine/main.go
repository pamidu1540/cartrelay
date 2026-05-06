package main

import (
	"log"
	"net"
	"os"

	"google.golang.org/grpc"
	pb "logistics-engine/api"
)

const socketPath = "/app/sockets/logistics.sock"

func main() {
	if err := VerifyLicense(); err != nil {
		log.Fatalf("License verification failed: %v", err)
	}

	if err := InitDatabase(); err != nil {
		log.Fatalf("Database initialization failed: %v", err)
	}

	os.MkdirAll("/app/sockets", 0755)
	os.Remove(socketPath)

	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		log.Fatalf("Failed to listen on socket: %v", err)
	}

	if err := os.Chmod(socketPath, 0600); err != nil {
		log.Fatalf("Failed to set socket permissions: %v", err)
	}

	server := grpc.NewServer(
		grpc.UnaryInterceptor(AuthInterceptor),
	)

	pb.RegisterLogisticsEngineServer(server, &Server{})

	log.Printf("Engine listening on %s", socketPath)
	if err := server.Serve(listener); err != nil {
		log.Fatalf("Failed to serve: %v", err)
	}
}