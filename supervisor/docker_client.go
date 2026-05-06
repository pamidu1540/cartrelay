package main

import (
	"context"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
)

type SystemStats struct {
	EngineStatus string
	BotStatus    string
	CPUUsage     string
	RAMUsage     string
}

func FetchLiveStats() SystemStats {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return SystemStats{EngineStatus: "ERROR", BotStatus: "ERROR"}
	}
	defer cli.Close()

	ctx := context.Background()
	containers, err := cli.ContainerList(ctx, container.ListOptions{All: true})
	
	stats := SystemStats{
		EngineStatus: "OFFLINE 🔴",
		BotStatus:    "OFFLINE 🔴",
		CPUUsage:     "2.1%", // Placeholder for implementation
		RAMUsage:     "34 MB",
	}

	if err != nil { return stats }

	for _, c := range containers {
		for _, name := range c.Names {
			if name == "/logistics_engine" && c.State == "running" { stats.EngineStatus = "ONLINE 🟢" }
			if name == "/logistics_bot" && c.State == "running" { stats.BotStatus = "ONLINE 🟢" }
		}
	}
	return stats
}