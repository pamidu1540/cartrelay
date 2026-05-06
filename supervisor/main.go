package main

import (
	"fmt"
	"os"
	"os/exec"

	"github.com/spf13/cobra"
)

func main() {
	var rootCmd = &cobra.Command{
		Use:   "logistics",
		Short: "Logistics System Supervisor CLI",
	}

	var startCmd = &cobra.Command{
		Use:   "start",
		Short: "Start all services",
		Run: func(cmd *cobra.Command, args []string) {
			executeDockerCompose("up", "-d", "--build")
		},
	}

	var stopCmd = &cobra.Command{
		Use:   "stop",
		Short: "Stop all services",
		Run: func(cmd *cobra.Command, args []string) {
			executeDockerCompose("down")
		},
	}

	var logsCmd = &cobra.Command{
		Use:   "logs",
		Short: "Follow service logs",
		Run: func(cmd *cobra.Command, args []string) {
			executeDockerCompose("logs", "-f")
		},
	}

	var dashCmd = &cobra.Command{
		Use:     "dashboard",
		Aliases: []string{"dash"},
		Short:   "Open the logistics dashboard",
		Run: func(cmd *cobra.Command, args []string) {
			StartDashboard()
		},
	}

	var updateCmd = &cobra.Command{
		Use:   "update",
		Short: "Check and apply system updates",
		Run: func(cmd *cobra.Command, args []string) {
			if err := CheckAndApplyUpdate(); err != nil {
				fmt.Printf("Update failed: %v\n", err)
				os.Exit(1)
			} else {
				fmt.Println("Update completed successfully.")
			}
		},
	}

	rootCmd.AddCommand(startCmd, stopCmd, logsCmd, dashCmd, updateCmd)

	if err := rootCmd.Execute(); err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
}

func executeDockerCompose(args ...string) {
	composeFile := "/opt/logistics-system/deploy/docker-compose.yml"
	if _, err := os.Stat(composeFile); os.IsNotExist(err) {
		composeFile = "../deploy/docker-compose.yml"
	}

	// Docker Compose V2: "docker compose" instead of "docker-compose"
	baseArgs := append([]string{"compose", "-f", composeFile}, args...)

	cmd := exec.Command("docker", baseArgs...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if err := cmd.Run(); err != nil {
		fmt.Printf("docker compose error: %v\n", err)
		os.Exit(1)
	}
}