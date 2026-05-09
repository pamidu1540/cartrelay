package main

import (
	"fmt"
	"os"
	"time"

	"github.com/spf13/cobra"
)

func main() {
	root := &cobra.Command{
		Use:   "logistics",
		Short: "Logistics System Supervisor CLI v4",
	}

	// ── start ─────────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "start",
		Short: "Start all services",
		RunE: func(cmd *cobra.Command, _ []string) error {
			return executeDockerCompose("up", "-d", "--build")
		},
	})

	// ── stop ──────────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "stop",
		Short: "Stop all services",
		RunE: func(cmd *cobra.Command, _ []string) error {
			return executeDockerCompose("down")
		},
	})

	// ── restart ───────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "restart",
		Short: "Restart all service containers",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if err := RestartContainers(); err != nil {
				return err
			}
			fmt.Println("Containers restarted.")
			return nil
		},
	})

	// ── status ────────────────────────────────────────────────────────────────
	// FIX: New one-shot status command; doesn't require opening the full TUI.
	root.AddCommand(&cobra.Command{
		Use:   "status",
		Short: "Print a quick container health snapshot",
		Run: func(cmd *cobra.Command, _ []string) {
			s := fetchLiveStats()
			fmt.Printf("Engine : %s\n", s.EngineStatus)
			fmt.Printf("Bot    : %s\n", s.BotStatus)
			fmt.Printf("CPU    : %s\n", s.CPUUsage)
			fmt.Printf("RAM    : %s\n", s.RAMUsage)
			fmt.Printf("At     : %s\n", s.CollectedAt.Format(time.RFC3339))
		},
	})

	// ── logs ──────────────────────────────────────────────────────────────────
	// FIX: --service flag tails only one container (engine or bot).
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
		Run: func(cmd *cobra.Command, _ []string) {
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
					return fmt.Errorf("UPDATE_MANIFEST_URL not set")
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
	updateCmd.Flags().Bool("dry-run", false, "Fetch and verify manifest without applying")
	root.AddCommand(updateCmd)

	// ── rollback ──────────────────────────────────────────────────────────────
	root.AddCommand(&cobra.Command{
		Use:   "rollback",
		Short: "Re-apply the most recent backup tarball",
		RunE: func(cmd *cobra.Command, _ []string) error {
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
		RunE: func(cmd *cobra.Command, _ []string) error {
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

	if err := root.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
