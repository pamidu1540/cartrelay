package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"sync/atomic"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	pb "logistics-supervisor/api"
)

// SystemStats holds the last-collected snapshot of container health.
type SystemStats struct {
	EngineStatus string
	BotStatus    string
	CPUUsage     string
	RAMUsage     string
	CollectedAt  time.Time
}

// statsAtom is updated by the background collector and read by the TUI.
// FIX: Decouples collection from rendering — the dashboard never blocks on Docker.
var statsAtom atomic.Value

func init() {
	statsAtom.Store(SystemStats{
		EngineStatus: "STARTING…",
		BotStatus:    "STARTING…",
		CPUUsage:     "…",
		RAMUsage:     "…",
		CollectedAt:  time.Now(),
	})
}

// StartStatsCollector runs a goroutine that refreshes container stats every
// interval. The TUI calls LatestStats() which returns immediately.
func StartStatsCollector(interval time.Duration) {
	go func() {
		for {
			statsAtom.Store(fetchLiveStats())
			time.Sleep(interval)
		}
	}()
}

// LatestStats returns the most-recently collected stats without blocking.
func LatestStats() SystemStats {
	return statsAtom.Load().(SystemStats)
}

// dockerContainerStats is the subset of Docker's ContainerStats JSON we need.
type dockerContainerStats struct {
	CPUStats struct {
		CPUUsage       struct{ TotalUsage uint64 `json:"total_usage"` } `json:"cpu_usage"`
		SystemCPUUsage uint64 `json:"system_cpu_usage"`
		OnlineCPUs     uint32 `json:"online_cpus"`
	} `json:"cpu_stats"`
	PreCPUStats struct {
		CPUUsage       struct{ TotalUsage uint64 `json:"total_usage"` } `json:"cpu_usage"`
		SystemCPUUsage uint64 `json:"system_cpu_usage"`
	} `json:"precpu_stats"`
	MemoryStats struct {
		Usage uint64 `json:"usage"`
	} `json:"memory_stats"`
}

func fetchLiveStats() SystemStats {
	result := SystemStats{
		EngineStatus: "OFFLINE 🔴",
		BotStatus:    "OFFLINE 🔴",
		CPUUsage:     "N/A",
		RAMUsage:     "N/A",
		CollectedAt:  time.Now(),
	}

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		result.EngineStatus = "DOCKER ERR 🔴"
		result.BotStatus = "DOCKER ERR 🔴"
		return result
	}
	defer cli.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()

	containers, err := cli.ContainerList(ctx, container.ListOptions{All: true})
	if err != nil {
		return result
	}

	for _, c := range containers {
		for _, name := range c.Names {
			switch name {
			case "/logistics_engine":
				if c.State == "running" {
					result.EngineStatus = "ONLINE 🟢"
					result.CPUUsage, result.RAMUsage = containerMetrics(cli, c.ID)
				}
			case "/logistics_bot":
				if c.State == "running" {
					result.BotStatus = "ONLINE 🟢"
				}
			}
		}
	}
	return result
}

func containerMetrics(cli *client.Client, containerID string) (cpu, ram string) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	resp, err := cli.ContainerStats(ctx, containerID, false)
	if err != nil {
		return "N/A", "N/A"
	}
	defer resp.Body.Close()

	var s dockerContainerStats
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return "N/A", "N/A"
	}

	cpuDelta := float64(s.CPUStats.CPUUsage.TotalUsage - s.PreCPUStats.CPUUsage.TotalUsage)
	sysDelta := float64(s.CPUStats.SystemCPUUsage - s.PreCPUStats.SystemCPUUsage)
	cpuPct := 0.0
	if sysDelta > 0 && s.CPUStats.OnlineCPUs > 0 {
		cpuPct = (cpuDelta / sysDelta) * float64(s.CPUStats.OnlineCPUs) * 100.0
	}
	ramMB := float64(s.MemoryStats.Usage) / (1024 * 1024)
	return fmt.Sprintf("%.1f%%", cpuPct), fmt.Sprintf("%.0f MB", ramMB)
}

// RestartContainers restarts both service containers via the Docker SDK.
func RestartContainers() error {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return fmt.Errorf("docker client: %w", err)
	}
	defer cli.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	timeout := 10
	for _, name := range []string{"logistics_engine", "logistics_bot"} {
		if err := cli.ContainerRestart(ctx, name, container.StopOptions{Timeout: &timeout}); err != nil {
			return fmt.Errorf("restart %s: %w", name, err)
		}
	}
	return nil
}

// executeDockerCompose runs an arbitrary docker compose sub-command.
func executeDockerCompose(args ...string) error {
	base := []string{"compose", "-f", "/opt/logistics-system/deploy/docker-compose.yml"}
	cmd := exec.Command("docker", append(base, args...)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// GRPCToggleMaintenance dials the engine socket and calls ToggleMaintenance.
// FIX: Uses the generated pb.StatusResponse type instead of a hand-rolled
// struct with protobuf field tags, which would break on proto field renumbering.
func GRPCToggleMaintenance() (bool, error) {
	socketPath := "/app/sockets/logistics.sock"
	token := os.Getenv("INTERNAL_TOKEN")
	if token == "" {
		return false, fmt.Errorf("INTERNAL_TOKEN not set")
	}

	conn, err := grpc.NewClient(
		"passthrough:///"+socketPath,
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socketPath)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return false, fmt.Errorf("connect: %w", err)
	}
	defer conn.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("x-internal-token", token))

	// FIX: Use the generated proto type — not a hand-crafted struct.
	var reply pb.StatusResponse
	if err := conn.Invoke(ctx, "/logistics.LogisticsEngine/ToggleMaintenance",
		&pb.Empty{}, &reply); err != nil {
		return false, fmt.Errorf("RPC: %w", err)
	}
	return reply.Message == "Maintenance mode enabled", nil
}
