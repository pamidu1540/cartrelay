package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// Version is injected at build time via -ldflags="-X main.Version=..."
var Version = "dev"

func loadEnv() {
	var content []byte
	var err error

	encPath := "/opt/logistics-system/deploy/.env.enc"
	if _, statErr := os.Stat(encPath); statErr == nil {
		content, err = decryptEnvFile(encPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "failed to decrypt .env.enc: %v\n", err)
			return
		}
	} else {
		content, err = os.ReadFile("/opt/logistics-system/deploy/.env")
		if err != nil {
			return
		}
	}

	lines := strings.Split(string(content), "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) == 2 {
			val := strings.TrimSpace(parts[1])
			val = strings.Trim(val, `"'`)
			_ = os.Setenv(strings.TrimSpace(parts[0]), val)
		}
	}
}

func main() {
	loadEnv()

	root := &cobra.Command{
		Use:   "cartrelay",
		Short: "CartRelay ecommerce deployment supervisor",
	}

	// ── start ─────────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "start",
		Short: "Start all services",
		RunE: func(_ *cobra.Command, _ []string) error {
			return executeDockerCompose("up", "-d")
		},
	})

	// ── stop ──────────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "stop",
		Short: "Stop all services",
		RunE: func(_ *cobra.Command, _ []string) error {
			return executeDockerCompose("down")
		},
	})

	// ── restart ───────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "restart",
		Short: "Restart all service containers",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := RestartContainers(); err != nil {
				return err
			}
			fmt.Println("Containers restarted.")
			return nil
		},
	})

	// ── status ────────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "status",
		Short: "Print a quick container health snapshot",
		Run: func(_ *cobra.Command, _ []string) {
			s := fetchLiveStats()
			fmt.Printf("Engine : %s\n", s.EngineStatus)
			fmt.Printf("Bot    : %s\n", s.BotStatus)
			fmt.Printf("CPU    : %s\n", s.CPUUsage)
			fmt.Printf("RAM    : %s\n", s.RAMUsage)
			fmt.Printf("At     : %s\n", s.CollectedAt.Format(time.RFC3339))
		},
	})

	// ── logs ──────────────────────────────────────────────────────────────────
	logsCmd := &cobra.Command{
		Use:   "logs",
		Short: "Follow service logs (use --service engine|bot to filter)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			svc, _ := cmd.Flags().GetString("service")
			args := []string{"logs", "-f"}
			switch svc {
			case "engine":
				args = append(args, "engine")
			case "bot":
				args = append(args, "bot")
			case "":
				// Follow all services.
			default:
				return fmt.Errorf("unknown service %q — use 'engine' or 'bot'", svc)
			}
			return executeDockerCompose(args...)
		},
	}
	logsCmd.Flags().StringP("service", "s", "", "Filter logs to a specific service (engine|bot)")
	root.AddCommand(logsCmd)

	// ── dashboard ─────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:     "dashboard",
		Aliases: []string{"dash"},
		Short:   "Open the interactive TUI dashboard",
		Run: func(_ *cobra.Command, _ []string) {
			StartDashboard()
		},
	})

	// ── update ────────────────────────────────────────────────────────────────
	updateCmd := &cobra.Command{
		Use:   "update",
		Short: "Check and apply OTA system updates",
		RunE: func(cmd *cobra.Command, _ []string) error {
			dryRun, _ := cmd.Flags().GetBool("dry-run")
			if dryRun {
				manifestURL := os.Getenv("UPDATE_MANIFEST_URL")
				if manifestURL == "" {
					manifestURL = "https://github.com/pamidu1540/cartrelay/releases/latest/download/manifest.json"
				}
				m, err := fetchManifest(manifestURL)
				if err != nil {
					return err
				}
				fmt.Printf("[dry-run] available version: %s\n", m.Version)
				fmt.Printf("[dry-run] tarball URL: %s\n", m.URL)
				fmt.Println("[dry-run] no changes applied.")
				return nil
			}
			if err := CheckAndApplyUpdate(); err != nil {
				return err
			}
			fmt.Println("Update completed successfully.")
			return nil
		},
	}
	updateCmd.Flags().Bool("dry-run", false, "Fetch manifest metadata without applying or verifying the payload")
	root.AddCommand(updateCmd)

	// ── rollback ──────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "rollback",
		Short: "Re-apply the most recent backup tarball",
		RunE: func(_ *cobra.Command, _ []string) error {
			if err := RollbackUpdate(); err != nil {
				return err
			}
			fmt.Println("Rollback completed.")
			return nil
		},
	})

	// ── maintenance ───────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "maintenance",
		Short: "Toggle engine maintenance mode via gRPC",
		RunE: func(_ *cobra.Command, _ []string) error {
			enabled, err := GRPCToggleMaintenance()
			if err != nil {
				return err
			}
			if enabled {
				fmt.Println("🔴 Maintenance mode ENABLED.")
			} else {
				fmt.Println("🟢 Maintenance mode DISABLED.")
			}
			return nil
		},
	})

	// ── version ───────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "version",
		Short: "Print supervisor version",
		Run: func(_ *cobra.Command, _ []string) {
			fmt.Printf("cartrelay %s\n", Version)
		},
	})

	if err := root.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
